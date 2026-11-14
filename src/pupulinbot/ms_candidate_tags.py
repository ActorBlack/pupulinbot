"""Four-player tags approved from the room-sample backtest."""

import json
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from itertools import accumulate, groupby, pairwise
from math import floor
from pathlib import Path

from .ms_level import seat_delta
from .ms_sample import number
from .ms_trait_tags import exposures, rate

# Each condition is (metric, minimum, maximum). A mean-relative condition uses
# the corresponding weighted reference metric from the room baseline.
RULES = {
    "燕返": (("riichis", 100, None), ("宣言铳率差", 0.02, None), ("立直后非瞬间放铳率差", None, 0)),
    "立直提款机": (("riichis", 100, None), ("立直收支差", 650, None), ("立直后放铳率差", None, 0)),
    "立直引战": (("riichis", 100, None), ("被追率差", 0.05, None), ("先制率差", 0, None)),
    "副露拉锯战": (
        ("calls", 100, None),
        ("副露率差", 0.04, None),
        ("副露后流局率差", 0.05, None),
        ("副露后放铳率差", None, 0),
    ),
    "日常踩雷": (
        ("deals", 60, None),
        ("classified_deals", 60, None),
        ("放铳率差", None, 0),
        ("默听放铳占比差", 0.08, None),
    ),
    "无声快刀": (
        ("wins", 50, None),
        ("默听率差", 0.05, None),
        ("和了巡数差", None, -0.3),
        ("平均打点差", 0, None),
    ),
    "过山车": (("games", 80, None), ("first_rate", 0.30, None), ("fourth_rate", 0.30, None)),
    "中庸之道": (
        ("games", 80, None),
        ("middle_rate", 0.65, None),
        ("second_rate", 0.20, None),
        ("third_rate", 0.20, None),
    ),
    "略胜一筹": (("games", 50, None), ("close_first", 3, None)),
    "棋差一着": (("games", 50, None), ("close_second", 3, None)),
    "签到游戏": (("games", 50, None), ("pt_range", 600, None), ("pt_net", -100, 100)),
    "精选立直": (("riichis", 100, None), ("立直率差", None, -0.03), ("立直好型2差", 0.08, None)),
    "振听达人": (("riichis", 150, None), ("振听立直率", 0.03, None), ("振听立直率差", 0.015, None)),
    "爱撞大运": (("deals", 40, None), ("放铳率差", None, -0.02), ("平均铳点差", 800, None)),
    "深度门清": (
        ("wins", 60, None),
        ("副露率差", None, -0.03),
        ("默听率", 0.20, None),
        ("riichi_win_share", 0.40, None),
    ),
    "悬赏猎人": (("riichi_wins", 50, None), ("里宝率差", 0.08, None), ("一发率差", 0.06, None)),
    "一骑绝尘": (
        ("games", 50, None),
        ("firsts", 15, None),
        ("runaway", 5, None),
        ("runaway_share", 0.40, None),
    ),
    "越挫越勇": (
        ("games", 50, None),
        ("after_fourth", 10, None),
        ("rebound", 4, None),
        ("rebound_rate", 0.50, None),
    ),
    "悬崖勒马": (("games", 50, None), ("close_third", 3, None)),
    "鏖战": (("dealer_streak", 6, None),),
    "一击必杀": (
        ("riichi_wins", 40, None),
        ("立直收入差", 700, None),
        ("立直后和牌率差", None, 0),
    ),
    "以小搏大": (("deals", 50, None), ("放铳率差", 0.01, None), ("平均铳点差", None, -650)),
    "被炸仙人": (("hands", 800, None), ("被炸率", 0.14, None), ("平均被炸点数", 11600, None)),
    "零点逃生": (("games", 50, None), ("zero_escape", 2, None)),
    "连败之道": (("games", 50, None), ("fourth_streak", 5, None)),
    "连胜之道": (("games", 50, None), ("first_streak", 5, None)),
    "顺位集邮": (("games", 80, None), ("rank_album", 0.16, None)),
    "马拉松": (("games", 50, None), ("long_games", 8, None), ("long_top2", 0.70, None)),
    "体面结局": (("games", 50, None), ("fourths", 10, None), ("healthy_fourth", 0.35, None)),
}

