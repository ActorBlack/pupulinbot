"""Public Tenhou monitoring via nodocchi's own live/history endpoints.

Never uses Mahjong Soul credentials. Only standard four-player ranked games
have their final table scores reconstructed (nodocchi uma: 40,10,-10,-20;
30k return). Special rules and sanma retain explicitly labelled result points.
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .query_cache import QueryCache
from .tenhou_profile import describe, phoenix_percentiles, rank_history

logger = logging.getLogger(__name__)

CN = ZoneInfo("Asia/Shanghai")
LOG_ID = re.compile(r"\b(\d{10}gm-[0-9a-f]{4}-\d{4}-[0-9a-f]{8})\b", re.I)
WATCH_ID = re.compile(r"^[0-9a-f]{8}$", re.I)


class TenhouError(Exception):
    pass


def validate_name(name):
    name = name.strip()
    if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
        raise TenhouError("请输入有效的天凤昵称（不是登录用的私密 ID）。")
    return name


def rule_name(info):
    n = int(info["playernum"])
    room = {0: "般", 1: "上", 2: "特", 3: "鳳"}.get(int(info["playerlevel"]), "?")
    length = {0: "?", 1: "東", 2: "南"}.get(int(info["playlength"]), "?")
    return (
        ("四" if n == 4 else "三")
        + room
        + length
        + ("喰" if info.get("kuitanari") else "")
        + ("赤" if info.get("akaari") else "")
        + ("速" if info.get("speed") else "")
    )


def grade_name(grade):
    grade = int(grade)
    if grade == 0:
        return "新人"
    if grade < 10:
        return f"{10 - grade}級"
    if grade < 20:
        return "初段" if grade == 10 else "一二三四五六七八九十"[grade - 10] + "段"
    return "天鳳位" if grade == 20 else f"天鳳位+{grade - 20}"


def history_rows(data, name):
    if not isinstance(data, dict) or data.get("name") != name:
        raise TenhouError("天凤历史返回的玩家不匹配。")
    if data.get("retry"):
        raise TenhouError("nodocchi 正在生成查询，请稍后重试。")
    rows = data.get("list", [])
    if not isinstance(rows, list):
        raise TenhouError("天凤历史数据格式异常。")
    clean = []
    for row in rows:
        try:
            n = int(row["playernum"])
            if n not in (3, 4) or name not in [row.get(f"player{i}") for i in range(1, n + 1)]:
                raise ValueError("player")
            if not 0 < float(row["starttime"]):
                raise ValueError("time")
            if row.get("during") is not None and not 0 <= float(row["during"]) <= 1440:
                raise ValueError("duration")
            for i in range(1, n + 1):
                if not math.isfinite(float(row[f"player{i}ptr"])):
                    raise ValueError("score")
            rule_name(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise TenhouError("天凤历史记录字段异常；已停止本次播报。") from exc
        clean.append(row)
    return sorted(clean, key=lambda r: float(r["starttime"]))


def record_id(row):
    match = LOG_ID.search(row.get("url", ""))
    if match:
        return match[1].lower()
    # Old public records sometimes have no replay URL. Still deduplicate them.
    return (
        str(row["starttime"])
        + ":"
        + ":".join(row[f"player{i}"] for i in range(1, int(row["playernum"]) + 1))
    )


def table_scores(row):
    """Reverse only verified normal 4p ranked scoring, with conservation checks."""
    if int(row["playernum"]) != 4 or row.get("sctype") not in ("b", "c"):
        return None
    if row.get("lobby", 0) not in (0, "0", None) or row.get("shuugi") is not None:
        return None
    match = LOG_ID.search(row.get("url", ""))
    if match and match[1].split("-")[2] != "0000":
        return None
    scores = [
        int((Decimal(str(row[f"player{i}ptr"])) - uma + 30) * 1000)
        for i, uma in enumerate((40, 10, -10, -20), 1)
    ]
    if sum(scores) != 100000 or scores != sorted(scores, reverse=True):
        return None
    return scores


def format_result(row):
    stamp = datetime.fromtimestamp(float(row["starttime"]), CN).strftime("%Y-%m-%d %H:%M")
    scores = table_scores(row)
    duration = f"{int(row['during'])}分钟" if row.get("during") is not None else "未提供"
    lines = [rule_name(row), f"{stamp}, 对局时长: {duration}"]
    if scores is None:
        lines.append("以下为结算点（含顺位分），非最终持点：")
    for i in range(1, int(row["playernum"]) + 1):
        score = str(scores[i - 1]) if scores else f"{float(row[f'player{i}ptr']):+.1f}"
        lines.append(f"{row[f'player{i}']}({score})")
    match = LOG_ID.search(row.get("url", ""))
    if match:
        lines.extend(("", f"https://tenhou.net/4/?log={match[1]}"))
    else:
        lines.extend(("", "该公开记录未提供牌谱链接。"))
    return "\n".join(lines)


class TenhouClient:
    def __init__(self, transport=None, *, history_directory=None):
        # This Mac's default IPv6 route intermittently stalls at TLS. Pin the
        # public-data client to IPv4 without changing OS/VPN settings.
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=10),
            transport=transport or httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
            headers={"User-Agent": "pupulinbot/0.1"},
        )
        self.lock = asyncio.Lock()
        self.next_request = 0.0
        self.cache = QueryCache(capacity=64)
        self.history_directory = Path(history_directory) if history_directory else None
        self._history_snapshots = {}
        self._history_tasks = {}
        self.requests = asyncio.Semaphore(2)
        self.history_requests = asyncio.Semaphore(1)
        self.preferred_host = "https://www.nodocchi.moe"

    async def close(self):
        tasks = list(self._history_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.cache.close()
        await self.http.aclose()

    async def _get(self, path, params=None, ttl=60, budget=60):
        key = (path, tuple(sorted((params or {}).items())))

        disk_expiry = [None]

        async def load():
            directory = self.history_directory / "public" if self.history_directory else None
            stored = None
            if directory and path in {"/api/phoenix_status.php", "/s/phoenix_scale_all.js"}:
                digest = hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
                stored = directory / (digest + ".json")
                try:
                    cached = json.loads(await asyncio.to_thread(stored.read_text))
                    age = time.time() - cached["fetched_at"]
                    if 0 <= age < ttl and isinstance(cached["data"], dict):
                        disk_expiry[0] = time.monotonic() + ttl - age
                        return cached["data"]
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            data = await self._fetch(path, params, budget)
            if stored and isinstance(data, dict):

                def save():
                    directory.mkdir(parents=True, exist_ok=True)
                    temporary = stored.with_suffix(f".{os.getpid()}.tmp")
                    temporary.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
                    os.replace(temporary, stored)
                    files = sorted(directory.glob("*.json"), key=lambda f: f.stat().st_mtime)
                    for old in files[:-128]:
                        old.unlink(missing_ok=True)

                try:
                    await asyncio.to_thread(save)
                except OSError:
                    logger.warning("天凤统计缓存写入失败")
            return data

        result = await self.cache.get(key, ttl, load)
        if disk_expiry[0] is not None and key in self.cache.entries:
            expiry, value = self.cache.entries[key]
            self.cache.entries[key] = (min(expiry, disk_expiry[0]), value)
        return result

    async def _fetch(self, path, params, budget):
        bootstrap = (
            path == "/api/listuser.php" and (params or {}).get("start") == "1" and budget > 60
        )
        async with self.history_requests if bootstrap else self.requests:
            try:
                hosts = (
                    self.preferred_host,
                    "https://nodocchi.moe"
                    if self.preferred_host.endswith("www.nodocchi.moe")
                    else "https://www.nodocchi.moe",
                )
                for attempt in range(2):
                    # Pace request starts, rather than serializing entire slow responses.
                    async with self.lock:
                        await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                        self.next_request = time.monotonic() + 1.1
                    try:
                        host = hosts[attempt]
                        async with asyncio.timeout(budget):
                            response = await self.http.get(
                                host + path,
                                params=params,
                                timeout=httpx.Timeout(budget, connect=min(20, budget)),
                            )
                        break
                    except (httpx.TransportError, TimeoutError):
                        if attempt:
                            raise
                        await asyncio.sleep(1.1)
                response.raise_for_status()
                data = response.json()
                if isinstance(data, dict) and data.get("retry"):
                    raise TenhouError("nodocchi 正在生成数据，请稍后重试。")
                if path == "/s/wg.js" and response.headers.get("last-modified"):
                    modified = parsedate_to_datetime(response.headers["last-modified"]).timestamp()
                    if time.time() - modified > 600:
                        raise TenhouError("观战快照已超过10分钟未更新，暂停开局播报。")
                self.preferred_host = host
                return data
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                raise TenhouError(f"天凤数据暂时不可用（{type(exc).__name__}）。") from exc

    async def history(self, name, start=0, budget=60):
        name = validate_name(name)
        if not start:
            return history_rows(await self._profile_history(name), name)
        data = await self._get(
            "/api/listuser.php", {"name": name, "start": str(int(start))}, ttl=60, budget=budget
        )
        return history_rows(data, name)

    async def user_history(self, name):
        """Validated public history plus nodocchi's rate and PT-reset metadata."""
        name = validate_name(name)
        data = await self._profile_history(name)
        history_rows(data, name)
        return data

    async def phoenix_stats(self, name, mode=4, length=None):
        name = validate_name(name)
        data = await self._get("/api/phoenix_status.php", {"all": "1", "username": name}, ttl=900)
        if not isinstance(data, dict):
            raise TenhouError("天凤凤凰桌统计格式异常。")
        key = f"s{mode}" + (f"_{length}" if length else "")
        stats = data.get(key)
        if stats is not None and not isinstance(stats, dict):
            raise TenhouError("天凤凤凰桌统计格式异常。")
        return stats or {}

    async def phoenix_percentiles(self, stats, mode=4, length=None):
        data = await self._get("/s/phoenix_scale_all.js", ttl=86400)
        if not isinstance(data, dict):
            raise TenhouError("天凤凤凰桌参考分布格式异常。")
        key = f"s{mode}" + (f"_{length}" if length else "")
        return phoenix_percentiles(stats, data.get(key))

    async def promotion(self, row, name):
        identity = record_id(row)
        stamp = float(row["starttime"])
        mode = int(row["playernum"])

        async def load():
            data = await self._profile_history(name)
            if not any(record_id(r) == identity for r in history_rows(data, name)):
                return None
            result = await asyncio.to_thread(
                rank_history, data, name, mode, stamp + 0.5, include_promotions=True
            )
            promotion = next((p for p in result["promotions"] if p["starttime"] == stamp), None)
            return grade_name(promotion["grade"]) if promotion else None

        return await self.cache.get(("promotion", name, identity), 3600, load)

    def _snapshot_path(self, name):
        digest = hashlib.sha256(name.encode()).hexdigest()
        return self.history_directory / (digest + ".json") if self.history_directory else None

    def warm_history(self, name):
        name = validate_name(name)
        if name not in self._history_snapshots and name not in self._history_tasks:
            self._start_history(name)

    def _start_history(self, name):
        task = asyncio.create_task(self._update_history(name))
        self._history_tasks[name] = task
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return task

    async def _profile_history(self, name):
        task = self._history_tasks.get(name) or self._start_history(name)
        # A card deadline must not discard a large first download's completed work.
        return await asyncio.shield(task)

    async def _update_history(self, name):
        try:
            raw = self._history_snapshots.get(name)
            path = self._snapshot_path(name)
            if raw is None and path and path.exists():
                try:
                    raw = json.loads(await asyncio.to_thread(path.read_text))
                    history_rows(raw, name)
                except (OSError, ValueError, TenhouError):
                    raw = None
            if raw is None:
                raw = await self._get(
                    "/api/listuser.php", {"name": name, "start": "1"}, ttl=86400, budget=180
                )
                history_rows(raw, name)
            else:
                latest = max((float(r["starttime"]) for r in raw.get("list", [])), default=1)
                delta = await self._get(
                    "/api/listuser.php",
                    {"name": name, "start": str(max(1, int(latest) - 7200))},
                    ttl=60,
                    budget=30,
                )
                history_rows(delta, name)
                rows = {record_id(r): r for r in raw.get("list", [])}
                rows.update({record_id(r): r for r in delta.get("list", [])})
                events = {json.dumps(e, sort_keys=True): e for e in raw.get("ptevents", [])}
                events.update({json.dumps(e, sort_keys=True): e for e in delta.get("ptevents", [])})
                raw = {
                    **raw,
                    **{k: v for k, v in delta.items() if k not in {"list", "ptevents"}},
                    "list": sorted(rows.values(), key=lambda r: float(r["starttime"])),
                }
                if events:
                    raw["ptevents"] = sorted(events.values(), key=lambda e: float(e["starttime"]))
            if path:

                def save():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(f".{os.getpid()}.tmp")
                    temporary.write_text(json.dumps(raw, ensure_ascii=False))
                    os.replace(temporary, path)
                    # Bound disk history cache without deleting user subscriptions.
                    files = sorted(
                        (
                            p
                            for p in path.parent.glob("*.json")
                            if re.fullmatch(r"[0-9a-f]{64}\.json", p.name)
                        ),
                        key=lambda p: p.stat().st_mtime,
                    )
                    for old in files[:-64]:
                        old.unlink(missing_ok=True)

                try:
                    await asyncio.to_thread(save)
                except OSError:
                    logger.warning("天凤历史缓存写入失败，本轮仍使用已获取数据")
            if len(self._history_snapshots) >= 16:
                self._history_snapshots.pop(next(iter(self._history_snapshots)))
            self._history_snapshots[name] = raw
            return raw
        finally:
            self._history_tasks.pop(name, None)

    async def live(self):
        data = await self._get("/s/wg.js", ttl=25)
        if not isinstance(data, list):
            raise TenhouError("天凤观战列表格式异常。")
        # nodocchi also publishes Ron2 tables; these are a different service.
        data = [g for g in data if not isinstance(g, dict) or not g.get("info", {}).get("ron2")]
        for game in data:
            try:
                info = game["info"]
                assert WATCH_ID.fullmatch(info["id"])
                assert int(info["playernum"]) in (3, 4)
                assert len(game["players"]) == int(info["playernum"])
                # Live feed timestamps can lead the local clock by roughly five minutes.
                # Preserve source time rather than inventing an offset.
                assert 0 < float(info["starttime"]) <= time.time() + 900
                rule_name(info)
                for player in game["players"]:
                    validate_name(player["name"])
            except (AssertionError, KeyError, TypeError, ValueError) as exc:
                raise TenhouError("天凤观战记录字段异常。") from exc
        return data

    async def profile(self, player, game, destination=None):
        """Incremental enrichment: partial results survive the card's deadline."""
        name = player["name"]
        mode = int(game["info"]["playernum"])
        scope = f"s{mode}_{int(game['info']['playlength'])}"
        profile = destination if destination is not None else {}
        profile.update(
            name=name,
            grade=player.get("grade"),
            rate=player.get("rate"),
            mode=mode,
            stats_scope=scope,
        )

        async def stats():
            try:
                data = await self._get(
                    "/api/phoenix_status.php", {"all": "1", "username": name}, ttl=900
                )
                values = data.get(scope) if isinstance(data, dict) else None
                profile["stats"] = values if isinstance(values, dict) else {}
                profile["stats_loaded"] = True
                scale = await self._get("/s/phoenix_scale_all.js", ttl=86400)
                population = scale.get(scope, {}) if isinstance(scale, dict) else {}
                profile["percentiles"] = phoenix_percentiles(profile["stats"], population)
                profile["scale_loaded"] = True
            except (TenhouError, ValueError, TypeError):
                pass

        async def history():
            try:
                data = await self._profile_history(name)
                rows = history_rows(data, name)
                profile["history_loaded"] = True
                before = [
                    r
                    for r in rows
                    if int(r["playernum"]) == mode
                    and float(r["starttime"]) < float(game["info"]["starttime"])
                ]
                profile["count"] = len(before)
                profile.update(
                    rank_history(
                        data, name, mode, float(game["info"]["starttime"]), player.get("grade")
                    )
                )
            except (TenhouError, ValueError, TypeError, KeyError, IndexError):
                pass

        await asyncio.gather(stats(), history())
        profile["tags"] = describe(profile)
        return profile
