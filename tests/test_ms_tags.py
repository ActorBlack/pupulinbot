import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw
from test_ms_profile import stats as profile_stats

from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_charts import render_profile
from pupulinbot.ms_options import parse_options
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
    ranks = ranks if ranks is not None else [(i // 2) % 4 + 1 for i in range(count)]
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
        ("高和高铳", {"win_rate": 0.235, "deal_in_rate": 0.149}),
        ("稳健防守", {"win_rate": 0.21, "deal_in_rate": 0.108}),
        ("高打点", {"win_rate": 0.21, "raw": {"平均打点": 7100}}),
        ("速攻流", {"win_rate": 0.235, "raw": {"平均打点": 6000, "和了巡数": 11.8}}),
        ("立直流", {"riichi_rate": 0.217}),
        ("默听流", {"riichi_rate": 0.18, "raw": {"默听率": 0.177}}),
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
        {"win_rate": 0.2349, "deal_in_rate": 0.149},
        {"win_rate": 0.21, "deal_in_rate": 0.1081},
        {"raw": {"平均打点": 7099}},
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
    assert supplemental_tags(stats(19, call_rate=0.4), records(count=19), 42) == []
    assert supplemental_tags(stats(20, call_rate=0.4), records(count=20), 42) == ["副露流"]
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
    "ranks,raw,expected,minimum",
    [
        ([1, 1, 2, 2, 3, 3, 4, 4] * 12 + [1, 2, 3, 4], {"局收支": 150}, "局收支重视", 25),
        ([1, 2, 2, 3, 1, 2, 2, 3, 3, 4] * 10, {}, "避四流", 50),
        ([1, 1, 1, 1, 2, 3, 3, 4, 4, 4] * 10, {}, "争一流", 50),
    ],
)
def test_placement_and_income_tags_have_distinct_minimum_samples(ranks, raw, expected, minimum):
    rows = records(ranks)
    assert expected in supplemental_tags(stats(raw=raw), rows, 42)
    assert expected in supplemental_tags(stats(minimum, raw=raw), rows[:minimum], 42)
    assert expected not in supplemental_tags(stats(minimum - 1, raw=raw), rows[: minimum - 1], 42)
    rows[-1].players[0]["rank"] = None
    if expected != "局收支重视":
        assert expected not in supplemental_tags(stats(raw=raw), rows, 42)


def test_first_place_tag_does_not_require_high_fourth_rate_or_exclude_avoidance():
    rows = records([1, 1, 1, 1, 2, 3, 3, 3, 4, 4] * 10)
    assert supplemental_tags(stats(), rows, 42) == ["争一流"]
    rows = records([1, 1, 1, 1, 2, 2, 2, 3, 3, 4] * 10)
    assert supplemental_tags(stats(), rows, 42) == ["上分糕手"]


def test_round_income_uses_real_net_points_and_hand_count_never_efficiency_or_pt():
    sample = stats(raw={"净打点效率": 1500, "打点效率": 2200, "平均铳点": 2000})
    rows = records()
    for game in rows:
        game.players[0].update(score=25000, gradingScore=300)
    assert "局收支重视" not in supplemental_tags(sample, rows, 42)
    for bad in (None, True, float("nan"), float("inf"), 149.99):
        sample.raw["局收支"] = bad
        assert "局收支重视" not in supplemental_tags(sample, rows, 42)
    sample.raw.pop("局收支")
    for game in rows:
        game.players[0]["score"] = 27400
    assert sample_round_income(sample.raw, rows, 42, 1200) == 200
    assert "局收支重视" in supplemental_tags(sample, rows, 42)
    rows[-1].players[0]["score"] = None
    assert "局收支重视" not in supplemental_tags(sample, rows, 42)


def test_priority_limit_preserves_calling_form_and_income_before_secondary_traits():
    # Recent results improve strongly despite 25 fourth places in the entire sample.
    ranks = [4] * 25 + [3] * 40 + [2] * 15 + [1] * 20
    sample = stats(
        call_rate=0.4,
        win_rate=0.26,
        deal_in_rate=0.15,
        riichi_rate=0.23,
        raw={"局收支": 200, "平均打点": 8000},
    )
    tags = supplemental_tags(sample, records(ranks), 42)
    assert tags[:2] == ["副露流", "好调中"]
    assert {"精打细算", "高和高铳", "高打点", "立直流", "火力全开"} <= set(tags)
    assert len(tags) <= 10


def test_ms_text_and_image_keep_main_style_and_append_the_same_tags(monkeypatch):
    rows = records()
    sample = stats(call_rate=0.4, deal_in_rate=0.10)
    player, options = Player(42, "示例玩家"), parse_options("-l 100 -r 玉南")
    expected = "#" + analyze_style(sample).label + " #副露流 #稳健防守"
    text = format_summary(player, rows, False, options, {"sample": sample}, now=NOW)
    assert text.splitlines()[-1] == "牌风: " + expected
    labels = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, value, *args, **kwargs):
        labels.append(str(value))
        return original(self, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    value = profile_stats()
    value.games, value.raw = sample.games, sample.raw
    for attr in ("win_rate", "deal_in_rate", "call_rate", "riichi_rate"):
        setattr(value, attr, getattr(sample, attr))
    picture = render_profile(player, value, scope="四麻 · 玉南 · 最近100场", games=rows)
    assert Image.open(io.BytesIO(picture)).size == (1600, 1258)
    assert "#" + analyze_style(sample).label in labels
    assert [label for label in labels if label in {"#副露流", "#稳健防守"}] == [
        "#副露流",
        "#稳健防守",
    ]
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
    assert reference_for("16")["source_mode"] == "16"
    source = json.loads(
        (Path(__file__).parents[1] / "src/pupulinbot/assets/ms_tag_baseline.json").read_text()
    )
    assert len(source["source_files"]) == 6
    assert all(len(entry["sha256"]) == 64 for entry in source["source_files"].values())


def test_room_specific_benchmarks_and_no_cross_room_fallback():
    sample = stats(call_rate=0.38)
    rows = records()
    assert "副露流" in supplemental_tags(sample, rows, 42)
    for row in rows:
        row.mode = "11"
    assert "副露流" not in supplemental_tags(sample, rows, 42)
    for mode in ("15", "24"):
        for row in rows:
            row.mode = mode
        assert supplemental_tags(sample, rows, 42) == []


@pytest.mark.parametrize("mode", ["8", "9", "11", "12", "16"])
def test_supported_rooms_use_matched_references_and_same_sample_guards(mode):
    rows = records(count=300)
    for row in rows:
        row.mode = mode
    reference = reference_for(mode)
    assert reference is not None
    sample = stats(300, call_rate=0.9, raw={"局收支": 200})
    tags = supplemental_tags(sample, rows, 42)
    assert "副露流" in tags and "局收支重视" in tags
    assert supplemental_tags(sample, rows, 42, mode=3) == []
    sample.games = 299
    assert supplemental_tags(sample, rows, 42) == []
    sample.games = 19
    assert supplemental_tags(sample, rows[:19], 42) == []


@pytest.mark.parametrize("mode", ["11", "12"])
def test_reference_builder_exactly_preserves_existing_jade_calibration(mode):
    from pupulinbot.ms_tag_reference import build_reference

    reference = reference_for(mode)
    rebuilt = build_reference(reference["levels"], reference["name"], source_mode=mode)
    assert rebuilt["means"] == reference["means"]
    assert rebuilt["thresholds"] == reference["thresholds"]
    assert rebuilt["player_game_appearances"] == reference["player_game_appearances"]
    assert rebuilt["player_hand_appearances"] == reference["player_hand_appearances"]


def test_throne_south_prefers_native_reference_then_falls_back_only_to_saint3(monkeypatch):
    from pupulinbot import ms_tags

    native, jade = reference_for("16"), reference_for("12")
    references = {"12": jade, "16": native}
    monkeypatch.setattr(ms_tags, "_references", lambda: references)
    assert reference_for("16") is native
    references.pop("16")
    fallback = reference_for("16")
    saint3 = jade["levels"]["10503"]
    assert fallback["source_mode"] == "12"
    assert fallback["reference_kind"] == "saint3_fallback"
    assert fallback["reference_levels"] == ["10503"]
    assert fallback["means"]["副露率"] == pytest.approx(saint3["extended"]["副露率"])
    assert fallback["means"]["rank_rates"] == saint3["basic"]["rank_rates"]
    rows = records(count=300)
    for row in rows:
        row.mode = "16"
    assert "副露流" in supplemental_tags(stats(300, call_rate=0.9), rows, 42)
    assert reference_for("8") is None and reference_for("15") is None
    assert reference_for("24") is None
    references.clear()
    assert reference_for("16") is None


@pytest.mark.parametrize("levels", [{}, {"10503": None}, {"10503": {"extended": None}}])
def test_reference_builder_rejects_missing_aggregate_data(levels):
    from pupulinbot.ms_tag_reference import build_reference

    with pytest.raises(ValueError):
        build_reference(levels, "王座南", source_mode="16")


@pytest.mark.parametrize(
    "mode,appearances,levels",
    [
        ("8", 205320189, {"10203", "10301", "10302", "10303", "10401", "10402", "10403", "10501"}),
        ("9", 324077136, {"10203", "10301", "10302", "10303", "10401", "10402", "10403", "10501"}),
        (
            "16",
            4886776,
            {"10403", "10501", "10502", "10503"} | {f"107{i:02}" for i in range(1, 21) if i != 17},
        ),
    ],
)
def test_new_room_references_include_full_native_population(mode, appearances, levels):
    reference = reference_for(mode)
    assert set(reference["levels"]) == levels
    assert reference["player_game_appearances"] == appearances
    assert reference["reference_kind"] == "room"
    assert reference["source_mode"] == mode


def test_reference_builder_normalizes_numeric_strings_without_mutating_source():
    from copy import deepcopy

    from pupulinbot.ms_tag_reference import HAND_METRICS, WIN_METRICS, build_reference

    reference = reference_for("12")
    levels = deepcopy(reference["levels"])
    for row in levels.values():
        basic, extended = row["basic"], row["extended"]
        basic["count"], extended["count"] = str(basic["count"]), str(extended["count"])
        basic["avg_rank"] = str(basic["avg_rank"])
        basic["rank_rates"] = [str(value) for value in basic["rank_rates"]]
        for key in HAND_METRICS + WIN_METRICS:
            extended[key] = str(extended[key])
    rebuilt = build_reference(levels, "玉南", source_mode="12")
    assert rebuilt["means"] == reference["means"]
    assert rebuilt["thresholds"] == reference["thresholds"]
    assert isinstance(next(iter(levels.values()))["basic"]["count"], str)


@pytest.mark.parametrize(
    "field,value",
    [("count", 12.5), ("rank_rates", [0.4, 0.4, 0.4, 0.4]), ("avg_rank", 1)],
)
def test_reference_builder_rejects_fractional_exposure_and_inconsistent_ranks(field, value):
    from copy import deepcopy

    from pupulinbot.ms_tag_reference import build_reference

    levels = deepcopy(reference_for("12")["levels"])
    next(iter(levels.values()))["basic"][field] = value
    with pytest.raises(ValueError):
        build_reference(levels, "玉南", source_mode="12")


def test_rate_sampling_guard_blocks_a_noisy_point_estimate_and_missing_hand_counts():
    rows = records()
    sample = stats(win_rate=0.26, deal_in_rate=0.149, raw={"_hand_count": 600})
    assert "高和高铳" not in supplemental_tags(sample, rows, 42)
    sample.raw["_hand_count"] = 1200
    assert "高和高铳" in supplemental_tags(sample, rows, 42)
    for bad in (None, True, 599, 600.5, float("nan"), float("inf")):
        sample.raw["_hand_count"] = bad
        assert "高和高铳" not in supplemental_tags(sample, rows, 42)
    sample = stats(call_rate=0.5, raw={"_hand_count": None, "count": 99999})
    assert "副露流" not in supplemental_tags(sample, rows, 42)
    assert wilson_interval(0.5, 100) == pytest.approx((0.40383, 0.59617), abs=0.00001)
    assert wilson_interval(None, 100) is None
    assert wilson_interval(0.5, True) is None


def test_high_win_high_deal_tag_requires_both_rates_to_clear_sampling_guard():
    rows = records()
    sample = stats(win_rate=0.235, deal_in_rate=0.17, raw={"_hand_count": 1200})
    assert "高和高铳" not in supplemental_tags(sample, rows, 42)
    sample.raw["_hand_count"] = 3600
    assert "高和高铳" in supplemental_tags(sample, rows, 42)
    assert "狂战士" not in supplemental_tags(sample, rows, 42)


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