RATE_BASES = {
    "放铳率": "hands",
    "副露率": "hands",
    "立直率": "hands",
    "默听率": "wins",
    "立直后放铳率": "riichis",
    "立直后非瞬间放铳率": "riichis",
    "被追率": "riichis",
    "先制率": "riichis",
    "副露后流局率": "calls",
    "副露后放铳率": "calls",
    "立直好型2": "riichis",
    "振听立直率": "riichis",
    "里宝率": "riichi_wins",
    "一发率": "riichi_wins",
    "立直后和牌率": "riichis",
}
AVERAGE_BASES = {
    "立直收支": "riichis",
    "和了巡数": "wins",
    "平均打点": "wins",
    "平均铳点": "deals",
    "立直收入": "riichi_wins",
}
BASES = {
    **RATE_BASES,
    **AVERAGE_BASES,
    "宣言铳率": "riichis",
    "默听放铳占比": "classified_deals",
}
DEAL_TYPES = ("放铳至立直", "放铳至副露", "放铳至默听")
GOOD_SHAPE_SINCE = datetime(2021, 11, 7, tzinfo=UTC)


def hand_features(raw, hands, *, win=None, deal=None, call=None, riichi=None):
    counts = exposures(raw, hands, win=win, call=call, riichi=riichi)
    if deal is not None:
        counts["deals"] = hands * deal
    metrics = {key: rate(raw.get(key)) for key in RATE_BASES}
    metrics.update((key, number(raw.get(key))) for key in AVERAGE_BASES)
    for key, value in (("放铳率", deal), ("副露率", call), ("立直率", riichi)):
        if value is not None:
            metrics[key] = value
    metrics["被炸率"] = rate(raw.get("被炸率"))
    metrics["平均被炸点数"] = number(raw.get("平均被炸点数"))
    total, late = metrics["立直后放铳率"], metrics["立直后非瞬间放铳率"]
    if total is not None and late is not None and late <= total:
        metrics["宣言铳率"] = total - late
    parts = [number(raw.get(key)) for key in DEAL_TYPES]
    if all(p is not None and p.is_integer() and p >= 0 for p in parts) and 0 < sum(parts) <= hands:
        counts["classified_deals"] = sum(parts)
        metrics["默听放铳占比"] = parts[-1] / sum(parts)
    if counts["wins"] and counts["riichi_wins"] is not None:
        metrics["riichi_win_share"] = counts["riichi_wins"] / counts["wins"]
    return metrics, counts


def build_candidate_means(levels):
    cohorts = [hand_features(row["extended"], row["extended"]["count"]) for row in levels.values()]
    means = {}
    for key, basis in BASES.items():
        pairs = [(metrics.get(key), counts.get(basis)) for metrics, counts in cohorts]
        if any(weight is None or (weight > 0 and value is None) for value, weight in pairs):
            continue
        weighted = [(value, weight) for value, weight in pairs if weight > 0]
        if weighted:
            means[key] = sum(value * weight for value, weight in weighted) / sum(
                weight for _, weight in weighted
            )
    return means


@lru_cache(maxsize=1)
def candidate_means():
    path = Path(__file__).with_name("assets") / "ms_candidate_baseline.json"
    return json.loads(path.read_text())["modes"]


def _score_features(games, ranks, account_id):
    counts = Counter()
    complete = True
    for game, rank in zip(games, ranks, strict=True):
        scores = [number(player.get("score")) for player in game.players]
        score = number(game.seat(account_id).get("score"))
        if len(scores) != 4 or any(value is None for value in scores):
            complete = False
            continue
        first, second, third, fourth = sorted(scores, reverse=True)
        if score != (first, second, third, fourth)[rank - 1]:
            complete = False
            continue
        if rank == 1 and 0 < first - second <= 1000:
            counts["close_first"] += 1
        if rank == 2 and 0 < first - second <= 1000:
            counts["close_second"] += 1
        if rank == 3 and 0 < third - fourth <= 1000:
            counts["close_third"] += 1
        if rank == 1 and first - second >= 20000:
            counts["runaway"] += 1
        if rank == 3 and 0 <= third <= 3000:
            counts["zero_escape"] += 1
        if rank == 4 and fourth >= 15000:
            counts["healthy_fourths"] += 1
    return counts if complete else Counter()


