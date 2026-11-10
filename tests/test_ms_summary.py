from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_options import parse_options
from pupulinbot.ms_summary import (
    format_level,
    format_summary,
    js_fixed,
    projection,
    rank_progress,
)
from pupulinbot.paipuya import PaipuyaClient

NOW = datetime(2026, 11, 7, 10, tzinfo=UTC)
LEVEL = {"id": 10502, "score": 3700, "delta": 85}


def games(count=100):
    result = []
    for i in range(count):
        rank = i % 4 + 1
        result.append(
            GameRecord(
                str(i),
                NOW - timedelta(hours=count - i),
                NOW,
                "12",
                [{"account_id": 42, "rank": rank, "score": (40000, 30000, 20000, 10000)[rank - 1]}],
                {},
            )
        )
    return result


def stats(count=100):
    return SimpleNamespace(
        games=count,
        win_rate=0.223,
        deal_in_rate=0.109,
        call_rate=0.254,
        riichi_rate=0.201,
        raw={
            "level": LEVEL,
            "max_level": {"id": 10502, "score": 5900, "delta": 80},
            "平均打点": 6800,
            "rank_rates": [0.25, 0.25, 0.25, 0.25],
            "rank_avg_score": [40000, 30000, 20000, 10000],
            "avg_rank": 2.5,
        },
    )


def test_rank_progress_includes_delta_and_adjusts_promotion_demotion_and_legacy():
    assert format_level(LEVEL) == "圣2 3785/6000"
    assert format_level({"id": 10502, "score": 5900, "delta": 80}) == "圣2 5980/6000"
    assert format_level({"id": 10502, "score": 5900, "delta": 110}) == "圣3 4500/9000"
    assert format_level({"id": 10501, "score": 10, "delta": -20}) == "豪3 1800/3600"
    assert format_level({"id": 10601, "score": 0, "delta": 0}) == "魂天Lv1 2.0/20.0 魂珠"
    assert format_level({"id": 10719, "score": 2000, "delta": 0}) == "魂天Lv20 0.0 魂珠"
    assert rank_progress({"id": 10502, "score": float("nan"), "delta": 0}) is None
    assert rank_progress(10502) is None


def test_summary_separates_sample_lifetime_and_activity_and_matches_sample_count():
    rows = games()
    options = parse_options("last=100 room=玉")
    context = {"total": stats(2193), "sample": stats(), "activity": (rows, False)}
    text = format_summary(Player(42, "喵杰"), rows, True, options, context, now=NOW)
    assert "喵杰(圣2 3785/6000) - 玉南最近100战" in text
    assert "圣2 5980/6000" in text and "2193" in text
    assert "24/100/100" in text and "22.3% / 10.9%" in text
    assert "[3412341234]" in text and "均顺2.50" in text
    assert "对战数: 2193" in text
    assert not any(
        note in text
        for note in (
            "评价:",
            "模型:",
            "口径:",
            "左旧右新",
            "本模式全部房间",
            "已达查询上限",
            "非接口直接返回",
        )
    )
    assert "牌风: 数据不足" in text and "#门前倾向" not in text
    context["sample"] = stats(101)
    text = format_summary(Player(42, "喵杰"), rows, False, options, context, now=NOW)
    assert "和/铳: — / —" in text and "牌风: 数据不足" in text


def test_projection_uses_api_averages_and_upstream_page_rounding():
    value = stats()
    # Upstream EV -15 and display round(3785/15)=252, rather than local ceil.
    result = projection(value, {"12"}, LEVEL)
    assert "豪1.00" in result and "252 战" in result and "-15.0 PT" in result
    assert "仅支持玉南/王座南" in projection(value, {"12", "16"}, LEVEL)
    assert "不足100" not in projection(stats(5), {"12"}, LEVEL)
    value.raw["rank_rates"] = [1, 0, 0, 0]
    assert "无四位" in projection(value, {"12"}, LEVEL)
    value.raw.pop("rank_avg_score")
    assert "安定段: —" in projection(value, {"12"}, LEVEL)
    assert js_fixed(0.25, 1) == "0.3" and js_fixed(-0.25, 1) == "-0.3"


def test_missing_stats_do_not_produce_style_or_fake_rates():
    text = format_summary(Player(42, "x"), games(5), False, parse_options("last=10"), {}, now=NOW)
    assert "—/—/—" in text and "和/铳: — / —" in text
    assert "最近5战" in text and "牌风: 数据不足" in text
    assert "平均打点: — | 平均铳点: —\n和铳度差: —" in text


