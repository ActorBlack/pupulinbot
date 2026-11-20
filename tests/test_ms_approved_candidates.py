import pytest
from test_ms_tags import records, stats

from pupulinbot.ms_candidate_tags import candidate_tags
from pupulinbot.ms_tag_help import tag_help
from pupulinbot.ms_tags import supplemental_tags
from pupulinbot.ms_trait_tags import TAG_COMBINATIONS, combine_tags, select_tags

MEANS = {
    "和牌率": 0.22,
    "副露率": 0.30,
    "立直率": 0.20,
    "平均起手向听": 3.5,
    "和了巡数": 12,
    "立直好型2": 0.60,
    "立直后和牌率": 0.50,
    "副露后和牌率": 0.30,
    "放铳率": 0.12,
}


def classify(monkeypatch, changes, count=100, hands=1000):
    monkeypatch.setattr("pupulinbot.ms_candidate_tags.candidate_means", lambda: {"12": MEANS})
    raw = {**MEANS, **changes}
    return candidate_tags(
        raw,
        records(count=count),
        [],
        42,
        hands=hands,
        win=raw["和牌率"],
        deal=raw["放铳率"],
        call=raw["副露率"],
        riichi=raw["立直率"],
    )


@pytest.mark.parametrize(
    "tag,changes,break_key,break_value",
    [
        ("天意弄人", {"立直好型2": 0.68, "立直后和牌率": 0.44}, "立直好型2", 0.679),
        ("出奇制胜", {"立直好型2": 0.52, "立直后和牌率": 0.56}, "立直后和牌率", 0.559),
        ("高开低走", {"平均起手向听": 3.45, "和牌率": 0.21, "和了巡数": 12.3}, "和了巡数", 12.299),
        ("逆流而上", {"平均起手向听": 3.55, "和牌率": 0.23, "和了巡数": 11.8}, "和牌率", 0.229),
        ("进退失据", {"副露后和牌率": 0.26, "立直后和牌率": 0.46, "放铳率": 0.14}, "放铳率", 0.139),
    ],
)
def test_approved_tags_boundaries_and_sample_guards(
    monkeypatch, tag, changes, break_key, break_value
):
    assert tag in classify(monkeypatch, changes)
    assert tag not in classify(monkeypatch, {**changes, break_key: break_value})
    assert tag not in classify(monkeypatch, changes, count=49)
    assert tag not in classify(monkeypatch, changes, hands=399)
    assert tag not in classify(monkeypatch, {**changes, break_key: None})
    assert "未找到" not in tag_help(tag)


@pytest.mark.parametrize(
    "left,right,tag",
    [
        ("闪电战", "神速立直", "电光石火"),
        ("先发制人", "神速立直", "先声夺人"),
        ("零点逃生", "悬崖勒马", "绝处逢生"),
        ("悬赏猎人", "被炸仙人", "福祸相依"),
        ("白手起家", "越挫越勇", "百折不挠"),
        ("中庸之道", "毫厘之间", "执两用中"),
    ],
)
def test_approved_combinations_include_luck_evidence(left, right, tag):
    assert tag in combine_tags([left, right], TAG_COMBINATIONS)
    assert tag not in combine_tags([left], TAG_COMBINATIONS)
    extra = {"achievement": [], "luck": [left], "style": [right]}
    result = select_tags([], extra, 10)
    assert tag in result
    assert left not in result and right not in result
    assert "未找到" not in tag_help(tag)


def test_nested_shared_combinations_and_luck_display_caps():
    evidence = ["中庸之道", "略胜一筹", "棋差一着", "闪电战", "先发制人", "神速立直"]
    tags = combine_tags(evidence, TAG_COMBINATIONS)
    assert {"执两用中", "电光石火", "先声夺人"} <= set(tags)
    assert tags == combine_tags(evidence, tuple(reversed(TAG_COMBINATIONS)))
    extra = {
        "achievement": [],
        "luck": ["里宝超人", "一发超人", "里宝绝缘体", "白手起家"],
        "style": ["越挫越勇"],
    }
    result = select_tags([], extra, 10)
    assert "百折不挠" in result
    assert "白手起家" not in result
    assert len(result) <= 10


