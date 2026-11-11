"""Compact, annotated multi-game Mortal reports drawn from saved results only."""

import io
import math
from datetime import datetime
from statistics import quantiles

from PIL import Image, ImageDraw

from .batch_insights import batch_insights, metric_group, metric_value
from .ms_options import MODE_NAMES, TZ
from .typography import face

INK = "#192b43"
MUTED = "#566983"
BLUE = "#3473c4"
CORAL = "#c65839"
TEAL = "#128568"
AMBER = "#b4740c"
PURPLE = "#a477d5"
GRID = "#e4ebf4"
PALETTE = (BLUE, PURPLE, TEAL, CORAL, "#986132", "#467b8d", "#9f4c71", "#637b34")
METRICS = (
    ("rating", "Rating", BLUE),
    ("agreement", "AI 一致率", CORAL),
    ("bad5_rate", "5% 恶手率", TEAL),
    ("bad10_rate", "10% 恶手率", AMBER),
)


class Canvas:
    scale = 2

    def __init__(self, height):
        self.height = height
        self.image = Image.new("RGBA", (2880, height * self.scale), "#eff3f8")
        self.draw = ImageDraw.Draw(self.image)

    def box(self, box):
        return tuple(round(v * self.scale) for v in box)

    def panel(self, box, fill="white", *, radius=18):
        self.draw.rounded_rectangle(self.box(box), radius=radius * self.scale, fill=fill)

    def rect(self, box, color):
        self.draw.rectangle(self.box(box), fill=color)

    def line(self, points, color, width=1, *, dashed=False):
        if dashed:
            phase = 0.0
            for first, last in zip(points, points[1:], strict=False):
                length = math.dist(first, last)
                position = 0.0
                while position < length:
                    step = min(length - position, (7 if phase < 7 else 12) - phase)
                    if phase < 7:
                        segment = [
                            tuple(
                                a + (b - a) * p / length for a, b in zip(first, last, strict=True)
                            )
                            for p in (position, position + step)
                        ]
                        self.line(segment, color, width)
                    position += step
                    phase = (phase + step) % 12
            return
        self.draw.line(
            [tuple(round(v * self.scale) for v in p) for p in points],
            fill=color,
            width=max(1, round(width * self.scale)),
            joint="curve",
        )

    def text(self, x, y, value, size=18, color=INK, style="regular", anchor="lm", width=None):
        font = face(size * self.scale, style)
        value = str(value).replace("\n", " ")
        if width and self.draw.textlength(value, font=font) > width * self.scale:
            while value and self.draw.textlength(value + "…", font=font) > width * self.scale:
                value = value[:-1]
            value += "…"
        self.draw.text(self.box((x, y)), value, font=font, fill=color, anchor=anchor)

    def dot(self, x, y, color, radius=3.4, *, rainbow=False, hollow=False):
        box = self.box((x - radius, y - radius, x + radius, y + radius))
        if rainbow:
            for start, stop, fill in (
                (0, 90, "#c58bdf"),
                (90, 180, "#65bbd9"),
                (180, 270, "#59cba1"),
                (270, 360, "#e6bc66"),
            ):
                self.draw.pieslice(box, start, stop, fill=fill)
        else:
            self.draw.ellipse(box, fill="white" if hollow else color, outline=color, width=2)
        if radius > 4:
            self.draw.ellipse(self.box((x - 1.6, y - 1.6, x + 1.6, y + 1.6)), fill="white")

    def rating(self, x, y, value, games, *, right):
        from .batch_chart import GOLD_RATING, RAINBOW_RATING, rating_color, shiny_rating_layer

        label = number(value)
        color = rating_color(value, games=games)
        if color not in {GOLD_RATING, RAINBOW_RATING}:
            self.text(x, y, label, 62, INK, "number")
            return
        left, right = self.box((x - 12, right - 12))
        layer = shiny_rating_layer(
            label, rainbow=color == RAINBOW_RATING, font_size=62 * self.scale
        )
        start, _, end, _ = layer.getbbox()
        layer = layer.crop((start, 0, end, layer.height))
        span = right - left
        if layer.width > span:
            layer = layer.resize(
                (span, max(1, round(layer.height * span / layer.width))), Image.Resampling.LANCZOS
            )
        self.image.alpha_composite(
            layer,
            (round((left + right - layer.width) / 2), round(y * self.scale - layer.height / 2)),
        )

    def png(self):
        output = io.BytesIO()
        self.image.convert("RGB").resize((1440, self.height), Image.Resampling.LANCZOS).save(
            output, format="PNG", optimize=True
        )
        return output.getvalue()


