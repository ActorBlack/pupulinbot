import math

import pytest

from pupulinbot.rate_tags import DEAL_EDGES, WIN_EDGES, rate_tag


@pytest.mark.parametrize(
    ("deal", "expected"),
    [
        (0.08, ["佛系观战", "佛系观战", "铁壁地藏", "稳健运营", "冷血猎手", "胜似AI"]),
        (0.10, ["佛系观战", "佛系观战", "铁壁地藏", "稳健运营", "旺仔", "内部号"]),
        (0.12, ["保守人类", "保守人类", "谨慎人类", "标准运营", "小边牧", "老边牧"]),
        (0.14, ["小送子", "无谋野猪", "野性打法", "火龙果", "激进对攻", "狂战士"]),
        (0.16, ["纯送子", "纯送子", "标准野猪", "猪突猛进", "莽夫上头", "每战必至"]),
        (0.18, ["自爆卡车", "自爆卡车", "自爆卡车", "自爆卡车", "自爆卡车", "发癫了"]),
    ],
)
def test_all_reference_cells(deal, expected):
    assert [rate_tag(win, deal) for win in (0.18, 0.19, 0.21, 0.22, 0.24, 0.25)] == expected


def test_exact_boundaries_enter_higher_band():
    for edge in WIN_EDGES:
        assert rate_tag(edge, 0.14) == rate_tag(math.nextafter(edge, 1), 0.14)
        assert rate_tag(math.nextafter(edge, 0), 0.14) != rate_tag(edge, 0.14)
    for edge in DEAL_EDGES:
        assert rate_tag(0.24, edge) == rate_tag(0.24, math.nextafter(edge, 1))
        assert rate_tag(0.24, math.nextafter(edge, 0)) != rate_tag(0.24, edge)


@pytest.mark.parametrize("value", [None, True, "0.22", float("nan"), float("inf"), -0.1, 22])
def test_missing_invalid_and_percentage_values_are_not_classified(value):
    assert rate_tag(value, 0.12) is None
    assert rate_tag(0.22, value) is None


def test_sanma_is_never_classified():
    assert rate_tag(0.22, 0.14, mode=3) is None


def test_both_majsoul_events_render_label_and_sanma_does_not(monkeypatch):
    from types import SimpleNamespace

    from PIL import ImageDraw
    from test_monitor_card import game

    from pupulinbot.monitor_card import render_monitor_card

    rendered = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        rendered.append(text)
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    profiles = {
        i: {"stats": SimpleNamespace(win_rate=0.22, deal_in_rate=0.14)} for i in range(1, 5)
    }
    for event in ("start", "finish"):
        rendered.clear()
        render_monitor_card(game(), event, profiles)
        assert rendered.count("火龙果") == 4
    rendered.clear()
    render_monitor_card(game(3), "start", profiles)
    assert "火龙果" not in rendered
