import json
import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest
from test_ms_summary import NOW, format_summary, games, stats

from pupulinbot.models import Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_options import parse_options
from pupulinbot.ms_style import analyze_style, classify, style_lines

ORACLE = json.loads((Path(__file__).parent / "fixtures/style-reference-oracle.json").read_text())
KINDS = {
    "後手反撃型": "后手反击型",
    "先手躱手型": "先手闪避型",
    "門前打点型": "门前打点型",
    "副露速度型": "副露速度型",
    "鉄壁地蔵型": "铁壁地藏型",
    "全局参加型": "全局参与型",
}


@pytest.mark.parametrize("case", ORACLE, ids=lambda c: c["name"])
def test_matches_original_javascript_oracle(case):
    result = analyze_style(SimpleNamespace(raw=case["metrics"], games=100))
    expected = case["expected"]
    assert result.x == pytest.approx(expected["X"], abs=1e-12)
    assert result.y == pytest.approx(expected["Y"], abs=1e-12)
    assert result.strength == expected["T"].replace("軽", "轻")
    assert result.kind == KINDS[expected["U"]]


@pytest.mark.parametrize(
    "threshold,at,above",
    [
        (1.47, "中庸", "轻度"),
        (3.2, "轻度", "中度"),
        (8.89, "中度", "重度"),
        (12.71, "重度", "超重度"),
    ],
)
def test_strict_strength_boundaries(threshold, at, above):
    assert classify(threshold, 0)[0] == at
    assert classify(math.nextafter(threshold, math.inf), 0)[0] == above


def test_axes_and_sector_edges_follow_source_js_not_arctangent_guess():
    assert classify(0, 0)[1] == "全局参与型"
    assert classify(0, 1)[1] == "先手闪避型"
    assert classify(0, -1)[1] == "全局参与型"
    assert classify(1, 1)[1] == "门前打点型"
    assert classify(1, -0.35)[1] == "铁壁地藏型"
    assert classify(-1, -1)[1] == "副露速度型"


@pytest.mark.parametrize("bad", [None, True, -0.1, 1.1, float("nan"), float("inf")])
def test_bad_or_missing_inputs_are_never_filled_with_zero(bad):
    raw = dict(ORACLE[-1]["metrics"])
    raw["先制率"] = bad
    result = analyze_style(SimpleNamespace(raw=raw))
    assert result.x is None and "立直先制率" in result.missing
    assert not analyze_style(None, 3).supported


def test_recent_summary_uses_sample_style_and_rejects_mismatched_count():
    sample = stats(100)
    sample.raw.update(ORACLE[-1]["metrics"])
    lifetime = stats(2193)
    lifetime.raw.update(ORACLE[1]["metrics"])
    text = format_summary(
        Player(42, "x"),
        games(),
        False,
        parse_options("last=100"),
        {"sample": sample, "total": lifetime},
        now=NOW,
    )
    assert "#" + analyze_style(sample).label in text and "最近100战" in text
    sample.games = 101
    text = format_summary(
        Player(42, "x"),
        games(),
        False,
        parse_options("last=100"),
        {"sample": sample, "total": lifetime},
        now=NOW,
    )
    assert "牌风: 数据不足" in text and "牌风: #" not in text
    assert style_lines(SimpleNamespace(raw=ORACLE[-1]["metrics"], games=5)) == [
        "牌风: #" + analyze_style(SimpleNamespace(raw=ORACLE[-1]["metrics"])).label
    ]


@pytest.mark.asyncio
async def test_style_query_loads_exact_sample_not_lifetime_and_propagates_filters():
    sample = stats(100)
    sample.raw.update(ORACLE[-1]["metrics"])
    sample.raw["level"] = {"id": 10403}
    current = {"id": 10502, "score": 3900, "delta": 15}
    client = SimpleNamespace(
        modes=[11, 12],
        resolve=AsyncMock(return_value=Player(42, "x")),
        history=AsyncMock(return_value=(games(), False)),
        range_stats=AsyncMock(return_value=sample),
        metadata=AsyncMock(return_value=SimpleNamespace(raw={"level": current})),
        stats=AsyncMock(),
    )
    service = MahjongQueries(None, client, client)
    with patch("pupulinbot.ms_commands.render_style", return_value=b"png") as render:
        assert await service.query("msstyle", "x last=100 room=玉南", 1, 2) == b"png"
    assert render.call_args.args[1] is sample
    assert render.call_args.args[3].modes == [12]
    assert render.call_args.kwargs["current_level"] == current
    client.metadata.assert_awaited_once_with(42)
    assert client.range_stats.call_args.kwargs["modes"] == [12]
    client.stats.assert_not_called()
    sample.games = 101
    assert (
        await service.sample_stats(Player(42, "x"), games(), parse_options("last=100"), client)
        is None
    )
    client.range_stats.side_effect = TimeoutError()
    assert (
        await service.sample_stats(Player(42, "x"), games(), parse_options("last=100"), client)
        is None
    )
    client.range_stats.reset_mock()
    assert (
        await service.sample_stats(Player(42, "x"), games(), parse_options("mode=3"), client)
        is None
    )
    client.range_stats.assert_not_called()


