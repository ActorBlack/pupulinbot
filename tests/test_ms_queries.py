import asyncio
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pupulinbot.db import Database
from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_charts import pt_series, render_history, sessions
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_options import level_label, parse_options
from pupulinbot.paipuya import PaipuyaClient, PaipuyaError


def game(i=0, delta=10):
    stamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)
    return GameRecord(
        str(i),
        stamp,
        stamp + timedelta(minutes=30),
        "12",
        [{"account_id": 42, "gradingScore": delta, "level": 10502}],
        {},
    )


def test_date_timezone_end_inclusive_and_mode_rooms():
    options = parse_options("喵杰 mode=3 room=玉,王 start=2026-01-01 end=2026-01-01 last=0")
    assert options.modes == [23, 24, 25, 26]
    assert options.last == 2000
    assert options.start == datetime(2025, 12, 31, 16, tzinfo=UTC)
    assert options.end - options.start == timedelta(days=1)
    assert level_label({"id": 10502}) == "雀圣2星"


@pytest.mark.parametrize(
    "text",
    [
        "last=2001",
        "mode=2",
        "-n nickname",
        "start=2026-02-01 end=2026-01-01",
        "-m -y",
        "room=银",
        "foo=1",
        "start=2026-01-01 -y",
    ],
)
def test_invalid_options_fail_before_network(text):
    with pytest.raises(ValueError):
        parse_options(text)


def test_delta_not_absolute_pt_and_missing_value_not_zero():
    assert pt_series([game(1, -15), game(0, 20)], 42) == [20, 5]
    values = pt_series([game(0), game(1, None), game(2)], 42)
    assert values[0] == 10 and all(math.isnan(v) for v in values[1:])
    assert [len(s) for s in sessions([game(0), game(1), game(4)])] == [2, 1]
    missing = game(0)
    missing.ended_at = None
    assert len(sessions([missing, game(1)])) == 2


@pytest.mark.parametrize("kind", ["mslife", "msheat", "msstat", "mssession"])
def test_chart_outputs_real_png(kind):
    data = render_history(
        kind, Player(42, "测试"), [game(0), game(1)], True, parse_options("last=2")
    )
    assert data.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_pagination_ceiling_filter_and_peek():
    client = PaipuyaClient("https://example.invalid")

    def raw(i):
        return {"uuid": str(i), "startTime": 1700000000 + i, "modeId": 12, "players": []}

    client._get = AsyncMock(side_effect=[[raw(4), raw(3)], [raw(2)], [raw(1)]])
    rows, more = await client.history(42, 2)
    assert [r.uuid for r in rows] == ["4", "3"] and more
    assert client._get.await_count == 2
    first_path = client._get.call_args_list[1].args[0]
    assert first_path.endswith("/1700000002999/0")
    with pytest.raises(PaipuyaError):
        await client.history(42, 2, modes=[26])


@pytest.mark.asyncio
async def test_follows_scope_and_other_binding_permission(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.initialize()
    client = SimpleNamespace(resolve=AsyncMock(return_value=Player(42, "测试")))
    service = MahjongQueries(db, client, client)
    for text in ("-a 测试", "-d 测试", "-c", "-g -a 测试"):
        with pytest.raises(ValueError, match="个人关注已取消"):
            await service.query("ms4", text, "alice", "group", role="owner")
    with pytest.raises(ValueError, match="本人"):
        await service.query("ms2", "测试", "alice", "group", mentioned="bob")
    assert await db.binding("bob") is None
    assert not await db.subscriptions()


@pytest.mark.asyncio
async def test_graph_selection_uses_correct_sanma_client_and_no_extra_stats():
    four = SimpleNamespace(history=AsyncMock())
    three = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "测试")),
        history=AsyncMock(return_value=([game()], False)),
    )
    service = MahjongQueries(None, four, three)
    with patch("pupulinbot.ms_commands.render_history", return_value=b"png"):
        result = await service.query("msheat", "测试 mode=3 room=玉 last=50", 1, 2)
    assert result == b"png"
    assert three.history.call_args.kwargs["modes"] == [24]
    four.history.assert_not_called()


@pytest.mark.asyncio
async def test_superuser_can_bind_another_member_without_group_admin_role(tmp_path):
    db = Database(tmp_path / "superuser.db")
    await db.initialize()
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "测试")),
        stats=AsyncMock(return_value=SimpleNamespace(raw={})),
    )
    service = MahjongQueries(db, client, client, admins={"285843647"})
    assert (
        await service.query("ms2", "测试", "285843647", "group", role="member", mentioned="bob")
        == "绑定成功"
    )
    assert (await db.binding("bob"))["account_id"] == 42
    client.stats.assert_not_awaited()


@pytest.mark.asyncio
async def test_ms2_existing_binding_returns_only_confirmation_and_preserves_account(tmp_path):
    db = Database(tmp_path / "existing.db")
    await db.initialize()
    await db.bind("bob", 42, "原昵称")
    client = SimpleNamespace(resolve=AsyncMock(), stats=AsyncMock())
    service = MahjongQueries(db, client, client)
    for name in ("原昵称", "另一个账号"):
        assert (
            await service.query("ms2", name, "alice", "group", role="admin", mentioned="bob")
            == "该群友已绑定"
        )
    assert (await db.binding("bob"))["account_id"] == 42
    assert (await db.binding("bob"))["nickname"] == "原昵称"
    client.resolve.assert_not_awaited()
    client.stats.assert_not_awaited()


@pytest.mark.asyncio
async def test_ms2_without_player_still_queries_bound_member(tmp_path):
    db = Database(tmp_path / "query.db")
    await db.initialize()
    await db.bind("bob", 42, "原昵称")
    client = SimpleNamespace(stats=AsyncMock(return_value=SimpleNamespace(raw={})))
    service = MahjongQueries(db, client, client)
    with (
        patch("pupulinbot.ms_commands.format_stats", return_value="原昵称的统计"),
        patch("pupulinbot.ms_commands.style_lines", return_value=[]),
    ):
        result = await service.query("ms2", "", "alice", "group", mentioned="bob")
    assert result.startswith("原昵称的统计")
    client.stats.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_concurrent_ms2_bindings_do_not_overwrite_each_other(tmp_path):
    db = Database(tmp_path / "concurrent.db")
    await db.initialize()

    async def resolve(name):
        return Player(42 if name == "玩家一" else 43, name)

    client = SimpleNamespace(resolve=AsyncMock(side_effect=resolve))
    service = MahjongQueries(db, client, client)
    results = await asyncio.gather(
        service.query("ms2", "玩家一", "alice", "group", role="admin", mentioned="bob"),
        service.query("ms2", "玩家二", "alice", "group", role="admin", mentioned="bob"),
    )
    assert sorted(results) == sorted(["绑定成功", "该群友已绑定"])
    assert (await db.binding("bob"))["account_id"] in {42, 43}
