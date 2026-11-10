"""Additional four-player traits with metric-specific event denominators.

Definitions follow amae-koromo's playerDetails.tsx. References reuse frozen
room aggregates; evaluating a sample never makes an additional API request.
"""

import math
from datetime import UTC, datetime

from .ms_sample import number

# key: (denominator, minimum events, high/low offsets, category, high/low names)
RATE_TRAITS = {
    "里宝率": ("riichi_wins", 30, 0.08, 0.08, "luck", "里宝超人", "里宝绝缘体"),
    "一发率": ("riichi_wins", 30, 0.06, 0.06, "luck", "一发超人", "一发绝缘体"),
    "自摸率": ("wins", 50, 0.07, None, "style", "自摸超人", None),
    "先制率": ("riichis", 50, 0.05, None, "style", "先手立直流", None),
    "追立率": ("riichis", 50, 0.05, None, "style", "追立斗士", None),
    "副露后和牌率": ("calls", 75, 0.05, None, "style", "鸣牌善战", None),
    "副露后放铳率": ("calls", 75, None, 0.03, "style", None, "鸣牌慎行"),
    "流听率": ("draws", 50, 0.08, None, "style", "流局听牌王", None),
    "立直后放铳率": ("riichis", 50, 0.03, 0.03, "style", "立直撞铳", "立直护体"),
    "立直后和牌率": ("riichis", 50, 0.05, 0.05, "style", "立直收割机", "立直苦战"),
    "立直后流局率": ("riichis", 50, 0.04, None, "style", "立直流局王", None),
    "立直多面": ("riichis", 50, 0.05, 0.05, "style", "多面立直流", "窄听立直流"),
}
AVERAGE_TRAITS = {
    "立直巡目": ("riichis", 50, None, 0.5, "style", None, "早巡立直"),
    "平均起手向听": ("hands", 300, 0.1, 0.1, "luck", "配牌修行", "配牌眷顾"),
    "打点效率": ("hands", 400, 200, None, "style", "火力全开", None),
    "铳点损失": ("hands", 400, 150, 150, "style", "铳点漏财", "铳点节流"),
    "净打点效率": ("hands", 400, 250, 250, "style", "攻防高效", "攻防承压"),
}
# Upstream dates collection to *around* 2021-09-10. Leave a rollout buffer.
MULTISIDED_SINCE = datetime(2021, 10, 1, tzinfo=UTC)


def efficiency_metrics(raw):
    """Author's exact products; reject inconsistent supplied rounded API values."""
    result = {}
    for name, rate_key, points_key in (
        ("打点效率", "和牌率", "平均打点"),
        ("铳点损失", "放铳率", "平均铳点"),
    ):
        probability, points = rate(raw.get(rate_key)), number(raw.get(points_key))
        if probability is None or points is None or points < 0:
            continue
        value = probability * points
        supplied = number(raw.get(name))
        if name in raw and (supplied is None or abs(supplied - value) > 2):
            continue
        result[name] = value
    if len(result) == 2:
        value = result["打点效率"] - result["铳点损失"]
        supplied = number(raw.get("净打点效率"))
        if "净打点效率" not in raw or (supplied is not None and abs(supplied - value) <= 2):
            result["净打点效率"] = value
    return result


def rate(value):
    value = number(value)
    return value if value is not None and 0 <= value <= 1 else None


def wilson_interval(value, trials):
    """95% binomial approximation, used as a conservative rate sampling guard."""
    value, trials = rate(value), number(trials)
    if value is None or trials is None or trials <= 0 or not trials.is_integer():
        return None
    z2 = 1.96**2
    divisor = 1 + z2 / trials
    center = (value + z2 / (2 * trials)) / divisor
    radius = 1.96 * math.sqrt(value * (1 - value) / trials + z2 / (4 * trials**2)) / divisor
    return max(0, center - radius), min(1, center + radius)


def _counter(raw, key, maximum):
    value = number(raw.get(key))
    return value if value is not None and value.is_integer() and 0 <= value <= maximum else None


