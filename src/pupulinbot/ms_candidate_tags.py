"""Four-player tags approved from the room-sample backtest."""

import json
from collections import Counter
from datetime import UTC, datetime
from functools import lru_cache
from itertools import accumulate, groupby, pairwise
from math import floor
from pathlib import Path

from .ms_level import seat_delta
from .ms_replay_tags import WEST_MIN_GAMES, WEST_MIN_RATE
from .ms_sample import number
from .ms_trait_tags import exposures, rate

# Each condition is (metric, minimum, maximum). A mean-relative condition uses
# the corresponding weighted reference metric from the room baseline.
RULES = {
    "得不偿失": (("铳点高于打点", 1, 1),),
    "天意弄人": (
        ("games", 50, None),
        ("hands", 400, None),
        ("riichis", 100, None),
        ("立直好型2差", 0.08, None),
        ("立直后和牌率差", None, -0.06),
    ),
    "出奇制胜": (
        ("games", 50, None),
        ("hands", 400, None),
        ("riichis", 100, None),
        ("立直好型2差", None, -0.08),
        ("立直后和牌率差", 0.06, None),
    ),
    "高开低走": (
        ("games", 50, None),
        ("hands", 400, None),
        ("wins", 80, None),
        ("平均起手向听差", None, -0.05),
        ("和牌率差", None, -0.01),
        ("和了巡数差", 0.3, None),
    ),
    "逆流而上": (
        ("games", 50, None),
        ("hands", 400, None),
        ("wins", 80, None),
        ("平均起手向听差", 0.05, None),
        ("和牌率差", 0.01, None),
        ("和了巡数差", None, -0.2),
    ),
    "进退失据": (
        ("games", 50, None),
        ("hands", 400, None),
        ("calls", 150, None),
        ("riichis", 100, None),
        ("副露后和牌率差", None, -0.04),
        ("立直后和牌率差", None, -0.04),
        ("放铳率差", 0.02, None),
    ),
    "燕返": (("riichis", 100, None), ("宣言铳率差", 0.02, None), ("立直后非瞬间放铳率差", None, 0)),
    "立直提款机": (("riichis", 100, None), ("立直收支差", 650, None), ("立直后放铳率差", None, 0)),
    "众矢之的": (("riichis", 100, None), ("被追率差", 0.05, None), ("先制率差", 0, None)),
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
    "略胜一筹": (
        ("games", 50, None),
        ("close_first", 3, None),
        ("close_first_sample_rate", 0.03, None),
    ),
    "棋差一着": (
        ("games", 50, None),
        ("close_second", 3, None),
        ("close_second_sample_rate", 0.03, None),
    ),
    "无功而返": (("games", 50, None), ("zero_pt_games", 2, None), ("zero_pt_rate", 0.02, None)),
    "签到游戏": (("games", 50, None), ("pt_range", 600, None), ("pt_net", -100, 100)),
    "精选立直": (("riichis", 100, None), ("立直率差", None, -0.03), ("立直好型2差", 0.08, None)),
    "振听达人": (("riichis", 150, None), ("振听立直率", 0.03, None), ("振听立直率差", 0.015, None)),
    "偶失荆州": (("deals", 40, None), ("放铳率差", None, -0.02), ("平均铳点差", 800, None)),
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
        ("firsts_sample_rate", 0.15, None),
        ("runaway", 5, None),
        ("runaway_sample_rate", 0.05, None),
        ("runaway_share", 0.40, None),
    ),
    "越挫越勇": (
        ("games", 50, None),
        ("after_fourth", 10, None),
        ("after_fourth_sample_rate", 0.10, None),
        ("rebound", 4, None),
        ("rebound_sample_rate", 0.04, None),
        ("rebound_rate", 0.50, None),
    ),
    "悬崖勒马": (
        ("games", 50, None),
        ("close_third", 3, None),
        ("close_third_sample_rate", 0.03, None),
    ),
    "咫尺天涯": (
        ("games", 50, None),
        ("close_fourth", 3, None),
        ("close_fourth_sample_rate", 0.03, None),
    ),
    "独裁者": (("dealer_streak", 6, None),),
    "鏖战": (("west_games", WEST_MIN_GAMES, None), ("west_rate", WEST_MIN_RATE, None)),
    "一击必杀": (
        ("riichi_wins", 40, None),
        ("立直收入差", 700, None),
        ("立直后和牌率差", None, 0),
    ),
    "以小搏大": (("deals", 50, None), ("放铳率差", 0.01, None), ("平均铳点差", None, -650)),
    "被炸仙人": (("hands", 800, None), ("被炸率", 0.14, None), ("平均被炸点数", 11600, None)),
    "零点逃生": (
        ("games", 50, None),
        ("zero_escape", 2, None),
        ("zero_escape_sample_rate", 0.02, None),
    ),
    "连败之道": (("games", 50, None), ("fourth_streak", 5, None)),
    "连胜之道": (("games", 50, None), ("first_streak", 5, None)),
    "顺位集邮": (("games", 80, None), ("rank_album", 0.16, None)),
    "马拉松": (
        ("games", 50, None),
        ("long_games", 8, None),
        ("long_games_sample_rate", 0.08, None),
        ("long_top2", 0.70, None),
    ),
    "体面结局": (
        ("games", 50, None),
        ("fourths", 10, None),
        ("fourths_sample_rate", 0.10, None),
        ("healthy_fourth", 0.35, None),
    ),
}