def number(value, suffix=""):
    return "—" if value is None else f"{value:.2f}{suffix}"


def delta(value):
    return "—" if value is None else "0.00" if abs(value) < 0.005 else f"{value:+.2f}"


def game_date(row):
    try:
        stamp = datetime.fromisoformat(row.get("started_at") or "")
    except (ValueError, TypeError):
        return None
    return (stamp.astimezone(TZ) if stamp.tzinfo else stamp).date()


def room_label(rows):
    labels = {}
    for row in rows:
        source = row.get("source", "majsoul")
        if source == "majsoul":
            label = MODE_NAMES.get(str(row.get("mode")))
        else:
            label = {"tenhou": "天凤", "riichi_city": "一番街"}.get(source)
        if label:
            labels[label] = None
    return " / ".join(labels)


def model_label(key):
    model, temperature, _ = key
    return str(model) if temperature is None else f"{model}  T={temperature}"


def draw_overview(c, groups):
    group = next(iter(groups.values()), {})
    summary = group.get("summary", {})
    cards = (
        ("平均 Rating", summary.get("mean_rating"), f"中位 {number(summary.get('median_rating'))}"),
        (
            "AI 一致率",
            summary.get("agreement"),
            ratio(summary.get("matches"), summary.get("decisions"), "一致决策"),
        ),
        (
            "5% 恶手率",
            summary.get("bad5_rate"),
            ratio(summary.get("bad5"), summary.get("badmove_decisions"), "恶手"),
        ),
        (
            "10% 恶手率",
            summary.get("bad10_rate"),
            ratio(summary.get("bad10"), summary.get("badmove_decisions"), "恶手"),
        ),
    )
    for i, ((label, value, detail), (_, _, color)) in enumerate(zip(cards, METRICS, strict=True)):
        left = 34 + i * 347
        c.panel((left, 116, left + 331, 254))
        c.panel((left + 19, 133, left + 48, 137), color, radius=2)
        c.text(left + 19, 156, label, 19, MUTED)
        if i == 0:
            c.rating(left + 20, 199, value, group.get("count", 0), right=left + 310)
        else:
            c.text(left + 20, 198, number(value, "%"), 43, INK, "number")
        c.text(left + 20, 234, detail, 16, MUTED)


def ratio(numerator, denominator, label):
    return f"{numerator:,} / {denominator:,} {label}" if denominator else f"{label} —"


def draw_group_overview(c, groups):
    c.panel((34, 116, 1406, 169 + 34 * len(groups)))
    columns = (480, 605, 769, 956, 1158, 1372)
    headers = ("温度", "已评分", "平均 Rating", "一致率", "5% 恶手率", "10% 恶手率")
    c.text(70, 142, "模型 / 指标口径", 17, MUTED, "bold")
    for x, label in zip(columns, headers, strict=True):
        c.text(x, 142, label, 17, MUTED, "bold", "rm")
    for i, (key, group) in enumerate(groups.items()):
        y = 173 + 34 * i
        model, temperature, schema = key
        summary = group["summary"]
        if i % 2 == 0:
            c.panel((48, y - 15, 1392, y + 15), "#f3f6fa", radius=6)
        c.dot(67, y, PALETTE[i % len(PALETTE)], 4)
        c.text(82, y, f"{model} / {schema}", 16, width=340)
        values = (
            "—" if temperature is None else str(temperature),
            str(group["count"]),
            number(summary["mean_rating"]),
            number(summary["agreement"], "%"),
            number(summary["bad5_rate"], "%"),
            number(summary["bad10_rate"], "%"),
        )
        for x, label in zip(columns, values, strict=True):
            c.text(x, y, label, 17, INK, "number", "rm")


def axis_bounds(values, *, rates=False):
    if rates:
        peak = max(values, default=0)
        step = 2 if peak < 8 else 5 if peak < 25 else 10 if peak < 50 else 20
        return 0, min(100, max(10, step * math.ceil((peak + step / 3) / step))), step
    if not values:
        return 70, 100, 5
    low, high = min(values), max(values)
    span = high - low
    step = 5 if span <= 30 else 10 if span <= 60 else 20
    top = min(100, max(2 * step, step * math.ceil((high + 1) / step)))
    bottom = max(0, min(top - 2 * step, step * math.floor((low - 1) / step)))
    return bottom, top, step


def draw_axes(c, box, px, py, ticks, limits, *, percent=False):
    left, top, right, bottom = box
    low, high, step = limits
    for index in ticks:
        c.line(((px(index), top), (px(index), bottom)), GRID, dashed=True)
    for value in range(low, high + 1, step):
        y = py(value)
        c.line(((left, y), (right, y)), GRID)
        c.text(left - 12, y, f"{value}%" if percent else value, 15, MUTED, "number", "rm")


