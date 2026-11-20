import pytest
from test_ms_tags import records, stats

from pupulinbot.ms_tags import reference_for, supplemental_tags
from pupulinbot.ms_trait_tags import trait_tags


def efficiency_sample(count, offset, hands=1200, win=0.3, deal=0.1):
    mean = reference_for("12")["traits"]["净打点效率"]["mean"]
    return stats(
        count,
        win_rate=win,
        deal_in_rate=deal,
        raw={
            "_hand_count": hands,
            "平均铳点": 5000,
            "平均打点": (mean + offset + deal * 5000) / win,
        },
    )


@pytest.mark.parametrize(
    "count,offset,hands,win,deal,eligible",
    [
        (49, 350, 1200, 0.3, 0.1, False),
        (50, 349.99, 1200, 0.3, 0.1, False),
        (50, 350, 1200, 0.3, 0.1, True),
        (50, 350, 399, 0.3, 0.1, False),
        (50, 350, 400, 0.3, 0.1, True),
        (50, 350, 400, 0.0725, 0.1, False),
        (50, 350, 400, 0.3, 0.0725, False),
        (50, 350, 400, 0.075, 0.075, True),
    ],
)
def test_high_efficiency_gate_boundaries(count, offset, hands, win, deal, eligible):
    sample = efficiency_sample(count, offset, hands, win, deal)
    # 局收支 is deliberately absent: it is not a prerequisite of 高效打点.
    tags = trait_tags(
        sample.raw,
        reference_for("12"),
        hands,
        win=win,
        call=0.32,
        riichi=0.19,
        games=records(count=count),
    )["style"]
    assert ("高效打点" in tags) is eligible


def test_high_efficiency_does_not_require_positive_round_income():
    sample = efficiency_sample(50, 400)
    sample.raw["局收支"] = -100
    tags = supplemental_tags(sample, records(count=50), 42, maximum=100)
    assert "高效打点" in tags
    assert "局收支重视" not in tags and "精打细算" not in tags
    sample.raw["局收支"] = 200
    tags = supplemental_tags(sample, records(count=50), 42, maximum=100)
    assert "精打细算" in tags and "高效打点" not in tags


def test_low_efficiency_keeps_its_previous_threshold_and_sample_minimum():
    sample = efficiency_sample(20, -250.01)
    assert (
        "低效打点"
        in trait_tags(
            sample.raw,
            reference_for("12"),
            1200,
            win=0.3,
            call=0.32,
            riichi=0.19,
            games=records(count=20),
        )["style"]
    )
    assert reference_for("12")["traits"]["净打点效率"]["low"] == round(
        reference_for("12")["traits"]["净打点效率"]["mean"] - 250, 12
    )
