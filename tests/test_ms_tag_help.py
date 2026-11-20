import re

import pytest

from pupulinbot.ms_candidate_tags import RULES
from pupulinbot.ms_tag_help import PENDING_RARITY, TAG_DESCRIPTIONS, tag_help
from pupulinbot.ms_trait_tags import (
    AVERAGE_TRAITS,
    RATE_TRAITS,
    TAG_COMBINATIONS,
    yakuman_tag_groups,
)


def all_tags():
    wins, victims = yakuman_tag_groups()
    return sorted(
        set(TAG_DESCRIPTIONS)
        | set(RULES)
        | set(wins)
        | set(victims)
        | {rule[-1] for rule in TAG_COMBINATIONS}
        | {tag for rule in (RATE_TRAITS | AVERAGE_TRAITS).values() for tag in rule[-2:] if tag}
    )


@pytest.mark.parametrize("tag", all_tags())
def test_every_current_tag_has_help(tag):
    result = tag_help(" ＃" + tag + " ")
    assert result.startswith(f"【{tag}】\n")
    assert "未找到" not in result
    assert "条件：" not in result
    assert not re.search(r"[0-9%≥≤×]", result)
    assert len(result.splitlines()) <= 3
    assert len(result) <= 130


@pytest.mark.parametrize("tag", sorted(PENDING_RARITY))
def test_only_unverified_tags_get_temporary_gray_notice(tag):
    result = tag_help(tag)
    assert "待核验" in result and "暂以灰色显示" in result and "不代表常见" in result
    assert "稀有度待核验" not in tag_help("全自动防守")


def test_help_stays_brief_when_the_classifier_threshold_changes(monkeypatch):
    previous = tag_help("偶失荆州")
    monkeypatch.setitem(RULES, "偶失荆州", (("deals", 55, None),))
    assert tag_help("偶失荆州") == previous
    assert "放铳偏少" in previous


def test_current_names_and_nested_combinations_are_explained():
    assert "被击飞" in tag_help("空中飞人")
    assert "原地踏步" in tag_help("无功而返")
    assert "连续坐庄" in tag_help("独裁者")
    assert "如履薄冰" in tag_help("炎凉") and "毫厘之间" in tag_help("炎凉")
    assert "先发制人" in tag_help("先声夺人") and "神速立直" in tag_help("先声夺人")
    assert "无声快刀" in tag_help("暗杀星") and "默听流" in tag_help("暗杀星")
    for old in ("过客", "惜败", "爱撞大运", "立直引战", "霸主"):
        assert "未找到" in tag_help(old)


def test_help_does_not_invent_ordinary_ron_for_four_concealed_triplets():
    result = tag_help("四暗刻受害者")
    assert "坐庄时被对手自摸" in result
    assert "放铳给" not in result