@pytest.mark.parametrize(
    "win,deal,win_points,deal_points,expected",
    [
        (0.223, 0.109, 6800, 5400, "+927.8"),
        (22.3, 10.9, 6800, 5400, "+927.8"),
        (0.1, 0.2, 6000, 7000, "-800.0"),
        (0, 0, 0, 0, "+0.0"),
        (None, 0.109, 6800, 5400, "—"),
        (0.223, 0.109, 6800, None, "—"),
        (0.223, 0.109, float("nan"), 5400, "—"),
        (0.223, 0.109, 6800, float("inf"), "—"),
        (0.223, 0.109, -6800, 5400, "—"),
    ],
)
def test_summary_uses_matching_sample_points_and_weighted_win_deal_difference(
    win, deal, win_points, deal_points, expected
):
    sample, total = stats(5), stats(2193)
    sample.win_rate, sample.deal_in_rate = win, deal
    sample.raw.update({"平均打点": win_points, "平均铳点": deal_points})
    # Lifetime averages must never supply missing sample data.
    total.raw.update({"平均打点": 9999, "平均铳点": 9999})
    text = format_summary(
        Player(42, "喵杰"),
        games(5),
        False,
        parse_options("-l 5"),
        {"total": total, "sample": sample},
        now=NOW,
    )
    assert f"和铳度差: {expected}\n" in text
    assert "9999" not in text
    if win_points == 6800 and deal_points == 5400:
        assert "平均打点: 6800 | 平均铳点: 5400" in text
    sample.games = 6
    text = format_summary(
        Player(42, "喵杰"),
        games(5),
        False,
        parse_options("-l 5"),
        {"total": total, "sample": sample},
        now=NOW,
    )
    assert "平均打点: — | 平均铳点: —\n和铳度差: —" in text


@pytest.mark.asyncio
async def test_swapped_commands_preserve_profile_and_default_summary():
    client = SimpleNamespace(
        modes=[11, 12],
        resolve=AsyncMock(return_value=Player(42, "喵杰")),
        history=AsyncMock(return_value=(games(), True)),
        stats=AsyncMock(return_value=stats(2193)),
        range_stats=AsyncMock(return_value=stats()),
    )
    service = MahjongQueries(None, client, client)
    result = await service.query("ms4", "喵杰 last=100 room=玉", 1, 2)
    assert "最近100战" in result and "22.3%" in result
    assert client.range_stats.call_args.kwargs["modes"] == [12]
    with patch("pupulinbot.ms_commands.format_stats", return_value="cumulative"):
        assert (await service.query("ms4", "喵杰", 1, 2)).startswith("cumulative")
        for other in ("ms2", "ms3"):
            assert (await service.query(other, "喵杰", 1, 2)).startswith("cumulative")
    service.summary_query = AsyncMock(return_value="sample")
    assert await service.query("ms", "喵杰", 1, 2) == "sample"


@pytest.mark.asyncio
async def test_range_stats_uses_exact_bounds_keeps_game_count_and_caches():
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(
        side_effect=[
            {"count": 100, "rank_rates": [25, 25, 25, 25]},
            {"count": 1100, "和牌率": 0.223},
        ]
    )
    start = NOW - timedelta(days=10)
    value = await client.range_stats(42, start=start, end=NOW, modes=[12])
    assert value.games == 100 and value.win_rate == 0.223
    assert str(int(start.timestamp() * 1000)) in client._get.call_args_list[0].args[0]
    assert str(int(NOW.timestamp() * 1000) - 1) in client._get.call_args_list[0].args[0]
    await client.range_stats(42, start=start, end=NOW, modes=[12])
    assert client._get.await_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_rank_metadata_does_not_wait_for_or_pollute_hand_stats_cache():
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(return_value={"count": 2193, "level": LEVEL})
    value = await client.metadata(42)
    assert value.games == 2193 and format_level(value.raw["level"]) == "圣2 3785/6000"
    await client.metadata(42)
    assert client._get.await_count == 1
    assert client._get.call_args.args[0].startswith("player_stats/")
    assert ("stats", 42) not in client.cache.entries
    await client.metadata(42, modes=[12])
    assert client._get.call_args.args[1]["mode"] == "12"
    await client.metadata(42, modes=[12])
    assert client._get.await_count == 2
    await client.metadata(42, modes=[16])
    assert client._get.call_args.args[1]["mode"] == "16"
    assert client._get.await_count == 3
    await client.close()


def test_api_aggregate_is_authoritative_even_when_record_distribution_differs():
    value = stats()
    value.raw["rank_rates"] = [0.30, 0.26, 0.18, 0.26]
    value.raw["avg_rank"] = 2.40
    text = format_summary(
        Player(42, "喵杰"),
        games(),
        False,
        parse_options("last=100"),
        {"total": stats(2193), "sample": value},
        now=NOW,
    )
    assert "30.0%, 26.0%, 18.0%, 26.0% (均顺2.40)" in text
    assert "#低放铳" not in text
    # A missing API distribution stays missing; records are not used as a substitute.
    value.raw.pop("rank_rates")
    text = format_summary(
        Player(42, "喵杰"), games(), False, parse_options("last=100"), {"sample": value}, now=NOW
    )
    assert "1~4位: — / — / — / — (均顺—)" in text


