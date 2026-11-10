import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_options import parse_options
from pupulinbot.ms_sample_chart import Figure, render_sample
from pupulinbot.ms_style import FEATURES, analyze_style
from pupulinbot.ms_summary import format_summary
from pupulinbot.ms_tags import (
    reference_for,
    sample_round_income,
    supplemental_tags,
    wilson_interval,
)
from pupulinbot.paipuya import PaipuyaClient

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def records(ranks=None, *, count=100):
    ranks = ranks if ranks is not None else [i % 4 + 1 for i in range(count)]
    return [
        GameRecord(
            str(i),
            NOW - timedelta(hours=len(ranks) - i),
            NOW,
            "12",
            [{"account_id": 42, "rank": rank, "score": 25000}],
            {},
        )
        for i, rank in enumerate(ranks)
    ]


def stats(count=100, **changes):
    raw = {key: mean for key, _, _, mean, *_ in FEATURES}
    raw.update({"平均打点": 6500, "和了巡数": 12.2, "默听率": 0.13, "_hand_count": count * 12})
    raw.update(changes.pop("raw", {}))
    values = dict(games=count, win_rate=0.22, deal_in_rate=0.12, call_rate=0.32, riichi_rate=0.19)
    values.update(changes)
    for attribute, key in (
        ("win_rate", "和牌率"),
        ("deal_in_rate", "放铳率"),
        ("call_rate", "副露率"),
        ("riichi_rate", "立直率"),
    ):
        raw[key] = values[attribute]
    return SimpleNamespace(raw=raw, **values)


@pytest.mark.parametrize(
    "tag,changes",
    [
        ("副露流", {"call_rate": 0.369}),
        ("门清流", {"call_rate": 0.268}),
        ("狂战士", {"win_rate": 0.22, "deal_in_rate": 0.149}),
        ("稳健防守", {"win_rate": 0.21, "deal_in_rate": 0.108}),
        ("高打点", {"win_rate": 0.21, "raw": {"平均打点": 7400}}),
        ("速攻派", {"win_rate": 0.235, "raw": {"平均打点": 6000, "和了巡数": 11.8}}),
        ("立直派", {"riichi_rate": 0.217}),
        ("默听派", {"riichi_rate": 0.18, "raw": {"默听率": 0.177}}),
    ],
)
def test_style_thresholds_are_inclusive_and_independent(tag, changes):
    assert supplemental_tags(stats(300, **changes), records(count=300), 42) == [tag]


@pytest.mark.parametrize(
    "changes",
    [
        {"call_rate": 0.3689},
        {"call_rate": 0.2681},
        {"win_rate": 0.2199, "deal_in_rate": 0.149},
        {"win_rate": 0.21, "deal_in_rate": 0.1081},
        {"raw": {"平均打点": 7399}},
        {"win_rate": 0.235, "raw": {"平均打点": 6001, "和了巡数": 11.8}},
        {"win_rate": 0.235, "raw": {"平均打点": 6000, "和了巡数": 11.81}},
        {"riichi_rate": 0.2169},
        {"riichi_rate": 0.18, "raw": {"默听率": 0.1769}},
    ],
)
def test_neutral_and_just_outside_thresholds_do_not_receive_tags(changes):
    assert supplemental_tags(stats(**changes), records(), 42) == []


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_or_missing_call_rate_is_not_treated_as_zero(bad):
    assert supplemental_tags(stats(call_rate=bad), records(), 42) == []


def test_minimum_sample_and_scope_guards():
    assert supplemental_tags(stats(99, call_rate=0.4), records(count=99), 42) == []
    assert supplemental_tags(stats(100, call_rate=0.4), records(count=100), 42) == ["副露流"]
    sample = stats(call_rate=0.4)
    rows = records()
    assert supplemental_tags(sample, rows, 42, mode=3) == []
    assert supplemental_tags(None, rows, 42) == []
    assert supplemental_tags(stats(101, call_rate=0.4), rows, 42) == []
    assert supplemental_tags(sample, rows, 99) == []
    rows[-1].mode = "16"
    assert supplemental_tags(sample, rows, 42) == []
    rows[-1].mode = "12"
    rows[-1].uuid = rows[0].uuid
    assert supplemental_tags(sample, rows, 42) == []


