"""Build room-specific tag references with the same Jade east/south calibration."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .ms_sample import number
from .ms_trait_tags import build_trait_reference

HAND_METRICS = ("和牌率", "放铳率", "副露率", "立直率")
WIN_METRICS = ("平均打点", "和了巡数", "默听率")


def _bound(mean, offset, step, *, up):
    value = Decimal(str(mean)) + Decimal(str(offset))
    unit = Decimal(str(step))
    return float(
        (value / unit).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * unit
    )


def build_reference(levels, name, *, source_mode, kind="room"):
    """Consume actual per-level aggregates; reject incomplete or empty references."""
    if not isinstance(levels, dict) or not levels:
        raise ValueError("标签参考统计为空")
    normalized = {}
    for level, data in levels.items():
        if not isinstance(data, dict):
            raise ValueError("标签参考统计格式错误")
        basic, extended = data.get("basic", {}), data.get("extended", {})
        if not isinstance(basic, dict) or not isinstance(extended, dict):
            raise ValueError("标签参考统计缺少基础或扩展数据")
        counts = [number(basic.get("count")), number(extended.get("count"))]
        if any(value is None or value <= 0 or not value.is_integer() for value in counts):
            raise ValueError("标签参考统计缺少有效场数或局数")
        basic, extended = dict(basic), dict(extended)
        basic["count"], extended["count"] = map(int, counts)
        for key in HAND_METRICS + WIN_METRICS:
            value = number(extended.get(key))
            if value is None or value < 0 or (key not in {"平均打点", "和了巡数"} and value > 1):
                raise ValueError("标签参考指标缺失或无效")
            extended[key] = value
        ranks = basic.get("rank_rates")
        if (
            not isinstance(ranks, list)
            or len(ranks) != 4
            or any(number(value) is None or not 0 <= number(value) <= 1 for value in ranks)
        ):
            raise ValueError("标签参考统计缺少四麻顺位")
        ranks = [number(value) for value in ranks]
        average = number(basic.get("avg_rank"))
        if average is None or not 1 <= average <= 4:
            raise ValueError("标签参考统计缺少有效均顺")
        if (
            abs(sum(ranks) - 1) > 1e-6
            or abs(sum((i + 1) * value for i, value in enumerate(ranks)) - average) > 1e-6
        ):
            raise ValueError("标签参考顺位比例或均顺不一致")
        basic.update(rank_rates=ranks, avg_rank=average)
        normalized[level] = {**data, "basic": basic, "extended": extended}
    levels = normalized
    rows = list(levels.values())
    appearances = sum(row["basic"]["count"] for row in rows)
    hands = sum(row["extended"]["count"] for row in rows)
    wins = sum(row["extended"]["count"] * row["extended"]["和牌率"] for row in rows)
    if wins <= 0:
        raise ValueError("标签参考统计没有有效和牌")
    means = {
        key: sum(row["extended"][key] * row["extended"]["count"] for row in rows) / hands
        for key in HAND_METRICS
    }
    means.update(
        {
            key: sum(
                row["extended"][key] * row["extended"]["count"] * row["extended"]["和牌率"]
                for row in rows
            )
            / wins
            for key in WIN_METRICS
        }
    )
    means["rank_rates"] = [
        sum(row["basic"]["rank_rates"][i] * row["basic"]["count"] for row in rows) / appearances
        for i in range(4)
    ]
    means["avg_rank"] = (
        sum(row["basic"]["avg_rank"] * row["basic"]["count"] for row in rows) / appearances
    )
    thresholds = {
        "call_high": _bound(means["副露率"], 0.05, 0.001, up=True),
        "call_low": _bound(means["副露率"], -0.05, 0.001, up=False),
        "riichi_high": _bound(means["立直率"], 0.03, 0.001, up=True),
        "deal_high": _bound(means["放铳率"], 0.02, 0.001, up=True),
        "deal_low": _bound(means["放铳率"], -0.02, 0.001, up=False),
        "win_floor": _bound(means["和牌率"], -0.01, 0.001, up=True),
        "win_attack": _bound(means["和牌率"], 0, 0.001, up=True),
        "win_high": _bound(means["和牌率"], 0.015, 0.001, up=True),
        "dama_high": _bound(means["默听率"], 0.05, 0.001, up=True),
        "points_high": _bound(means["平均打点"], 500, 100, up=True),
        "points_low": _bound(means["平均打点"], -500, 200, up=False),
        "turn_low": _bound(means["和了巡数"], -0.4, 0.1, up=False),
        "first_high": _bound(means["rank_rates"][0], 0.05, 0.01, up=True),
        "fourth_low": _bound(means["rank_rates"][3], -0.05, 0.001, up=False),
        "income_high": 150,
    }
    return {
        "name": name,
        "source_mode": str(source_mode),
        "reference_kind": kind,
        "reference_levels": list(levels),
        "player_game_appearances": appearances,
        "player_hand_appearances": hands,
        "means": means,
        "thresholds": thresholds,
        "levels": levels,
        "traits": build_trait_reference(levels),
        "cohort_percentiles_available": False,
    }