@pytest.mark.asyncio
async def test_style_without_room_uses_latest_four_player_room_and_its_mean():
    records = [replace(game, mode="16") for game in games()]
    sample = stats(len(records))
    client = SimpleNamespace(
        modes=[8, 9, 11, 12, 15, 16],
        resolve=AsyncMock(return_value=Player(42, "x")),
        history=AsyncMock(side_effect=[([records[0]], False), (records, False)]),
        range_stats=AsyncMock(return_value=sample),
    )
    service = MahjongQueries(None, client, None)
    with patch("pupulinbot.ms_commands.render_style", return_value=b"png") as render:
        assert await service.query("msstyle", "x", 1, 2) == b"png"
    assert client.history.await_args_list == [
        call(42, 1, modes=None),
        call(42, 200, start=None, end=None, modes=[16]),
    ]
    assert render.call_args.args[3].modes == [16]
    assert client.range_stats.call_args.kwargs["modes"] == [16]


def test_style_report_renders_complete_and_missing_inputs_without_faking_coordinates():
    import io

    from PIL import Image

    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_chart import SIZE, render_style
    from pupulinbot.ms_style_radar import comparison_reference

    ref = comparison_reference("12", rank_identity(10502))
    complete = SimpleNamespace(
        raw={**ORACLE[-1]["metrics"], **ref.metrics, "_hand_count": 1024}, games=len(games())
    )
    for value in (None, complete):
        image = Image.open(
            io.BytesIO(
                render_style(
                    Player(42, "超长昵称" * 20),
                    value,
                    games(),
                    parse_options("last=100"),
                    current_level={"id": 10502, "score": 3900, "delta": 15},
                )
            )
        )
        assert image.size == SIZE and image.mode == "RGB"


