import pytest

from pupulinbot.ms_candidate_tags import RULES
from pupulinbot.ms_tag_help import PENDING_RARITY, TAG_DESCRIPTIONS, tag_conditions, tag_help
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
    wins, victims = yakuman_tag_groups()
    if tag not in wins | victims:
        assert tag_conditions(tag)
        assert "条件：" in result


@pytest.mark.parametrize("tag", sorted(PENDING_RARITY))
def test_only_unverified_tags_get_temporary_gray_notice(tag):
    result = tag_help(tag)
    assert "待核验" in result and "暂以灰色显示" in result and "不代表常见" in result
    assert "稀有度：待核验" not in tag_help("全自动防守")


def test_candidate_help_reads_the_actual_thresholds(monkeypatch):
    assert "放铳次数≥40" in tag_help("偶失荆州")
    monkeypatch.setitem(RULES, "偶失荆州", (("deals", 55, None),))
    result = tag_help("偶失荆州")
    assert "放铳次数≥55" in result
    assert "放铳次数≥40" not in result


def test_current_changes_and_nested_combinations_are_explained():
    assert "至少 12 次" in tag_help("空中飞人")
    assert "零PT场次占比≥2%" in tag_help("无功而返")
    assert "查询对局数≥50" in tag_help("无功而返")
    assert "最大连庄次数≥6" in tag_help("独裁者")
    assert "如履薄冰＋毫厘之间" in tag_help("炎凉")
    assert "0.7巡" in tag_help("先声夺人")
    assert "立直和牌占比达到40%" in tag_help("暗杀星")
    assert "副露率25%～27%" in tag_help("普普林")
    for old in ("过客", "惜败", "爱撞大运", "立直引战", "霸主"):
        assert "未找到" in tag_help(old)


def test_help_does_not_invent_ordinary_ron_for_four_concealed_triplets():
    result = tag_help("四暗刻受害者")
    assert "坐庄时被对手自摸" in result
    assert "放铳给" not in result