def draw_trace(c, values, px, py, color, *, dashed=False):
    segment = []
    for index, value in enumerate(values, 1):
        if value is None:
            if len(segment) > 1:
                c.line(segment, color, 1.8, dashed=dashed)
            segment = []
        else:
            segment.append((px(index), py(value)))
    if len(segment) > 1:
        c.line(segment, color, 1.8, dashed=dashed)


def rolling_rating(values):
    result = [None] * len(values)
    for i in range(4, len(values)):
        window = values[i - 4 : i + 1]
        if None not in window:
            result[i] = sum(window) / 5
    return result


def overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def crosses_trace(bounds, traces):
    left, top, right, bottom = bounds
    for (x1, y1), (x2, y2) in traces:
        if x1 >= right or x2 <= left:
            continue
        ys = [y1 + (y2 - y1) * (x - x1) / (x2 - x1) for x in (max(x1, left), min(x2, right))]
        if min(ys) < bottom and max(ys) > top:
            return True
    return False


def place_labels(points, traces, point_boxes, box):
    left, top, right, bottom = box
    taken, placed = [], []
    for _, x, y, _, _, below, half_width in points:
        nearby = [b for b in taken + point_boxes if b[0] < x + 38 and b[2] > x - 38]
        segments = [pair for pair in traces if pair[0][0] < x + 38 and pair[1][0] > x - 38]
        candidates = []
        for horizontal in (0, -3, 3, -7, 7, -11, 11):
            xx = max(left - 9 + half_width, min(right + 9 - half_width, x + horizontal))
            for distance in (11, 15, 19, 23, 28, 34, 42, 52):
                for direction in (1, -1):
                    offset = distance * direction
                    yy = y + offset
                    if not top + 6 <= yy <= bottom - 6:
                        continue
                    bounds = (xx - half_width, yy - 6.8, xx + half_width, yy + 6.8)
                    if any(overlaps(bounds, b) for b in nearby):
                        continue
                    cost = distance + 0.9 * abs(x - xx) + 5 * ((direction == 1) != below)
                    cost += 8 * crosses_trace(bounds, segments)
                    candidates.append((cost, xx, yy, bounds))
        if not candidates:
            return None
        _, xx, yy, bounds = min(candidates)
        taken.append(bounds)
        placed.append((xx, yy))
    return placed


