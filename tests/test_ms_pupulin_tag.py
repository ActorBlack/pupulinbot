import pytest
from test_ms_tags import records, stats

from pupulinbot.ms_style import style_lines
from pupulinbot.ms_tags import supplemental_tags
from pupulinbot.ms_trait_tags import RATE_TRAITS, is_pupulin_profile


def profile():
    sample = stats(
        50,
        call_rate=0.26,
        riichi_rate=0.195,
        deal_in_rate=0.10,
        win_rate=0.22,
        raw={"平均打点": 6600, "净打点效率": 800, "_hand_count": None},
    )
    sample.raw.pop("平均铳点", None)
    return sample


def test_profile_includes_both_numeric_minimums_without_extra_event_or_hand_requirements():
    sample, rows = profile(), records(count=50)
    assert is_pupulin_profile(sample)
    assert supplemental_tags(sample, rows, 42) == ["普普林"]
    assert style_lines(sample, games=rows, account_id=42)[0].endswith(" #普普林")
    assert RATE_TRAITS["立直多面"][-1] == "愚形爱好者"


@pytest.mark.parametrize(
    "attribute,low,high",
    [
        ("call_rate", 0.25, 0.27),
        ("riichi_rate", 0.185, 0.205),
        ("deal_in_rate", 0.095, 0.115),
        ("win_rate", 0.215, 0.24),
    ],
)
def test_each_rate_is_inclusive_and_every_single_violation_rejects_profile(attribute, low, high):
    sample = profile()
    for value in (low, high):
        setattr(sample, attribute, value)
        assert is_pupulin_profile(sample)
    for value in (low - 1e-7, high + 1e-7, None, True, float("nan"), float("inf")):
        setattr(sample, attribute, value)
        assert not is_pupulin_profile(sample)


@pytest.mark.parametrize("key,minimum", [("净打点效率", 800), ("平均打点", 6600)])
def test_each_point_metric_includes_equality_and_rejects_missing_or_invalid_value(key, minimum):
    sample = profile()
    for value in (minimum, minimum + 1):
        sample.raw[key] = value
        assert is_pupulin_profile(sample)
    for value in (minimum - 0.001, None, True, float("nan"), float("inf")):
        sample.raw[key] = value
        assert not is_pupulin_profile(sample)


def test_efficiency_uses_api_value_when_present_and_formula_only_when_absent():
    sample = profile()
    sample.raw["平均铳点"] = 6520.01
    assert is_pupulin_profile(sample)  # API rounded value is exactly 800.
    sample.raw.pop("净打点效率")
    assert not is_pupulin_profile(sample)  # Exact product is below 800.
    sample.raw["平均铳点"] = 6520
    assert is_pupulin_profile(sample)
    sample.raw["净打点效率"] = None
    assert not is_pupulin_profile(sample)
    sample.raw.pop("净打点效率")
    sample.raw.pop("平均铳点")
    assert not is_pupulin_profile(sample)


@pytest.mark.parametrize("bad", [49, 0, 50.5, None, True, float("nan")])
def test_fifty_games_is_an_independent_hard_minimum(bad):
    sample = profile()
    sample.games = bad
    assert not is_pupulin_profile(sample)


def test_profile_preserves_exact_sample_identity_and_four_player_scope_guards():
    sample, rows = profile(), records(count=50)
    assert supplemental_tags(sample, rows, 42, mode=3) == []
    assert supplemental_tags(sample, rows, 99) == []
    sample.games = 51
    assert supplemental_tags(sample, rows, 42) == []
    sample.games = 50
    rows[-1].uuid = rows[0].uuid
    assert supplemental_tags(sample, rows, 42) == []
    rows[-1].uuid = "last"
    rows[-1].mode = "11"
    assert supplemental_tags(sample, rows, 42) == []
    for row in rows:
        row.mode = "15"  # Absolute rule needs no frozen cohort reference.
    assert supplemental_tags(sample, rows, 42) == ["普普林"]
    for row in rows:
        row.mode = "24"  # Never apply it to actual sanma records.
    assert supplemental_tags(sample, rows, 42) == []


def test_custom_profile_is_retained_inside_the_ten_tag_cap(monkeypatch):
    extra = {
        "style": [f"其他打法{i}" for i in range(10)],
        "luck": [f"手气{i}" for i in range(3)],
        "achievement": [f"成就{i}" for i in range(3)],
    }
    monkeypatch.setattr("pupulinbot.ms_tags.trait_tags", lambda *a, **k: extra)
    sample = profile()
    sample.raw["_hand_count"] = 600
    tags = supplemental_tags(sample, records(count=50), 42)
    assert len(tags) == 10 and tags.count("普普林") == 1
