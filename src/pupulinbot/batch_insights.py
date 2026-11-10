"""Sample-only Mortal statistics, separated by model and metric definition."""

import math
import statistics

from .batch_review import summaries


def metric_value(row, field):
    if row.get("status") != "done":
        return None
    value = (row.get("metrics") or {}).get(field)
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def metric_group(row):
    metric = row.get("metrics") or {}
    return (
        metric.get("model", "未标注模型"),
        metric.get("temperature"),
        metric.get("metric_schema", "legacy"),
    )


def rainbow_threshold(games=1):
    return 92 if games >= 100 else 93 if games >= 50 else 94


def batch_insights(rows):
    ordered = sorted(rows, key=lambda row: row.get("started_at") or "")
    grouped = {}
    for index, row in enumerate(ordered, 1):
        if row.get("status") != "done" or not isinstance(row.get("metrics"), dict):
            continue
        group = grouped.setdefault(metric_group(row), {"rows": [], "points": []})
        value = metric_value(row, "rating")
        group["rows"].append({**row, "metrics": {**row["metrics"], "rating": value}})
        if value is not None:
            group["points"].append((index, value, row))
    for group in grouped.values():
        points = group["points"]
        values = [p[1] for p in points]
        count = len(values)
        group.update(
            count=count,
            threshold=rainbow_threshold(count),
            summary=summaries(group["rows"])[0],
            best=max(points, key=lambda p: p[1]) if count else None,
            worst=min(points, key=lambda p: p[1]) if count else None,
            gold_count=sum(value >= 90 for value in values),
            rainbow_count=sum(value >= rainbow_threshold(count) for value in values),
            deviation=statistics.pstdev(values) if count >= 2 else None,
            recent=None,
        )
        window = min(10, count // 2)
        if window >= 3:
            earlier = [p[2] for p in points[-2 * window : -window]]
            recent = [p[2] for p in points[-window:]]
            before, after = summaries(earlier)[0], summaries(recent)[0]
            changes = {
                field: after[field] - before[field]
                if before[field] is not None and after[field] is not None
                else None
                for field in ("mean_rating", "agreement", "bad5_rate", "bad10_rate")
            }
            group["recent"] = {"window": window, "before": before, "after": after, **changes}
    return {
        "rows": ordered,
        "groups": grouped,
        "completed": sum(row.get("status") == "done" for row in ordered),
        "total": len(ordered),
        "failed": sum(row.get("status") == "failed" for row in ordered),
    }