def _game_features(games, ranks, account_id):
    count = len(games)
    placements = Counter(ranks)
    scores = _score_features(games, ranks, account_id)
    scores["runaway_share"] = scores["runaway"] / placements[1] if placements[1] else 0
    scores["healthy_fourth"] = scores["healthy_fourths"] / placements[4] if placements[4] else 0
    after_fourth = [
        b_rank
        for (a, a_rank), (b, b_rank) in pairwise(zip(games, ranks, strict=True))
        if a_rank == 4 and 0 <= (b.started_at - a.ended_at).total_seconds() <= 3600
    ]
    rebound = after_fourth.count(1)
    durations = [(game.ended_at - game.started_at).total_seconds() for game in games]
    long_ranks = [rank for rank, duration in zip(ranks, durations, strict=True) if duration >= 3300]
    metrics = {
        **scores,
        "firsts": placements[1],
        "fourths": placements[4],
        "first_rate": placements[1] / count,
        "second_rate": placements[2] / count,
        "third_rate": placements[3] / count,
        "fourth_rate": placements[4] / count,
        "middle_rate": (placements[2] + placements[3]) / count,
        "after_fourth": len(after_fourth),
        "rebound": rebound,
        "rebound_rate": rebound / len(after_fourth) if after_fourth else 0,
        "first_streak": max((len(list(g)) for rank, g in groupby(ranks) if rank == 1), default=0),
        "fourth_streak": max((len(list(g)) for rank, g in groupby(ranks) if rank == 4), default=0),
    }
    if count >= 4:
        metrics["rank_album"] = sum(len(set(ranks[i : i + 4])) == 4 for i in range(count - 3)) / (
            count - 3
        )
    if long_ranks and all(0 < duration < 86400 for duration in durations):
        metrics["long_games"] = len(long_ranks)
        metrics["long_top2"] = sum(rank <= 2 for rank in long_ranks) / len(long_ranks)
    return metrics


def _matches(value, low, high):
    return (
        value is not None
        and (low is None or value >= low - 1e-9)
        and (high is None or value <= high + 1e-9)
    )


def candidate_tags(raw, games, ranks, account_id, *, hands, win, deal, call, riichi, fallback=None):
    means = candidate_means().get(str(games[0].mode), {})
    if fallback is not None:
        means = build_candidate_means(fallback)
    values = {"games": len(games)}
    if hands is not None:
        metrics, counts = hand_features(raw, hands, win=win, deal=deal, call=call, riichi=riichi)
        if games[0].started_at < GOOD_SHAPE_SINCE:
            metrics.pop("立直好型2", None)
        values.update(metrics)
        values.update(
            (key, floor(value + 1e-6)) for key, value in counts.items() if value is not None
        )
        values.update(
            (f"{key}差", metrics[key] - mean)
            for key, mean in means.items()
            if metrics.get(key) is not None
        )
    if ranks:
        values.update(_game_features(games, ranks, account_id))
    if len(games) >= 50:
        deltas = [seat_delta(game.seat(account_id)) for game in games]
        if all(unit == "PT" and value is not None for value, unit in deltas):
            equity = [0, *accumulate(value for value, _ in deltas)]
            values.update(pt_range=max(equity) - min(equity), pt_net=equity[-1])
    streak = number(raw.get("最大连庄"))
    if streak is not None and streak.is_integer():
        values["dealer_streak"] = streak
    return [
        name
        for name, conditions in RULES.items()
        if all(_matches(values.get(metric), low, high) for metric, low, high in conditions)
    ]
