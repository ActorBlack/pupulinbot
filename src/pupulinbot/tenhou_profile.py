"""Tenhou history-derived PT and descriptive tags, with explicit provenance.

PT rules: official tenhou.net/man; historic revisions and population quantiles:
nodocchi's public THR.ptcfg / phoenix_scale_all.js. PT is an estimate from
indexed history, and is shown only when its resulting grade matches live data.
Tags are our documented heuristics, not nodocchi's or the reference bot's model.
"""

import copy
import math
from bisect import bisect_right

from .rate_tags import rate_tag


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def grade_config(grade, stamp):
    if grade < 10:
        limits = [20, 20, 20, 20, 40, 60, 80, 100, 100, 100]
        if stamp < 1508792400:
            limits[:7] = [30, 30, 30, 60, 60, 60, 90]
        return 0, limits[grade], -max(0, grade - 7) * 10
    dan = grade - 9
    return dan * 200, dan * 400, -(dan + 2) * 10


def history_rank(row, name):
    # Public listuser stores players in final rank order (uma breaks point ties).
    return next(i for i in range(1, int(row["playernum"]) + 1) if row[f"player{i}"] == name)


def rank_history(
    data, name, mode, cutoff, live_grade=None, *, include_promotions=False, include_records=False
):
    """Replay both modes: ID expiry/reinitialization can affect both at once."""
    rows = sorted(
        (copy.deepcopy(r) for r in data.get("list", []) if float(r["starttime"]) < cutoff),
        key=lambda r: float(r["starttime"]),
    )
    if not rows:
        return {}
    # Match nodocchi's sequence resets, which indicate a different ID life.
    indexed = {3: [], 4: []}
    resets = []
    for i, row in enumerate(rows):
        n = int(row["playernum"])
        if row.get("sctype") not in ("b", "c"):
            continue
        indexed[n].append(i)
        seq = int(row.get("seq", len(indexed[n])))
        if len(indexed[n]) > seq:
            pos = len(indexed[n]) - seq - 1
            resets.append([indexed[n][pos - 1] + 1 if pos else 0, indexed[n][pos + 1] - 1])
    # Source intersects overlapping reset intervals.
    merged = []
    for start, end in resets:
        hit = next((r for r in merged if r[0] <= end and start <= r[1]), None)
        if hit is not None:
            hit[:] = [max(start, hit[0]), min(end, hit[1])]
        else:
            merged.append([start, end])
    reset_ends = {r[0] for r in merged}
    previous_by_mode = {}
    for event in sorted(data.get("ptevents", []), key=lambda e: float(e["starttime"])):
        if float(event["starttime"]) >= cutoff:
            continue
        index = bisect_right([float(r["starttime"]) for r in rows], float(event["starttime"]))
        if not event.get("playernum"):
            if index < len(rows):
                rows[index]["reinit"] = event
        else:
            previous_by_mode = {int(r["playernum"]): j for j, r in enumerate(rows[:index])}
            prev = previous_by_mode.get(int(event["playernum"]))
            if prev is not None:
                rows[prev].setdefault("ptevents", []).append(event)

    def initial():
        return {
            n: dict(
                grade=0,
                pt=0,
                high_grade=0,
                high_pt=0,
                games=0,
                dan_games=0,
                changes=[0],
                change_times=[],
                recent=[],
            )
            for n in (3, 4)
        }

    states = initial()
    previous_time = None
    promotions = []
    lifetime_start = 0
    for index, row in enumerate(rows):
        stamp = float(row["starttime"])
        reset = index in reset_ends or (
            row.get("reinit") is not None and not row["reinit"].get("continuous")
        )
        active = any(float(a) <= stamp <= float(b) for a, b in data.get("rseq", []))
        if previous_time is not None and stamp - previous_time >= 181 * 86400:
            reset |= all(s["grade"] < 16 for s in states.values()) and not active
        if reset:
            states = initial()
            lifetime_start = index
        previous_time = stamp
        n = int(row["playernum"])
        state = states[n]
        grade = state["grade"]
        rank = history_rank(row, name)
        state["recent"].append(rank)
        if row.get("sctype") not in ("b", "c"):
            continue
        state["games"] += 1
        state["dan_games"] += 1
        # Beyond tenhou-i there is no ordinary PT; never invent virtual PT.
        if grade >= 20:
            continue
        _, limit, last = grade_config(grade, stamp)
        level = int(row["playerlevel"])
        factor = 1.0 if stamp < 1220194800 or int(row["playlength"]) == 1 else 1.5
        if rank == n:
            delta = last * factor
        elif n == 3:
            delta = (30, 50, 70, 90)[level] * factor if rank == 1 else 0
        else:
            first, second = ((20, 10), (40, 10), (50, 20), (60, 30))[level]
            if level == 0 and stamp < 1508792400:
                first, second = 30, 0
            delta = (first if rank == 1 else second if rank == 2 else 0) * factor
        state["pt"] += delta
        for event in row.get("ptevents", []):
            if event.get("addpt"):
                state["pt"] += float(event["addpt"])
            elif event.get("newgrade") and event.get("newpt"):
                state["grade"], state["pt"] = int(event["newgrade"]), float(event["newpt"])
        if state["pt"] < 0:
            if grade >= 10:
                state["grade"] -= 1
                state["pt"] = grade_config(state["grade"], stamp)[0]
            else:
                state["pt"] = 0
        elif state["pt"] >= limit:
            state["grade"] += 1
            state["pt"] = grade_config(state["grade"], stamp)[0]
        if (
            include_promotions
            and n == mode
            and state["grade"] > grade
            and state["grade"] >= 10
            and not reset
            and not row.get("ptevents")
        ):
            promotions.append({"starttime": stamp, "grade": state["grade"]})
        if state["grade"] != grade:
            state["changes"].append(state["grade"])
            state["change_times"].append(stamp)
            state["dan_games"] = 0
        if (state["grade"], state["pt"]) > (state["high_grade"], state["high_pt"]):
            state["high_grade"], state["high_pt"] = state["grade"], state["pt"]
    state = states[mode]
    result = {
        "recent": state["recent"][-10:],
        "ranked_count": state["games"],
        "dan_games": state["dan_games"],
        "estimated_grade": state["grade"],
        "grade_changes_30d": sum(t >= cutoff - 30 * 86400 for t in state["change_times"]),
    }
    if include_promotions:
        result["promotions"] = promotions
    if include_records:
        result["current_records"] = rows[lifetime_start:]
    if live_grade is not None and int(live_grade) != state["grade"]:
        result["pt_unavailable"] = "公开历史推算段位与观战段位不一致"
        return result
    if state["grade"] >= 20:
        result["pt_unavailable"] = "天鳳位不显示普通PT"
        return result
    result.update(
        pt=state["pt"],
        pt_max=grade_config(state["grade"], cutoff)[1],
        high_grade=state["high_grade"],
        high_pt=state["high_pt"],
        trajectory=state["changes"][-10:],
        pt_estimated=True,
    )
    return result


