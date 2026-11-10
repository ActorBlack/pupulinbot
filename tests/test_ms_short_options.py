from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from test_ms_summary import games

from pupulinbot.models import Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_options import parse_options


@pytest.mark.parametrize(
    "room,expected",
    [("玉", 12), ("玉东", 11), ("王座", 16), ("王座东", 15), ("金", 9), ("金东", 8)],
)
def test_short_room_and_count_equal_existing_options(room, expected):
    short = parse_options(f'"玩家 名字" -l 100 -r {room} -i', single_room=True)
    original = parse_options(f'"玩家 名字" last=100 room={room} -i', single_room=True)
    assert short == original
    assert short.player == "玩家 名字" and short.modes == [expected]
    assert short.last == 100 and short.last_explicit and short.image


@pytest.mark.parametrize("text", ["-l", "-r", "-l -i", "-r -l 100", "-l nope", "-l 2001"])
@pytest.mark.asyncio
async def test_invalid_short_options_fail_before_network(text):
    client = SimpleNamespace(resolve=AsyncMock(), history=AsyncMock())
    service = MahjongQueries(None, client, client)
    with pytest.raises(ValueError):
        await service.query("ms", "玩家 " + text, 1, 2)
    client.resolve.assert_not_awaited()
    client.history.assert_not_awaited()


@pytest.mark.parametrize("command", ["ms", "ms4", "ms2", "ms3"])
@pytest.mark.parametrize("count", [50, 200])
@pytest.mark.asyncio
async def test_short_count_selects_sample_without_being_overridden(command, count):
    client = SimpleNamespace(resolve=AsyncMock(return_value=Player(42, "玩家")))
    service = MahjongQueries(None, client, client)
    service.summary_query = AsyncMock(return_value="sample")
    assert await service.query(command, f"玩家 -l {count} -r 王座", 1, 2) == "sample"
    options = service.summary_query.call_args.args[1]
    assert options.last == count and options.modes == ([26] if command == "ms3" else [16])


@pytest.mark.parametrize("command,count", [("mslast", 5), ("msdetail", 200)])
@pytest.mark.asyncio
async def test_short_count_is_used_for_records_and_charts(command, count):
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "玩家")),
        history=AsyncMock(return_value=(games(5), False)),
    )
    service = MahjongQueries(None, client, client)
    service.sample_stats = AsyncMock(return_value=None)
    with patch("pupulinbot.ms_commands.render_sample", return_value=b"png"):
        await service.query(command, f"玩家 -l {count} -r 玉", 1, 2)
    client.history.assert_awaited_once_with(42, count, start=None, end=None, modes=[12])


@pytest.mark.asyncio
async def test_short_count_without_room_keeps_latest_room_selection():
    latest = games(1)[0]
    latest.mode = "16"
    client = SimpleNamespace(
        modes=[8, 9, 11, 12, 15, 16],
        resolve=AsyncMock(return_value=Player(42, "玩家")),
        history=AsyncMock(return_value=([latest], False)),
    )
    service = MahjongQueries(None, client, client)
    service.summary_query = AsyncMock(return_value="sample")
    assert await service.query("ms", "玩家 -l 50", 1, 2) == "sample"
    client.history.assert_awaited_once_with(42, 1, modes=None)
    options = service.summary_query.call_args.args[1]
    assert options.last == 50 and options.modes == [16]


@pytest.mark.parametrize("count", ["-l 500", "last=500", "-l 0", "last=0"])
@pytest.mark.asyncio
async def test_ms_limit_is_500_for_bound_users_in_both_option_formats(count):
    db = SimpleNamespace(binding=AsyncMock(return_value={"account_id": 42, "nickname": "玩家"}))
    client = SimpleNamespace(history=AsyncMock())
    service = MahjongQueries(db, client, client)
    service.summary_query = AsyncMock(return_value="summary")
    assert await service.query("ms", f"{count} -r 玉", 1, 2) == "summary"
    options = service.summary_query.call_args.args[1]
    assert options.last == 500 and options.last_explicit


@pytest.mark.parametrize("count", ["-l 501", "last=501", "-l 2000", "last=2000"])
@pytest.mark.asyncio
async def test_ms_rejects_over_500_before_lookup(count):
    client = SimpleNamespace(resolve=AsyncMock(), history=AsyncMock())
    service = MahjongQueries(None, client, client)
    with pytest.raises(ValueError, match="1–500"):
        await service.query("ms", "玩家 " + count, 1, 2)
    client.resolve.assert_not_awaited()
    client.history.assert_not_awaited()
