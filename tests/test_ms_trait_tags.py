from copy import deepcopy

import pytest
from test_ms_tags import records, stats

from pupulinbot.ms_style import style_lines
from pupulinbot.ms_tags import reference_for, supplemental_tags
from pupulinbot.ms_trait_tags import (
    AVERAGE_TRAITS,
    RATE_TRAITS,
    build_trait_reference,
    exposures,
    select_tags,
    trait_tags,
)


def sample_raw():
    reference = reference_for("12")
    raw = {key: entry["mean"] for key, entry in reference["traits"].items()}
    raw.update(
        和牌率=0.22,
        立直率=0.19,
        副露率=0.32,
        流局率=0.14,
        立直后和牌率=0.5,
        立直和了=342,
    )
    return raw


def classify(raw, hands=3600):
    return trait_tags(raw, reference_for("12"), hands, games=records(count=300))


@pytest.mark.parametrize("mode", ["8", "9", "11", "12", "16"])
def test_each_supported_room_has_complete_trait_references(mode):
    reference = reference_for(mode)
    traits = reference["traits"]
    assert set(traits) == set(RATE_TRAITS) | set(AVERAGE_TRAITS)
    for key, entry in traits.items():
        assert entry["events"] > 100
        assert entry["mean"] >= 0
        if key in RATE_TRAITS:
            assert entry["mean"] <= 1
    assert traits == build_trait_reference(reference["levels"])


def test_neutral_reference_metrics_do_not_receive_extra_traits():
    assert classify(sample_raw()) == {"style": [], "luck": [], "achievement": []}


@pytest.mark.parametrize(
    "key,side,category,label",
    [
        ("里宝率", "high", "luck", "里宝超人"),
        ("里宝率", "low", "luck", "里宝绝缘体"),
        ("一发率", "high", "luck", "一发超人"),
        ("一发率", "low", "luck", "一发绝缘体"),
        ("自摸率", "high", "style", "自摸超人"),
        ("先制率", "high", "style", "先手立直流"),
        ("追立率", "high", "style", "追立斗士"),
        ("副露后和牌率", "high", "style", "鸣牌善战"),
        ("副露后放铳率", "low", "style", "鸣牌慎行"),
        ("流听率", "high", "style", "流局听牌王"),
        ("立直巡目", "low", "style", "早巡立直"),
        ("平均起手向听", "low", "luck", "配牌眷顾"),
        ("平均起手向听", "high", "luck", "配牌修行"),
    ],
)
def test_metric_specific_traits_trigger_at_effect_threshold_and_not_just_outside(
    key, side, category, label
):
    raw = sample_raw()
    threshold = reference_for("12")["traits"][key][side]
    raw[key] = threshold
    if key in {"先制率", "追立率"}:
        raw["追立率" if key == "先制率" else "先制率"] = 1 - threshold
    assert classify(raw)[category] == [label]
    raw[key] += -1e-7 if side == "high" else 1e-7
    if key in {"先制率", "追立率"}:
        raw["追立率" if key == "先制率" else "先制率"] = 1 - raw[key]
    assert label not in classify(raw)[category]


@pytest.mark.parametrize("key,label", [("役满", "役满达成"), ("流满", "流局满贯")])
def test_achievements_require_real_valid_sample_counters(key, label):
    raw = sample_raw()
    raw[key] = 1
    assert classify(raw)["achievement"] == [label]
    for bad in (None, True, 0, -1, 0.5, 100000, float("nan"), float("inf")):
        raw[key] = bad
        assert classify(raw)["achievement"] == []
    raw = sample_raw()
    raw["累计役满"] = 1
    assert classify(raw)["achievement"] == []


@pytest.mark.parametrize("key", list(RATE_TRAITS) + list(AVERAGE_TRAITS))
@pytest.mark.parametrize("bad", [None, True, -1, float("nan"), float("inf")])
def test_missing_or_invalid_metrics_are_not_substituted_with_zero(key, bad):
    raw = sample_raw()
    raw[key] = bad
    assert classify(raw) == {"style": [], "luck": [], "achievement": []}


@pytest.mark.parametrize("key", list(RATE_TRAITS))
def test_out_of_range_rates_are_not_classified(key):
    raw = sample_raw()
    raw[key] = 1.01
    assert classify(raw) == {"style": [], "luck": [], "achievement": []}


def test_luck_rates_use_riichi_win_count_instead_of_hands_wins_or_riichis():
    raw = sample_raw()
    raw.update(里宝率=0.6, 一发率=0.5, 立直和了=29)
    raw["立直后和牌率"] = 29 / 684
    assert classify(raw)["luck"] == []
    raw["立直和了"] = 30
    raw["立直后和牌率"] = 30 / 684
    assert classify(raw)["luck"] == ["里宝超人", "一发超人"]
    raw.pop("立直和了")
    raw["立直后和牌率"] = 0.02
    assert classify(raw)["luck"] == []
    raw["立直后和牌率"] = 0.5
    assert classify(raw)["luck"] == ["里宝超人", "一发超人"]
    raw.pop("立直后和牌率")
    assert classify(raw)["luck"] == []


@pytest.mark.parametrize("bad", [None, True, -1, 99.5, 800, float("nan")])
def test_invalid_exact_riichi_win_counter_does_not_fall_back_to_larger_estimate(bad):
    raw = sample_raw()
    raw.update(里宝率=0.6, 立直和了=bad)
    assert classify(raw)["luck"] == []


def test_impossible_inferred_riichi_wins_do_not_inflate_luck_evidence():
    raw = sample_raw()
    raw.pop("立直和了")
    raw.update(和牌率=0.1, 立直率=0.8, 立直后和牌率=0.9, 里宝率=0.6)
    assert exposures(raw, 3600)["riichi_wins"] is None
    assert classify(raw)["luck"] == []