def test_first_strike_gate_inclusive_boundaries_and_missing_data():
    from pupulinbot.ms_tags import first_strike_eligible

    ref = {"traits": {"先制率": {"mean": 0.8}, "立直巡目": {"mean": 10}}}
    raw = {"先制率": 0.88, "立直巡目": 9.3, "立直率": 0.2}
    assert first_strike_eligible(raw, ref, 500)
    assert not first_strike_eligible(raw, ref, 499)
    for changes in ({"先制率": 0.879}, {"立直巡目": 9.301}, {"立直巡目": None}, {"立直率": None}):
        assert not first_strike_eligible({**raw, **changes}, ref, 500)
    assert not first_strike_eligible(raw, {"traits": {}}, 500)


def test_first_strike_gate_filters_actual_output_without_removing_parent_tags():
    from test_ms_tags import stats

    from pupulinbot.ms_tags import reference_for, supplemental_tags

    ref = reference_for("12")["traits"]
    raw = {"先制率": ref["先制率"]["mean"] + 0.09, "立直巡目": ref["立直巡目"]["mean"] - 0.8}
    raw["追立率"] = 1 - raw["先制率"]
    strong = stats(100, raw=raw)
    assert "先声夺人" in supplemental_tags(strong, records(), 42, maximum=100)
    raw["立直巡目"] = ref["立直巡目"]["mean"] - 0.6
    weak = stats(100, raw=raw)
    tags = supplemental_tags(weak, records(), 42, maximum=100)
    assert "先声夺人" not in tags
    assert {"先发制人", "神速立直"} <= set(tags)


@pytest.mark.parametrize(
    "count,zeros,expected",
    [
        (1, 1, False),
        (49, 2, False),
        (50, 1, False),
        (50, 2, True),
        (100, 1, False),
        (100, 2, True),
        (101, 2, False),
        (101, 3, True),
        (200, 3, False),
        (200, 4, True),
        (500, 9, False),
        (500, 10, True),
    ],
)
def test_zero_pt_tag_count_and_percentage(count, zeros, expected):
    games = records(count=count)
    for i, game in enumerate(games):
        game.players[0]["gradingScore"] = 0 if i < zeros else 30
    sample = stats(count)
    assert ("无功而返" in supplemental_tags(sample, games, 42, maximum=100)) is expected
    assert "无功而返" not in supplemental_tags(sample, games, 42, mode=3)
    assert "未找到" not in tag_help("无功而返")


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("gradingScore", 0, True),
        ("grading_score", 0, True),
        ("gradingScore", None, False),
        ("gradingScore", False, False),
        ("gradingScore", float("nan"), False),
        ("gradingScore", 0.0001, False),
    ],
)
def test_zero_pt_tag_requires_exact_own_delta(field, value, expected):
    games = records(count=50)
    for game in games:
        game.players[0]["gradingScore"] = 30
    games[0].players[0]["gradingScore"] = 0
    games[1].players[0].pop("gradingScore")
    games[1].players[0][field] = value
    games[1].players.append({"account_id": 999, "gradingScore": 0})
    assert ("无功而返" in supplemental_tags(stats(50), games, 42, maximum=100)) is expected


def test_zero_pt_tag_does_not_use_missing_scores_or_cancelled_deltas():
    games = records(count=50)
    assert "无功而返" not in supplemental_tags(stats(50), games, 42, maximum=100)
    for i, game in enumerate(games):
        game.players[0]["gradingScore"] = -30 if i % 2 else 30
    assert "无功而返" not in supplemental_tags(stats(50), games, 42, maximum=100)


@pytest.mark.parametrize(
    "tag,rank,points,minimum",
    [
        ("略胜一筹", 1, [31000, 30500, 20000, 18500], 3),
        ("棋差一着", 2, [31000, 30500, 20000, 18500], 3),
        ("悬崖勒马", 3, [40000, 30000, 15500, 14500], 3),
        ("咫尺天涯", 4, [40000, 30000, 15500, 14500], 3),
        ("零点逃生", 3, [50000, 49000, 1000, 0], 2),
        ("马拉松", 2, [40000, 30000, 20000, 10000], 8),
    ],
)
def test_match_tags_require_count_and_share_in_actual_samples(tag, rank, points, minimum):
    from dataclasses import replace
    from datetime import timedelta

    for count, matches, expected in (
        (100, minimum - 1, False),
        (100, minimum, True),
        (200, minimum, False),
        (200, 2 * minimum, True),
        (201, 2 * minimum, False),
        (201, 2 * minimum + 1, True),
    ):
        games = records(count=count)
        for i, game in enumerate(games):
            selected = i < matches
            placement = rank if selected else 2
            scores = points if selected else [40000, 30000, 20000, 10000]
            game.players[:] = [
                {
                    "account_id": 42 if place == placement else 100 + place,
                    "rank": place,
                    "score": score,
                }
                for place, score in enumerate(scores, start=1)
            ]
            duration = 55 if selected and tag == "马拉松" else 30
            games[i] = replace(game, ended_at=game.started_at + timedelta(minutes=duration))
        tags = supplemental_tags(stats(count), games, 42, maximum=100)
        assert (tag in tags) is expected, (tag, count, matches, tags)


