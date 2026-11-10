import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from pupulinbot.db import Database
from pupulinbot.formatting import format_records, format_stats, percent
from pupulinbot.models import GameRecord, PlayerStats
from pupulinbot.paipuya import PaipuyaClient
from pupulinbot.review import ReviewResult, extract_uuid, render_review


def game(ended=True):
    return GameRecord(
        "240101-test",
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 1, 1, tzinfo=UTC) if ended else None,
        "四人南",
        [{"account_id": 42, "rank": 1, "score": 42000}],
        {"uuid": "240101-test"},
    )


def test_extract_uuid():
    assert extract_uuid("https://game.maj-soul.com/1/?paipu=abc_def&lang=zh") == "abc_def"
    assert extract_uuid("abc") == "abc"
    with pytest.raises(ValueError):
        extract_uuid("")
    with pytest.raises(ValueError):
        extract_uuid("https://evil.invalid/path")


def test_record_parser_and_format():
    item = {
        "uuid": "x",
        "startTime": 1_700_000_000_000,
        "endTime": 1_700_000_100_000,
        "mode": "玉之间",
        "players": [{"account_id": 42, "rank": 2, "score": 28000}],
    }
    parsed = PaipuyaClient._record(item)
    assert parsed.uuid == "x"
    assert "28000" in format_records([parsed], 42)


@pytest.mark.asyncio
async def test_database_bind_subscribe_and_deduplicate(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    await db.bind("100", 42, "雀士")
    assert (await db.binding("100"))["account_id"] == 42
    await db.subscribe("200", "100")
    subscriptions = await db.subscriptions()
    assert len(subscriptions) == 1
    assert subscriptions[0]["auto_review"] == 0
    await db.subscribe("200", "100", True)
    assert (await db.subscriptions())[0]["auto_review"] == 1
    assert await db.claim_game_event(game(), 42, True)
    assert not await db.claim_game_event(game(), 42, True)


@pytest.mark.asyncio
async def test_delivery_retry_lease_and_group_isolation(tmp_path):
    db = Database(tmp_path / "delivery.db")
    await db.initialize()
    assert await db.claim_event("game", "group1:user", "finish")
    assert not await db.claim_event("game", "group1:user", "finish")
    assert await db.claim_event("game", "group2:user", "finish")
    await db.release_event("game", "group1:user", "finish")
    assert await db.claim_event("game", "group1:user", "finish")
    with patch("pupulinbot.db.time.time", return_value=10**12):
        assert await db.claim_event("game", "group1:user", "finish")
    await db.complete_event("game", "group1:user", "finish")
    await db.release_event("game", "group1:user", "finish")
    with patch("pupulinbot.db.time.time", return_value=10**13):
        assert not await db.claim_event("game", "group1:user", "finish")


@pytest.mark.asyncio
async def test_old_database_migration_and_new_subscriptions(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE bindings(qq_id TEXT PRIMARY KEY, account_id INTEGER NOT NULL,
                nickname TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE subscriptions(group_id TEXT, qq_id TEXT,
                notify_start INTEGER DEFAULT 1, notify_finish INTEGER DEFAULT 1,
                PRIMARY KEY(group_id,qq_id),
                FOREIGN KEY(qq_id) REFERENCES bindings(qq_id) ON DELETE CASCADE);
            CREATE TABLE game_events(uuid TEXT, subscriber TEXT, event TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(uuid,subscriber,event));
            INSERT INTO bindings(qq_id,account_id,nickname) VALUES('1',42,'old');
            INSERT INTO subscriptions(group_id,qq_id) VALUES('2','1');
            INSERT INTO game_events(uuid,subscriber,event) VALUES('old','2:1','finish');
        """)
    db = Database(path)
    await db.initialize()
    await db.initialize()
    assert (await db.subscriptions())[0]["created_at"]
    assert not await db.claim_event("old", "2:1", "finish")
    await db.subscribe("3", "1")
    assert all(row["created_at"] for row in await db.subscriptions())
    # Repair NULL timestamps inserted by the older nullable-column migration.
    async with db.connect() as conn:
        await conn.execute("UPDATE subscriptions SET created_at=NULL")
        await conn.commit()
    await db.initialize()
    assert all(row["created_at"] for row in await db.subscriptions())
    await db.unbind("1")
    assert not await db.subscriptions()


@pytest.mark.asyncio
async def test_game_record_is_updated_on_settlement(tmp_path):
    db = Database(tmp_path / "games.db")
    await db.initialize()
    assert await db.claim_game_event(game(False), 42, False)
    assert await db.claim_game_event(game(True), 42, True)
    async with db.connect() as conn:
        row = await (await conn.execute("SELECT ended_at FROM games")).fetchone()
        assert row["ended_at"] == game(True).ended_at.isoformat()


def test_render_png():
    data = render_review(ReviewResult(87.5, "A", None, {}), "player", "uuid")
    assert data.startswith(b"\x89PNG")


def test_stats_format_accepts_fractional_rates():
    stats = PlayerStats(
        42, "四麻", 100, 0.3, 0.25, 0.25, 0.2, 2.35, 0.02, 0.22, 0.11, 0.18, 0.31, {}
    )
    rendered = format_stats("雀士", stats)
    assert "平均顺位：2.35" in rendered
    assert "和牌 22.00%" in rendered
    assert percent(12.3) == "12.30%"
