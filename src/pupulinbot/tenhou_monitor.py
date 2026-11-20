"""Persistent, group-scoped public Tenhou subscriptions and delivery deduplication."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from itertools import zip_longest

from .promotion import promotion_line
from .tenhou import (
    TenhouError,
    format_result,
    format_start_players,
    grade_name,
    record_id,
    rule_name,
    validate_name,
    watch_url,
)
from .tenhou_card import render_start

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenhouStartNotice:
    picture: bytes
    text: str


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenhou_bindings(
 qq_id TEXT PRIMARY KEY, name TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tenhou_subscriptions(
 group_id TEXT NOT NULL, owner_id TEXT NOT NULL, name TEXT NOT NULL,
 enabled_at REAL NOT NULL, cursor REAL NOT NULL,
 checked_at REAL, error TEXT,
 PRIMARY KEY(group_id,owner_id,name)
);
CREATE TABLE IF NOT EXISTS tenhou_starts(
 group_id TEXT NOT NULL, game_id TEXT NOT NULL, payload TEXT NOT NULL,
 detected_at REAL NOT NULL, PRIMARY KEY(group_id,game_id)
);
"""


class TenhouStore:
    def __init__(self, db):
        self.db = db

    async def initialize(self):
        async with self.db.connect() as conn:
            await conn.executescript(SCHEMA)
            await conn.commit()

    async def binding(self, user_id):
        async with self.db.connect() as conn:
            row = await (
                await conn.execute(
                    "SELECT name FROM tenhou_bindings WHERE qq_id=?", (str(user_id),)
                )
            ).fetchone()
            return row["name"] if row else None

    async def member_names(self, member_ids):
        async with self.db.connect() as conn:
            rows = await (
                await conn.execute(
                    """SELECT DISTINCT name FROM tenhou_bindings
                    WHERE qq_id IN (SELECT value FROM json_each(?)) ORDER BY name""",
                    (json.dumps([str(qq) for qq in member_ids]),),
                )
            ).fetchall()
            return [row["name"] for row in rows]

    async def bind(self, user_id, name, *, if_absent=False):
        name = validate_name(name)
        async with self.db.connect() as conn:
            conflict = "DO NOTHING" if if_absent else "DO UPDATE SET name=excluded.name"
            cur = await conn.execute(
                "INSERT INTO tenhou_bindings(qq_id,name) VALUES(?,?) "
                "ON CONFLICT(qq_id) " + conflict,
                (str(user_id), name),
            )
            await conn.commit()
            return cur.rowcount > 0

    async def unbind(self, user_id):
        async with self.db.connect() as conn:
            cur = await conn.execute("DELETE FROM tenhou_bindings WHERE qq_id=?", (str(user_id),))
            await conn.commit()
            return cur.rowcount > 0

    async def add(self, group, owner, name):
        name = validate_name(name)
        now = time.time()
        async with self.db.connect() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO tenhou_subscriptions VALUES(?,?,?,?,?,NULL,NULL)",
                (str(group), str(owner), name, now, now - 7200),
            )
            await conn.commit()

    async def remove(self, group, owner=None, name=None):
        async with self.db.connect() as conn:
            sql = "DELETE FROM tenhou_subscriptions WHERE group_id=?"
            params = [str(group)]
            if owner is not None:
                sql += " AND owner_id=?"
                params.append(str(owner))
            if name:
                sql += " AND name=?"
                params.append(name)
            cur = await conn.execute(sql, params)
            await conn.commit()
            return cur.rowcount

    async def subscriptions(self, group=None, owner=None):
        sql = "SELECT * FROM tenhou_subscriptions WHERE 1=1"
        params = []
        for col, value in (("group_id", group), ("owner_id", owner)):
            if value is not None:
                sql += f" AND {col}=?"
                params.append(str(value))
        async with self.db.connect() as conn:
            rows = await (await conn.execute(sql, params)).fetchall()
            return [dict(r) for r in rows]

    async def health(self, sub, error=None, cursor=None):
        async with self.db.connect() as conn:
            await conn.execute(
                "UPDATE tenhou_subscriptions SET checked_at=?,error=?,cursor=COALESCE(?,cursor) "
                "WHERE group_id=? AND owner_id=? AND name=? AND enabled_at=?",
                (
                    time.time(),
                    error,
                    cursor,
                    sub["group_id"],
                    sub["owner_id"],
                    sub["name"],
                    sub["enabled_at"],
                ),
            )
            await conn.commit()

    async def enqueue(self, group, game):
        async with self.db.connect() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO tenhou_starts VALUES(?,?,?,?)",
                (str(group), game["info"]["id"], json.dumps(game, ensure_ascii=False), time.time()),
            )
            await conn.commit()

    async def pending(self):
        async with self.db.connect() as conn:
            await conn.execute(
                "DELETE FROM tenhou_starts WHERE detected_at<?", (time.time() - 7200,)
            )
            rows = await (await conn.execute("SELECT * FROM tenhou_starts")).fetchall()
            await conn.commit()
            return [dict(r) for r in rows]

    async def dequeue(self, group, game_id):
        async with self.db.connect() as conn:
            await conn.execute(
                "DELETE FROM tenhou_starts WHERE group_id=? AND game_id=?", (str(group), game_id)
            )
            await conn.commit()


