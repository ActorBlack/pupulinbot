"""Ranked-room tags calibrated against actual per-room aggregate data.

Frozen, auditable cohort means; effect-size and sample checks, never invented
cohort percentiles. These descriptive rules are not a playing-strength model.
"""

import json
import math
from functools import lru_cache
from pathlib import Path

from .ms_sample import number
from .ms_tag_reference import build_reference
from .ms_trait_tags import (
    is_pupulin_profile,
    select_tags,
    trait_tags,
    wilson_interval,
    yakuman_victim,
)

MIN_GAMES = 20
RANK_MIN_GAMES = 50
MIN_HANDS = 100
MIN_WINS = 30
MAX_TAGS = 10


@lru_cache(maxsize=1)
def _references():
    return json.loads((Path(__file__).parent / "assets/ms_tag_baseline.json").read_text())["modes"]


def reference_for(mode):
    mode = str(mode)
    reference = _references().get(mode)
    if reference:
        return reference
    if mode != "16":
        return None
    # Only Throne south may fall back, and only to the actual Jade-south Saint 3 cohort.
    saint3 = _references().get("12", {}).get("levels", {}).get("10503")
    try:
        return build_reference(
            {"10503": saint3}, "王座南", source_mode="12", kind="saint3_fallback"
        )
    except ValueError:
        return None


def _rate(value):
    value = number(value)
    return value if value is not None and 0 <= value <= 1 else None


def _at_least(value, bound):
    return value is not None and value >= bound


def _at_most(value, bound):
    return value is not None and value <= bound


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
    if len(previous) < 30 or len(recent) != 20:
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
    # Supported four-player ranked rooms all start at 25,000 points.
    return sum(value - 25000 for value in scores) / hands


def supplemental_tags(stats, games, account_id, *, mode=4):
    """Classify one matched room sample; never substitute a player's lifetime stats."""
    if mode != 4 or stats is None:
        return []
    finished = sorted(
        (g for g in games if g.ended_at and g.seat(account_id)), key=lambda g: g.started_at
    )
    count = len(finished)
    if (
        not count
        or number(getattr(stats, "games", None)) != count
        or len({g.uuid for g in finished}) != count
        or len({g.mode for g in finished}) != 1
    ):
        return []
    pupulin = str(finished[0].mode) in {"8", "9", "11", "12", "15", "16"} and is_pupulin_profile(
        stats
    )
    reference = reference_for(finished[0].mode)
    if reference is None:
        return ["普普林"] if pupulin else []
    mean, cut = reference["means"], reference["thresholds"]
    raw = getattr(stats, "raw", {}) or {}
    hands = number(raw.get("_hand_count"))
    known_hands = hands is not None and hands.is_integer() and hands >= count
    reliable_hands = known_hands and hands >= MIN_HANDS and count >= MIN_GAMES
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
    if ranks and count >= RANK_MIN_GAMES:
        form = _recent_form(ranks, reference)
        if form:
            tags.append(form)
    if reliable_hands and hands >= 300:
        income = sample_round_income(raw, finished, account_id, hands)
        if _at_least(income, cut["income_high"]):
            tags.append("高局收支")
    if ranks and count >= RANK_MIN_GAMES:
        first, last = ranks.count(1) / count, ranks.count(4) / count
        top2 = sum(r <= 2 for r in ranks) / count
        base_first, _, _, base_last = mean["rank_rates"]
        if (
            last <= cut["fourth_low"]
            and top2 >= sum(mean["rank_rates"][:2])
            and _below(last, count, base_last)
        ):
            tags.append("避四流")
        # High first-place and low fourth-place rates can coexist. Neither trait
        # implies that the player must also have poor results at the other end.
        if first >= cut["first_high"] and _above(first, count, base_first):
            tags.append("争一流")
    if reliable_hands:
        if (
            _at_least(win, cut["win_high"])
            and _at_least(deal, cut["deal_high"])
            and _above(win, hands, mean["和牌率"])
            and _above(deal, hands, mean["放铳率"])
        ):
            tags.append("高和高铳")
        elif (
            _at_most(deal, cut["deal_low"])
            and _at_least(win, cut["win_floor"])
            and _below(deal, hands, mean["放铳率"])
        ):
            tags.append("稳健防守")
        wins = math.floor(hands * win + 1e-6) if win is not None else 0
        if wins >= MIN_WINS:
            if _at_least(points, cut["points_high"]):
                tags.append("高打点")
            elif (
                points is not None
                and 0 < points <= cut["points_low"]
                and turn is not None
                and 0 < turn <= cut["turn_low"]
                and _at_least(win, cut["win_high"])
                and _above(win, hands, mean["和牌率"])
            ):
                tags.append("速攻流")
            dama = _rate(raw.get("默听率"))
            if (
                _at_least(dama, cut["dama_high"])
                and _at_most(riichi, mean["立直率"])
                and _above(dama, wins, mean["默听率"])
            ):
                tags.append("默听流")
        if _at_least(riichi, cut["riichi_high"]) and _above(riichi, hands, mean["立直率"]):
            tags.append("立直流")
    if known_hands:
        extra = trait_tags(
            raw,
            reference,
            hands,
            win=win,
            call=call,
            riichi=riichi,
            games=finished,
            include_style=reliable_hands,
        )
    else:
        extra = {"style": [], "luck": [], "achievement": []}
        if yakuman_victim(raw, finished):
            extra["achievement"].append("役满受害者")
    if pupulin:
        tags.insert(0, "普普林")
    return select_tags(tags, extra, MAX_TAGS)
