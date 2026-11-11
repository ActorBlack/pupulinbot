"""Four-player style radar backed by the exact sample and saved room means."""

import io
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw

from .ms_level import point_text, rank_identity, rank_label, rank_progress
from .ms_options import MODE_NAMES, TZ
from .ms_sample import number, sample_summary
from .ms_style import analyze_style
from .ms_trait_tags import efficiency_metrics
from .typography import profile_face as face

SIZE = (1600, 1270)
INK, MUTED, BLUE = "#172b45", "#536780", "#326fce"
GRID, NEUTRAL = "#dce5ef", "#a6b5c8"


class Metric(NamedTuple):
    key: str
    label: str
    kind: str
    scale: float
    direction: int = 1


AXES = (
    (
        "进攻",
        (Metric("和牌率", "和牌率", "%", 0.05), Metric("自摸率", "自摸占比", "%", 0.12)),
    ),
    (
        "防守",
        (
            Metric("放铳率", "放铳率", "%", 0.04, -1),
            Metric("平均铳点", "平均铳点", "点", 1200, -1),
        ),
    ),
    (
        "副露",
        (
            Metric("副露率", "副露率", "%", 0.12),
            Metric("副露后和牌率", "副露后和牌", "%", 0.10),
            Metric("副露后放铳率", "副露后放铳", "%", 0.06, -1),
        ),
    ),
    (
        "立直",
        (
            Metric("立直率", "立直率", "%", 0.07),
            Metric("先制率", "先制立直", "%", 0.12),
            Metric("立直后和牌率", "立直后和牌", "%", 0.12),
        ),
    ),
    (
        "速度",
        (
            Metric("和了巡数", "和牌巡数", "巡", 1.5, -1),
            Metric("立直巡目", "立直巡目", "巡", 1.5, -1),
        ),
    ),
    (
        "打点",
        (
            Metric("平均打点", "平均打点", "点", 1200),
            Metric("净打点效率", "净打点效率", "点", 500),
        ),
    ),
)


def metric_value(raw, metric):
    value = number(raw.get(metric.key))
    if value is None or (metric.kind == "%" and not 0 <= value <= 1):
        return None
    if metric.key != "净打点效率" and value < 0:
        return None
    return value


@lru_cache(maxsize=1)
def _room_means():
    path = Path(__file__).with_name("assets") / "ms_style_room_means.json"
    return json.loads(path.read_text(encoding="utf-8"))["rooms"]


def comparison_reference(room):
    reference = _room_means().get(room)
    name = MODE_NAMES.get(room, "所选房间")
    if reference is None:
        return f"{name}暂无均值", {}, []
    return f"{name}均值", reference["metrics"], reference["rank_rates"]


def axis_score(group, actual, baseline):
    changes = []
    for metric in group:
        value, reference = metric_value(actual, metric), metric_value(baseline, metric)
        if value is None or reference is None:
            return None
        difference = metric.direction * (value - reference) / metric.scale
        changes.append(max(-1, min(1, difference)))
    return round(50 + 50 * sum(changes) / len(changes))


def display(value, kind):
    if value is None:
        return "—"
    if kind == "%":
        return f"{value:.1%}"
    return f"{value:,.0f}" if kind == "点" else f"{value:.1f}"


def difference(value, base, kind):
    if value is None or base is None:
        return "—"
    delta = value - base
    if kind == "%":
        return f"{delta * 100:+.1f}pp" if abs(delta) >= 0.0005 else "0.0pp"
    precision = 0 if kind == "点" else 1
    return f"{delta:+,.{precision}f}" if round(delta, precision) else f"{0:.{precision}f}"


