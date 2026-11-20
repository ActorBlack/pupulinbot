from itertools import combinations, permutations

from test_ms_tags import records, stats

from pupulinbot.ms_style_rarity import RAINBOW_TAGS, rarity_tier
from pupulinbot.ms_tag_help import tag_help
from pupulinbot.ms_tags import supplemental_tags
from pupulinbot.ms_trait_tags import COMBINATION_TAGS, TAG_COMBINATIONS, combine_tags

LEAVES = ("咫尺天涯", "悬崖勒马", "略胜一筹", "棋差一着")


def test_yanliang_requires_all_four_leaves_and_resolves_independent_of_order():
    for order in permutations(LEAVES):
        assert combine_tags(list(order), TAG_COMBINATIONS) == ["炎凉"]
    for size in range(4):
        for subset in combinations(LEAVES, size):
            assert "炎凉" not in combine_tags(list(subset), TAG_COMBINATIONS)
    assert combine_tags(list(LEAVES), tuple(reversed(TAG_COMBINATIONS))) == ["炎凉"]


def test_nested_inputs_hide_their_prerequisites():
    assert combine_tags(["咫尺天涯", "悬崖勒马"], TAG_COMBINATIONS) == ["如履薄冰"]
    assert combine_tags(["如履薄冰", "毫厘之间"], TAG_COMBINATIONS) == ["炎凉"]
    assert combine_tags(["炎凉", *LEAVES, "如履薄冰", "毫厘之间"], TAG_COMBINATIONS) == ["炎凉"]
    assert {"如履薄冰", "炎凉"} <= COMBINATION_TAGS
    assert "炎凉" in RAINBOW_TAGS
    assert rarity_tier("炎凉") == "easter_egg"
    assert "如履薄冰" not in RAINBOW_TAGS
    assert "咫尺天涯" in tag_help("如履薄冰")
    assert "毫厘之间" in tag_help("炎凉")


def test_actual_scored_records_form_yanliang():
    ranks = [1] * 3 + [2] * 3 + [3] * 3 + [4] * 3 + [2] * 38
    games = records(ranks=ranks)
    scores = [31000, 30000, 20000, 19000]
    for game, rank in zip(games, ranks, strict=True):
        game.players = [{"account_id": 42, "score": scores[rank - 1], "rank": rank}]
        game.players.extend(
            {"account_id": 100 + i, "score": score, "rank": i + 1}
            for i, score in enumerate(scores)
            if i != rank - 1
        )
    tags = supplemental_tags(stats(50), games, 42, maximum=100)
    assert "炎凉" in tags
    assert not (set(LEAVES) | {"如履薄冰", "毫厘之间"}) & set(tags)
    # Losing one of the three qualifying fourths invalidates only that side.
    games[11].players[0]["score"] = 18000
    tags = supplemental_tags(stats(50), games, 42, maximum=100)
    assert "炎凉" not in tags
    assert "悬崖勒马" in tags and "毫厘之间" in tags
