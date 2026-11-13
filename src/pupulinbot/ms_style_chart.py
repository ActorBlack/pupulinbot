"""Four-player style radar for the selected sample and current grade."""

import io
import math
from statistics import NormalDist

from PIL import Image, ImageDraw

from .ms_level import point_text, rank_identity, rank_label, rank_progress
from .ms_options import MODE_NAMES, TZ
from .ms_sample import number, sample_summary
from .ms_style import analyze_style
from .ms_style_radar import (
    AXES,
    axis_score,
    comparison_reference,
    composite_metrics,
    metric_value,
    speed_turns,
)
from .typography import profile_face as face

SIZE = (1600, 1270)
INK, MUTED, BLUE = "#172b45", "#536780", "#326fce"
GRID, NEUTRAL = "#dce5ef", "#a6b5c8"
BONUS_INK, BONUS_LINE = "#936000", "#be8317"
BONUS_FILL, BONUS_BACKGROUND = "#faebc6", "#fff8e9"
NORMAL = NormalDist()
BONUS_START = {
    "进攻": 2.0,
    "防守": 1.6,
    "副露": 2.0,
    "立直": 2.6,
    "速度": 3.0,
    "打点": 2.0,
}
BONUS_CAP_DEVIATION = 5.0
BONUS_CAP_THRESHOLD = NORMAL.cdf(BONUS_CAP_DEVIATION)


def radar_bonus_score(score, *, start=3.0):
    """Map the upper tail of an unrounded radar score to a display-only extension."""
    if score is None or score / 100 <= NORMAL.cdf(start):
        return None
    if score / 100 >= BONUS_CAP_THRESHOLD:
        return 150
    deviation = NORMAL.inv_cdf(score / 100)
    points_per_deviation = 50 / (BONUS_CAP_DEVIATION - start)
    bonus = round(100 + points_per_deviation * (deviation - start))
    return bonus if bonus > 100 else None


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


