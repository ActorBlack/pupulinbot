"""Reimplementation of yurakuurame's published 12-feature four-player model.

Formula/data reference: https://yurakuurame.github.io/4ma-majang-type-check/
Coefficients and thresholds are factual model parameters, not a skill rating.
"""

import math
from dataclasses import dataclass

from .ms_sample import number

SOURCE = "https://yurakuurame.github.io/4ma-majang-type-check/"
# (API key, display name, percentage, mean, standard deviation, X weight, Y weight)
FEATURES = (
    ("和牌率", "和牌率", True, 0.229400816, 0.01018886, -1.166081274, 0.22551386),
    ("放铳率", "放铳率", True, 0.11106952, 0.009595166, -0.202381694, 0.889258806),
    ("副露率", "副露率", True, 0.331713127, 0.037372193, -1.258740534, -0.453560713),
    ("立直率", "立直率", True, 0.182824374, 0.018407074, -0.013917045, 0.451204072),
    ("默听率", "默胡率", True, 0.128029668, 0.029703506, 0.708071254, -1.48123253),
    ("平均打点", "平均打点", False, 6454.787778, 235.6563516, 1.249496931, -0.194681556),
    ("和了巡数", "平均和牌巡数", False, 12.12006667, 0.11553016, 0.73499073, 0.531014201),
    ("平均铳点", "平均放铳点数", False, 5387.771667, 141.1658779, -0.231466343, 0.202878547),
    ("流听率", "流局听牌率", True, 0.421591309, 0.04623791, -0.585817047, 0.81983416),
    ("立直巡目", "立直巡目", False, 9.298394589, 0.193397116, 0.831715773, 0.644693651),
    ("先制率", "立直先制率", True, 0.828159779, 0.021060104, -0.612817769, -2.393675857),
    ("追立率", "立直追立率", True, 0.171840221, 0.021060104, 0.546947012, 0.75875334),
)
ATTRIBUTES = {
    "和牌率": "win_rate",
    "放铳率": "deal_in_rate",
    "副露率": "call_rate",
    "立直率": "riichi_rate",
}


@dataclass(frozen=True)
class StyleAnalysis:
    metrics: dict
    missing: tuple
    supported: bool = True
    x: float | None = None
    y: float | None = None
    strength: str = ""
    kind: str = ""

    @property
    def label(self):
        return self.strength + self.kind if self.x is not None else "牌风数据不足"


def classify(x, y):
    """Preserve JS division/NaN semantics at X=0 and strict source thresholds."""
    radius = math.hypot(x, y)
    strength = next(
        (
            label
            for threshold, label in (
                (12.71, "超重度"),
                (8.89, "重度"),
                (3.2, "中度"),
                (1.47, "轻度"),
            )
            if radius > threshold
        ),
        "中庸",
    )
    slope = y / x if x else (math.copysign(math.inf, y) if y else math.nan)
    if slope > 1:
        kind = "后手反击型" if x > 0 else "先手闪避型"
    elif slope > -0.35:
        kind = "门前打点型" if x > 0 else "副露速度型"
    else:
        kind = "铁壁地藏型" if x > 0 else "全局参与型"
    return strength, kind


def analyze_style(stats, mode=4):
    if mode != 4:
        return StyleAnalysis({}, (), supported=False)
    raw = getattr(stats, "raw", {}) or {}
    metrics, missing = {}, []
    for key, label, percentage, *_ in FEATURES:
        value = number(raw[key] if key in raw else getattr(stats, ATTRIBUTES.get(key, ""), None))
        if value is None or value < 0 or (percentage and value > 1):
            missing.append(label)
        else:
            metrics[key] = value
    if missing:
        return StyleAnalysis(metrics, tuple(missing))
    # Match the site's percentage-input round trip and left-to-right JS sums.
    x = y = 0.0
    for key, _, percentage, mean, std, wx, wy in FEATURES:
        value = metrics[key] * 100 / 100 if percentage else metrics[key]
        standardized = (value - mean) / std
        x += standardized * wx
        y += standardized * wy
    if not math.isfinite(x) or not math.isfinite(y):
        return StyleAnalysis(metrics, ("指标超出计算范围",))
    strength, kind = classify(x, y)
    return StyleAnalysis(metrics, (), x=x, y=y, strength=strength, kind=kind)


def style_lines(stats, *, mode=4, scope="本次样本", games=(), account_id=None):
    result = analyze_style(stats, mode)
    if not result.supported:
        return ["牌风: 暂不支持三麻"]
    from .ms_tags import supplemental_tags

    extra = supplemental_tags(stats, games, account_id, mode=mode)
    base = "数据不足" if result.missing else "#" + result.label
    suffix = "".join(" #" + tag for tag in extra)
    return [f"牌风: {base}{suffix}"]