def percentile(distribution, value):
    """Match the public site's quantile-bin index; lower deal-in means less dealing in."""
    if not finite(value) or not isinstance(distribution, dict):
        return None
    try:
        values = [float(v) for v in distribution.get("data", [])]
    except (ValueError, TypeError):
        return None
    if (
        not values
        or not all(finite(v) for v in values)
        or values != sorted(values)
        or values[0] == values[-1]
    ):
        return None
    return min(100, max(0, 100 * (bisect_right(values, value) - 1) / len(values)))


def phoenix_percentiles(stats, population):
    """Compare with the published cohort; its min field describes that cohort."""
    if not isinstance(stats, dict) or not isinstance(population, dict):
        return {}
    count = stats.get("totalrecord")
    if not finite(count) or count <= 0:
        return {}
    result = {}
    for key, distribution in population.items():
        if not isinstance(distribution, dict):
            continue
        try:
            minimum = float(distribution.get("min", 0))
        except (TypeError, ValueError):
            continue
        # min=500 selects the reference cohort, not the queried player's eligibility.
        if not finite(minimum) or minimum < 0:
            continue
        pct = percentile(distribution, stats.get(key))
        if pct is not None:
            result[key] = pct
    return result


def describe(profile):
    """Four-player rate matrix plus independent form/history indicators."""
    tags = []
    ranks = profile.get("recent", [])
    n = profile.get("mode", 4)
    if len(ranks) >= 10:
        average = sum(ranks[-10:]) / 10
        if average <= (1.6 if n == 3 else 2.0):
            tags.append("好调")
        elif average >= (2.4 if n == 3 else 3.0):
            tags.append("低迷")
    trail = profile.get("trajectory", [])
    if len(trail) >= 5 and profile.get("grade_changes_30d", 0) >= 3:
        if all(g >= 15 for g in trail[-5:]):
            tags.append("段位起伏")
    p = profile.get("percentiles", {})
    win, deal, call, riichi = (p.get(k) for k in ("agariC", "houjuuC", "fuuroC", "riichC"))
    stats = profile.get("stats", {})
    matrix = rate_tag(stats.get("agariC"), stats.get("houjuuC"), mode=n)
    if matrix:
        tags.append(matrix)
    if n == 3 and all(finite(v) for v in (win, deal, call, riichi)):
        if win >= 80 and deal <= 30:
            tags.append("攻守兼备")
        elif deal <= 10 and win <= 35:
            tags.append("铁壁地藏")
        elif win <= 40 and deal <= 40:
            tags.append("谨慎运营")
        elif call >= 80:
            tags.append("副露进攻")
        elif riichi >= 80:
            tags.append("立直进攻")
        else:
            tags.append("标准运营")
    return tags[:3]