def test_every_match_count_has_percentage_except_streaks():
    from pupulinbot.ms_candidate_tags import GAME_COUNT_METRICS, RULES

    covered = set()
    for tag, conditions in RULES.items():
        for metric, low, _ in conditions:
            if metric in GAME_COUNT_METRICS:
                assert (f"{metric}_sample_rate", low / 100, None) in conditions, tag
                covered.add(metric)
    assert covered == GAME_COUNT_METRICS
    for tag in ("连胜之道", "连败之道"):
        assert not any("rate" in metric for metric, _, _ in RULES[tag])


@pytest.mark.parametrize("tag,rank", [("连胜之道", 1), ("连败之道", 4)])
def test_streak_tags_still_trigger_at_five_in_large_samples(tag, rank):
    games = records(ranks=[rank] * 5 + [2, 3] * 247 + [2])
    assert len(games) == 500
    assert tag in supplemental_tags(stats(500), games, 42, maximum=100)


def test_assassin_drops_riichi_share_without_granting_deep_menzen():
    from pupulinbot.ms_tags import reference_for

    count = 100
    reference = reference_for("12")
    raw = {
        "_hand_count": 1000,
        "默听率": 0.60,
        "和了巡数": 10,
        "平均打点": 8000,
        "立直后和牌率": 0.50,
    }
    sample = stats(count, win_rate=0.25, call_rate=0.15, riichi_rate=0.05, raw=raw)
    tags = supplemental_tags(sample, records(count=count), 42, maximum=100)
    assert "暗杀星" in tags
    assert "深度门清" not in tags
    assert "静默猎手" not in tags
    assert "无声快刀" not in tags
    assert "默听流" not in tags
    # Raising the share to the original threshold still follows the old chain.
    original = stats(
        count,
        win_rate=0.25,
        call_rate=0.15,
        riichi_rate=0.10,
        raw={**raw, "立直后和牌率": 1.0},
    )
    assert "暗杀星" in supplemental_tags(original, records(count=count), 42, maximum=100)
    # Other deep-menzen requirements continue to gate the relaxed combination.
    low_wins = stats(
        count, win_rate=0.25, call_rate=0.15, riichi_rate=0.05, raw={**raw, "_hand_count": 236}
    )
    assert "暗杀星" not in supplemental_tags(low_wins, records(count=count), 42, maximum=100)
    from pupulinbot.ms_candidate_tags import room_means

    call_mean = room_means("12", reference_levels=reference["levels"])["副露率"]
    high_call = stats(count, win_rate=0.25, call_rate=call_mean - 0.029, riichi_rate=0.05, raw=raw)
    assert "暗杀星" not in supplemental_tags(high_call, records(count=count), 42, maximum=100)
    # A relaxed door-clear profile alone cannot replace the other two parents.
    late = stats(
        count, win_rate=0.25, call_rate=0.15, riichi_rate=0.05, raw={**raw, "和了巡数": 14}
    )
    assert "暗杀星" not in supplemental_tags(late, records(count=count), 42, maximum=100)
    assert "不要求立直和牌占比" in tag_help("暗杀星")


def test_qianlong_requires_all_parents_and_resolves_nested_cannon():
    from pupulinbot.ms_style_rarity import rarity_tier

    parents = ["门清流", "重炮手", "稳健防守"]
    expanded = ["门清流", "一击必杀", "高打点", "稳健防守"]
    for evidence in (parents, expanded):
        assert combine_tags(evidence, TAG_COMBINATIONS) == ["潜龙"]
        assert combine_tags(evidence, tuple(reversed(TAG_COMBINATIONS))) == ["潜龙"]
        for missing in evidence:
            assert "潜龙" not in combine_tags(
                [tag for tag in evidence if tag != missing], TAG_COMBINATIONS
            )
    extra = {"style": [], "luck": [], "achievement": []}
    assert select_tags(expanded, extra, 1) == ["潜龙"]
    assert rarity_tier("潜龙") == "easter_egg"
    assert "门清流" in tag_help("潜龙")
    assert "彩色终极" in tag_help("潜龙")