@pytest.mark.parametrize(
    "previous,recent,expected",
    [
        ([2] * 20 + [3] * 40 + [4] * 20, [1] * 8 + [2] * 8 + [3] * 4, "好调中"),
        ([1] * 20 + [2] * 40 + [3] * 20, [3] * 10 + [4] * 10, "恶调中"),
        ([2] * 80, [1] * 6 + [2] * 8 + [3] * 6, None),
        ([3] * 80, [2] * 4 + [3] * 8 + [4] * 8, None),
    ],
)
def test_recent_form_compares_last_twenty_to_own_baseline_with_variance_guard(
    previous, recent, expected
):
    rows = records(previous + recent)
    # Unsorted API results must not change what counts as the newest twenty.
    tags = supplemental_tags(stats(100), list(reversed(rows)), 42)
    assert [tag for tag in tags if tag in {"好调中", "恶调中"}] == ([expected] if expected else [])
    rows[0].players[0]["rank"] = None
    assert not {"好调中", "恶调中"} & set(supplemental_tags(stats(100), rows, 42))


@pytest.mark.parametrize(
    "ranks,raw,expected",
    [
        ([1, 2, 3, 4] * 25, {"局收支": 200}, "高局收支"),
        ([1, 2, 2, 3, 1, 2, 2, 3, 3, 4] * 10, {}, "避四派"),
        ([1, 1, 1, 1, 2, 3, 3, 4, 4, 4] * 10, {}, "争一派"),
    ],
)
def test_placement_tags_need_full_one_hundred_game_sample(ranks, raw, expected):
    rows = records(ranks)
    assert supplemental_tags(stats(raw=raw), rows, 42) == [expected]
    assert expected not in supplemental_tags(stats(99, raw=raw), rows[:-1], 42)
    rows[-1].players[0]["rank"] = None
    if expected != "高局收支":
        assert expected not in supplemental_tags(stats(raw=raw), rows, 42)


def test_round_income_uses_real_net_points_and_hand_count_never_efficiency_or_pt():
    sample = stats(raw={"净打点效率": 1500, "打点效率": 2200, "平均铳点": 2000})
    rows = records()
    for game in rows:
        game.players[0].update(score=25000, gradingScore=300)
    assert "高局收支" not in supplemental_tags(sample, rows, 42)
    for bad in (None, True, float("nan"), float("inf"), 199.99):
        sample.raw["局收支"] = bad
        assert "高局收支" not in supplemental_tags(sample, rows, 42)
    sample.raw.pop("局收支")
    for game in rows:
        game.players[0]["score"] = 27400
    assert sample_round_income(sample.raw, rows, 42, 1200) == 200
    assert "高局收支" in supplemental_tags(sample, rows, 42)
    rows[-1].players[0]["score"] = None
    assert "高局收支" not in supplemental_tags(sample, rows, 42)


def test_priority_limit_preserves_calling_form_and_income_before_secondary_traits():
    # Recent results improve strongly despite 25 fourth places in the entire sample.
    ranks = [4] * 25 + [3] * 40 + [2] * 15 + [1] * 20
    sample = stats(
        call_rate=0.4,
        win_rate=0.24,
        deal_in_rate=0.15,
        riichi_rate=0.23,
        raw={"局收支": 200, "平均打点": 8000},
    )
    assert supplemental_tags(sample, records(ranks), 42) == [
        "副露流",
        "好调中",
        "高局收支",
        "狂战士",
    ]


