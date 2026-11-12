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
        raw={**ORACLE[-1]["metrics"], **ref.metrics, "_hand_count": 1024}, games=100
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
    assert comparison_reference("12", None) is None
    assert comparison_reference("21", promoted) is None
    assert comparison_reference("12", rank_identity(10712)) is None
    assert comparison_reference("16", rank_identity(10712)) is not None


def test_radar_bonus_uses_unrounded_extremes_and_caps_at_150():
    from pupulinbot.ms_style_chart import radar_bonus_score

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
    assert values["和立牌平均巡目"] == pytest.approx((0.25 * 12 + 0.15 * 9) / 0.40)
    assert values["净打点效率"] == pytest.approx(1000)

    heavier = composite_metrics({**raw, "平均铳点": 8000})
    assert heavier["铳点损失"] == pytest.approx(800)
    assert heavier["净打点效率"] == pytest.approx(800)
    more_riichi = composite_metrics({**raw, "立直率": 0.25})
    assert more_riichi["立直局均收支"] == pytest.approx(700)
    assert more_riichi["和立牌平均巡目"] == pytest.approx(10.5)
    assert composite_metrics({**raw, "平均铳点": None})["铳点损失"] is None
    assert composite_metrics({**raw, "平均铳点": None, "放铳率": 0})["铳点损失"] == 0
    assert composite_metrics({**raw, "副露后和牌率": 0.9})["副露净结果率"] is None


def test_radar_shrinks_event_rates_and_amounts_by_observable_sampling_variance():
    from pupulinbot.ms_level import rank_identity
    from pupulinbot.ms_style_radar import AXES, axis_score, comparison_reference

    ref = comparison_reference("12", rank_identity(10502))
    attack, defense, call, _, speed, points = AXES
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
    key = speed.metric.key
    actual = {**ref.metrics, key: ref.metrics[key] - ref.scales[key]}
    assert axis_score(speed, actual, ref, 1000) == pytest.approx(84.13447)
    assert axis_score(speed, {**actual, "和牌率": 0.01, "立直率": 0.01}, ref, 1000) is None


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
    with pytest.raises(ValueError, match="仅适用于四麻"):
        await service.query("msstyle", "x mode=3", 1, 2)