# Per-match counts use all selected matches as denominator. Keep exposure
# minima (games/hands/wins/riichis) and consecutive streaks independent.
GAME_COUNT_METRICS = frozenset(
    {
        "close_first",
        "close_second",
        "close_third",
        "close_fourth",
        "zero_escape",
        "long_games",
        "firsts",
        "runaway",
        "after_fourth",
        "rebound",
        "fourths",
    }
)

RATE_BASES = {
    "和牌率": "hands",
    "副露后和牌率": "calls",
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
    "平均起手向听": "hands",
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
    for key, value in (("和牌率", win), ("放铳率", deal), ("副露率", call), ("立直率", riichi)):
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
        if rank == 4 and 0 < third - fourth <= 1000:
            counts["close_fourth"] += 1
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


def room_means(room, *, reference_levels=None, fallback=None):
    means = candidate_means().get(str(room), {})
    if fallback is not None:
        means = build_candidate_means(fallback)
    if reference_levels is not None and fallback is None:
        means = {**build_candidate_means(reference_levels), **means}
    return means


def assassin_menzen_eligible(
    raw, room, *, hands, win, deal, call, riichi, reference_levels=None, fallback=None
):
    """Reuse deep-menzen requirements, excluding only the riichi-win share."""
    if hands is None:
        return False
    means = room_means(room, reference_levels=reference_levels, fallback=fallback)
    metrics, counts = hand_features(raw, hands, win=win, deal=deal, call=call, riichi=riichi)
    call_rate, call_mean = metrics.get("副露率"), means.get("副露率")
    values = {
        "wins": counts["wins"],
        "默听率": metrics.get("默听率"),
        "副露率差": call_rate - call_mean
        if call_rate is not None and call_mean is not None
        else None,
    }
    return all(
        _matches(values.get(metric), low, high)
        for metric, low, high in RULES["深度门清"]
        if metric != "riichi_win_share"
    )


def candidate_tags(
    raw,
    games,
    ranks,
    account_id,
    *,
    hands,
    win,
    deal,
    call,
    riichi,
    fallback=None,
    reference_levels=None,
):
    means = room_means(games[0].mode, reference_levels=reference_levels, fallback=fallback)
    values = {"games": len(games)}
    win_points = number(raw.get("平均打点"))
    deal_points = number(raw.get("平均铳点"))
    if win_points is not None and deal_points is not None and win_points > 0 and deal_points > 0:
        # This user-defined comparison is strictly greater, without threshold tolerance.
        values["铳点高于打点"] = int(deal_points > win_points)
    # Use the exact per-match grading delta, never a rounded display value,
    # final table score, or a zero-filled missing field.
    zero_pt_games = sum(
        number(seat.get("gradingScore", seat.get("grading_score"))) == 0
        for game in games
        if game.ended_at and (seat := game.seat(account_id))
    )
    values.update(zero_pt_games=zero_pt_games, zero_pt_rate=zero_pt_games / len(games))
    winds = raw.get("_replay_winds")
    if isinstance(winds, dict):
        values["west_games"] = sum(
            game.mode in {"9", "12", "16"}
            and type(winds.get(game.uuid)) is int
            and winds[game.uuid] in (2, 3)
            for game in games
        )
        values["west_rate"] = values["west_games"] / len(games)
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
    values.update(
        (f"{metric}_sample_rate", values[metric] / len(games))
        for metric in GAME_COUNT_METRICS
        if values.get(metric) is not None
    )
    return [
        name
        for name, conditions in RULES.items()
        if all(_matches(values.get(metric), low, high) for metric, low, high in conditions)
    ]
