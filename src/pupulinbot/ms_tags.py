"""Jade-room tags calibrated against actual 10402–10503 aggregate data.

Frozen, auditable cohort means; effect-size and sample checks, never invented
cohort percentiles. These descriptive rules are not a playing-strength model.
"""

import json
import math
from functools import lru_cache
from pathlib import Path

from .ms_sample import number

MIN_GAMES = 100
MIN_HANDS = 600
MAX_TAGS = 4


@lru_cache(maxsize=1)
def _references():
    return json.loads((Path(__file__).parent / "assets/ms_tag_baseline.json").read_text())["modes"]


def reference_for(mode):
    return _references().get(str(mode))


def _rate(value):
    value = number(value)
    return value if value is not None and 0 <= value <= 1 else None


def _at_least(value, bound):
    return value is not None and value >= bound


def _at_most(value, bound):
    return value is not None and value <= bound


def wilson_interval(rate, trials):
    """95% binomial approximation, used as a conservative rate sampling guard."""
    rate, trials = _rate(rate), number(trials)
    if rate is None or trials is None or trials <= 0 or not trials.is_integer():
        return None
    z2 = 1.96**2
    divisor = 1 + z2 / trials
    center = (rate + z2 / (2 * trials)) / divisor
    radius = 1.96 * math.sqrt(rate * (1 - rate) / trials + z2 / (4 * trials**2)) / divisor
    return max(0, center - radius), min(1, center + radius)


def _above(rate, trials, mean):
    interval = wilson_interval(rate, trials)
    return interval is not None and interval[0] > mean


def _below(rate, trials, mean):
    interval = wilson_interval(rate, trials)
    return interval is not None and interval[1] < mean


def _placements(games, account_id):
    ranks = [number(g.seat(account_id).get("rank")) for g in games]
    return ranks if all(r in {1, 2, 3, 4} for r in ranks) else None


def _recent_form(ranks, reference):
    previous, recent = ranks[:-20], ranks[-20:]
    if len(previous) < 80 or len(recent) != 20:
        return None

    def mean_variance(values):
        mean = sum(values) / len(values)
        return mean, sum((value - mean) ** 2 for value in values) / (len(values) - 1)

    before, variance_before = mean_variance(previous)
    now, variance_now = mean_variance(recent)
    error = math.sqrt(variance_before / len(previous) + variance_now / 20)
    if abs(before - now) < max(0.30, 1.96 * error):
        return None
    baseline = reference["means"]["avg_rank"]
    if (
        now <= baseline - 0.20
        and now < before
        and sum(r <= 2 for r in recent) >= 13
        and recent.count(4) <= 3
    ):
        return "好调中"
    if now >= baseline + 0.20 and now > before and recent.count(4) >= 7:
        return "恶调中"
    return None


def sample_round_income(raw, games, account_id, hands):
    """Exact net point change per hand, not win/deal efficiency or grading PT."""
    value = number(raw.get("局收支"))
    if value is not None:
        return value
    scores = [number(g.seat(account_id).get("score")) for g in games]
    if (
        hands is None
        or hands <= 0
        or not all(v is not None and v.is_integer() and abs(v) <= 10_000_000 for v in scores)
    ):
        return None
    # Jade east and south are both four-player, 25,000 starting points.
    return sum(value - 25000 for value in scores) / hands


def supplemental_tags(stats, games, account_id, *, mode=4):
    """Classify one matched jade-room sample; no network or lifetime substitution."""
    if mode != 4 or stats is None:
        return []
    finished = sorted(
        (g for g in games if g.ended_at and g.seat(account_id)), key=lambda g: g.started_at
    )
    count = len(finished)
    if (
        count < MIN_GAMES
        or number(getattr(stats, "games", None)) != count
        or len({g.uuid for g in finished}) != count
        or len({g.mode for g in finished}) != 1
    ):
        return []
    reference = reference_for(finished[0].mode)
    if reference is None:
        return []
    mean, cut = reference["means"], reference["thresholds"]
    raw = getattr(stats, "raw", {}) or {}
    hands = number(raw.get("_hand_count"))
    reliable_hands = hands is not None and hands.is_integer() and hands >= max(MIN_HANDS, count)
    win, deal, call, riichi = (
        _rate(getattr(stats, key, None))
        for key in ("win_rate", "deal_in_rate", "call_rate", "riichi_rate")
    )
    points, turn = (number(raw.get(key)) for key in ("平均打点", "和了巡数"))
    tags = []
    if reliable_hands:
        if _at_least(call, cut["call_high"]) and _above(call, hands, mean["副露率"]):
            tags.append("副露流")
        elif _at_most(call, cut["call_low"]) and _below(call, hands, mean["副露率"]):
            tags.append("门清流")
    ranks = _placements(finished, account_id)
    if ranks:
        form = _recent_form(ranks, reference)
        if form:
            tags.append(form)
    if reliable_hands:
        income = sample_round_income(raw, finished, account_id, hands)
        if _at_least(income, cut["income_high"]):
            tags.append("高局收支")
    if ranks:
        first, last = ranks.count(1) / count, ranks.count(4) / count
        top2 = sum(r <= 2 for r in ranks) / count
        base_first, _, _, base_last = mean["rank_rates"]
        if (
            last <= cut["fourth_low"]
            and top2 >= sum(mean["rank_rates"][:2])
            and _below(last, count, base_last)
        ):
            tags.append("避四派")
        elif first >= cut["first_high"] and last >= base_last and _above(first, count, base_first):
            tags.append("争一派")
    if reliable_hands:
        if (
            _at_least(win, cut["win_attack"])
            and _at_least(deal, cut["deal_high"])
            and _above(deal, hands, mean["放铳率"])
        ):
            tags.append("狂战士")
        elif (
            _at_most(deal, cut["deal_low"])
            and _at_least(win, cut["win_floor"])
            and _below(deal, hands, mean["放铳率"])
        ):
            tags.append("稳健防守")
        wins = round(hands * win) if win is not None else 0
        if wins >= 100:
            if _at_least(points, cut["points_high"]) and _at_least(win, cut["win_floor"]):
                tags.append("高打点")
            elif (
                points is not None
                and 0 < points <= cut["points_low"]
                and turn is not None
                and 0 < turn <= cut["turn_low"]
                and _at_least(win, cut["win_high"])
                and _above(win, hands, mean["和牌率"])
            ):
                tags.append("速攻派")
            dama = _rate(raw.get("默听率"))
            if (
                _at_least(dama, cut["dama_high"])
                and _at_most(riichi, mean["立直率"])
                and _above(dama, wins, mean["默听率"])
            ):
                tags.append("默听派")
        if _at_least(riichi, cut["riichi_high"]) and _above(riichi, hands, mean["立直率"]):
            tags.append("立直派")
    return tags[:MAX_TAGS]