def exposures(raw, hands, *, win=None, call=None, riichi=None):
    """Keep denominator estimates separate; never replace a missing one by hands.

    A supplied but invalid exact riichi-win counter is not silently replaced.
    Aggregate weights remain fractional; sample trials are floored by the caller.
    """
    win, call, riichi = (
        rate(raw.get(key) if value is None else value)
        for key, value in (("和牌率", win), ("副露率", call), ("立直率", riichi))
    )
    counts = {
        "hands": hands,
        "wins": hands * win if win is not None else None,
        "calls": hands * call if call is not None else None,
        "riichis": hands * riichi if riichi is not None else None,
    }
    deal = rate(raw.get("放铳率"))
    counts["deals"] = hands * deal if deal is not None else None
    draw_rate = rate(raw.get("流局率"))
    counts["draws"] = hands * draw_rate if draw_rate is not None else None
    if "立直和了" in raw:
        bounds = [hands] + [counts[key] for key in ("wins", "riichis") if counts[key] is not None]
        counts["riichi_wins"] = _counter(raw, "立直和了", min(bounds) + 1e-6)
        post_win = rate(raw.get("立直后和牌率"))
        if (
            counts["riichi_wins"] is not None
            and counts["riichis"] is not None
            and post_win is not None
            and abs(counts["riichi_wins"] - counts["riichis"] * post_win) > 1e-6
        ):
            counts["riichi_wins"] = None
    else:
        post_win = rate(raw.get("立直后和牌率"))
        counts["riichi_wins"] = (
            counts["riichis"] * post_win
            if counts["riichis"] is not None and post_win is not None
            else None
        )
        if (
            counts["riichi_wins"] is not None
            and counts["wins"] is not None
            and counts["riichi_wins"] > counts["wins"] + 1e-6
        ):
            counts["riichi_wins"] = None
    return counts


def build_trait_reference(levels):
    """Weight each conditional metric by its own actual exposure, not total hands.

    If any positive/unknown-weight cohort is missing a metric, omit that metric
    rather than silently comparing the sample against a different population.
    """
    result = {}
    for key, definition in {**RATE_TRAITS, **AVERAGE_TRAITS}.items():
        basis, _, high, low, *_ = definition
        weighted = total = 0.0
        for row in levels.values():
            raw = row["extended"]
            weight = exposures(raw, raw["count"])[basis]
            value = (
                rate(raw.get(key))
                if key in RATE_TRAITS
                else efficiency_metrics(raw).get(key)
                if key in {"打点效率", "铳点损失", "净打点效率"}
                else number(raw.get(key))
            )
            invalid = value is None or (key != "净打点效率" and value < 0)
            if weight is None or (weight > 0 and invalid):
                break
            if weight > 0:
                weighted += value * weight
                total += weight
        else:
            if total > 0:
                mean = weighted / total
                result[key] = {
                    "basis": basis,
                    "events": total,
                    "mean": mean,
                    "high": round(mean + high, 12) if high is not None else None,
                    "low": round(mean - low, 12) if low is not None else None,
                }
    return result


def yakuman_victim(raw, games):
    """A real large deal-in, matched to this exact sample; never infer from a final score."""
    loss = raw.get("最近大铳")
    if not isinstance(loss, dict) or not isinstance(loss.get("id"), str):
        return False
    game = next((game for game in games if game.uuid == loss["id"]), None)
    if game is None:
        return False
    timestamp = number(loss.get("start_time"))
    if timestamp is None or abs(timestamp - game.started_at.timestamp()) > 1:
        return False
    fans = loss.get("fans")
    if not isinstance(fans, list) or not fans:
        return False
    total = yakuman = 0
    for fan in fans:
        if not isinstance(fan, dict):
            return False
        count, limit = number(fan.get("count")), number(fan.get("役满"))
        if any(v is None or not v.is_integer() or not 0 <= v <= 100 for v in (count, limit)):
            return False
        total += count
        yakuman += limit
    return yakuman > 0 or total >= 13


def achievement_tags(raw, games, counts):
    tags = []
    for key, label, basis in (("役满", "役满达成", "wins"), ("流满", "流局满贯", "hands")):
        maximum = counts[basis]
        value = _counter(raw, key, maximum + 1e-6) if maximum is not None else None
        if value is not None and value >= 1:
            tags.append(label)
    if yakuman_victim(raw, games):
        tags.append("役满受害者")
    return tags


