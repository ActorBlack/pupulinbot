"""User-supplied four-player win/deal-in rate matrix (rates are fractions)."""

import math
from bisect import bisect_right

WIN_EDGES = (0.185, 0.20, 0.215, 0.23, 0.245)
DEAL_EDGES = (0.09, 0.11, 0.13, 0.15, 0.17)
TAGS = (
    ("佛系观战", "佛系观战", "铁壁地藏", "稳健运营", "冷血猎手", "胜似AI"),
    ("佛系观战", "佛系观战", "铁壁地藏", "稳健运营", "旺仔", "内部号"),
    ("保守人类", "保守人类", "谨慎人类", "标准运营", "小边牧", "老边牧"),
    ("小送子", "无谋野猪", "野性打法", "火龙果", "激进对攻", "狂战士"),
    ("纯送子", "纯送子", "标准野猪", "猪突猛进", "莽夫上头", "每战必至"),
    ("自爆卡车", "自爆卡车", "自爆卡车", "自爆卡车", "自爆卡车", "发癫了"),
)


def rate_tag(win, deal, *, mode=4):
    """Boundary values enter the higher band; never classify sanma/missing data."""
    if mode != 4 or any(
        not isinstance(v, (int, float))
        or isinstance(v, bool)
        or not math.isfinite(v)
        or not 0 <= v <= 1
        for v in (win, deal)
    ):
        return None
    return TAGS[bisect_right(DEAL_EDGES, deal)][bisect_right(WIN_EDGES, win)]
