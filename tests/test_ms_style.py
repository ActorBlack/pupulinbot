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
    client = SimpleNamespace(
        modes=[11, 12],
        resolve=AsyncMock(return_value=Player(42, "x")),
        history=AsyncMock(return_value=(games(), False)),
        range_stats=AsyncMock(return_value=sample),
        stats=AsyncMock(),
    )
    service = MahjongQueries(None, client, client)
    with patch("pupulinbot.ms_commands.render_style", return_value=b"png") as render:
        assert await service.query("msstyle", "x last=100 room=玉南", 1, 2) == b"png"
    assert render.call_args.args[1] is sample
    assert render.call_args.args[3].modes == [12]
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
        call(42, 100, start=None, end=None, modes=[16]),
    ]
    assert render.call_args.args[3].modes == [16]
    assert client.range_stats.call_args.kwargs["modes"] == [16]


def test_style_report_renders_complete_and_missing_inputs_without_faking_coordinates():
    import io

    from PIL import Image

    from pupulinbot.ms_style_chart import SIZE, render_style

    for value in (None, SimpleNamespace(raw=ORACLE[-1]["metrics"], games=100)):
        image = Image.open(
            io.BytesIO(
                render_style(Player(42, "超长昵称" * 20), value, games(), parse_options("last=100"))
            )
        )
        assert image.size == SIZE and image.mode == "RGB"


def test_radar_uses_only_the_selected_room_mean_and_leaves_missing_axes_blank():
    from pupulinbot.ms_options import MODE_NAMES, ROOMS
    from pupulinbot.ms_style_chart import AXES, axis_score, comparison_reference
    from pupulinbot.ms_tags import reference_for

    room_means = []
    for room in (str(mode) for pair in ROOMS[4].values() for mode in pair):
        label, base, ranks = comparison_reference(room)
        assert label == f"{MODE_NAMES[room]}均值"
        assert sum(ranks) == pytest.approx(1)
        assert [axis_score(group, base, base) for _, group in AXES] == [50] * 6
        room_means.append(base["和牌率"])
    assert len(set(room_means)) == 6

    _, base, _ = comparison_reference("12")
    assert base["和牌率"] == pytest.approx(0.216924, abs=0.000001)
    cohort = reference_for("12")["levels"]["10502"]["extended"]
    assert base["和牌率"] != pytest.approx(cohort["和牌率"])
    defense = AXES[1][1]
    assert axis_score(defense, {**base, "放铳率": base["放铳率"] - 0.01}, base) > 50
    assert axis_score(defense, {**base, "平均铳点": None}, base) is None
    assert comparison_reference("21") == ("三麻金东暂无均值", {}, [])


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