def render_style(player, stats, games, options):
    finished = sorted(
        (g for g in games if g.ended_at and g.seat(player.account_id)), key=lambda g: g.started_at
    )
    count = len(finished)
    if getattr(stats, "games", None) != count or not count:
        stats = None
    style = analyze_style(stats, options.mode)
    raw = getattr(stats, "raw", {}) or {}
    actual = {**raw, **style.metrics}
    if "净打点效率" not in actual:
        actual.update(efficiency_metrics(actual))
    level = raw.get("level") or (
        finished[-1].seat(player.account_id).get("level") if finished else None
    )
    progress = rank_progress(level)
    rank = progress or rank_identity(level)
    rooms = {str(g.mode) for g in finished}
    selected = {str(mode) for mode in (options.modes or ())}
    scope = selected or rooms
    room = next(iter(scope)) if len(scope) == 1 and rooms <= scope else ""
    room_name = MODE_NAMES.get(room, "")
    reference_label, baseline, reference_ranks = comparison_reference(room)
    scores = [axis_score(group, actual, baseline) for _, group in AXES]
    summary = sample_summary(finished, player.account_id, options.mode)
    image = Image.new("RGB", SIZE, "#eef3f8")
    draw = ImageDraw.Draw(image, "RGBA")

    def text(x, y, value, size=21, color=INK, style="regular", anchor="lt", width=None):
        font = face(size, style)
        value = str(value).replace("\n", " ")
        if width and draw.textlength(value, font=font) > width:
            while value and draw.textlength(value + "…", font=font) > width:
                value = value[:-1]
            value += "…"
        draw.text((x, y), value, fill=color, font=font, anchor=anchor)

    def card(bounds, fill="white", radius=20):
        draw.rounded_rectangle(bounds, radius=radius, fill=fill)

    card((36, 26, 1564, 165))
    text(66, 47, player.nickname, 43, style="title", width=870)
    sample_scope = f"四麻{room_name}  "
    dates = ""
    if finished:
        lo, hi = (g.started_at.astimezone(TZ) for g in (finished[0], finished[-1]))
        dates = f"　{lo:%m-%d}" if lo.date() == hi.date() else f"　{lo:%m-%d} 至 {hi:%m-%d}"
    text(68, 111, f"{sample_scope}{count:,} 场{dates}", 23, MUTED, width=645)
    if rank:
        rank_title = rank_label(rank, full=True)
        if progress:
            rank_title += "　" + point_text(progress, capacity=False, unit=True)
        text(1534, 51, rank_title, 33, BLUE, "bold", "rt", 500)
    tag = "#" + style.label if style.x is not None else "牌风数据不足"
    text(1534, 111, tag, 24, MUTED, "bold", "rt", 600)

    card((36, 185, 690, 913))
    card((710, 185, 1564, 913))
    text(64, 211, "牌风雷达", 31, style="bold")
    text(64, 259, f"{reference_label} = 50" if baseline else reference_label, 20, MUTED)
    cx, cy, radius = 362, 566, 205
    angles = [math.tau * i / len(AXES) - math.pi / 2 for i in range(len(AXES))]

    def vertex(angle, score):
        distance = radius * score / 100
        return cx + distance * math.cos(angle), cy + distance * math.sin(angle)

    for tick in (20, 40, 60, 80, 100):
        outline = [vertex(angle, tick) for angle in angles]
        draw.line([*outline, outline[0]], fill=GRID, width=2)
        text(cx + 7, cy - radius * tick / 100 + 4, tick, 13, MUTED)
    for angle in angles:
        draw.line([(cx, cy), vertex(angle, 100)], fill=GRID, width=2)
    if baseline:
        middle = [vertex(angle, 50) for angle in angles]
        for a, b in zip(middle, middle[1:] + middle[:1], strict=True):
            for step in range(0, 12, 2):
                segment = [
                    tuple(x + (y - x) * t / 12 for x, y in zip(a, b, strict=True))
                    for t in (step, step + 1)
                ]
                draw.line(segment, fill=NEUTRAL, width=3)
    points = [
        vertex(angle, score) if score is not None else None
        for angle, score in zip(angles, scores, strict=True)
    ]
    if all(point is not None for point in points):
        draw.polygon(points, fill=(50, 111, 206, 46))
    for a, b in zip(points, points[1:] + points[:1], strict=True):
        if a is not None and b is not None:
            draw.line((a, b), fill=BLUE, width=5)
    for angle, (name, _), score, point in zip(angles, AXES, scores, points, strict=True):
        if point is not None:
            x, y = point
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=BLUE, outline="white", width=2)
        x, y = vertex(angle, 125)
        text(x, y - 18, name, 24, style="bold", anchor="mt")
        text(
            x,
            y + 12,
            "—" if score is None else score,
            24,
            BLUE if score is not None else MUTED,
            "number",
            "mt",
        )
    draw.line((68, 888, 104, 888), fill=BLUE, width=5)
    text(118, 878, "本人", 20)
    if baseline:
        for x in range(233, 269, 12):
            draw.line((x, 888, x + 7, 888), fill=NEUTRAL, width=3)
        text(283, 878, "均值", 20, MUTED)
    if baseline and any(score is None for score in scores):
        text(663, 878, "缺数据的维度留空", 17, MUTED, anchor="rt")

    text(736, 211, "各维度指标", 31, style="bold")
    text(1536, 222, "pp 为百分点", 18, MUTED, anchor="rt")
    group_x, metric_x, own_x, mean_x, delta_x = 754, 889, 1168, 1345, 1519
    card((734, 266, 1540, 311), "#eaf0f8", 10)
    for label, x, anchor in (
        ("维度", group_x, "lm"),
        ("指标", metric_x, "lm"),
        ("本人", own_x, "rm"),
        ("均值", mean_x, "rm"),
        ("差值", delta_x, "rm"),
    ):
        text(x, 289, label, 20, BLUE if label == "本人" else MUTED, "bold", anchor)

    insights = []
    top = 322
    for i, ((name, group), score) in enumerate(zip(AXES, scores, strict=True)):
        height = 36 * len(group) + 10
        center = top + height // 2
        if i % 2 == 0:
            card((734, top, 1540, top + height - 2), "#f5f8fc", 10)
        text(group_x, center, name, 22, style="bold", anchor="lm")
        if score is not None:
            card((808, center - 14, 855, center + 14), "#e7effd", 8)
            text(832, center, score, 18, BLUE, "number", "mm")
        candidates = []
        for j, metric in enumerate(group):
            y = top + 23 + j * 36
            own, base = metric_value(actual, metric), metric_value(baseline, metric)
            text(metric_x, y, metric.label, 21, anchor="lm")
            text(own_x, y, display(own, metric.kind), 23, BLUE, "number", "rm")
            text(mean_x, y, display(base, metric.kind), 21, MUTED, "number", "rm")
            text(delta_x, y, difference(own, base, metric.kind), 21, INK, "number", "rm")
            if own is not None and base is not None:
                candidates.append((abs(own - base) / metric.scale, metric, own, base))
        if candidates:
            spread, metric, own, base = max(candidates, key=lambda value: value[0])
            insights.append((spread, name, metric, own, base))
        top += height

    card((36, 933, 690, 1224))
    card((710, 933, 1564, 1224))
    text(64, 954, "顺位分布", 27, style="bold")
    known = summary["known_ranks"]
    if known:
        average = f"均顺 {summary['average_rank']:.2f}"
        if known != count:
            average += f"（已知 {known} 场）"
        text(471, 963, average, 19, MUTED, anchor="rt")
    text(565, 963, "本人", 17, BLUE, anchor="rt")
    text(661, 963, "均值", 17, MUTED, anchor="rt")
    ranks = [n / known for n in summary["rank_counts"]] if known else [None] * 4
    for i, own in enumerate(ranks):
        base = number(reference_ranks[i]) if i < len(reference_ranks) else None
        y = 1000 + i * 46
        text(65, y + 3, f"{i + 1}位", 20, style="bold")
        draw.rounded_rectangle((127, y + 2, 472, y + 28), 6, fill="#edf2f8")
        for value, top, color in ((own, y + 3, BLUE), (base, y + 17, NEUTRAL)):
            if value is not None and value > 0:
                end = 127 + round(345 * value)
                draw.rounded_rectangle((127, top, end, top + 9), 4, fill=color)
        text(565, y + 4, display(own, "%"), 20, BLUE, "number", "rt")
        text(661, y + 4, display(base, "%"), 19, MUTED, "number", "rt")

    text(736, 954, "牌风看点", 27, style="bold")
    standout = sorted((v for v in insights if v[0] >= 0.05), key=lambda v: v[0], reverse=True)[:3]
    for i, (_, name, metric, own, base) in enumerate(standout):
        y = 1001 + i * 49
        card((736, y, 805, y + 33), "#e8f0fc", 8)
        text(771, y + 7, name, 18, BLUE, "bold", "mt")
        note = (
            f"{metric.label} {display(own, metric.kind)}，"
            f"较均值 {difference(own, base, metric.kind)}"
        )
        text(823, y + 5, note, 21, width=700)
    if not standout:
        note = "暂无可比牌风指标" if not insights else "各项指标与房间均值接近"
        text(736, 1010, note, 21, MUTED)
    extras = (("默和", "默听率"), ("流局听牌", "流听率"), ("追立", "追立率"))
    extra_line = "　".join(
        f"{label} {display(style.metrics[key], '%')}"
        for label, key in extras
        if key in style.metrics
    )
    if extra_line:
        text(736, 1179, extra_line, 19, MUTED, width=794)
    text(37, 1240, "数据来源  雀魂牌谱屋", 16, MUTED)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