def test_ms_text_and_image_keep_main_style_and_append_the_same_tags(monkeypatch):
    rows = records()
    sample = stats(call_rate=0.4, deal_in_rate=0.10)
    player, options = Player(42, "示例玩家"), parse_options("-l 100 -r 玉南")
    expected = "#" + analyze_style(sample).label + " #副露流 #稳健防守"
    text = format_summary(player, rows, False, options, {"sample": sample}, now=NOW)
    assert text.splitlines()[-1] == "牌风: " + expected
    labels = []
    original = Figure.text

    def capture(self, x, y, value, **kwargs):
        labels.append(value)
        return original(self, x, y, value, **kwargs)

    monkeypatch.setattr(Figure, "text", capture)
    picture = render_sample(player, rows, False, options, sample)
    assert Image.open(io.BytesIO(picture)).size == (1680, 1260)
    assert "样本牌风 · " + expected in labels
    sample.games = 101
    text = format_summary(player, rows, False, options, {"sample": sample}, now=NOW)
    assert "副露流" not in text and "稳健防守" not in text


def test_frozen_reference_contains_actual_exposure_weighted_cohort_not_all_rank_percentiles():
    south, east = reference_for("12"), reference_for("11")
    assert south["player_game_appearances"] == 127564970
    assert south["player_hand_appearances"] == 1329927664
    assert set(south["levels"]) == {"10402", "10403", "10501", "10502", "10503"}
    assert south["means"]["副露率"] == pytest.approx(0.3187393596468567)
    assert south["means"]["平均打点"] == pytest.approx(6547.0080090804995)
    assert not south["cohort_percentiles_available"]
    assert east["thresholds"]["call_high"] == 0.389
    assert south["thresholds"]["call_high"] == 0.369
    assert reference_for("16") is None
    source = json.loads(
        (Path(__file__).parents[1] / "src/pupulinbot/assets/ms_tag_baseline.json").read_text()
    )
    assert len(source["source_files"]) == 3
    assert all(len(entry["sha256"]) == 64 for entry in source["source_files"].values())


def test_room_specific_benchmarks_and_no_cross_room_fallback():
    sample = stats(call_rate=0.38)
    rows = records()
    assert "副露流" in supplemental_tags(sample, rows, 42)
    for row in rows:
        row.mode = "11"
    assert "副露流" not in supplemental_tags(sample, rows, 42)
    for mode in ("8", "9", "15", "16", "24"):
        for row in rows:
            row.mode = mode
        assert supplemental_tags(sample, rows, 42) == []


def test_rate_sampling_guard_blocks_a_noisy_point_estimate_and_missing_hand_counts():
    rows = records()
    sample = stats(win_rate=0.22, deal_in_rate=0.149, raw={"_hand_count": 600})
    assert "狂战士" not in supplemental_tags(sample, rows, 42)
    sample.raw["_hand_count"] = 1200
    assert "狂战士" in supplemental_tags(sample, rows, 42)
    for bad in (None, True, 599, 600.5, float("nan"), float("inf")):
        sample.raw["_hand_count"] = bad
        assert "狂战士" not in supplemental_tags(sample, rows, 42)
    sample = stats(call_rate=0.5, raw={"_hand_count": None, "count": 99999})
    assert "副露流" not in supplemental_tags(sample, rows, 42)
    assert wilson_interval(0.5, 100) == pytest.approx((0.40383, 0.59617), abs=0.00001)
    assert wilson_interval(None, 100) is None
    assert wilson_interval(0.5, True) is None


def test_state_needs_more_than_a_small_last_twenty_improvement():
    rows = records([1, 2, 3, 4] * 20 + [1] * 4 + [2] * 10 + [3] * 4 + [4] * 2)
    assert "好调中" not in supplemental_tags(stats(), rows, 42)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["stats", "range_stats"])
async def test_api_retains_hand_count_separately_from_game_count(method):
    client = PaipuyaClient("https://5-data.amae-koromo.com/api/v2/pl4")
    client._get = AsyncMock(side_effect=[{"count": 100}, {"count": 912, "和牌率": 0.22}])
    try:
        if method == "stats":
            result = await client.stats(42)
        else:
            result = await client.range_stats(
                42, start=NOW - timedelta(days=1), end=NOW, modes=[12]
            )
        assert result.games == 100 and result.raw["count"] == 100
        assert result.raw["_hand_count"] == 912
    finally:
        await client.close()