class TenhouMonitor:
    def __init__(self, store, client):
        self.store, self.client = store, client
        self.live_error = None
        self.live_checked = None
        self._cards = {}
        self._jobs = {}
        self._histories = {}
        self._card_jobs = {}
        self._closed = False
        self._history_slots = asyncio.Semaphore(4)

    async def deliver(self, group, identity, event, message, send):
        subscriber = f"tenhou:group:{group}"
        uuid = "tenhou:" + identity
        if not await self.store.db.claim_event(uuid, subscriber, event):
            async with self.store.db.connect() as conn:
                row = await (
                    await conn.execute(
                        "SELECT status FROM game_events WHERE uuid=? AND subscriber=? AND event=?",
                        (uuid, subscriber, event),
                    )
                ).fetchone()
            # A lease left by a crashed sender is not a completed notification.
            return bool(row and row["status"] == "sent")
        try:
            if callable(message):
                message = await message()
            await asyncio.wait_for(send(group, message), timeout=270)
        except BaseException:
            await self.store.db.release_event(uuid, subscriber, event)
            raise
        await self.store.db.complete_event(uuid, subscriber, event)
        return True

    async def finish_message(self, row, names):
        names = [names] if isinstance(names, str) else sorted(set(names))

        async def progress(name):
            if not hasattr(self.client, "finished_progress"):
                line = await promotion_line(self.client, row, name, budget_seconds=10)
                return f"{name}：{line}" if line else ""
            try:
                async with asyncio.timeout(10):
                    result = await self.client.finished_progress(row, name)
                if not result:
                    return f"{name}：PT 暂不可用"
                grade = result.get("estimated_grade")
                level = grade_name(grade) if grade is not None else ""
                pt, cap = result.get("pt"), result.get("pt_max")
                if pt is not None and cap is not None:
                    line = f"{name}：{level}　推算 PT {pt:g} / {cap:g}"
                elif grade is not None and grade >= 20:
                    line = f"{name}：{level}"
                else:
                    line = f"{name}：PT 暂不可用"
                if result.get("promotion"):
                    line += "\n" + f"{name}：欢迎新{result['promotion']}"
                return line
            except Exception:
                return f"{name}：PT 暂不可用"

        lines = await asyncio.gather(*(progress(name) for name in names))
        return "\n".join([format_result(row), *(line for line in lines if line)])

    async def start_message(self, game):
        identity = (game["info"]["id"], game["info"]["starttime"])
        task = self._card_jobs.get(identity)
        if task is None or task.done():
            task = asyncio.create_task(self._build_start_message(game))
            self._card_jobs[identity] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._card_jobs.get(identity) is task:
                del self._card_jobs[identity]

    async def _build_start_message(self, game):
        identity = (game["info"]["id"], game["info"]["starttime"])
        cached = self._cards.get(identity)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        profiles = [dict(p) for p in game["players"]]

        async def enrich(i, player):
            await self.client.profile(player, game, destination=profiles[i])
            if not all(
                profiles[i].get(key) for key in ("stats_loaded", "scale_loaded", "history_loaded")
            ):
                await asyncio.sleep(3)
                await self.client.profile(player, game, destination=profiles[i])

        try:
            async with asyncio.timeout(360):
                await asyncio.gather(*(enrich(i, p) for i, p in enumerate(game["players"])))
        except TimeoutError:
            log.warning("Tenhou start card loading exceeded 360 seconds; keeping loaded fields")
        from .tenhou_profile import describe

        if not any(p.get("stats_loaded") or p.get("history_loaded") for p in profiles):
            raise TenhouError("观战资料暂不可用，保留任务等待重试。")
        for profile in profiles:
            profile["tags"] = describe(profile)
        picture = await asyncio.to_thread(render_start, game, profiles)
        if len(self._cards) > 32:
            self._cards.clear()
        complete = all(
            p.get("stats_loaded") and p.get("scale_loaded") and p.get("history_loaded")
            for p in profiles
        )
        self._cards[identity] = (picture, time.monotonic() + (600 if complete else 10))
        self.last_profiles = profiles
        return picture

    def _launch(self, key, factory):
        """Coalesce ticks while this group's previous delivery is still running."""
        task = self._jobs.get(key)
        if task is None or task.done():
            task = asyncio.create_task(factory(), name=f"thwatch:{key}")
            self._jobs[key] = task
            task.add_done_callback(self._job_done)
        return task

    @staticmethod
    def _job_done(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("Tenhou monitoring worker failed", exc_info=error)

    async def drain(self):
        """Wait for workers, including start deliveries spawned by live discovery."""
        while tasks := [task for task in self._jobs.values() if not task.done()]:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self._closed = True
        tasks = {*self._jobs.values(), *self._histories.values(), *self._card_jobs.values()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._jobs.clear()
        self._histories.clear()
        self._card_jobs.clear()

    async def poll(self, send, *, wait=True):
        if self._closed or getattr(self.client, "cooldown_seconds", 0):
            return
        subs = await self.store.subscriptions()
        if not subs:
            return
        self._jobs = {key: task for key, task in self._jobs.items() if not task.done()}
        self._histories = {key: task for key, task in self._histories.items() if not task.done()}
        groups = {}
        starts = {}
        for sub in subs:
            groups.setdefault(sub["group_id"], []).append(sub)
            name = sub["name"]
            starts[name] = min(starts.get(name, sub["cursor"]), sub["cursor"])
        histories = {}
        ready = {
            group: members
            for group, members in groups.items()
            if ("finish", group) not in self._jobs
        }
        # Interleave groups before entering the bounded network queue. A large
        # watch list must not enqueue all its players ahead of smaller groups.
        for turn in zip_longest(*ready.values()):
            for sub in turn:
                if sub is None:
                    continue
                name = sub["name"]
                key = (name, starts[name])
                task = self._histories.get(key)
                if task is None:
                    task = asyncio.create_task(self._history(name, starts[name]))
                    self._histories[key] = task
                histories[name] = task
        for group, members in ready.items():
            self._launch(
                ("finish", group),
                lambda members=members: self._finishes(members, histories, send),
            )
        # Live discovery and expensive start cards never hold up result delivery.
        self._launch(("live",), lambda: self._starts(subs, send))
        if wait:
            await self.drain()

    async def _history(self, name, start):
        try:
            async with self._history_slots:
                async with asyncio.timeout(75):
                    return await self.client.history(name, start)
        except TimeoutError:
            return TenhouError("战绩查询超时，下轮继续尝试。")
        except Exception as exc:
            return exc

    async def _starts(self, subs, send):
        try:
            live = await self.client.live()
            self.live_error = None
            for game in live:
                names = {p["name"] for p in game["players"]}
                groups = {s["group_id"] for s in subs if s["name"] in names}
                for group in groups:
                    await self.store.enqueue(group, game)
        except TenhouError as exc:
            self.live_error = str(exc)
            log.warning("Tenhou spectator source: %s", exc)
        self.live_checked = time.time()
        groups = {}
        for pending in await self.store.pending():
            groups.setdefault(pending["group_id"], []).append(pending)
        for group, rows in groups.items():
            self._launch(
                ("start", group),
                lambda rows=rows: self._start_group(rows, subs, send),
            )

    async def _start_group(self, pending_rows, subs, send):
        for pending in pending_rows:
            game = json.loads(pending["payload"])
            names = {p["name"] for p in game["players"]}
            active = any(s["group_id"] == pending["group_id"] and s["name"] in names for s in subs)
            if active:
                try:
                    info = game["info"]
                    picture = (
                        await self.start_message(game) if int(info["playerlevel"]) == 3 else None
                    )
                    current = await self.store.subscriptions(pending["group_id"])
                    if not any(s["name"] in names for s in current):
                        await self.store.dequeue(pending["group_id"], pending["game_id"])
                        continue
                    # Include start timestamp in the key: short watch IDs can be reused.
                    key = f"wg:{info['id']}:{info['starttime']}"
                    watch_id = info["id"].upper()
                    text = (
                        rule_name(info)
                        + " 对局开始\n"
                        + format_start_players(game)
                        + f"\n观战：{watch_url(watch_id)}\n逐局播报：thlive {watch_id}"
                    )
                    notice = TenhouStartNotice(picture, text) if picture is not None else text
                    if not await self.deliver(pending["group_id"], key, "start", notice, send):
                        continue
                except Exception:
                    log.exception("Tenhou start delivery failed; retained for retry")
                    continue
            await self.store.dequeue(pending["group_id"], pending["game_id"])

    async def _finishes(self, subs, histories, send):
        # Each group owns its cursor and retry path; another group's slow send
        # cannot hold the next scheduled tick or consume this group's worker.
        for sub in subs:
            name = sub["name"]
            rows = await asyncio.shield(histories[name])
            if isinstance(rows, Exception):
                await self.store.health(sub, str(rows))
                continue
            try:
                current = await self.store.subscriptions(sub["group_id"], sub["owner_id"])
                if not any(
                    s["name"] == name and s["enabled_at"] == sub["enabled_at"] for s in current
                ):
                    continue
                for row in rows:
                    # Public times have minute precision. Exclude records that finished
                    # before enabling; keep overlap to allow delayed indexing and retries.
                    ended = float(row["starttime"]) + float(row.get("during") or 0) * 60
                    if ended < sub["enabled_at"]:
                        continue
                    names = {
                        s["name"]
                        for s in subs
                        if s["group_id"] == sub["group_id"]
                        and ended >= s["enabled_at"]
                        and s["name"] in {row.get(f"player{i}") for i in range(1, 5)}
                    }
                    if not await self.deliver(
                        sub["group_id"],
                        record_id(row),
                        "finish",
                        lambda row=row, names=names: self.finish_message(row, names),
                        send,
                    ):
                        raise TenhouError("等待上次发送租约到期后重试。")
                latest = max((float(r["starttime"]) for r in rows), default=sub["cursor"])
                await self.store.health(sub, cursor=max(sub["cursor"], latest - 7200))
            except Exception:
                log.exception("Tenhou finish delivery failed; cursor retained")
                await self.store.health(sub, "结算发送失败，将重试。")
