import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings(
  qq_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL, nickname TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS subscriptions(
  group_id TEXT NOT NULL, qq_id TEXT NOT NULL,
  notify_start INTEGER NOT NULL DEFAULT 1, notify_finish INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(group_id, qq_id), FOREIGN KEY(qq_id) REFERENCES bindings(qq_id) ON DELETE CASCADE
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
CREATE TABLE IF NOT EXISTS reviews(
  uuid TEXT PRIMARY KEY, status TEXT NOT NULL, requester TEXT NOT NULL,
  result TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.executescript(SCHEMA)
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
            await db.execute(
                "INSERT INTO bindings(qq_id,account_id,nickname) VALUES(?,?,?) "
                "ON CONFLICT(qq_id) DO UPDATE SET "
                "account_id=excluded.account_id,nickname=excluded.nickname",
                (qq_id, account_id, nickname),
            )
            await db.commit()

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

    async def subscribe(self, group_id: str, qq_id: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO subscriptions(group_id,qq_id) VALUES(?,?)", (group_id, qq_id)
            )
            await db.commit()

    async def unsubscribe(self, group_id: str, qq_id: str) -> bool:
        async with self.connect() as db:
            cur = await db.execute(
                "DELETE FROM subscriptions WHERE group_id=? AND qq_id=?", (group_id, qq_id)
            )
            await db.commit()
            return cur.rowcount > 0

    async def subscriptions(self):
        async with self.connect() as db:
            return await (
                await db.execute(
                    "SELECT s.*,b.account_id,b.nickname FROM subscriptions s "
                    "JOIN bindings b USING(qq_id)"
                )
            ).fetchall()

    async def claim_game_event(
        self, game, account_id: int, finished: bool, subscriber: str = "default"
    ) -> bool:
        """Atomically mark an event; returns false when it was already announced."""
        event = "finish" if finished else "start"
        async with self.connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO games"
                "(uuid,account_id,started_at,ended_at,payload) VALUES(?,?,?,?,?)",
                (
                    game.uuid,
                    account_id,
                    game.started_at.isoformat(),
                    game.ended_at.isoformat() if game.ended_at else None,
                    json.dumps(game.raw, ensure_ascii=False),
                ),
            )
            cursor = await db.execute(
                "INSERT OR IGNORE INTO game_events(uuid,subscriber,event) VALUES(?,?,?)",
                (game.uuid, subscriber, event),
            )
            await db.commit()
            return cursor.rowcount > 0
