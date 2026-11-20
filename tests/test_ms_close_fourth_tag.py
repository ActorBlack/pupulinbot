import pytest
from test_ms_tags import records, stats

from pupulinbot.ms_tag_help import tag_help
from pupulinbot.ms_tags import supplemental_tags


def sample(count=50, close_count=3, gap=1000, rank=4):
    games = records(ranks=[rank] * close_count + [2] * (count - close_count))
    for i, game in enumerate(games):
        if i < close_count:
            score = 10000 - gap if rank == 4 else 10000
            other = 10000 if rank == 4 else 10000 - gap
            game.players = [
                {"account_id": 42, "rank": rank, "score": score},
                {"account_id": 1, "score": 45000},
                {"account_id": 2, "score": 35000},
                {"account_id": 3, "score": other},
            ]
        else:
            game.players = [
                {"account_id": 42, "rank": 2, "score": 25000},
                {"account_id": 1, "score": 35000},
                {"account_id": 2, "score": 23000},
                {"account_id": 3, "score": 17000},
            ]
    return games


@pytest.mark.parametrize(
    "count,events,gap,eligible",
    [
        (49, 3, 1000, False),
        (50, 2, 1000, False),
        (50, 3, 0, False),
        (50, 3, 1, True),
        (50, 3, 1000, True),
        (50, 3, 1001, False),
    ],
)
def test_close_fourth_tag_requires_three_small_positive_deficits(count, events, gap, eligible):
    games = sample(count, events, gap)
    tags = supplemental_tags(stats(count), games, 42, maximum=100)
    assert ("咫尺天涯" in tags) is eligible
    assert "悬崖勒马" not in tags


def test_third_place_is_the_existing_mirror_tag():
    tags = supplemental_tags(stats(50), sample(rank=3), 42, maximum=100)
    assert "悬崖勒马" in tags
    assert "咫尺天涯" not in tags


def test_incomplete_scores_do_not_invent_close_fourths():
    games = sample()
    games[-1].players[-1].pop("score")
    assert "咫尺天涯" not in supplemental_tags(stats(50), games, 42, maximum=100)
    assert "微小点差" in tag_help("咫尺天涯")
