"""Original seat nicknames, independent of the reviewer's anonymized MJAI names."""

import asyncio
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .ms_ids import perspective_to_account
from .paipuya import PaipuyaError
from .review import ReviewError, parse_review_link


class EmptyTenhouLog(ReviewError):
    pass


def tenhou_names(content):
    if len(content) > 4 * 1024 * 1024 or b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise ReviewError("天凤牌谱格式异常")
    root = ET.fromstring(content)
    node = root.find("UN")
    if root.tag != "mjloggm" or node is None:
        raise ReviewError("天凤牌谱缺少座位昵称")
    return [unquote(node.get(f"n{i}", ""), errors="strict") for i in range(4)]


def majsoul_head_seats(head, uuid):
    if (
        not isinstance(head, dict)
        or head.get("uuid") != uuid
        or type(head.get("end_time")) is not int
        or head["end_time"] <= 0
    ):
        return []
    accounts = head.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != 4:
        return []
    names = {}
    account_ids = set()
    for player in accounts:
        if not isinstance(player, dict):
            return []
        seat = player.get("seat")
        name = player.get("nickname")
        account_id = player.get("account_id")
        if (
            type(seat) is not int
            or seat not in range(4)
            or seat in names
            or not isinstance(name, str)
            or not name.strip()
            or type(account_id) is not int
            or account_id <= 0
            or account_id in account_ids
        ):
            return []
        names[seat] = name.strip()
        account_ids.add(account_id)
    order = head.get("result_seats")
    ranks = {}
    if (
        isinstance(order, list)
        and len(order) == 4
        and all(type(seat) is int for seat in order)
        and set(order) == names.keys()
    ):
        ranks = {seat: rank for rank, seat in enumerate(order, 1)}
    return [dict(name=names[seat], rank=ranks.get(seat)) for seat in range(4)]


