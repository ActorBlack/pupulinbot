import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.db import Database
from pupulinbot.models import GameRecord, Player
from pupulinbot.monitor import GameMonitor
from pupulinbot.ms_watch import WatchCommands


async def database(tmp_path):
    db = Database(tmp_path / "watch.db")
    await db.initialize()
    await db.bind("alice", 42, "雀士")
    return db


@pytest.mark.asyncio
async def test_modes_coexist_and_unbind_cascades(tmp_path):
    db = await database(tmp_path)
    await db.subscribe("g", "alice", True)
    await db.subscribe("g", "alice", mode=3)
    assert {r["mode"] for r in await db.subscriptions()} == {3, 4}
    await db.unsubscribe("g", "alice", 3)
    assert [r["mode"] for r in await db.subscriptions()] == [4]
    await db.unbind("alice")
    assert not await db.subscriptions()


@pytest.mark.asyncio
async def test_existing_four_player_data_and_delivery_marks_migrate(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.executescript("""CREATE TABLE bindings(qq_id TEXT PRIMARY KEY,
        account_id INTEGER,nickname TEXT);
        CREATE TABLE subscriptions(group_id TEXT,qq_id TEXT,notify_start INTEGER DEFAULT 1,
        notify_finish INTEGER DEFAULT 1,auto_review INTEGER DEFAULT 0,created_at TEXT,
        PRIMARY KEY(group_id,qq_id));
        CREATE TABLE game_events(uuid TEXT,subscriber TEXT,event TEXT,created_at TEXT,
        PRIMARY KEY(uuid,subscriber,event));
        INSERT INTO bindings VALUES('u',42,'name');
        INSERT INTO subscriptions VALUES('g','u',1,1,1,'2020-01-01');
        INSERT INTO game_events VALUES('old','g:u','finish','2020-01-02');""")
    db = Database(path)
    await db.initialize()
    await db.initialize()
    row = (await db.subscriptions())[0]
    assert row["mode"] == 4 and row["auto_review"] == 1 and row["created_at"] == "2020-01-01"
    assert not await db.claim_event("old", row["subscriber"], "finish")
    await db.subscribe("g", "u", mode=3)
    assert len(await db.subscriptions()) == 2


@pytest.mark.asyncio
async def test_dynamic_lists_and_group_dedup_do_not_remove_other_owners(tmp_path):
    db = await database(tmp_path)
    player = Player(42, "雀士")
    await db.subscribe("g", "alice")
    for scope, owner in [("user", "bob"), ("group", "g")]:
        await db.follow(scope, owner, player, 4, group_id="g")
        await db.set_follow_monitor(scope, owner, "g", 4, True)
    assert len(await db.subscriptions()) == 1
    await db.unfollow("user", "bob", 4, group_id="g")
    await db.unsubscribe("g", "alice")
    assert len(await db.subscriptions()) == 1
    await db.unfollow("group", "g", 4, group_id="g")
    assert not await db.subscriptions()
    await db.follow("group", "g", Player(43, "新人"), 4, group_id="g")
    assert (await db.subscriptions())[0]["account_id"] == 43
    await db.set_follow_monitor("group", "g", "g", 4, False)
    assert not await db.subscriptions()
    assert await db.follows("group", "g", 4, group_id="g")


@pytest.mark.asyncio
async def test_group_delivery_scopes_and_follow_activation_cutoff(tmp_path):
    db = await database(tmp_path)
    await db.follow("user", "alice", Player(42, "雀士"), 4, group_id="g")
    await db.set_follow_monitor("user", "alice", "g", 4, True)
    await db.set_follow_monitor("user", "alice", "other", 4, True)
    await db.follow("user", "alice", Player(42, "雀士"), 4, group_id="other")
    async with db.connect() as c:
        await c.execute("UPDATE ms_follows SET created_at='2026-01-02T01:00:00+08:00'")
        await c.execute("UPDATE follow_monitors SET created_at='2026-01-01 00:00:00'")
        await c.commit()
    rows = await db.subscriptions()
    assert len(rows) == 2 and rows[0]["subscriber"] != rows[1]["subscriber"]
    assert all(r["created_at"] == "2026-01-02T01:00:00+08:00" for r in rows)
    before = rows[0]["created_at"]
    await db.set_follow_monitor("user", "alice", "g", 4, True)
    assert next(r for r in await db.subscriptions() if r["group_id"] == "g")["created_at"] == before


@pytest.mark.asyncio
async def test_flag_cutoffs_do_not_backdate_later_auto_review(tmp_path):
    db = await database(tmp_path)
    await db.subscribe("g", "alice", True)
    await db.follow("group", "g", Player(42, "雀士"), 4, group_id="g")
    await db.set_follow_monitor("group", "g", "g", 4, True)
    async with db.connect() as c:
        await c.execute("UPDATE subscriptions SET created_at='2026-01-03 00:00:00',notify_start=0")
        await c.execute("UPDATE ms_follows SET created_at='2026-01-01T01:00:00+08:00'")
        await c.execute("UPDATE follow_monitors SET created_at='2026-01-01T00:00:00+08:00'")
        await c.commit()
    row = (await db.subscriptions())[0]
    assert row["finish_since"] == "2026-01-01T01:00:00+08:00"
    assert row["start_since"] == "2026-01-01T01:00:00+08:00"
    assert row["review_since"] == "2026-01-03 00:00:00"


@pytest.mark.asyncio
async def test_watch_commands_permissions_and_mode_isolation(tmp_path):
    db = await database(tmp_path)
    client = SimpleNamespace(resolve=AsyncMock(return_value=Player(42, "雀士")))
    service = WatchCommands(
        db, SimpleNamespace(enabled=False), manager="alice", clients={3: client, 4: client}
    )
    for role in ("member", "admin", "owner"):
        with pytest.raises(ValueError, match="负责人"):
            await service.favorites("on 雀士", "bob", "g", role)
    assert "已添加" in await service.favorites("on 雀士 mode=3", "alice", "g")
    assert await service.favorites("list", "bob", "g") == "四麻：空\n三麻：雀士"
    assert "四麻：空\n三麻：空" == await service.favorites("list mode=4", "bob", "g")
    assert "已移除" in await service.favorites("off 雀士 mode=3", "alice", "g")
    assert not await db.subscriptions()
    assert not hasattr(service, "own")


@pytest.mark.asyncio
async def test_poller_routes_sanma_shares_fetch_and_sends_once_per_group(tmp_path):
    db = await database(tmp_path)
    await db.subscribe("g", "alice", mode=3)
    await db.follow("user", "bob", Player(42, "雀士"), 3, group_id="g")
    await db.set_follow_monitor("user", "bob", "g", 3, True)
    await db.set_follow_monitor("user", "bob", "other", 3, True)
    await db.follow("user", "bob", Player(42, "雀士"), 3, group_id="other")
    async with db.connect() as c:
        for table in ("subscriptions", "ms_follows", "follow_monitors"):
            await c.execute(f"UPDATE {table} SET created_at='2020-01-01'")
        await c.commit()
    record = GameRecord(
        "sanma",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        "24",
        [{"account_id": 42, "rank": 1, "score": 40000}],
        {},
    )
    four = SimpleNamespace(records=AsyncMock())
    three = SimpleNamespace(records=AsyncMock(return_value=[record]))
    send = AsyncMock()
    monitor = GameMonitor(db, four, SimpleNamespace(enabled=False), sanma_client=three)
    rows = await db.subscriptions()
    await monitor.poll(rows, send)
    assert send.await_count == 2 and three.records.await_count == 1
    four.records.assert_not_called()
    await monitor.poll(rows, send)
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_indexed_unfinished_record_is_not_announced_as_live(tmp_path):
    db = await database(tmp_path)
    await db.subscribe("g", "alice")
    record = GameRecord("partial", datetime.now(UTC), None, "12", [], {})
    monitor = GameMonitor(
        db,
        SimpleNamespace(records=AsyncMock(return_value=[record])),
        SimpleNamespace(enabled=False),
    )
    send = AsyncMock()
    await monitor.poll(await db.subscriptions(), send)
    send.assert_not_called()


@pytest.mark.asyncio
async def test_bound_favorite_can_enable_monitor_without_network(tmp_path):
    db = await database(tmp_path)
    await db.follow("group", "g", Player(42, "雀士"), 4, group_id="g")
    client = SimpleNamespace(resolve=AsyncMock(side_effect=RuntimeError("offline")))
    watch = WatchCommands(db, SimpleNamespace(enabled=False), manager="alice", clients={4: client})
    assert "已添加" in await watch.favorites("on 雀士", "alice", "g")
    client.resolve.assert_not_called()
    assert len(await db.subscriptions()) == 1


@pytest.mark.asyncio
async def test_english_favorite_watch_actions_preserve_scopes(tmp_path):
    db = await database(tmp_path)
    client = SimpleNamespace(resolve=AsyncMock(return_value=Player(42, "雀士")))
    service = WatchCommands(
        db, SimpleNamespace(enabled=False), manager="alice", clients={4: client}
    )
    assert "已添加" in await service.favorites("on 雀士", "alice", "g")
    assert "雀士" in await service.favorites("status", "bob", "g")
    assert "四麻：空\n三麻：空" == await service.favorites("list", "bob", "other")
    assert "已移除" in await service.favorites("off 42", "alice", "g")
    assert "四麻：空\n三麻：空" == await service.favorites("list", "alice", "g")


@pytest.mark.asyncio
async def test_chinese_monitor_actions_are_rejected_without_subscription_changes(tmp_path):
    db = await database(tmp_path)
    service = WatchCommands(db, SimpleNamespace(enabled=False), manager="alice")
    for text in ("开", "关", "状态", "on", "off", "on -g 雀士"):
        with pytest.raises(ValueError):
            await service.favorites(text, "alice", "g")
    assert not await db.subscriptions()


@pytest.mark.asyncio
async def test_global_start_switch_keeps_own_and_follow_finish_notifications(tmp_path):
    db = await database(tmp_path)
    await db.subscribe("own", "alice")
    await db.follow("user", "alice", Player(42, "雀士"), 4, group_id="follow")
    await db.set_follow_monitor("user", "alice", "follow", 4, True)
    async with db.connect() as conn:
        for table in ("subscriptions", "ms_follows", "follow_monitors"):
            await conn.execute(f"UPDATE {table} SET created_at='2020-01-01'")
        await conn.commit()
    record = GameRecord(
        "finished-only",
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        "12",
        [{"account_id": 42, "rank": 1, "score": 40000}],
        {},
    )
    live = SimpleNamespace(enabled=True, current_game=AsyncMock())
    records = SimpleNamespace(records=AsyncMock(return_value=[record]))
    monitor = GameMonitor(db, records, live, notify_starts=False)
    send = AsyncMock()
    await monitor.poll(await db.subscriptions(), send)
    live.current_game.assert_not_awaited()
    assert send.await_count == 2
    assert {call.args[0]["group_id"] for call in send.await_args_list} == {"own", "follow"}
    assert (
        WatchCommands(db, live, notify_starts=False).boundary()
        == "开局提醒已关闭；仅播报对局结算。"
    )