def test_upstream_celestial_boundary_and_fractional_pt_display():
    value = stats()
    value.raw["rank_rates"] = [0.15, 0.35, 0.35, 0.15]
    # Royal-south base EV39, Saint2 penalty33.75 => EV5.25; page shows5.3.
    # Celestial EV is exactly0, whose upstream discrete label is 魂天1 (0).
    text = projection(value, {"16"}, LEVEL)
    assert "魂天Lv1 (0.0魂珠)" in text and "+5.3 PT/战" in text


@pytest.mark.parametrize(
    "text,mode,expected",
    [
        ("last=100", 4, [12]),
        ("room=玉", 4, [12]),
        ("room=王座", 4, [16]),
        ("game=东", 4, [11]),
        ("room=王 game=东", 4, [15]),
        ("room=玉东", 4, [11]),
        ("last=100", 3, [24]),
        ("room=王座南", 3, [26]),
    ],
)
def test_single_room_query_defaults_and_explicit_filters(text, mode, expected):
    assert parse_options(text, mode=mode, single_room=True).modes == expected


@pytest.mark.asyncio
async def test_summary_defaults_to_latest_room_and_rejects_mixed_rooms_before_lookup():
    rows = games()
    for row in rows:
        row.mode = "16"
    client = SimpleNamespace(
        modes=[11, 12, 15, 16],
        resolve=AsyncMock(return_value=Player(42, "y024")),
        history=AsyncMock(side_effect=[([rows[-1]], True), (rows, False)]),
        stats=AsyncMock(return_value=stats(1165)),
        range_stats=AsyncMock(return_value=stats()),
    )
    service = MahjongQueries(None, client, client)
    result = await service.query("ms", "y024 last=100", 1, 2)
    assert "王座南最近100战" in result and "仅支持玉南" not in result
    assert client.history.call_args_list[0].args == (42, 1)
    assert client.history.call_args_list[0].kwargs == {"modes": None}
    assert client.history.call_args.kwargs["modes"] == [16]
    assert client.range_stats.call_args.kwargs["modes"] == [16]
    client.resolve.reset_mock()
    with pytest.raises(ValueError, match="只能查询一个房间"):
        await service.query("ms", "y024 room=玉,王", 1, 2)
    client.resolve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,mode,room,lookup_modes",
    [
        ("y024", 4, 9, None),
        ("y024 -i last=50", 4, 11, None),
        ("y024 game=南", 4, 16, [9, 12, 16]),
        ("y024 game=东", 4, 15, [8, 11, 15]),
        ("y024 mode=3", 3, 26, None),
        ("y024 start=2026-01-01 end=2026-01-02", 4, 16, None),
    ],
)
async def test_ms_latest_room_respects_mode_and_length_but_not_sample_dates(
    text, mode, room, lookup_modes
):
    latest = games(1)[0]
    latest.mode = str(room)
    client = SimpleNamespace(
        modes=[8, 9, 11, 12, 15, 16] if mode == 4 else [21, 22, 23, 24, 25, 26],
        resolve=AsyncMock(return_value=Player(42, "y024")),
        history=AsyncMock(),
        query_history=AsyncMock(return_value=([latest], True)),
    )
    other = SimpleNamespace(resolve=AsyncMock(), history=AsyncMock())
    service = MahjongQueries(None, client if mode == 4 else other, client if mode == 3 else other)
    service.summary_query = AsyncMock(return_value="summary")
    assert await service.query("ms", text, 1, 2) == "summary"
    client.query_history.assert_awaited_once_with(42, 1, modes=lookup_modes)
    client.history.assert_not_awaited()
    other.resolve.assert_not_awaited()
    options = service.summary_query.call_args.args[1]
    assert options.modes == [room] and options.mode == mode
    assert options.last == (50 if "last=50" in text else 100)
    assert options.image == ("-i" in text)
    if "start=" in text:
        assert options.start is not None and options.end is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("text,room", [("room=玉", 12), ("room=王座东", 15), ("r=金南", 9)])
async def test_ms_explicit_room_skips_latest_room_lookup(text, room):
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "y024")), history=AsyncMock()
    )
    service = MahjongQueries(None, client, client)
    service.summary_query = AsyncMock(return_value="summary")
    assert await service.query("ms", "y024 " + text, 1, 2) == "summary"
    assert service.summary_query.call_args.args[1].modes == [room]
    client.history.assert_not_awaited()


@pytest.mark.asyncio
async def test_ms_without_records_does_not_fall_back_to_jade_south():
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "y024")),
        history=AsyncMock(return_value=([], False)),
    )
    service = MahjongQueries(None, client, client)
    service.summary_query = AsyncMock()
    assert await service.query("ms", "y024", 1, 2) == "y024: 暂无已结算收录对局"
    client.history.assert_awaited_once_with(42, 1, modes=None)
    service.summary_query.assert_not_awaited()
