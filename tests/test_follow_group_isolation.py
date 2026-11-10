import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.db import Database
from pupulinbot.models import Player
from pupulinbot.ms_watch import WatchCommands


def legacy_database(path, *, second_group=False):
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE ms_follows(
          scope TEXT,scope_id TEXT,account_id INTEGER,nickname TEXT,mode INTEGER,
          created_at TEXT,PRIMARY KEY(scope,scope_id,account_id,mode));
        CREATE TABLE follow_monitors(
          scope TEXT,scope_id TEXT,group_id TEXT,mode INTEGER,created_at TEXT,
          PRIMARY KEY(scope,scope_id,group_id,mode));
        INSERT INTO ms_follows VALUES('user','alice',42,'旧关注',4,'2026-01-01');
        INSERT INTO ms_follows VALUES('user','bob',43,'未启用关注',4,'2026-01-02');
        INSERT INTO ms_follows VALUES('group','old',44,'群关注',4,'2026-01-03');
        INSERT INTO follow_monitors VALUES('user','alice','old',4,'2026-01-04');
        """)
        if second_group:
            conn.execute(
                "INSERT INTO follow_monitors VALUES('user','alice','second',4,'2026-01-04')"
            )


@pytest.mark.asyncio
async def test_migration_keeps_old_group_data_and_leaves_new_group_empty(tmp_path):
    path = tmp_path / "db.sqlite"
    legacy_database(path)
    db = Database(path, legacy_follow_group="old")
    await db.initialize()
    await db.initialize()
    assert [r["account_id"] for r in await db.follows("user", "alice", 4, group_id="old")] == [42]
    assert [r["account_id"] for r in await db.follows("user", "bob", 4, group_id="old")] == [43]
    assert await db.follows("group", "old", 4, group_id="old")
    assert not await db.follows("user", "alice", 4, group_id="new")
    assert not await db.follows("user", "bob", 4, group_id="new")
    assert {(r["group_id"], r["account_id"]) for r in await db.subscriptions()} == {("old", 42)}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legacy_ms_follows").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM ms_follows").fetchone()[0] == 3
        assert (
            conn.execute("SELECT created_at FROM ms_follows WHERE account_id=42").fetchone()[0]
            == "2026-01-01"
        )


@pytest.mark.asyncio
async def test_previously_enabled_groups_get_independent_snapshots(tmp_path):
    path = tmp_path / "db.sqlite"
    legacy_database(path, second_group=True)
    db = Database(path, legacy_follow_group="old")
    await db.initialize()
    await db.unfollow("user", "alice", 4, group_id="old")
    assert await db.follows("user", "alice", 4, group_id="second")
    assert {(r["group_id"], r["account_id"]) for r in await db.subscriptions()} == {("second", 42)}
    await db.initialize()
    assert not await db.follows("user", "alice", 4, group_id="old")


@pytest.mark.asyncio
async def test_same_member_add_clear_and_monitor_switches_stay_in_current_group(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.initialize()
    players = {"旧玩家": Player(42, "旧玩家"), "新玩家": Player(43, "新玩家")}
    client = SimpleNamespace(resolve=AsyncMock(side_effect=lambda name: players[name]))
    watch = WatchCommands(db, SimpleNamespace(enabled=False), manager="alice", clients={4: client})
    await watch.favorites("on 旧玩家", "alice", "old")
    assert "四麻：空\n三麻：空" == await watch.favorites("list", "bob", "new")
    await watch.favorites("on 新玩家", "alice", "new")
    assert {(r["group_id"], r["account_id"]) for r in await db.subscriptions()} == {
        ("old", 42),
        ("new", 43),
    }
    await watch.favorites("off all", "alice", "new")
    assert "旧玩家" in await watch.favorites("list", "bob", "old")
    assert {(r["group_id"], r["account_id"]) for r in await db.subscriptions()} == {("old", 42)}


@pytest.mark.asyncio
async def test_owner_list_migration_preserves_active_targets_and_cutoffs_once(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.initialize()
    await db.bind("alice", 42, "本人")
    await db.subscribe("old", "alice")
    await db.follow("user", "bob", Player(43, "个人监控"), 4, group_id="old")
    await db.set_follow_monitor("user", "bob", "old", 4, True)
    await db.follow("user", "alice", Player(44, "新群监控"), 3, group_id="new")
    await db.set_follow_monitor("user", "alice", "new", 3, True)
    await db.follow("user", "alice", Player(45, "未开启的收藏"), 4, group_id="old")
    before = {
        (r["group_id"], r["account_id"], r["mode"]): r["finish_since"]
        for r in await db.subscriptions()
    }
    await db.migrate_group_monitor_list()
    after = {
        (r["group_id"], r["account_id"], r["mode"]): r["finish_since"]
        for r in await db.subscriptions()
    }
    assert before == after
    async with db.connect() as conn:
        assert (await (await conn.execute("SELECT COUNT(*) FROM subscriptions")).fetchone())[0] == 0
        assert (
            await (
                await conn.execute("SELECT COUNT(*) FROM ms_follows WHERE scope='user'")
            ).fetchone()
        )[0] == 0
        archived = await (
            await conn.execute("SELECT payload FROM monitor_configuration_archive")
        ).fetchone()
        assert "未开启的收藏" in archived["payload"]
    await db.unfollow("group", "old", 4, 43, group_id="old")
    await db.migrate_group_monitor_list()
    assert {(r["group_id"], r["account_id"]) for r in await db.subscriptions()} == {
        ("old", 42),
        ("new", 44),
    }
