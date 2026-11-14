"""Additional four-player traits with metric-specific event denominators.

Definitions follow amae-koromo's playerDetails.tsx. References reuse frozen
room aggregates; evaluating a sample never makes an additional API request.
"""

import math
from datetime import UTC, datetime
from functools import lru_cache

from .majsoul_rounds import yaku_names
from .ms_sample import number

# key: (denominator, minimum events, high/low offsets, category, high/low names)
RATE_TRAITS = {
    "里宝率": ("riichi_wins", 30, 0.08, 0.08, "luck", "里宝超人", "里宝绝缘体"),
    "一发率": ("riichi_wins", 30, 0.06, 0.06, "luck", "一发超人", "一发绝缘体"),
    "自摸率": ("wins", 50, 0.07, None, "style", "自摸超人", None),
    "先制率": ("riichis", 50, 0.05, None, "style", "先发制人", None),
    "追立率": ("riichis", 50, 0.05, None, "style", "后发制人", None),
    "副露后和牌率": ("calls", 75, 0.05, None, "style", "副露进攻", None),
    "副露后放铳率": ("calls", 75, None, 0.03, "style", None, "副露防守"),
    "流听率": ("draws", 50, 0.08, None, "style", "罚符小偷", None),
    "立直后放铳率": ("riichis", 50, 0.03, 0.03, "style", "全自动放铳", "全自动防守"),
    "立直后和牌率": ("riichis", 50, 0.05, 0.05, "style", "立直收割机", "场供贡献者"),
    "立直后流局率": ("riichis", 50, 0.04, None, "style", "立直流局王", None),
    "立直多面": ("riichis", 50, 0.05, 0.05, "style", "好形爱好者", "愚形爱好者"),
}
AVERAGE_TRAITS = {
    "立直巡目": ("riichis", 50, None, 0.5, "style", None, "神速立直"),
    "平均起手向听": ("hands", 300, 0.1, 0.1, "luck", "白手起家", "生于罗马"),
    "打点效率": ("hands", 400, 200, None, "style", "火力全开", None),
    "铳点损失": ("hands", 400, 150, None, "style", "散财童子", None),
    "净打点效率": ("hands", 400, 250, 250, "style", "高效打点", "低效打点"),
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


def is_pupulin_profile(stats):
    """User-defined absolute profile: all six metric limits and at least 50 games.

    Use the API's displayed net efficiency when supplied, including its integer
    rounding. Only compute the published formula when that field is absent.
    No additional hand/event minimum or cohort confidence test applies here.
    """
    games = number(getattr(stats, "games", None))
    if games is None or not games.is_integer() or games < 50:
        return False
    for attribute, lower, upper in (
        ("call_rate", 0.25, 0.27),
        ("riichi_rate", 0.185, 0.205),
        ("deal_in_rate", 0.095, 0.115),
        ("win_rate", 0.215, 0.24),
    ):
        value = rate(getattr(stats, attribute, None))
        if value is None or not lower <= value <= upper:
            return False
    raw = getattr(stats, "raw", None)
    if not isinstance(raw, dict):
        return False
    points = number(raw.get("平均打点"))
    if points is None or points < 6600:
        return False
    if "净打点效率" in raw:
        efficiency = number(raw["净打点效率"])
    else:
        source = {**raw, "和牌率": rate(stats.win_rate), "放铳率": rate(stats.deal_in_rate)}
        efficiency = efficiency_metrics(source).get("净打点效率")
    return efficiency is not None and efficiency >= 800


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


@lru_cache(maxsize=1)
def yakuman_tag_groups():
    names = yaku_names()
    wins = frozenset(names[str(key)] for key in range(35, 51))
    losses = frozenset(_victim_tag(name) for name in wins)
    return wins, losses


def _victim_tag(name):
    return ("国士" if name == "国士无双" else name) + "受害者"


def is_named_yakuman_tag(tag):
    wins, losses = yakuman_tag_groups()
    return tag in wins or tag in losses


def yakuman_names(fans):
    """Identify named limit hands from Paipuya's role-labelled fan details."""
    if not isinstance(fans, list) or not fans:
        return ()
    names = []
    allowed, _ = yakuman_tag_groups()
    for fan in fans:
        if not isinstance(fan, dict):
            return ()
        count, limit = number(fan.get("count")), number(fan.get("役满"))
        if any(v is None or not v.is_integer() or not 0 <= v <= 100 for v in (count, limit)):
            return ()
        label = fan.get("label")
        if limit and isinstance(label, str) and label in allowed:
            names.append(label)
    return tuple(dict.fromkeys(names))


def yakuman_victim(raw, games):
    """Return the roles in a real large deal-in matched to this exact sample."""
    loss = raw.get("最近大铳")
    if not isinstance(loss, dict) or not isinstance(loss.get("id"), str):
        return ()
    game = next((game for game in games if game.uuid == loss["id"]), None)
    timestamp = number(loss.get("start_time"))
    if game is None or timestamp is None or abs(timestamp - game.started_at.timestamp()) > 1:
        return ()
    return yakuman_names(loss.get("fans"))


def achievement_tags(raw, games, counts=None):
    tags = []
    if counts is not None:
        wins = counts["wins"]
        count = _counter(raw, "役满", wins + 1e-6) if wins is not None else None
        names = raw.get("_yakuman_win_names")
        if count and isinstance(names, (list, tuple)):
            allowed, _ = yakuman_tag_groups()
            named_wins = tuple(
                dict.fromkeys(name for name in names if isinstance(name, str) and name in allowed)
            )
            if named_wins:
                tags.append(named_wins)
        hands = counts["hands"]
        if hands is not None and _counter(raw, "流满", hands + 1e-6):
            tags.append("流局满贯")
    losses = yakuman_victim(raw, games)
    if losses:
        tags.append(tuple(_victim_tag(name) for name in losses))
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
    if "副露进攻" in result["style"] and (
        post_deal is None or deal_ref is None or post_deal > deal_ref["mean"]
    ):
        result["style"].remove("副露进攻")
    if "副露防守" in result["style"] and (
        post_win is None or win_ref is None or post_win < win_ref["mean"] - 0.03
    ):
        result["style"].remove("副露防守")
    if "副露进攻" in result["style"] and "副露防守" in result["style"]:
        attack_position = result["style"].index("副露进攻")
        result["style"][attack_position] = "进退自如"
        result["style"].remove("副露防守")

    # This is a combination of two observed rates, not a fabricated conditional
    # chase-deal-in rate (the aggregate API does not provide that joint count).
    if "后发制人" in result["style"] and "全自动放铳" in result["style"]:
        result["style"].remove("后发制人")
        result["style"].remove("全自动放铳")
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


TAG_COMBINATIONS = (
    ("一击必杀", "高打点", "重炮手"),
    ("高效打点", "局收支重视", "精打细算"),
    ("略胜一筹", "棋差一着", "毫厘之间"),
    ("深度门清", "默听流", "静默猎手"),
    ("争一流", "避四流", "上分糕手"),
)
COMBINATION_TAGS = frozenset(
    ("进退自如", "追立冒险家", *(combined for _, _, combined in TAG_COMBINATIONS))
)


def select_tags(existing, extra, maximum, *, easter_egg=None):
    """Reserve display slots for achievements, luck and an optional easter egg."""
    if maximum <= 0:
        return []
    achievements = list(dict.fromkeys(extra["achievement"]))[: min(3, maximum)]
    luck = list(dict.fromkeys(extra["luck"]))[: min(3, maximum - len(achievements))]
    slots = maximum - len(luck) - len(achievements)
    eggs = [easter_egg] if easter_egg and slots > 0 else []
    base_priority = 2 - len(eggs)
    styles = list(
        dict.fromkeys(existing[:base_priority] + extra["style"] + existing[base_priority:])
    )
    for left, right, combined in TAG_COMBINATIONS:
        if left in styles and right in styles:
            first, second = sorted((styles.index(left), styles.index(right)))
            styles[first] = combined
            del styles[second]
    # A group reserves one slot even when several distinct yaku are visible.
    visible_achievements = [
        name for group in achievements for name in ((group,) if isinstance(group, str) else group)
    ]
    return visible_achievements + eggs + styles[: slots - len(eggs)] + luck
