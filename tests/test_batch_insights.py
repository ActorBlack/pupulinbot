import math

import pytest

from pupulinbot.batch_chart import GOLD_RATING, RAINBOW_RATING, rating_color
from pupulinbot.batch_insights import batch_insights, rainbow_threshold


def row(rating, *, model="4.1b", schema="current", total=100, matches=80, bad5=5):
    return {
        "status": "done",
        "metrics": {
            "rating": rating,
            "model": model,
            "metric_schema": schema,
            "decisions": total,
            "matches": matches,
            "agreement": matches / total * 100,
            "bad5": bad5,
            "bad10": bad5 + 1,
            "badmove_decisions": total,
            "bad5_rate": bad5 / total * 100,
            "bad10_rate": (bad5 + 1) / total * 100,
        },
    }


@pytest.mark.parametrize("games,threshold", [(1, 94), (49, 94), (50, 93), (99, 93), (100, 92)])
def test_rating_tier_boundaries(games, threshold):
    assert rainbow_threshold(games) == threshold
    assert rating_color(threshold, games=games) == RAINBOW_RATING
    assert rating_color(threshold - 0.01, games=games) == GOLD_RATING
    assert rating_color(90, games=games) == GOLD_RATING
    assert rating_color(89.99, games=games) not in {GOLD_RATING, RAINBOW_RATING}
    assert rating_color(math.nan, games=games) not in {GOLD_RATING, RAINBOW_RATING}


def test_unfinished_and_unrated_games_do_not_unlock_a_larger_sample_tier():
    rows = [row(93.5) for _ in range(49)] + [row(None)]
    rows += [{"status": "pending"}] * 49 + [{"status": "failed", "metrics": {"rating": 99}}]
    data = batch_insights(rows)
    group = next(iter(data["groups"].values()))
    assert data["total"] == 100 and data["completed"] == 50
    assert group["count"] == 49 and group["threshold"] == 94
    assert group["rainbow_count"] == 0
    assert group["best"][1] == group["worst"][1] == 93.5


def test_model_and_metric_schema_groups_are_never_combined():
    data = batch_insights(
        [row(92.5)] * 50 + [row(93.5, model="other")] * 49 + [row(99, schema="legacy")]
    )
    assert len(data["groups"]) == 3
    assert [g["count"] for g in data["groups"].values()] == [50, 49, 1]
    assert [g["threshold"] for g in data["groups"].values()] == [93, 94, 94]
    assert [g["summary"]["mean_rating"] for g in data["groups"].values()] == [92.5, 93.5, 99]


def test_weighted_rates_order_and_recent_window_changes():
    rows = [row(80, total=10, matches=8, bad5=1)] * 10
    rows += [row(90, total=90, matches=81, bad5=3)] * 10
    for i, item in enumerate(rows):
        rows[i] = {**item, "started_at": f"2026-11-{i + 1:02d}"}
    group = next(iter(batch_insights(list(reversed(rows)))["groups"].values()))
    summary = group["summary"]
    assert summary["agreement"] == pytest.approx(89)
    assert (summary["matches"], summary["decisions"]) == (890, 1000)
    assert summary["bad5_rate"] == pytest.approx(4)
    assert (summary["bad5"], summary["bad10"], summary["badmove_decisions"]) == (40, 60, 1000)
    assert group["deviation"] == 5
    assert group["best"][0] == 11 and group["worst"][0] == 1
    recent = group["recent"]
    assert recent["window"] == 10
    assert recent["mean_rating"] == 10 and recent["agreement"] == 10
    assert recent["bad5_rate"] == pytest.approx(100 / 30 - 10)


def test_pending_report_has_no_invented_analysis():
    data = batch_insights([{"status": "pending"}, {"status": "failed"}])
    assert data["groups"] == {} and data["completed"] == 0
