"""Descriptive statistics for indexed, finished games; no hand-level inference."""

import math
from collections import Counter

from .ms_level import delta_totals, seat_delta


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def sample_summary(games, account_id, mode=4):
    finished = sorted((g for g in games if g.ended_at), key=lambda g: g.started_at)
    ranks, scores, deltas = [], [], []
    streak = best = 0
    for game in finished:
        seat = game.seat(account_id) or {}
        rank = number(seat.get("rank"))
        valid = rank is not None and rank.is_integer() and 1 <= rank <= mode
        ranks.append(int(rank) if valid else None)
        score, delta = number(seat.get("score")), number(seat.get("gradingScore"))
        if score is not None:
            scores.append(score)
        if delta is not None:
            deltas.append(delta)
        streak = streak + 1 if valid and rank == 1 else 0
        best = max(best, streak)
    known = [r for r in ranks if r is not None]
    counts = Counter(known)
    return {
        "games": len(finished),
        "read_games": len(games),
        "known_ranks": len(known),
        "rank_counts": [counts[r] for r in range(1, mode + 1)],
        "ranks": ranks,
        "average_rank": sum(known) / len(known) if known else None,
        "first_rate": counts[1] / len(known) if known else None,
        "top2_rate": sum(r <= 2 for r in known) / len(known) if known else None,
        "average_score": sum(scores) / len(scores) if scores else None,
        "negative_rate": sum(s < 0 for s in scores) / len(scores) if scores else None,
        "known_scores": len(scores),
        "known_deltas": sum(seat_delta(g.seat(account_id) or {})[0] is not None for g in finished),
        # Keep raw total for existing consumers; all displays use unit-separated totals.
        "pt_delta": sum(deltas) if deltas else None,
        "point_totals": delta_totals(finished, account_id),
        "best_first_streak": best if known else None,
        "current_first_streak": streak if ranks and ranks[-1] is not None else None,
        "first_at": finished[0].started_at if finished else None,
        "last_at": finished[-1].started_at if finished else None,
    }


def rolling_ranks(ranks, window=10):
    values = [math.nan] * len(ranks)
    for i in range(window - 1, len(ranks)):
        sample = ranks[i - window + 1 : i + 1]
        if all(r is not None for r in sample):
            values[i] = sum(sample) / window
    return values