@pytest.mark.parametrize(
    "metric,rate_key,minimum,category,label",
    [
        ("先制率", "立直率", 50, "style", "先手立直流"),
        ("自摸率", "和牌率", 50, "style", "自摸超人"),
        ("副露后和牌率", "副露率", 75, "style", "鸣牌善战"),
        ("流听率", "流局率", 50, "style", "流局听牌王"),
    ],
)
def test_conditional_event_minimums_do_not_use_total_hand_count(
    metric, rate_key, minimum, category, label
):
    raw = sample_raw()
    raw[metric] = 0.95
    if metric == "先制率":
        raw["追立率"] = 0.05
    raw[rate_key] = (minimum - 1) / 3600
    assert label not in classify(raw)[category]
    raw[rate_key] = minimum / 3600
    assert label in classify(raw)[category]


def test_sampling_guard_can_reject_a_high_point_estimate_even_after_minimum_events():
    raw = sample_raw()
    raw.update(立直和了=100, 里宝率=reference_for("12")["traits"]["里宝率"]["high"])
    raw["立直后和牌率"] = 100 / 684
    # An eight-point gap is still noisy with only 100 riichi wins.
    assert "里宝超人" not in classify(raw)["luck"]
    raw.update(立直和了=342, 立直后和牌率=0.5)
    assert "里宝超人" in classify(raw)["luck"]
    threshold = reference_for("12")["traits"]["先制率"]["high"]
    raw.update(立直率=100 / 3600, 先制率=threshold, 追立率=1 - threshold)
    # Five points above mean is still uncertain with only 100 declarations.
    assert "先手立直流" not in classify(raw)["style"]
    raw.update(立直率=0.19)
    assert "先手立直流" in classify(raw)["style"]


def test_inconsistent_first_and_chase_riichi_rates_cannot_produce_conflicting_tags():
    raw = sample_raw()
    raw.update(先制率=0.99, 追立率=0.99)
    assert classify(raw)["style"] == []


def test_open_hand_traits_keep_their_guardrails_and_can_coexist():
    raw = sample_raw()
    raw.update(副露后和牌率=0.5, 副露后放铳率=0.25)
    assert classify(raw)["style"] == []
    raw.update(副露后和牌率=0.05, 副露后放铳率=0.04)
    assert classify(raw)["style"] == []
    raw.update(副露后和牌率=0.5, 副露后放铳率=0.04)
    assert classify(raw)["style"] == ["鸣牌善战", "鸣牌慎行"]


def test_reference_weights_follow_the_actual_event_denominator_and_fail_closed_for_missing_groups():
    def row(hands, wins, ura):
        return {"extended": {"count": hands, "立直和了": wins, "里宝率": ura}}

    levels = {"a": row(1000, 100, 0.2), "b": row(9000, 300, 0.6)}
    reference = build_trait_reference(levels)
    assert reference["里宝率"]["mean"] == pytest.approx(0.5)
    assert reference["里宝率"]["events"] == 400
    assert reference["里宝率"]["basis"] == "riichi_wins"
    levels["b"]["extended"].pop("里宝率")
    assert "里宝率" not in build_trait_reference(levels)
    levels["b"]["extended"]["立直和了"] = 0
    assert build_trait_reference(levels)["里宝率"]["mean"] == pytest.approx(0.2)


def test_selection_limits_total_ten_luck_three_and_achievements_three():
    extra = {
        "style": ["先手立直流", "早巡立直", "多面立直流", "火力全开"],
        "luck": ["里宝超人", "一发超人", "配牌眷顾", "额外手气"],
        "achievement": ["役满达成", "流局满贯", "役满受害者", "额外成就"],
    }
    existing = ["门清流", "好调中", "争一流", "高打点"]
    assert select_tags(existing, extra, 10) == [
        "役满达成",
        "流局满贯",
        "役满受害者",
        "门清流",
        "好调中",
        "先手立直流",
        "早巡立直",
        "里宝超人",
        "一发超人",
        "配牌眷顾",
    ]
    for maximum in range(11):
        assert len(select_tags(existing, extra, maximum)) <= maximum


def test_new_traits_flow_into_ms_output_and_keep_existing_sample_and_mode_guards():
    rows = records(count=300)
    raw = sample_raw()
    raw.update(里宝率=0.6, 一发率=0.5, 役满=1)
    sample = stats(300, raw=raw)
    tags = supplemental_tags(sample, rows, 42)
    assert tags == ["役满达成", "里宝超人", "一发超人"]
    assert "#役满达成 #里宝超人 #一发超人" in style_lines(sample, games=rows, account_id=42)[0]
    assert supplemental_tags(sample, rows, 42, mode=3) == []
    sample.games = 301
    assert supplemental_tags(sample, rows, 42) == []
    sample.games = 19
    assert supplemental_tags(sample, rows[:19], 42) == ["役满达成"]
    sample.games = 300
    sample.raw["_hand_count"] = None
    assert supplemental_tags(sample, rows, 42) == []


def test_throne_fallback_builds_new_traits_only_from_saint3(monkeypatch):
    from pupulinbot import ms_tags

    jade = deepcopy(reference_for("12"))
    monkeypatch.setattr(ms_tags, "_references", lambda: {"12": jade})
    reference = reference_for("16")
    assert reference["reference_kind"] == "saint3_fallback"
    assert reference["traits"]["里宝率"]["mean"] == pytest.approx(
        jade["levels"]["10503"]["extended"]["里宝率"]
    )
