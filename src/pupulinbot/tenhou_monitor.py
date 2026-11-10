"""Persistent, group-scoped public Tenhou subscriptions and delivery deduplication."""

import asyncio
import json
import logging
import time

from .promotion import promotion_line
from .tenhou import TenhouError, format_result, record_id, validate_name
from .tenhou_card import render_start

log = logging.getLogger(__name__)
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
            await asyncio.wait_for(send(group, message), timeout=95)
        except BaseException:
            await self.store.db.release_event(uuid, subscriber, event)
            raise
        await self.store.db.complete_event(uuid, subscriber, event)
        return True

    async def finish_message(self, row, names):
        names = [names] if isinstance(names, str) else sorted(set(names))
        lines = await asyncio.gather(
            *(promotion_line(self.client, row, name, budget_seconds=10) for name in names)
        )
        congratulations = [
            f"{name}：{line}" for name, line in zip(names, lines, strict=True) if line
        ]
        return "\n".join([format_result(row), *congratulations])

    async def start_message(self, game):
        identity = (game["info"]["id"], game["info"]["starttime"])
        cached = self._cards.get(identity)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        profiles = [dict(p) for p in game["players"]]

        async def enrich(i, player):
            await self.client.profile(player, game, destination=profiles[i])

        try:
            async with asyncio.timeout(180):
                await asyncio.gather(*(enrich(i, p) for i, p in enumerate(game["players"])))
        except TimeoutError:
            pass
        from .tenhou_profile import describe

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

    async def poll(self, send):
        subs = await self.store.subscriptions()
        if not subs:
            return
        if hasattr(self.client, "warm_history"):
            for name in {sub["name"] for sub in subs}:
                self.client.warm_history(name)
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
        for pending in await self.store.pending():
            game = json.loads(pending["payload"])
            names = {p["name"] for p in game["players"]}
            active = any(s["group_id"] == pending["group_id"] and s["name"] in names for s in subs)
            if active:
                try:
                    picture = await self.start_message(game)
                    current = await self.store.subscriptions(pending["group_id"])
                    if not any(s["name"] in names for s in current):
                        await self.store.dequeue(pending["group_id"], pending["game_id"])
                        continue
                    # Include start timestamp in the key: short watch IDs can be reused.
                    key = f"wg:{game['info']['id']}:{game['info']['starttime']}"
                    if not await self.deliver(pending["group_id"], key, "start", picture, send):
                        continue
                except Exception:
                    log.exception("Tenhou start delivery failed; retained for retry")
                    continue
            await self.store.dequeue(pending["group_id"], pending["game_id"])
        # One history request per player, regardless of number of subscribed groups.
        histories = {}
        for sub in subs:
            name = sub["name"]
            if name not in histories:
                start = min(s["cursor"] for s in subs if s["name"] == name)
                try:
                    histories[name] = await self.client.history(name, start)
                except TenhouError as exc:
                    histories[name] = exc
            rows = histories[name]
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
