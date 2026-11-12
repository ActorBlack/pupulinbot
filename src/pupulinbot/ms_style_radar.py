"""Descriptive four-player scores centered on the player's current grade."""

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from .ms_sample import number
from .ms_trait_tags import rate


class Metric(NamedTuple):
    key: str
    label: str
    kind: str
    signed: bool = False


class Axis(NamedTuple):
    name: str
    direction: int
    metric: Metric
    inputs: tuple[Metric, ...]


AXES = (
    Axis(
        "进攻",
        1,
        Metric("进攻触达率", "和牌或流局听牌", "%"),
        (
            Metric("和牌率", "和牌率", "%"),
            Metric("流局率", "流局率", "%"),
            Metric("流听率", "流局听牌率", "%"),
        ),
    ),
    Axis(
        "防守",
        -1,
        Metric("铳点损失", "放铳损失 / 局", "点"),
        (Metric("放铳率", "放铳率", "%"), Metric("平均铳点", "平均铳点", "点")),
    ),
    Axis(
        "副露",
        1,
        Metric("副露净结果率", "副露和铳净贡献", "%", signed=True),
        (
            Metric("副露率", "副露率", "%"),
            Metric("副露后和牌率", "副露后和牌", "%"),
            Metric("副露后放铳率", "副露后放铳", "%"),
        ),
    ),
    Axis(
        "立直",
        1,
        Metric("立直局均收支", "立直收支 / 总局", "点", signed=True),
        (
            Metric("立直率", "立直率", "%"),
            Metric("立直收支", "立直收支 / 立直局", "点", signed=True),
        ),
    ),
    Axis(
        "速度",
        -1,
        Metric("和立牌平均巡目", "和牌 / 立直事件巡目", "巡"),
        (Metric("和了巡数", "和牌巡数", "巡"), Metric("立直巡目", "立直巡目", "巡")),
    ),
    Axis(
        "打点",
        1,
        Metric("净打点效率", "和铳净得点 / 局", "点", signed=True),
        (
            Metric("平均打点", "平均打点", "点"),
            Metric("打点效率", "和牌得点 / 局", "点"),
            Metric("铳点损失", "放铳损失 / 局", "点"),
        ),
    ),
)


class RadarReference(NamedTuple):
    metrics: dict[str, float]
    rank_rates: list[float]
    scales: dict[str, float]
    hands: int
    min_hands: int


def metric_value(raw, metric):
    value = number(raw.get(metric.key))
    if value is None or (value < 0 and not metric.signed):
        return None
    if metric.kind == "%" and abs(value) > 1:
        return None
    return value


def contribution(frequency, conditional_value):
    if frequency == 0:
        return 0.0
    if frequency is None or conditional_value is None:
        return None
    return frequency * conditional_value


def composite_metrics(raw):
    """Derive each axis in its natural units from the same set of hand statistics."""
    win, deal, call, riichi = (rate(raw.get(k)) for k in ("和牌率", "放铳率", "副露率", "立直率"))

    def nonnegative(key):
        value = number(raw.get(key))
        return value if value is not None and value >= 0 else None

    drawn_tenpai = contribution(rate(raw.get("流局率")), rate(raw.get("流听率")))
    attack = win + drawn_tenpai if win is not None and drawn_tenpai is not None else None
    win_points = contribution(win, nonnegative("平均打点"))
    loss = contribution(deal, nonnegative("平均铳点"))
    call_win = contribution(call, rate(raw.get("副露后和牌率")))
    call_deal = contribution(call, rate(raw.get("副露后放铳率")))
    call_net = None
    if call is not None and call_win is not None and call_deal is not None:
        # Winning and dealing in are mutually exclusive for the same player.
        if call_win + call_deal <= call + 1e-9:
            call_net = call_win - call_deal
    riichi_net = contribution(riichi, number(raw.get("立直收支")))
    win_turns = contribution(win, nonnegative("和了巡数"))
    riichi_turns = contribution(riichi, nonnegative("立直巡目"))
    speed = None
    if (
        win is not None
        and riichi is not None
        and win_turns is not None
        and riichi_turns is not None
    ):
        if win + riichi:
            speed = (win_turns + riichi_turns) / (win + riichi)
    return {
        "进攻触达率": attack,
        "铳点损失": loss,
        "副露净结果率": call_net,
        "副露和牌贡献率": call_win,
        "副露放铳贡献率": call_deal,
        "立直局均收支": riichi_net,
        "和立牌平均巡目": speed,
        "打点效率": win_points,
        "净打点效率": win_points - loss if win_points is not None and loss is not None else None,
    }


@lru_cache(maxsize=1)
def _reference_rooms():
    path = Path(__file__).with_name("assets") / "ms_style_radar_reference.json"
    return json.loads(path.read_text(encoding="utf-8"))["rooms"]


def comparison_reference(room, rank):
    """Select the exact room and four-player grade exposed by the source."""
    if rank is None:
        return None
    room_data = _reference_rooms().get(room)
    if room_data is None:
        return None
    level = str(10000 + rank["major"] * 100 + rank["star"])
    cohort = room_data["levels"].get(level)
    if cohort is None:
        return None
    return RadarReference(
        cohort["metrics"],
        cohort["rank_rates"],
        room_data["scales"],
        cohort["hands"],
        room_data["min_hands"],
    )


def axis_score(axis, actual, reference, hands):
    """Map a standardized difference to 0–100 around the same-grade mean."""
    if reference is None or hands is None or min(hands, reference.hands) < reference.min_hands:
        return None
    value = metric_value(actual, axis.metric)
    mean = metric_value(reference.metrics, axis.metric)
    key = axis.metric.key
    scale = reference.scales.get(key)
    if value is None or mean is None or scale is None:
        return None

    variance = 0.0
    if key == "进攻触达率":
        variance = mean * (1 - mean)
    elif key == "副露净结果率":
        # A hand contributes +1 for an open win, -1 for an open deal-in, or zero.
        p_win = reference.metrics["副露和牌贡献率"]
        p_deal = reference.metrics["副露放铳贡献率"]
        variance = p_win + p_deal - mean**2
    elif key in {"铳点损失", "立直局均收支"}:
        # Between-event variance. Dispersion within the event is unavailable.
        frequency = reference.metrics["放铳率" if key == "铳点损失" else "立直率"]
        if frequency:
            variance = mean**2 * (1 - frequency) / frequency
    elif key == "净打点效率":
        # Win, deal-in and other hands are disjoint. Each product is per hand.
        second_moment = 0.0
        for product, rate_key in (("打点效率", "和牌率"), ("铳点损失", "放铳率")):
            frequency = reference.metrics[rate_key]
            if frequency:
                second_moment += reference.metrics[product] ** 2 / frequency
        variance = second_moment - mean**2
    elif key == "和立牌平均巡目":
        win, riichi = (rate(actual.get(field)) for field in ("和牌率", "立直率"))
        if win is None or riichi is None:
            return None
        events = hands * (win + riichi)
        typical = reference.min_hands * (reference.metrics["和牌率"] + reference.metrics["立直率"])
        if round(events) < math.ceil(typical):
            return None

    noise = max(0.0, variance) * (1 / hands + 1 / reference.hands)
    weight = scale**2 / (scale**2 + noise)
    deviation = axis.direction * (value - mean) / scale * weight
    return 50 * (1 + math.erf(deviation / math.sqrt(2)))
