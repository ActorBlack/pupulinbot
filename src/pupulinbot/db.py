import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings(
  qq_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, nickname TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS vip2_members(
  qq_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS capsolver_members(
  qq_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS daily_identities(
  qq_id TEXT PRIMARY KEY, assigned_on TEXT NOT NULL, identity TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_reactions(
  group_id INTEGER NOT NULL, feature TEXT NOT NULL, enabled INTEGER NOT NULL,
  PRIMARY KEY(group_id, feature)
);
CREATE TABLE IF NOT EXISTS subscriptions(
  group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
  notify_start INTEGER NOT NULL DEFAULT 1, notify_finish INTEGER NOT NULL DEFAULT 1,
  auto_review INTEGER NOT NULL DEFAULT 0,
  mode INTEGER NOT NULL DEFAULT 4 CHECK(mode IN (3,4)),
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(group_id, qq_id, mode),
  FOREIGN KEY(qq_id) REFERENCES bindings(qq_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS games(
  uuid TEXT PRIMARY KEY, account_id INTEGER NOT NULL, started_at TEXT NOT NULL,
  ended_at TEXT, payload TEXT NOT NULL, announced_start INTEGER NOT NULL DEFAULT 0,
  announced_finish INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS game_events(
  uuid TEXT NOT NULL, subscriber TEXT NOT NULL, event TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(uuid, subscriber, event)
);
CREATE TABLE IF NOT EXISTS ms_follows(
  group_id TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL, account_id INTEGER NOT NULL,
  nickname TEXT NOT NULL, mode INTEGER NOT NULL CHECK(mode IN (3,4)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(group_id,scope,scope_id,account_id,mode)
);
CREATE TABLE IF NOT EXISTS follow_monitors(
  scope TEXT NOT NULL CHECK(scope IN ('user','group')), scope_id TEXT NOT NULL,
  group_id TEXT NOT NULL, mode INTEGER NOT NULL CHECK(mode IN (3,4)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(scope,scope_id,group_id,mode)
);
CREATE TABLE IF NOT EXISTS monitor_health(
  subscriber TEXT PRIMARY KEY, checked_at TEXT NOT NULL,
  last_success TEXT, error TEXT, capped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reviews(
  uuid TEXT PRIMARY KEY, status TEXT NOT NULL, requester TEXT NOT NULL,
  result TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path, *, legacy_follow_group=None):
        self.path = path
        self.legacy_follow_group = legacy_follow_group

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.executescript(SCHEMA)
            async with db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='disabled_group_reactions'"
            ) as cursor:
                has_legacy_reactions = await cursor.fetchone() is not None
            if has_legacy_reactions:
                await db.execute(
                    "INSERT OR IGNORE INTO group_reactions (group_id, feature, enabled) "
                    "SELECT group_id, feature, 0 FROM disabled_group_reactions"
                )
            columns = {
                r["name"]
                for r in await (await db.execute("PRAGMA table_info(ms_follows)")).fetchall()
            }
            if "group_id" not in columns:
                await db.commit()
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("ALTER TABLE ms_follows RENAME TO legacy_ms_follows")
                await db.execute("""CREATE TABLE ms_follows(
                    group_id TEXT NOT NULL, scope TEXT NOT NULL, scope_id TEXT NOT NULL,
                    account_id INTEGER NOT NULL, nickname TEXT NOT NULL,
                    mode INTEGER NOT NULL CHECK(mode IN (3,4)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(group_id,scope,scope_id,account_id,mode))""")
                await db.execute("""INSERT OR IGNORE INTO ms_follows
                    SELECT scope_id,scope,scope_id,account_id,nickname,mode,created_at
                    FROM legacy_ms_follows WHERE scope='group'""")
                await db.execute("""INSERT OR IGNORE INTO ms_follows
                    SELECT m.group_id,f.scope,f.scope_id,f.account_id,f.nickname,f.mode,f.created_at
                    FROM legacy_ms_follows f JOIN follow_monitors m
                    ON m.scope=f.scope AND m.scope_id=f.scope_id AND m.mode=f.mode
                    WHERE f.scope='user'""")
                if self.legacy_follow_group:
                    await db.execute(
                        """INSERT OR IGNORE INTO ms_follows
                        SELECT ?,f.scope,f.scope_id,f.account_id,f.nickname,f.mode,f.created_at
                        FROM legacy_ms_follows f WHERE f.scope='user'
                        AND NOT EXISTS(SELECT 1 FROM follow_monitors m
                            WHERE m.scope=f.scope AND m.scope_id=f.scope_id AND m.mode=f.mode)""",
                        (str(self.legacy_follow_group),),
                    )
            cursor = await db.execute("PRAGMA table_info(subscriptions)")
            columns = {row["name"] for row in await cursor.fetchall()}
            if "auto_review" not in columns:
                await db.execute(
                    "ALTER TABLE subscriptions ADD COLUMN auto_review INTEGER NOT NULL DEFAULT 0"
                )
            if "created_at" not in columns:
                await db.execute("ALTER TABLE subscriptions ADD COLUMN created_at TEXT")
            await db.execute(
                "UPDATE subscriptions SET created_at=CURRENT_TIMESTAMP WHERE created_at IS NULL"
            )
            if "mode" not in columns:
                # Rebuild inside one transaction so old four-player subscriptions survive.
                await db.commit()
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("""CREATE TABLE subscriptions_v2(
                    group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
                    notify_start INTEGER NOT NULL DEFAULT 1,
                    notify_finish INTEGER NOT NULL DEFAULT 1,
                    auto_review INTEGER NOT NULL DEFAULT 0,
                    mode INTEGER NOT NULL DEFAULT 4 CHECK(mode IN (3,4)),
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(group_id,qq_id,mode),
                    FOREIGN KEY(qq_id) REFERENCES bindings(qq_id) ON DELETE CASCADE
                )""")
                await db.execute("""INSERT INTO subscriptions_v2
                    SELECT group_id,qq_id,notify_start,notify_finish,auto_review,4,created_at
                    FROM subscriptions""")
                await db.execute("DROP TABLE subscriptions")
                await db.execute("ALTER TABLE subscriptions_v2 RENAME TO subscriptions")
            cursor = await db.execute("PRAGMA table_info(game_events)")
            columns = {row["name"] for row in await cursor.fetchall()}
            # Existing deduplication records describe notifications already sent.
            if "status" not in columns:
                await db.execute(
                    "ALTER TABLE game_events ADD COLUMN status TEXT NOT NULL DEFAULT 'sent'"
                )
            if "lease_until" not in columns:
                await db.execute(
                    "ALTER TABLE game_events ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"
                )
            # Carry forward sent and pending leases when adopting account-level group dedup.
            await db.execute("""INSERT OR IGNORE INTO game_events
                (uuid,subscriber,event,created_at,status,lease_until)
                SELECT e.uuid,'group:'||s.group_id||':account:'||b.account_id||':mode:4',
                    e.event,e.created_at,e.status,e.lease_until
                FROM game_events e JOIN subscriptions s
                    ON e.subscriber=s.group_id||':'||s.qq_id
                JOIN bindings b ON b.qq_id=s.qq_id WHERE s.mode=4""")
            await db.commit()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            await db.close()

    async def bind(self, qq_id: str, account_id: int, nickname: str) -> None:
        async with self.connect() as db:
            # A different account starts a new monitoring history.
            await db.execute(
                "UPDATE subscriptions SET created_at=CURRENT_TIMESTAMP WHERE qq_id=? "
                "AND EXISTS(SELECT 1 FROM bindings WHERE qq_id=? AND account_id<>?)",
                (qq_id, qq_id, account_id),
            )
            await db.execute(
                "INSERT INTO bindings(qq_id,account_id,nickname) VALUES(?,?,?) "
                "ON CONFLICT(qq_id) DO UPDATE SET "
                "account_id=excluded.account_id,nickname=excluded.nickname",
                (qq_id, account_id, nickname),
            )
            await db.commit()

    async def assign_daily_identity(self, qq_id: str, candidate: str) -> tuple[str, bool]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            today = datetime.now(BEIJING_TZ).date().isoformat()
            async with db.execute(
                "SELECT identity, assigned_on FROM daily_identities WHERE qq_id=?", (qq_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None and row["assigned_on"] == today:
                return row["identity"], False
            await db.execute(
                """INSERT INTO daily_identities (qq_id, assigned_on, identity) VALUES (?, ?, ?)
                ON CONFLICT(qq_id) DO UPDATE SET assigned_on=excluded.assigned_on,
                    identity=excluded.identity""",
                (qq_id, today, candidate),
            )
            await db.commit()
            return candidate, True

    async def group_reaction_overrides(self) -> dict[tuple[int, str], bool]:
        async with self.connect() as db:
            async with db.execute(
                "SELECT group_id, feature, enabled FROM group_reactions"
            ) as cursor:
                return {
                    (row["group_id"], row["feature"]): bool(row["enabled"]) async for row in cursor
                }

    async def set_group_reaction(self, group_id: int, feature: str, enabled: bool) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO group_reactions (group_id, feature, enabled) VALUES (?, ?, ?) "
                "ON CONFLICT(group_id, feature) DO UPDATE SET enabled=excluded.enabled",
                (group_id, feature, enabled),
            )
            await db.commit()

    async def bind_if_absent(self, qq_id: str, account_id: int, nickname: str) -> bool:
        async with self.connect() as db:
            cursor = await db.execute(
                "INSERT INTO bindings(qq_id,account_id,nickname) VALUES(?,?,?) "
                "ON CONFLICT(qq_id) DO NOTHING",
                (qq_id, account_id, nickname),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def unbind(self, qq_id: str) -> bool:
        async with self.connect() as db:
            cursor = await db.execute("DELETE FROM bindings WHERE qq_id=?", (qq_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def binding(self, qq_id: str):
        async with self.connect() as db:
            return await (
                await db.execute("SELECT * FROM bindings WHERE qq_id=?", (qq_id,))
            ).fetchone()

    async def is_vip2(self, qq_id: str) -> bool:
        async with self.connect() as db:
            row = await (
                await db.execute("SELECT 1 FROM vip2_members WHERE qq_id=?", (qq_id,))
            ).fetchone()
            return row is not None

    async def set_vip2(self, qq_id: str, enabled: bool) -> bool:
        async with self.connect() as db:
            sql = (
                "INSERT INTO vip2_members(qq_id) VALUES(?) ON CONFLICT(qq_id) DO NOTHING"
                if enabled
                else "DELETE FROM vip2_members WHERE qq_id=?"
            )
            result = await db.execute(sql, (qq_id,))
            await db.commit()
            return result.rowcount > 0

    async def vip2_members(self) -> list[str]:
        async with self.connect() as db:
            rows = await (
                await db.execute("SELECT qq_id FROM vip2_members ORDER BY qq_id")
            ).fetchall()
            return [row["qq_id"] for row in rows]

    async def set_capsolver_member(self, qq_id: str, enabled: bool) -> bool:
        async with self.connect() as db:
            sql = (
                "INSERT INTO capsolver_members(qq_id) VALUES(?) ON CONFLICT(qq_id) DO NOTHING"
                if enabled
                else "DELETE FROM capsolver_members WHERE qq_id=?"
            )
            result = await db.execute(sql, (qq_id,))
            await db.commit()
            return result.rowcount > 0

    async def capsolver_members(self) -> list[str]:
        async with self.connect() as db:
            rows = await (
                await db.execute("SELECT qq_id FROM capsolver_members ORDER BY qq_id")
            ).fetchall()
            return [row["qq_id"] for row in rows]

    async def migrate_group_monitor_list(self):
        """Archive personal controls once, preserving effective per-group deliveries."""
        active = await self.subscriptions()
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("""CREATE TABLE IF NOT EXISTS monitor_configuration_archive(
                migration TEXT PRIMARY KEY,payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            exists = await (
                await db.execute(
                    "SELECT 1 FROM monitor_configuration_archive "
                    "WHERE migration='group-owner-only-v1'"
                )
            ).fetchone()
            if exists:
                await db.commit()
                return
            previous = {}
            for table in ("subscriptions", "ms_follows", "follow_monitors"):
                previous[table] = [
                    dict(r) for r in await (await db.execute(f"SELECT * FROM {table}")).fetchall()
                ]
            await db.execute(
                "INSERT INTO monitor_configuration_archive(migration,payload) VALUES(?,?)",
                ("group-owner-only-v1", json.dumps(previous, ensure_ascii=False)),
            )
            for table in ("subscriptions", "ms_follows", "follow_monitors"):
                await db.execute(f"DELETE FROM {table}")
            for row in active:
                group = row["group_id"]
                since = row.get("finish_since") or row["created_at"]
                await db.execute(
                    "INSERT INTO ms_follows"
                    "(group_id,scope,scope_id,account_id,nickname,mode,created_at) "
                    "VALUES(?,'group',?,?,?,?,?)",
                    (group, group, row["account_id"], row["nickname"], row["mode"], since),
                )
                await db.execute(
                    """INSERT INTO follow_monitors(scope,scope_id,group_id,mode,created_at)
                    VALUES('group',?,?,?,?) ON CONFLICT(scope,scope_id,group_id,mode) DO UPDATE SET
                    created_at=CASE WHEN julianday(excluded.created_at)<julianday(created_at)
                        THEN excluded.created_at ELSE created_at END""",
                    (group, group, row["mode"], since),
                )
            await db.commit()

    async def subscribe(
        self, group_id: str, qq_id: str, auto_review: bool = False, mode: int = 4
    ) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO subscriptions(group_id,qq_id,auto_review,mode,created_at) "
                "VALUES(?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(group_id,qq_id,mode) DO UPDATE SET auto_review=excluded.auto_review",
                (group_id, qq_id, int(auto_review), mode),
            )
            await db.commit()

    async def unsubscribe(self, group_id: str, qq_id: str, mode: int = 4) -> bool:
        async with self.connect() as db:
            cur = await db.execute(
                "DELETE FROM subscriptions WHERE group_id=? AND qq_id=? AND mode=?",
                (group_id, qq_id, mode),
            )
            await db.commit()
            return cur.rowcount > 0

    async def subscriptions(self):
        async with self.connect() as db:
            own = await (
                await db.execute(
                    "SELECT s.*,b.account_id,b.nickname FROM subscriptions s "
                    "JOIN bindings b USING(qq_id)"
                )
            ).fetchall()
            followed = await (
                await db.execute("""SELECT m.group_id,
                m.scope||':'||m.scope_id AS qq_id,f.account_id,f.nickname,m.mode,
                1 AS notify_start,1 AS notify_finish,0 AS auto_review,
                CASE WHEN julianday(m.created_at)>julianday(f.created_at)
                    THEN m.created_at ELSE f.created_at END AS created_at
                FROM follow_monitors m JOIN ms_follows f
                    ON m.group_id=f.group_id AND m.scope=f.scope
                        AND m.scope_id=f.scope_id AND m.mode=f.mode""")
            ).fetchall()
        groups = {}
        for value in [*own, *followed]:
            row = dict(value)
            key = (row["group_id"], row["account_id"], row["mode"])
            for event in ("start", "finish"):
                row[event + "_since"] = row["created_at"] if row["notify_" + event] else None
            row["review_since"] = row["created_at"] if row["auto_review"] else None
            row["subscriber"] = f"group:{key[0]}:account:{key[1]}:mode:{key[2]}"
            if key not in groups:
                groups[key] = row
                continue
            current = groups[key]

            def stamp(value):
                try:
                    parsed = datetime.fromisoformat(value)
                    return (
                        parsed.replace(tzinfo=UTC)
                        if parsed.tzinfo is None
                        else parsed.astimezone(UTC)
                    )
                except (TypeError, ValueError):
                    return datetime.max.replace(tzinfo=UTC)

            for field in ("created_at", "start_since", "finish_since", "review_since"):
                candidates = [v for v in (current.get(field), row.get(field)) if v is not None]
                current[field] = min(candidates, key=stamp) if candidates else None
            for flag in ("notify_start", "notify_finish", "auto_review"):
                current[flag] = int(bool(current[flag] or row[flag]))
        return list(groups.values())

    async def member_bindings(self, member_ids):
        async with self.connect() as db:
            return await (
                await db.execute(
                    """SELECT account_id, MIN(nickname) AS nickname FROM bindings
                    WHERE qq_id IN (SELECT value FROM json_each(?))
                    GROUP BY account_id ORDER BY nickname, account_id""",
                    (json.dumps([str(qq) for qq in member_ids]),),
                )
            ).fetchall()

    async def own_subscriptions(self, group_id: str, qq_id: str):
        async with self.connect() as db:
            return await (
                await db.execute(
                    "SELECT * FROM subscriptions WHERE group_id=? AND qq_id=? ORDER BY mode",
                    (group_id, qq_id),
                )
            ).fetchall()

    async def set_follow_monitor(self, scope, scope_id, group_id, mode, enabled):
        if scope not in {"user", "group"} or mode not in {3, 4}:
            raise ValueError("关注监控范围或模式无效")
        async with self.connect() as db:
            if enabled:
                await db.execute(
                    "INSERT INTO follow_monitors(scope,scope_id,group_id,mode) VALUES(?,?,?,?) "
                    "ON CONFLICT(scope,scope_id,group_id,mode) DO NOTHING",
                    (scope, scope_id, group_id, mode),
                )
            else:
                await db.execute(
                    "DELETE FROM follow_monitors WHERE scope=? AND scope_id=? "
                    "AND group_id=? AND mode=?",
                    (scope, scope_id, group_id, mode),
                )
            await db.commit()

    async def follow_monitor(self, scope, scope_id, group_id, mode):
        async with self.connect() as db:
            return await (
                await db.execute(
                    "SELECT * FROM follow_monitors WHERE scope=? AND scope_id=? "
                    "AND group_id=? AND mode=?",
                    (scope, scope_id, group_id, mode),
                )
            ).fetchone()

    async def claim_game_event(
        self, game, account_id: int, finished: bool, subscriber: str = "default"
    ) -> bool:
        """Store a record and reserve its notification until acknowledged or expired."""
        event = "finish" if finished else "start"
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO games"
                "(uuid,account_id,started_at,ended_at,payload) VALUES(?,?,?,?,?) "
                "ON CONFLICT(uuid) DO UPDATE SET "
                "ended_at=COALESCE(excluded.ended_at,games.ended_at),payload=excluded.payload",
                (
                    game.uuid,
                    account_id,
                    game.started_at.isoformat(),
                    game.ended_at.isoformat() if game.ended_at else None,
                    json.dumps(game.raw, ensure_ascii=False),
                ),
            )
            await db.commit()
        return await self.claim_event(game.uuid, subscriber, event)

    async def claim_event(self, uuid: str, subscriber: str, event: str) -> bool:
        """Reserve an event for five minutes; abandoned sends can then be retried."""
        now = time.time()
        async with self.connect() as db:
            cursor = await db.execute(
                "INSERT INTO game_events(uuid,subscriber,event,status,lease_until) "
                "VALUES(?,?,?,'pending',?) ON CONFLICT(uuid,subscriber,event) DO UPDATE SET "
                "lease_until=excluded.lease_until "
                "WHERE game_events.status='pending' AND game_events.lease_until<=?",
                (uuid, subscriber, event, now + 300, now),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def complete_event(self, uuid: str, subscriber: str, event: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE game_events SET status='sent',lease_until=0 "
                "WHERE uuid=? AND subscriber=? AND event=?",
                (uuid, subscriber, event),
            )
            await db.commit()

    async def release_event(self, uuid: str, subscriber: str, event: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "DELETE FROM game_events WHERE uuid=? AND subscriber=? AND event=? "
                "AND status='pending'",
                (uuid, subscriber, event),
            )
            await db.commit()

    async def follows(self, scope: str, scope_id: str, mode: int, *, group_id: str):
        async with self.connect() as db:
            return await (
                await db.execute(
                    "SELECT * FROM ms_follows WHERE group_id=? "
                    "AND scope=? AND scope_id=? AND mode=? "
                    "ORDER BY nickname,account_id",
                    (str(group_id), scope, scope_id, mode),
                )
            ).fetchall()

    async def follow(self, scope: str, scope_id: str, player, mode: int, *, group_id: str):
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO ms_follows(group_id,scope,scope_id,account_id,nickname,mode) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(group_id,scope,scope_id,account_id,mode) DO UPDATE SET "
                "nickname=excluded.nickname",
                (str(group_id), scope, scope_id, player.account_id, player.nickname, mode),
            )
            await db.commit()

    async def unfollow(
        self, scope: str, scope_id: str, mode: int, account_id=None, *, group_id: str
    ):
        async with self.connect() as db:
            query = "DELETE FROM ms_follows WHERE group_id=? AND scope=? AND scope_id=? AND mode=?"
            params = [str(group_id), scope, scope_id, mode]
            if account_id is not None:
                query += " AND account_id=?"
                params.append(account_id)
            cursor = await db.execute(query, params)
            await db.commit()
            return cursor.rowcount

    async def set_monitor_health(self, subscriber, checked_at, error=None, capped=False):
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO monitor_health(subscriber,checked_at,last_success,error,capped) "
                "VALUES(?,?,?,?,?) ON CONFLICT(subscriber) DO UPDATE SET "
                "checked_at=excluded.checked_at,error=excluded.error,capped=excluded.capped,"
                "last_success=COALESCE(excluded.last_success,monitor_health.last_success)",
                (subscriber, checked_at, checked_at if error is None else None, error, int(capped)),
            )
            await db.commit()

    async def monitor_health(self, subscriber):
        async with self.connect() as db:
            return await (
                await db.execute("SELECT * FROM monitor_health WHERE subscriber=?", (subscriber,))
            ).fetchone()