def test_style_displays_tags_in_ascending_rarity_with_stable_ties(monkeypatch):
    from PIL import ImageDraw

    from pupulinbot import ms_style_chart

    tags = [
        "体面结局",
        "副露进攻",
        "重炮手",
        "无声快刀",
        "日常踩雷",
        "国士受害者",
        "一击必杀",
        "普普林",
        "略胜一筹",
        "大三元",
        "副露进攻",
    ]
    tiers = {
        "体面结局": "gray",
        "副露进攻": "blue",
        "无声快刀": "gold",
        "日常踩雷": "orange",
        "一击必杀": "purple",
        "普普林": "easter_egg",
        "略胜一筹": "green",
    }
    drawn = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, value, *args, **kwargs):
        if value in tags:
            drawn.append(value)
        return original(draw, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    monkeypatch.setattr(ms_style_chart, "supplemental_tags", lambda *args, **kwargs: tags)
    monkeypatch.setattr(ms_style_chart, "rarity_tier", tiers.__getitem__)
    ms_style_chart.render_style(
        Player(42, "测试"), stats(100), games(), parse_options("-r 玉南 -l 100")
    )
    assert drawn == [
        "体面结局",
        "略胜一筹",
        "副露进攻",
        "一击必杀",
        "日常踩雷",
        "无声快刀",
        "国士受害者",
        "大三元",
        "重炮手",
        "普普林",
    ]


def test_radar_centers_exact_current_grade_in_each_room():
    from pupulinbot.ms_level import rank_identity, rank_progress
    from pupulinbot.ms_style_radar import AXES, axis_score, comparison_reference

    for room, level in (
        ("8", 10403),
        ("9", 10403),
        ("11", 10502),
        ("12", 10502),
        ("15", 10503),
        ("16", 10503),
    ):
        ref = comparison_reference(room, rank_identity(level))
        assert sum(ref.rank_rates) == pytest.approx(1)
        for axis in AXES:
            assert ref.scales[axis.metric.key] > 0
            assert axis_score(axis, ref.metrics, ref, 1000) == 50

    promoted = rank_progress({"id": 10501, "score": 3980, "delta": 50})
    saint2 = comparison_reference("12", promoted)
    master2 = comparison_reference("12", rank_identity(10402))
    assert saint2.metrics["放铳率"] < master2.metrics["放铳率"]
    assert saint2.rank_rates != master2.rank_rates
    assert comparison_reference("99", promoted) is None
    assert comparison_reference("16", rank_identity(10712)).level == 10712


@pytest.mark.parametrize(
    ("room", "player_level", "baseline_level"),
    [
        ("12", 10702, 10701),
        ("12", None, 10701),
        ("9", 10702, 10501),
        ("16", 10717, 10720),
        ("15", 10716, 10720),
    ],
)
def test_radar_falls_back_to_highest_usable_grade_in_the_same_room(
    room, player_level, baseline_level
):
    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_radar import comparison_reference

    fallback = comparison_reference(room, rank_identity(player_level))
    assert fallback.level == baseline_level
    assert fallback == comparison_reference(room, rank_identity(baseline_level))


def test_radar_bonus_uses_unrounded_extremes_and_caps_at_150():
    from pupulinbot.ms_style_chart import BONUS_START, radar_bonus_score

    assert radar_bonus_score(None) is None
    for deviation, expected in (
        (-5, None),
        (0, None),
        (2.9, None),
        (3, None),
        (4, 125),
        (5, 150),
        (9, 150),
    ):
        score = 50 * (1 + math.erf(deviation / math.sqrt(2)))
        assert radar_bonus_score(score) == expected

    score = 50 * (1 + math.erf(2.7 / math.sqrt(2)))
    assert {axis: radar_bonus_score(score, start=start) for axis, start in BONUS_START.items()} == {
        "进攻": 112,
        "防守": 116,
        "副露": 112,
        "立直": 102,
        "速度": None,
        "打点": 112,
    }
    assert all(radar_bonus_score(100, start=start) == 150 for start in BONUS_START.values())


def test_radar_combines_opportunities_and_propagates_cross_axis_changes():
    from pupulinbot.ms_style_radar import composite_metrics

    raw = {
        "和牌率": 0.25,
        "放铳率": 0.10,
        "流局率": 0.15,
        "流听率": 0.40,
        "平均打点": 6400,
        "平均铳点": 6000,
        "副露率": 0.30,
        "副露后和牌率": 0.40,
        "副露后放铳率": 0.20,
        "立直率": 0.15,
        "立直收支": 2800,
        "和了巡数": 12,
        "立直巡目": 9,
    }
    values = composite_metrics(raw)
    assert values["进攻触达率"] == pytest.approx(0.31)
    assert values["铳点损失"] == pytest.approx(600)
    assert values["副露净结果率"] == pytest.approx(0.06)
    assert values["立直局均收支"] == pytest.approx(420)
    assert values["净打点效率"] == pytest.approx(1000)

    heavier = composite_metrics({**raw, "平均铳点": 8000})
    assert heavier["铳点损失"] == pytest.approx(800)
    assert heavier["净打点效率"] == pytest.approx(800)
    more_riichi = composite_metrics({**raw, "立直率": 0.25})
    assert more_riichi["立直局均收支"] == pytest.approx(700)
    assert composite_metrics({**raw, "平均铳点": None})["铳点损失"] is None
    assert composite_metrics({**raw, "平均铳点": None, "放铳率": 0})["铳点损失"] == 0
    assert composite_metrics({**raw, "副露后和牌率": 0.9})["副露净结果率"] is None


def test_radar_shrinks_event_rates_and_amounts_by_observable_sampling_variance():
    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_radar import AXES, axis_score, comparison_reference

    ref = comparison_reference("12", rank_identity(10502))
    attack, defense, call, _, _, points = AXES
    attack_mean = ref.metrics[attack.metric.key]
    call_mean = ref.metrics[call.metric.key]
    call_second_moment = ref.metrics["副露和牌贡献率"] + ref.metrics["副露放铳贡献率"]
    loss_mean = ref.metrics[defense.metric.key]
    deal_rate = ref.metrics["放铳率"]
    for axis, variance in (
        (attack, attack_mean * (1 - attack_mean)),
        (call, call_second_moment - call_mean**2),
        (defense, loss_mean**2 * (1 - deal_rate) / deal_rate),
    ):
        key = axis.metric.key
        mean, scale = ref.metrics[key], ref.scales[key]
        actual = {key: mean + axis.direction * scale}
        assert axis_score(axis, actual, ref, 99) is None
        small = axis_score(axis, actual, ref, 100)
        large = axis_score(axis, actual, ref, 1000)
        assert 50 < small < large < 84.14
        noise = variance * (1 / 1000 + 1 / ref.hands)
        expected = 50 * (1 + math.erf((scale**2 / (scale**2 + noise)) / math.sqrt(2)))
        assert large == pytest.approx(expected)

    assert axis_score(defense, {}, ref, 1000) is None
    assert axis_score(points, {}, ref, 1000) is None


@pytest.mark.parametrize("room, level", [("9", 10303), ("12", 10503), ("16", 10503)])
def test_radar_speed_compares_turns_at_fixed_event_mix_and_accounts_for_sample_size(room, level):
    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_radar import AXES, axis_score, comparison_reference, speed_turns

    ref = comparison_reference(room, rank_identity(level))
    speed = next(axis for axis in AXES if axis.name == "速度")
    key = speed.metric.key
    same_turns = {**ref.metrics, "和牌率": 0.32, "立直率": 0.15}
    same_turns[key] = speed_turns(same_turns, ref.speed_win_weight)
    assert axis_score(speed, same_turns, ref, 495) == pytest.approx(50)

    actual = {**same_turns, "立直巡目": same_turns["立直巡目"] - 0.5}
    actual[key] = speed_turns(actual, ref.speed_win_weight)
    assert 50 < axis_score(speed, actual, ref, 100) < axis_score(speed, actual, ref, 1000)
    scale = ref.scales[key]
    noise = ref.speed_sampling_variance * (1 / 495 + 1 / ref.hands)
    deviation = (ref.metrics[key] - actual[key]) * scale / (scale**2 + noise)
    assert axis_score(speed, actual, ref, 495) == pytest.approx(
        50 * (1 + math.erf(deviation / math.sqrt(2)))
    )
    assert axis_score(speed, {**actual, "和牌率": 0.01, "立直率": 0.01}, ref, 1000) is None
    assert axis_score(speed, {**actual, "立直率": 0}, ref, 1000) is None


def test_radar_speed_uses_independent_gold_south_sampling():
    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_radar import comparison_reference

    grade = rank_identity(10403)
    gold, jade, throne = (comparison_reference(room, grade) for room in ("9", "12", "16"))
    assert gold.speed_sampling_variance > 0
    assert gold.speed_sampling_variance != pytest.approx(jade.speed_sampling_variance)
    assert throne.speed_sampling_variance == jade.speed_sampling_variance


@pytest.mark.asyncio
async def test_profile_filter_does_not_use_summary_image_route():
    sample = stats(100)
    sample.raw.update(ORACLE[-1]["metrics"])
    client = SimpleNamespace(
        modes=[12],
        resolve=AsyncMock(return_value=Player(42, "x")),
        history=AsyncMock(return_value=(games(), False)),
        range_stats=AsyncMock(return_value=sample),
        stats=AsyncMock(),
    )
    service = MahjongQueries(None, client, client)
    service.profile_query = AsyncMock(return_value=b"profile")
    assert await service.query("ms4", "x -i -l 100 -r 玉南", 1, 2) == b"profile"
    assert service.profile_query.call_args.args[1].last == 100
    with pytest.raises(ValueError, match="文字摘要"):
        await service.query("ms", "x last=100 -i", 1, 2)


@pytest.mark.asyncio
async def test_sanma_style_uses_its_exact_room_sample_and_rank():
    records = [replace(game, mode="24") for game in games()]
    sample = stats(len(records))
    current = {"id": 20403, "score": 2700, "delta": 10}
    sanma = SimpleNamespace(
        modes=[24, 22, 26],
        resolve=AsyncMock(return_value=Player(42, "x")),
        history=AsyncMock(return_value=(records, False)),
        range_stats=AsyncMock(return_value=sample),
        metadata=AsyncMock(return_value=SimpleNamespace(raw={"level": current})),
        recent_yakuman_names=AsyncMock(),
    )
    service = MahjongQueries(None, None, sanma)
    with patch("pupulinbot.ms_commands.render_style", return_value=b"png") as render:
        assert await service.query("msstyle3", "x -r 玉南 -l 100", 1, 2) == b"png"
    assert render.call_args.args[1] is sample
    assert render.call_args.args[3].modes == [24]
    assert render.call_args.kwargs["current_level"] == current
    assert sanma.range_stats.call_args.kwargs["modes"] == [24]
    sanma.recent_yakuman_names.assert_not_called()


def test_sanma_radar_has_independent_room_and_grade_references():
    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_radar import AXES, axis_score, comparison_reference

    grade = rank_identity(20403)
    jade = comparison_reference("24", grade)
    gold = comparison_reference("22", grade)
    yonma = comparison_reference("12", rank_identity(10403))
    assert jade.level == gold.level == 20403
    assert yonma.level == 10403
    assert len(jade.rank_rates) == 3
    assert len(yonma.rank_rates) == 4
    assert jade.metrics != gold.metrics
    assert jade.scales != gold.scales
    assert jade.scales != yonma.scales
    assert yonma.point_sampling_variances is None
    assert all(value > 0 for value in jade.point_sampling_variances.values())
    defense = AXES[1]
    lower_loss = {**jade.metrics, defense.metric.key: jade.metrics[defense.metric.key] - 300}
    uncalibrated = jade._replace(point_sampling_variances=None)
    assert (
        50
        < axis_score(defense, lower_loss, jade, 900)
        < axis_score(defense, lower_loss, uncalibrated, 900)
    )
    for ref in (jade, gold):
        for axis in AXES:
            assert axis_score(axis, ref.metrics, ref, 900) == pytest.approx(50)
    for room in (21, 22, 23, 24, 26):
        ref = comparison_reference(str(room), grade)
        assert ref.speed_sampling_variance > 0
        assert len(ref.rank_rates) == 3
        assert sum(ref.rank_rates) == pytest.approx(1)
    assert comparison_reference("25", rank_identity(20720)) is None


def test_sanma_style_image_skips_four_player_achievements():
    from pupulinbot.ms_style_chart import render_style

    sample = stats(3)
    sample.raw["_hand_count"] = 18
    records = [replace(game, mode="24") for game in games(3)]
    options = parse_options("mode=3 -r 玉南")
    with (
        patch("pupulinbot.ms_style_chart.supplemental_tags") as tags,
        patch("pupulinbot.ms_style_chart.analyze_style") as classification,
    ):
        png = render_style(Player(42, "x"), sample, records, options, current_level=20403)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    tags.assert_not_called()
    classification.assert_not_called()


@pytest.mark.parametrize(
    "query, rooms",
    [("x", [21, 22, 23, 24, 26]), ("x game=东", [21, 23])],
)
@pytest.mark.asyncio
async def test_sanma_style_room_filters_skip_throne_east(query, rooms):
    player = Player(42, "x")
    records = [replace(game, mode="23") for game in games()]
    sanma = SimpleNamespace(
        modes=[21, 22, 23, 24, 25, 26],
        resolve=AsyncMock(return_value=player),
        history=AsyncMock(side_effect=[([records[0]], False), (records, False)]),
        range_stats=AsyncMock(return_value=stats(len(records))),
    )
    service = MahjongQueries(None, None, sanma)
    with pytest.raises(ValueError, match="暂未开放王座东"):
        await service.query("msstyle3", "x -r 王座东", 1, 2)
    with pytest.raises(ValueError, match="请使用 msstyle3"):
        await service.query("msstyle", "x mode=3", 1, 2)
    with patch("pupulinbot.ms_commands.render_style", return_value=b"png"):
        assert await service.query("msstyle3", query, 1, 2) == b"png"
    assert sanma.history.await_args_list == [
        call(42, 1, modes=rooms),
        call(42, 200, start=None, end=None, modes=[23]),
    ]


def test_sanma_style_renders_gold_radar_table_and_insight():
    import io

    from PIL import Image, ImageColor

    from pupulinbot.ms_style_chart import BONUS_FILL, BONUS_LINE, radar_bonus_score, render_style

    sample = stats(3)
    records = [replace(game, mode="24") for game in games(3)]
    options = parse_options("mode=3 -r 玉南")
    with patch("pupulinbot.ms_style_chart.axis_score", side_effect=[99.99, 40, 50, 55, 52, 56]):
        png = render_style(Player(42, "x"), sample, records, options, current_level=20403)
    image = Image.open(io.BytesIO(png))
    bonus = radar_bonus_score(99.99, start=2)
    assert image.getpixel((362, round(558 - 180 * bonus / 100))) == ImageColor.getrgb(BONUS_LINE)
    assert image.getpixel((813, 377)) == ImageColor.getrgb(BONUS_FILL)
    assert image.getpixel((742, 1016)) == ImageColor.getrgb(BONUS_FILL)