def stagger_labels(points, px, box):
    """Reserve distinct rows for nearby labels when the local placements are full."""
    left, top, right, bottom = box
    widest = 2 * max(point[-1] for point in points)
    spacing = px(2) - px(1)
    stride = max(1, math.ceil((widest + 12) / spacing))
    step = 14
    middle = sorted(point[2] for point in points)[len(points) // 2]
    span = (2 * stride - 1) * step
    start = max(top + 8, min(bottom - 8 - span, middle - span / 2))
    return [
        (
            max(left - 9 + width, min(right + 9 - width, x)),
            start + ((index - 1) % stride + stride * below) * step,
        )
        for index, x, _, _, _, below, width in points
    ]


def draw_labels(c, series, px, py, box):
    font = face(23, "number")
    points, traces, point_boxes = [], [], []
    for values, color, below in series:
        step = 2 if len(values) > 50 else 1
        previous = None
        for index, value in enumerate(values, 1):
            if value is None:
                previous = None
                continue
            x, y = px(index), py(value)
            if previous is not None:
                traces.append((previous, (x, y)))
            previous = (x, y)
            point_boxes.append((x - 4, y - 4, x + 4, y + 4))
            if (index - 1) % step:
                continue
            label = f"{value:.1f}"
            width = c.draw.textlength(label, font=font) / (2 * c.scale) + 1.35
            points.append((index, x, y, label, color, below, width))
    if not points:
        return
    points.sort(key=lambda point: point[0])
    placed = place_labels(points, traces, point_boxes, box)
    if placed is None:
        placed = stagger_labels(points, px, box)

    for (_, x, y, *_), (xx, yy) in zip(points, placed, strict=True):
        if abs(y - yy) >= 23 or abs(x - xx) >= 8:
            c.line(((x, y), (xx, yy)), "#aebed0", 0.75)
    for (_, _, _, label, color, *_), (xx, yy) in zip(points, placed, strict=True):
        c.draw.text(
            c.box((xx, yy)),
            label,
            fill=color,
            font=font,
            anchor="mm",
            stroke_width=c.scale,
            stroke_fill="white",
        )


def draw_plot(c, groups, rows, dates, ticks, top, quartiles):
    from .batch_chart import GOLD_RATING, RAINBOW_RATING, rating_color

    left, right = 98, 1354

    def px(index):
        return left + (index - 1) * (right - left) / max(1, len(rows) - 1)

    sole = next(iter(groups.values())) if len(groups) == 1 else None
    c.panel((34, top, 1406, top + 666))
    c.text(57, top + 35, "Rating 与一致率", 22, INK, "bold")
    c.text(57, top + 361, "恶手率", 22, INK, "bold")

    for i, (field, label, color) in enumerate(METRICS):
        y = top + (35 if i < 2 else 361)
        x = 329 + (i % 2) * 205
        dashed = not sole and i % 2 == 1
        c.line(((x, y), (x + 20, y)), color if sole else MUTED, 2, dashed=dashed)
        if dashed:
            c.dot(x + 11, y, MUTED, 3.5, hollow=True)
        mean = sole["summary"].get("mean_rating" if field == "rating" else field) if sole else None
        suffix = "" if field == "rating" else "%"
        c.text(
            x + 30, y, label + (f"  {number(mean, suffix)}" if mean is not None else ""), 17, MUTED
        )
    if sole:
        c.dot(1050, top + 35, "#dfa037", 4.8)
        c.text(1063, top + 35, "90+", 16, MUTED)
        c.dot(1131, top + 35, PURPLE, 4.8, rainbow=True)
        c.text(1145, top + 35, f"{sole['threshold']}+", 16, MUTED)
        c.line(((1230, top + 35), (1252, top + 35)), MUTED, dashed=True)
        c.text(1261, top + 35, "全样本均值", 16, MUTED)
    elif groups:
        c.text(1375, top + 35, "颜色对应上方模型分组", 17, MUTED, anchor="rm")
    if quartiles:
        c.panel((1000, top + 324, 1026, top + 340), "#e9f0fd", radius=4)
        c.text(
            1037, top + 332, f"Rating 中间 50%   {quartiles[0]:.2f}–{quartiles[1]:.2f}", 16, MUTED
        )

    last_date = None
    for index in ticks:
        date = dates[index - 1]
        if date and date != last_date:
            c.text(px(index), top + 78, date.strftime("%m/%d"), 15, MUTED, "number", "mm")
            last_date = date
        c.text(px(index), top + 623, index, 15, MUTED, "number", "mm")
    c.text(right, top + 646, "对局序号", 15, MUTED, anchor="rm")

    panels = ((METRICS[:2], top + 100, top + 321), (METRICS[2:], top + 395, top + 597))
    for fields, start, end in panels:
        box = (left, start, right, end)
        rates = fields[0][0] == "bad5_rate"
        values = [
            value
            for row in rows
            for field, _, _ in fields
            if (value := metric_value(row, field)) is not None
        ]
        limits = axis_bounds(values, rates=rates)
        low, high, _ = limits

        def py(value, bottom=end, height=end - start, floor=low, span=high - low):
            return bottom - (value - floor) * height / span

        if not rates and sole:
            for threshold, tint in ((90, "#fffcf4"), (sole["threshold"], "#faf6ff")):
                if high > threshold:
                    c.rect((left, start, right, py(max(low, threshold))), tint)
            if quartiles:
                c.rect((left, py(quartiles[1]), right, py(quartiles[0])), "#e9f0fd")
        draw_axes(c, box, px, py, ticks, limits, percent=rates)
        labels, dots, rating_dots = [], [], []
        for i, (key, group) in enumerate(groups.items()):
            for j, (field, _, field_color) in enumerate(fields):
                color = field_color if sole else PALETTE[i % len(PALETTE)]
                sequence = [
                    metric_value(row, field) if metric_group(row) == key else None for row in rows
                ]
                if sole:
                    mean = group["summary"].get("mean_rating" if field == "rating" else field)
                    if mean is not None and low <= mean <= high:
                        c.line(((left, py(mean)), (right, py(mean))), color, 1.1, dashed=True)
                labels.append((sequence, color, field in {"agreement", "bad5_rate"}))
                if field == "rating":
                    moving = rolling_rating(sequence)
                    if any(
                        a is not None and b is not None
                        for a, b in zip(moving, moving[1:], strict=False)
                    ):
                        draw_trace(c, moving, px, py, "#7c8da4" if sole else color)
                        if sole:
                            c.line(((99, top + 332), (119, top + 332)), "#7c8da4", 1.8)
                            c.text(128, top + 332, "5 场均线", 15, MUTED)
                draw_trace(c, sequence, px, py, color, dashed=not sole and j == 1)
                for index, value in enumerate(sequence, 1):
                    if value is None:
                        continue
                    special = (
                        rating_color(value, games=group["count"]) if field == "rating" else None
                    )
                    target = rating_dots if field == "rating" else dots
                    target.append((px(index), py(value), color, special, not sole and j == 1))
        for x, y, color, special, hollow in dots + rating_dots:
            c.dot(
                x,
                y,
                "#dfa037" if special == GOLD_RATING else color,
                4.8 if special in {GOLD_RATING, RAINBOW_RATING} else 2.7,
                rainbow=special == RAINBOW_RATING,
                hollow=hollow,
            )
        draw_labels(c, labels, px, py, box)
        if not values:
            c.text(720, (start + end) / 2, "暂无分析数据", 20, MUTED, anchor="mm")


def draw_footer(c, group, top):
    c.panel((34, top, 1406, top + 141))
    for x in (489, 950):
        c.line(((x, top + 23), (x, top + 119)), GRID)
    count = group.get("count", 0)
    if count:
        best, worst = group["best"], group["worst"]
        extrema = f"最高 {best[1]:.2f}    最低 {worst[1]:.2f}"
        spread = f"第 {best[0]} / {worst[0]} 场    标准差 {number(group['deviation'])}"
        high_scores = (
            f"90+   {group['gold_count']} / {count} 场（{group['gold_count'] / count:.0%}）"
        )
        threshold = group["threshold"]
        rainbow = group["rainbow_count"]
        rainbow_scores = f"{threshold}+   {rainbow} / {count} 场（{rainbow / count:.0%}）"
    else:
        extrema = spread = high_scores = rainbow_scores = "—"

    recent = group.get("recent")
    window = recent["window"] if recent else None
    trend = f"Rating {delta(recent['mean_rating'])}" if recent else "—"
    rate_changes = (
        f"一致 {delta(recent['agreement'])}　5% {delta(recent['bad5_rate'])}　"
        f"10% {delta(recent['bad10_rate'])} pp"
        if recent
        else "样本不足"
    )
    for x, title, value, detail in (
        (57, "评分分布", extrema, spread),
        (514, f"近 {window} 场较前 {window} 场" if window else "近期变化", trend, rate_changes),
        (975, "高分占比", high_scores, rainbow_scores),
    ):
        c.text(x, top + 30, title, 17, MUTED, "bold")
        c.text(x, top + 70, value, 24, INK, "number" if value.isascii() else "bold")
        c.text(x, top + 108, detail, 16, MUTED, width=419)


def render_dashboard(job):
    data = batch_insights(job["rows"])
    rows, groups = data["rows"], data["groups"]
    mixed = len(groups) > 1
    plot_top = 273 if not mixed else 187 + 34 * len(groups)
    plot_end = plot_top + 666
    c = Canvas(plot_end + (47 if mixed else 201))
    name = job.get("nickname") or "Mortal"
    room = room_label(rows)
    title = "  /  ".join(filter(None, (name, room, "Mortal 复盘")))
    c.text(34, 45, title, 35, INK, "title", width=1090)
    c.panel((1177, 25, 1406, 65), "#e5edf9", radius=12)
    c.text(1291, 45, f"{data['completed']} / {data['total']} 场完成", 18, BLUE, "bold", "mm")

    dates = [game_date(row) for row in rows]
    dated = [date for date in dates if date is not None]
    period = (
        " 至 ".join(f"{date:%Y.%m.%d}" for date in sorted({min(dated), max(dated)}))
        if dated
        else ""
    )
    model = (
        f"{len(groups)} 组模型 / 口径"
        if mixed
        else model_label(next(iter(groups)))
        if groups
        else ""
    )
    c.text(36, 85, "    ".join(filter(None, (period, model))), 18, MUTED, width=1060)
    if data["failed"]:
        c.text(1406, 85, f"{data['failed']} 场失败", 17, CORAL, anchor="rm")

    quartiles = None
    if mixed:
        draw_group_overview(c, groups)
    else:
        draw_overview(c, groups)
        group = next(iter(groups.values()), {})
        if group.get("count", 0) >= 4:
            quartiles = quantiles([p[1] for p in group["points"]], n=4, method="inclusive")[::2]

    count = len(rows)
    tick_count = min(count, 10)
    ticks = [1 + round(i * (count - 1) / max(1, tick_count - 1)) for i in range(tick_count)]
    draw_plot(c, groups, rows, dates, ticks, plot_top, quartiles)
    if not mixed:
        draw_footer(c, group, plot_end + 18)
    c.text(1406, c.height - 21, job.get("id", ""), 14, MUTED, "number", "rm")
    return c.png()