def render_style(player, stats, games, options, *, current_level=None):
    finished = sorted(
        (g for g in games if g.ended_at and g.seat(player.account_id)), key=lambda g: g.started_at
    )
    count = len(finished)
    if getattr(stats, "games", None) != count or not count:
        stats = None
    style = analyze_style(stats, options.mode)
    raw = getattr(stats, "raw", {}) or {}
    actual = {**raw, **style.metrics}
    actual.update(composite_metrics(actual))
    progress = rank_progress(current_level)
    rank = progress or rank_identity(current_level)
    rooms = {str(g.mode) for g in finished}
    selected = {str(mode) for mode in (options.modes or ())}
    scope = selected or rooms
    room = next(iter(scope)) if len(scope) == 1 and rooms <= scope else ""
    room_name = MODE_NAMES.get(room, "")
    reference = comparison_reference(room, rank)
    if reference:
        actual["和立牌平均巡目"] = speed_turns(actual, reference.speed_win_weight)
    baseline = reference.metrics if reference else {}
    reference_ranks = reference.rank_rates if reference else []
    reference_label = f"{room_name}{rank_label(rank)}均值 = 50" if reference else "暂无同段位均值"
    hands = number(raw.get("_hand_count"))
    if hands is not None and (not hands.is_integer() or hands < count):
        hands = None
    scores = [axis_score(axis, actual, reference, hands) for axis in AXES]
    bonuses = [
        radar_bonus_score(score, start=BONUS_START[axis.name])
        for axis, score in zip(AXES, scores, strict=True)
    ]
    display_scores = [
        bonus if bonus is not None else score for score, bonus in zip(scores, bonuses, strict=True)
    ]
    has_bonus = any(bonuses)
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
        dates = f"　{lo:%Y-%m-%d}"
        if lo.date() != hi.date():
            dates += f" 至 {hi:%Y-%m-%d}"
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
    text(64, 259, reference_label, 20, MUTED)
    cx, cy, radius = (362, 558, 180) if has_bonus else (362, 566, 205)
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
    if reference:
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
        for angle, score in zip(angles, display_scores, strict=True)
    ]
    if all(point is not None for point in points):
        draw.polygon(points, fill=(50, 111, 206, 46))
        if has_bonus:
            outer_fill = Image.new("L", SIZE)
            fill_draw = ImageDraw.Draw(outer_fill)
            fill_draw.polygon(points, fill=114)
            fill_draw.polygon([vertex(angle, 100) for angle in angles], fill=0)
            image.paste("#f7ce7b", (0, 0, *SIZE), outer_fill)
    for i, a in enumerate(points):
        j = (i + 1) % len(points)
        b = points[j]
        if a is None or b is None:
            continue
        a_color = BONUS_LINE if bonuses[i] is not None else BLUE
        b_color = BONUS_LINE if bonuses[j] is not None else BLUE
        if a_color == b_color:
            draw.line((a, b), fill=a_color, width=5)
        else:
            # In the wedge between these axes, the 100 ring is the line whose
            # two radial coordinates sum to 100.
            a_score, b_score = display_scores[i], display_scores[j]
            fraction = (100 - a_score) / (b_score - a_score)
            crossing = tuple(x + (y - x) * fraction for x, y in zip(a, b, strict=True))
            draw.line((a, crossing), fill=a_color, width=5)
            draw.line((crossing, b), fill=b_color, width=5)
    for angle, axis, score, point, bonus in zip(
        angles, AXES, display_scores, points, bonuses, strict=True
    ):
        if point is not None:
            x, y = point
            fill = BONUS_LINE if bonus is not None else BLUE
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=fill, outline="white", width=2)
        if bonus is not None:
            x, y = vertex(angle, bonus)
            horizontal = math.cos(angle)
            if abs(horizontal) < 0.01:
                label_x = x
                label_y = y - 54 if math.sin(angle) < 0 else y + 8
            else:
                label_x = x + math.copysign(44, horizontal)
                label_y = y - 24
            text(label_x, label_y, axis.name, 24, BONUS_INK, "bold", "mt")
            text(label_x, label_y + 25, bonus, 28, BONUS_INK, "number", "mt")
            continue
        x, y = vertex(angle, 125)
        text(x, y - 18, axis.name, 24, style="bold", anchor="mt")
        text(
            x,
            y + 12,
            "—" if score is None else round(score),
            24,
            BLUE if score is not None else MUTED,
            "number",
            "mt",
        )
    legend_y = 895 if has_bonus else 888
    draw.line((68, legend_y, 104, legend_y), fill=BLUE, width=5)
    text(118, legend_y - 10, "本人", 20)
    if reference:
        for x in range(233, 269, 12):
            draw.line((x, legend_y, x + 7, legend_y), fill=NEUTRAL, width=3)
        text(283, legend_y - 10, "同段均值", 20, MUTED)

    text(736, 211, "各维度指标", 31, style="bold")
    text(1536, 222, "pp 为百分点", 18, MUTED, anchor="rt")
    group_x, metric_x, own_x, mean_x, delta_x = 754, 889, 1168, 1345, 1519
    card((734, 266, 1540, 311), "#eaf0f8", 10)
    for label, x, anchor in (
        ("维度", group_x, "lm"),
        ("指标", metric_x, "lm"),
        ("本人", own_x, "rm"),
        ("同段均值", mean_x, "rm"),
        ("差值", delta_x, "rm"),
    ):
        text(x, 289, label, 20, BLUE if label == "本人" else MUTED, "bold", anchor)

    insights = []
    top = 322
    for i, (axis, score, bonus) in enumerate(zip(AXES, scores, bonuses, strict=True)):
        group = (axis.metric, *axis.inputs)
        height = 25 * len(group) + 10
        center = top + height // 2
        accent = BONUS_INK if bonus is not None else BLUE
        label_color = BONUS_INK if bonus is not None else INK
        mean_color = "#8e7648" if bonus is not None else MUTED
        if bonus is not None:
            card((734, top, 1540, top + height - 2), BONUS_BACKGROUND, 10)
        elif i % 2 == 0:
            card((734, top, 1540, top + height - 2), "#f5f8fc", 10)
        text(group_x, center, axis.name, 22, label_color, "bold", "lm")
        if score is not None:
            score_fill = BONUS_FILL if bonus is not None else "#e7effd"
            card((808, center - 14, 855, center + 14), score_fill, 8)
            text(
                832,
                center,
                bonus if bonus is not None else round(score),
                18,
                accent,
                "number",
                "mm",
            )
        for j, metric in enumerate(group):
            y = top + 18 + j * 25
            own, base = metric_value(actual, metric), metric_value(baseline, metric)
            font_style = "bold" if j == 0 else "regular"
            text(metric_x, y, metric.label, 19, label_color, font_style, "lm")
            text(own_x, y, display(own, metric.kind), 21, accent, "number", "rm")
            text(mean_x, y, display(base, metric.kind), 20, mean_color, "number", "rm")
            text(delta_x, y, difference(own, base, metric.kind), 20, label_color, "number", "rm")
            if j == 0 and score is not None:
                insights.append((abs(score - 50), axis.name, metric, own, base, bonus))
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
    text(661, 963, "同段均值", 17, MUTED, anchor="rt")
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
    standout = sorted((v for v in insights if v[0] >= 25), key=lambda v: v[0], reverse=True)[:3]
    for i, (_, name, metric, own, base, bonus) in enumerate(standout):
        y = 1001 + i * 49
        accent, fill = (BONUS_INK, BONUS_FILL) if bonus is not None else (BLUE, "#e8f0fc")
        card((736, y, 805, y + 33), fill, 8)
        text(771, y + 7, name, 18, accent, "bold", "mt")
        note = (
            f"{metric.label} {display(own, metric.kind)}，"
            f"较同段 {difference(own, base, metric.kind)}"
        )
        text(823, y + 5, note, 21, BONUS_INK if bonus is not None else INK, width=700)
    if not standout:
        note = "暂无可比牌风指标" if not insights else "暂无明显偏离"
        text(736, 1010, note, 21, MUTED)
    extras = (("自摸", "自摸率"), ("默和", "默听率"), ("先制", "先制率"))
    extra_line = "　".join(
        f"{label} {display(number(actual[key]), '%')}" for label, key in extras if key in actual
    )
    if extra_line:
        text(736, 1179, extra_line, 19, MUTED, width=794)
    text(37, 1240, "数据来源  雀魂牌谱屋", 16, MUTED)
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