def trait_tags(
    raw, reference, hands, *, win=None, call=None, riichi=None, games=(), include_style=True
):
    """Return tactics/results, luck and achievements from an already matched sample."""
    result = {"style": [], "luck": [], "achievement": []}
    counts = exposures(raw, hands, win=win, call=call, riichi=riichi)
    result["achievement"] = achievement_tags(raw, games, counts)
    if not include_style:
        return result
    references = reference.get("traits", {})
    matched = {}
    for key, definition in RATE_TRAITS.items():
        basis, minimum, _, _, category, high_name, low_name = definition
        ref, value, trials = references.get(key), rate(raw.get(key)), counts[basis]
        if ref is None or value is None or trials is None:
            continue
        if key == "立直多面" and (
            not games or any(game.started_at < MULTISIDED_SINCE for game in games)
        ):
            continue
        if key.startswith("立直后"):
            outcomes = [rate(raw.get(k)) for k in ("立直后和牌率", "立直后放铳率", "立直后流局率")]
            if sum(v for v in outcomes if v is not None) > 1 + 1e-6:
                continue
            if (
                key == "立直后和牌率"
                and "立直和了" in raw
                and (
                    counts["riichi_wins"] is None
                    or abs(value * trials - counts["riichi_wins"]) > 1e-6
                )
            ):
                continue
        if key in {"先制率", "追立率"}:
            other = rate(raw.get("追立率" if key == "先制率" else "先制率"))
            if other is not None and abs(value + other - 1) > 1e-6:
                continue
        trials = math.floor(trials + 1e-6)
        if trials < minimum:
            continue
        interval = wilson_interval(value, trials)
        if high_name and value >= ref["high"] and interval[0] > ref["mean"]:
            matched[key] = high_name
        elif low_name and value <= ref["low"] and interval[1] < ref["mean"]:
            matched[key] = low_name
        if key in matched:
            result[category].append(matched[key])

    # Successful open hands without above-reference deal-in results; a low
    # conditional deal-in rate alone is insufficient to call them productive.
    post_deal, post_win = rate(raw.get("副露后放铳率")), rate(raw.get("副露后和牌率"))
    deal_ref, win_ref = references.get("副露后放铳率"), references.get("副露后和牌率")
    if "鸣牌善战" in result["style"] and (
        post_deal is None or deal_ref is None or post_deal > deal_ref["mean"]
    ):
        result["style"].remove("鸣牌善战")
    if "鸣牌慎行" in result["style"] and (
        post_win is None
        or win_ref is None
        or post_win < win_ref["mean"] - 0.03
        or "鸣牌善战" in result["style"]
    ):
        result["style"].remove("鸣牌慎行")

    # This is a combination of two observed rates, not a fabricated conditional
    # chase-deal-in rate (the aggregate API does not provide that joint count).
    if "追立斗士" in result["style"] and "立直撞铳" in result["style"]:
        result["style"].remove("追立斗士")
        result["style"].remove("立直撞铳")
        result["style"].append("追立冒险家")

    efficiencies = efficiency_metrics(raw)
    for key, definition in AVERAGE_TRAITS.items():
        basis, minimum, _, _, category, high_name, low_name = definition
        ref, trials = references.get(key), counts[basis]
        value = (
            efficiencies.get(key)
            if key in {"打点效率", "铳点损失", "净打点效率"}
            else number(raw.get(key))
        )
        if (
            ref is None
            or value is None
            or (key != "净打点效率" and value < 0)
            or trials is None
            or math.floor(trials + 1e-6) < minimum
        ):
            continue
        if key == "立直巡目" and not 0 < value <= 30:
            continue
        if key == "平均起手向听" and value > 13:
            continue
        if key in {"打点效率", "净打点效率"} and (
            counts["wins"] is None or math.floor(counts["wins"] + 1e-6) < 30
        ):
            continue
        if key in {"铳点损失", "净打点效率"} and (
            counts["deals"] is None or math.floor(counts["deals"] + 1e-6) < 30
        ):
            continue
        if high_name and value >= ref["high"]:
            result[category].append(high_name)
        elif low_name and value <= ref["low"]:
            result[category].append(low_name)

    return result


def select_tags(existing, extra, maximum):
    """Up to ten total, three luck and three achievements, with stable priorities."""
    if maximum <= 0:
        return []
    achievements = list(dict.fromkeys(extra["achievement"]))[: min(3, maximum)]
    luck = list(dict.fromkeys(extra["luck"]))[: min(3, maximum - len(achievements))]
    slots = maximum - len(luck) - len(achievements)
    styles = list(dict.fromkeys(existing[:2] + extra["style"] + existing[2:]))
    return achievements + styles[: max(0, slots)] + luck