class RecordNames:
    def __init__(
        self, db=None, *, transport=None, archive_directory=None, clients=None, majsoul_browser=None
    ):
        self.db, self.transport = db, transport
        self.clients = clients or {}
        self.majsoul_browser = majsoul_browser
        self.archive_directory = Path(archive_directory) if archive_directory else None
        self.cache, self.pending = {}, {}
        self.results, self.result_pending = {}, {}
        self.checks = {}
        self.gate = asyncio.Lock()
        self.next_at = 0.0

    async def __call__(self, row):
        log = parse_review_link(row["url"], row["seat"])
        if log.game_id != row["uuid"]:
            return []
        key = log.source, log.game_id
        if key in self.cache:
            return self.cache[key]
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(self._bounded_load(log))
        task = self.pending[key]
        try:
            names = await asyncio.shield(task)
            if len(names) == 4 and all(isinstance(n, str) and n.strip() for n in names):
                if len(self.cache) >= 256:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = names
            return names
        finally:
            if task.done() and self.pending.get(key) is task:
                self.pending.pop(key, None)

    async def _bounded_load(self, log):
        async with asyncio.timeout(230):
            return await self._load(log)

    def _remember_game(self, log, players):
        if len(players) != 4:
            return
        if {p.get("seat") for p in players} == {0, 1, 2, 3}:
            players = sorted(players, key=lambda p: p["seat"])
        ranks = [p.get("rank") for p in players]
        if any(type(r) is not int for r in ranks) or set(ranks) != {1, 2, 3, 4}:
            scores = [p.get("score") for p in players]
            if all(type(v) in (int, float) and math.isfinite(v) for v in scores):
                order = sorted(range(4), key=lambda i: (-scores[i], i))
                ranks = [order.index(i) + 1 for i in range(4)]
            else:
                ranks = [None] * 4
        self._remember_results(
            log, [dict(name=p.get("nickname"), rank=r) for p, r in zip(players, ranks, strict=True)]
        )

    def _remember_results(self, log, result):
        if len(self.results) >= 256:
            self.results.pop(next(iter(self.results)))
        self.results[log.source, log.game_id] = result

    async def seat_result(self, row):
        log = parse_review_link(row["url"], row["seat"])
        if log.game_id != row["uuid"]:
            return {}
        key = log.source, log.game_id
        if self.checks.get(key, {}).get("status") == "not_found":
            return {}
        if key not in self.results:
            if key not in self.result_pending:
                self.result_pending[key] = asyncio.create_task(self._load_result(log))
            task = self.result_pending[key]
            try:
                await asyncio.shield(task)
            finally:
                if task.done() and self.result_pending.get(key) is task:
                    self.result_pending.pop(key, None)
        results = self.results.get(key, [])
        return results[row["seat"]] if len(results) == 4 else {}

    async def _load_result(self, log):
        if log.source == "riichi_city":
            await self._riichi_city(log)
        elif log.source == "tenhou":
            await self._tenhou(log)
        else:
            if self.db:
                async with self.db.connect() as conn:
                    record = await (
                        await conn.execute(
                            "SELECT payload FROM games WHERE uuid=? LIMIT 1", (log.game_id,)
                        )
                    ).fetchone()
                if record:
                    raw = json.loads(record["payload"])
                    self._remember_game(log, raw.get("players") or raw.get("accounts") or [])
                    if (log.source, log.game_id) in self.results:
                        return
            await self._majsoul(log)

    async def _load(self, log):
        archived = await asyncio.to_thread(self._archived_names, log)
        if archived:
            return archived
        if log.source == "riichi_city":
            return await self._riichi_city(log)
        if log.source == "tenhou":
            for attempt in range(3):
                try:
                    return await self._tenhou(log)
                except (httpx.TransportError, ET.ParseError, EmptyTenhouLog):
                    if attempt == 2:
                        raise
                except httpx.HTTPStatusError as exc:
                    if attempt == 2 or exc.response.status_code < 500:
                        raise
                await asyncio.sleep(2**attempt)
        if self.db:
            async with self.db.connect() as conn:
                record = await (
                    await conn.execute(
                        "SELECT payload FROM games WHERE uuid=? LIMIT 1", (log.game_id,)
                    )
                ).fetchone()
            if record:
                data = json.loads(record["payload"])
                players = data.get("players") or data.get("accounts") or []
                if len(players) == 4:
                    if {p.get("seat") for p in players} == {0, 1, 2, 3}:
                        players = sorted(players, key=lambda p: p["seat"])
                    names = [p.get("nickname", "") for p in players]
                    if all(isinstance(n, str) and n.strip() for n in names):
                        self._remember_game(log, players)
                        return names
        return await self._majsoul(log)

    async def _majsoul(self, log):
        key = log.source, log.game_id
        self.checks.pop(key, None)
        names = []
        error = None
        try:
            # Leave time for the official-session fallback within the 230s lookup budget.
            async with asyncio.timeout(60):
                names = await self._paipuya_names(log)
        except (PaipuyaError, httpx.HTTPError, ReviewError, TimeoutError) as exc:
            error = exc
        if len(names) == 4 and all(names):
            return names
        if self.majsoul_browser is not None:
            head = await self.majsoul_browser.record_head(log.url, log.game_id)
            result = majsoul_head_seats(head, log.game_id)
            if result:
                self._remember_results(log, result)
                self.checks[key] = {
                    "source": "majsoul_official",
                    "uuid": log.game_id,
                    "status": "found",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
                return [player["name"] for player in result]
        if error is not None:
            raise error
        return names

    async def _paipuya_names(self, log):
        key = log.source, log.game_id
        client = self.clients.get(4)
        if client is None:
            raise ReviewError("未配置牌谱屋客户端，昵称尚未核验")
        value = parse_qs(urlsplit(log.url).query).get("paipu", [""])[0]
        perspective = re.search(r"_a([0-9]+)(?:_[0-9]+)?$", value)
        date = re.match(r"([0-9]{6})-", log.game_id)
        game = None
        if perspective and date:
            account_id = perspective_to_account(int(perspective[1]))
            day = datetime.strptime("20" + date[1], "%Y%m%d").replace(tzinfo=UTC)
            games, _ = await client.query_history(
                account_id, 200, start=day - timedelta(days=1), end=day + timedelta(days=2)
            )
            game = next((g for g in games if g.uuid == log.game_id), None)
            if game and not game.seat(account_id):
                raise ReviewError("牌谱屋记录与链接视角不符，昵称尚未核验")
        # Missing perspective, a capped player history, or a stale cached page
        # must not turn a searchable game into a blank nickname.
        if game is None:
            game = await client.find_game(log.game_id)
        checked = {
            "source": "paipuya",
            "uuid": log.game_id,
            "checked_at": datetime.now(UTC).isoformat(),
        }
        if game is None:
            self.checks[key] = {**checked, "status": "not_found"}
            return []
        if game.uuid != log.game_id or not game.ended_at or len(game.players) != 4:
            raise ReviewError("牌谱屋记录尚不完整，昵称尚未核验")
        players = game.players
        if any("seat" in p for p in players):
            if {p.get("seat") for p in players} != {0, 1, 2, 3}:
                raise ReviewError("牌谱屋记录座位异常，昵称尚未核验")
            players = sorted(players, key=lambda p: p["seat"])
        names = [
            p.get("nickname", "").strip() if isinstance(p.get("nickname"), str) else ""
            for p in players
        ]
        self.checks[key] = {**checked, "status": "found" if all(names) else "incomplete"}
        self._remember_game(log, players)
        return names

    def name_check(self, row):
        return dict(self.checks.get((row.get("source", "majsoul"), row["uuid"]), {}))

    def _archived_names(self, log):
        if not self.archive_directory:
            return []
        for path in self.archive_directory.glob("*/job.json"):
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                job = json.loads(path.read_text())
                names = {}
                for row in job["rows"]:
                    if (
                        row.get("source", "majsoul") == log.source
                        and row.get("uuid") == log.game_id
                        and row.get("player_name_source") == "original"
                        and type(row.get("seat")) is int
                        and row["seat"] in range(4)
                        and isinstance(row.get("player_name"), str)
                        and row["player_name"].strip()
                    ):
                        if row["seat"] in names and names[row["seat"]] != row["player_name"]:
                            names.clear()
                            break
                        names[row["seat"]] = row["player_name"]
                if len(names) == 4:
                    return [names[i] for i in range(4)]
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return []

    async def _wait_public_turn(self):
        async with self.gate:
            await asyncio.sleep(max(0, self.next_at - time.monotonic()))
            self.next_at = time.monotonic() + 1.1

    async def _riichi_city(self, log):
        await self._wait_public_turn()
        async with httpx.AsyncClient(
            transport=self.transport,
            headers={"User-Agent": "PupulinBot/1.0"},
            timeout=httpx.Timeout(45, connect=15),
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST", "https://rc.honk.li/api/log", json={"log_id": log.game_id}
            ) as response:
                response.raise_for_status()
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > 4 * 1024 * 1024:
                        raise ReviewError("一番街牌谱过大")
        data = json.loads(content)
        names = data.get("name") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or data.get("ref") != log.game_id
            or data.get("ratingc") != "PF4"
            or not isinstance(names, list)
            or len(names) != 4
            or not all(isinstance(n, str) and n.strip() for n in names)
        ):
            raise ReviewError("一番街牌谱编号或四人座位信息不完整")
        self._remember_results(log, [dict(name=n, rank=None) for n in names])
        return names

    async def _tenhou(self, log):
        await self._wait_public_turn()
        async with httpx.AsyncClient(
            transport=self.transport,
            headers={"User-Agent": "PupulinBot/1.0"},
            timeout=httpx.Timeout(20, connect=10),
            follow_redirects=False,
        ) as client:
            async with client.stream("GET", f"https://tenhou.net/0/log/?{log.game_id}") as response:
                response.raise_for_status()
                content = bytearray()
                async for part in response.aiter_bytes():
                    content.extend(part)
                    if len(content) > 4 * 1024 * 1024:
                        raise ReviewError("天凤牌谱过大")
                if not content:
                    raise EmptyTenhouLog(f"天凤返回HTTP {response.status_code}但牌谱正文为空")
        names = tenhou_names(bytes(content))
        root = ET.fromstring(content)
        final = next((n.get("owari") for n in reversed(list(root)) if n.get("owari")), None)
        ranks = [None] * 4
        if final:
            try:
                values = [float(n) for n in final.split(",")]
                if len(values) == 8 and all(math.isfinite(v) for v in values):
                    order = sorted(range(4), key=lambda i: (-values[2 * i], -values[2 * i + 1], i))
                    ranks = [order.index(i) + 1 for i in range(4)]
            except ValueError:
                pass
        self._remember_results(
            log, [dict(name=n, rank=r) for n, r in zip(names, ranks, strict=True)]
        )
        return names
