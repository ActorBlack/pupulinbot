import pytest
from test_ms_tags import records, stats

from pupulinbot.ms_tag_help import tag_help
from pupulinbot.ms_tags import supplemental_tags


@pytest.mark.parametrize(
    "win_points,deal_points,expected",
    [
        (6000, 6500, True),
        (6000, 6000.000001, True),
        (6000, 6000, False),
        (6500, 6000, False),
        (None, 6500, False),
        (6000, None, False),
        (0, 6500, False),
        (6000, 0, False),
        (-1, 6500, False),
        (6000, -1, False),
        (True, 6500, False),
        (6000, float("inf"), False),
        (float("nan"), 6500, False),
        ("invalid", 6500, False),
        ("6000", "6500", True),
    ],
)
def test_loss_exceeds_win_is_strict_and_rejects_invalid_averages(win_points, deal_points, expected):
    sample = stats(100, raw={"平均打点": win_points, "平均铳点": deal_points})
    tags = supplemental_tags(sample, records(), 42, maximum=100)
    assert ("得不偿失" in tags) is expected


def test_loss_exceeds_win_has_no_extra_minimum_games_or_hands():
    sample = stats(1, raw={"平均打点": 5000, "平均铳点": 6000, "_hand_count": None})
    assert "得不偿失" in supplemental_tags(sample, records(count=1), 42, maximum=100)
    assert "严格大于" in tag_help("得不偿失")
    sample.raw.pop("平均铳点")
    assert "得不偿失" not in supplemental_tags(sample, records(count=1), 42, maximum=100)
