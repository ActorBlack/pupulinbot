import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

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


async def test_mslast_returns_one_block_per_game_with_links_and_pt():
    games = [game(2), game(1)]
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "测试")),
        history=AsyncMock(return_value=(games, False)),
    )
    service = MahjongQueries(None, client, None)
    blocks = await service.query("mslast", "测试 -r 玉南 -l 2", 1, 2)
    assert len(blocks) == 2 and blocks[0].startswith("测试（四麻）")
    for block, record in zip(blocks, games, strict=True):
        assert "PT +10" in block and block.endswith(record.url)


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


def test_mslife_reports_peak_to_trough_drawdown_and_marks_incomplete_totals(monkeypatch):
    from pupulinbot.batch_chart import Figure

    labels = []
    original = Figure.text

    def capture(figure, x, y, value, *args, **kwargs):
        labels.append(str(value))
        return original(figure, x, y, value, *args, **kwargs)

    monkeypatch.setattr(Figure, "text", capture)
    games = [game(i, delta) for i, delta in enumerate([100, -40, -70, 0, 20])]
    player, options = Player(42, "测试"), parse_options("-l 5 -r 玉南")
    render_history("mslife", player, games, False, options)
    assert {"净变化", "+10", "+120", "-110", "最大回撤", "110"} <= set(labels)
    assert any("单场最高升分 +100（第 1 场）　最大掉分 -70（第 3 场）" in s for s in labels)
    labels.clear()
    games[1] = game(1, None)
    render_history("mslife", player, games, False, options)
    assert {"已记录净变化", "+50", "—"} <= set(labels)
    assert any("有效 4/5 场" in s for s in labels)


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
async def test_removed_follow_options_do_not_modify_bindings(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    await db.initialize()
    client = SimpleNamespace(resolve=AsyncMock(return_value=Player(42, "测试")))
    service = MahjongQueries(db, client, client)
    for text in ("-a 测试", "-d 测试", "-c", "-g -a 测试"):
        with pytest.raises(ValueError, match="个人关注已取消"):
            await service.query("ms4", text, "alice", "group")
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
@pytest.mark.parametrize(
    ("command", "renderer", "options"),
    [
        ("mslife", "render_history", ""),
        ("msheat", "render_history", ""),
        ("msstat", "render_history", ""),
        ("mssession", "render_history", ""),
        ("mscompare", "render_comparison", ""),
        ("mslast", "render_recent", " -i"),
    ],
)
async def test_room_filtered_charts_default_to_latest_room(command, renderer, options):
    latest = game(2)
    latest.mode = "15"
    sample = game(1)
    sample.mode = "15"
    client = SimpleNamespace(
        modes=[8, 9, 11, 12, 15, 16],
        resolve=AsyncMock(return_value=Player(42, "测试")),
        history=AsyncMock(side_effect=[([latest], False), ([sample], False)]),
    )
    service = MahjongQueries(None, client, None)
    with patch(f"pupulinbot.ms_commands.{renderer}", return_value=b"png"):
        assert await service.query(command, "测试 -l 5" + options, 1, 2) == b"png"
    assert client.history.await_args_list == [
        call(42, 1, modes=None),
        call(42, 5, start=None, end=None, modes=[15]),
    ]


@pytest.mark.asyncio
async def test_mslife_applies_dates_to_sample_and_game_length_to_room_selection():
    latest = game(200)
    latest.mode = "11"
    sample = game(1)
    sample.mode = "11"
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "测试")),
        history=AsyncMock(side_effect=[([latest], False), ([sample], False)]),
    )
    service = MahjongQueries(None, client, None)
    text = "测试 game=东 start=2026-01-01 end=2026-01-02"
    dates = parse_options(text)
    with patch("pupulinbot.ms_commands.render_history", return_value=b"png"):
        assert await service.query("mslife", text, 1, 2) == b"png"
    assert client.history.await_args_list == [
        call(42, 1, modes=[8, 11, 15]),
        call(42, 200, start=dates.start, end=dates.end, modes=[11]),
    ]
