"""Compact, annotated multi-game Mortal reports drawn from saved results only."""

import io
import math

from PIL import Image, ImageDraw, ImageFilter

from .batch_insights import batch_insights, metric_group, metric_value
from .typography import face

INK = "#22324b"
MUTED = "#8190a5"
BLUE = "#4c83d4"
TEAL = "#138b83"
AMBER = "#dfa037"
CORAL = "#d97978"
PURPLE = "#a477d5"
PALETTE = (BLUE, PURPLE, TEAL, CORAL)


class Canvas:
    scale = 2

    def __init__(self):
        self.image = Image.new("RGBA", (2880, 2280), "#f2f5fa")
        self.draw = ImageDraw.Draw(self.image)

    def box(self, box):
        return tuple(round(v * self.scale) for v in box)

    def panel(self, box):
        self.draw.rounded_rectangle(self.box(box), radius=22 * self.scale, fill="white")

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
        value = str(value)
        while width and self.draw.textlength(value, font=face(size * 2, style)) > width * 2:
            value = value[:-2] + "…"
        self.draw.text(
            (round(x * 2), round(y * 2)),
            value,
            font=face(size * 2, style),
            fill=color,
            anchor=anchor,
        )

    def dot(self, x, y, color, radius=3.4, *, rainbow=False):
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
            self.draw.ellipse(box, fill=color)
        if radius > 4:
            self.draw.ellipse(self.box((x - 1.6, y - 1.6, x + 1.6, y + 1.6)), fill="white")

    def rating(self, x, y, value, games):
        from .batch_chart import GOLD_RATING, RAINBOW_RATING, rating_color

        label = number(value)
        color = rating_color(value, games=games)
        if color not in {GOLD_RATING, RAINBOW_RATING}:
            self.text(x, y, label, 62, INK, "number")
            return
        font = face(124, "number")
        width = math.ceil(self.draw.textlength(label, font=font)) + 32
        mask = Image.new("L", (width, 164))
        ImageDraw.Draw(mask).text((16, 16), label, font=font, fill=255, anchor="lt")
        bounds = mask.getbbox()
        if bounds is None:
            return
        stops = (
            (
                (255, 128, 218),
                (199, 151, 255),
                (101, 190, 255),
                (37, 211, 235),
                (62, 217, 166),
                (255, 189, 76),
                (255, 160, 120),
            )
            if color == RAINBOW_RATING
            else (
                (224, 154, 18),
                (255, 211, 60),
                (255, 244, 170),
                (255, 205, 45),
                (242, 173, 18),
                (216, 145, 11),
            )
        )
        gradient = Image.new("RGBA", mask.size)
        painter = ImageDraw.Draw(gradient)
        axis = 0 if color == RAINBOW_RATING else 1
        for i in range(mask.size[axis]):
            position = min(1, max(0, (i - bounds[axis]) / max(1, bounds[axis + 2] - bounds[axis])))
            position *= len(stops) - 1
            index = min(len(stops) - 2, int(position))
            weight = position - index
            rgb = tuple(
                round(a + (b - a) * weight)
                for a, b in zip(stops[index], stops[index + 1], strict=True)
            )
            painter.line(
                (i, 0, i, mask.height) if axis == 0 else (0, i, width, i), fill=(*rgb, 255)
            )
        shadow = Image.new("RGBA", mask.size, "#cbb2df" if axis == 0 else "#daa548")
        shadow.putalpha(mask.filter(ImageFilter.GaussianBlur(2)).point(lambda p: round(p * 0.35)))
        gradient.putalpha(mask)
        shadow.alpha_composite(gradient)
        self.image.alpha_composite(
            shadow, (round(x * 2 - 16), round(y * 2 - (bounds[1] + bounds[3]) / 2))
        )

    def png(self):
        output = io.BytesIO()
        self.image.convert("RGB").resize((1440, 1140), Image.Resampling.LANCZOS).save(
            output, format="PNG", optimize=True
        )
        return output.getvalue()


def number(value, suffix=""):
    return "—" if value is None else f"{value:.2f}{suffix}"


def delta(value):
    return "—" if value is None else "0.00" if abs(value) < 0.005 else f"{value:+.2f}"


def coordinates(box, count, low, high):
    left, top, right, bottom = box
    return (
        lambda index: left + (index - 1) * (right - left) / max(1, count - 1),
        lambda value: bottom - (value - low) * (bottom - top) / max(1, high - low),
    )


def axes(c, box, count, low, high, ticks):
    left, top, right, bottom = box
    px, py = coordinates(box, count, low, high)
    for value in ticks:
        if low <= value <= high:
            y = py(value)
            c.line(((left, y), (right, y)), "#e9eef5", 0.8)
            c.text(left - 16, y, str(value), 15, MUTED, "number", anchor="rm")
    spacing = 20 if count > 60 else 10 if count > 20 else 5 if count > 10 else 1
    xticks = sorted({1, count, *range(spacing, count + 1, spacing)})
    for i in xticks:
        if count > 1 and i != count and abs(px(i) - px(count)) < 28:
            continue
        c.text(px(i), bottom + 22, str(i), 15, MUTED, "number", anchor="mm")
    return px, py


def draw_series(c, rows, key, field, box, low, high, color, *, width=2.2, dashed=False):
    px, py = coordinates(box, len(rows), low, high)
    segment = []
    for i, row in enumerate(rows, 1):
        value = metric_value(row, field) if metric_group(row) == key else None
        if value is None:
            if len(segment) > 1:
                c.line(segment, color, width, dashed=dashed)
            segment = []
            continue
        point = (px(i), py(value))
        segment.append(point)
        c.dot(*point, color, 2.5)
    if len(segment) > 1:
        c.line(segment, color, width, dashed=dashed)


def annotation(c, x, y, value, label, *, above):
    bx = min(1280, max(160, x))
    by = min(610, max(416, y - 38 if above else y + 38))
    c.line(((x, y), (bx, by)), "#b8c6d8")
    c.draw.rounded_rectangle(
        c.box((bx - 78, by - 18, bx + 78, by + 18)), radius=10 * 2, fill="#f3f6fb"
    )
    c.text(bx, by, f"{label} {value:.2f}", 17, INK, "bold", anchor="mm")


def render_dashboard(job):
    from .batch_chart import GOLD_RATING, RAINBOW_RATING, rating_color

    data = batch_insights(job["rows"])
    rows, groups = data["rows"], data["groups"]
    sole = next(iter(groups.values())) if len(groups) == 1 else None
    summary = sole["summary"] if sole else {}
    c = Canvas()
    c.text(40, 49, job.get("nickname", "Mortal"), 34, INK, "bold", width=1000)
    dates = [r.get("started_at", "")[:10] for r in rows if r.get("started_at")]
    date_start = 0 if dates and min(dates)[:4] != max(dates)[:4] else 5
    period = (
        f"{min(dates)[date_start:].replace('-', '.')}—{max(dates)[date_start:].replace('-', '.')}"
        if dates
        else ""
    )
    models = " · ".join(dict.fromkeys(str(key[0]) for key in groups))
    subtitle = [
        f"Mortal · {len(rows)}场复盘",
        period,
        models if len(groups) < 2 else f"{len(groups)}组模型",
    ]
    c.text(40, 90, "   ·   ".join(s for s in subtitle if s), 18, MUTED, width=1200)
    c.draw.rounded_rectangle(c.box((1190, 32, 1400, 79)), radius=15 * 2, fill="#e6edf9")
    c.text(1295, 56, f"{data['completed']} / {data['total']} 已完成", 20, BLUE, "bold", anchor="mm")

    c.panel((40, 122, 1400, 302))
    c.text(66, 153, "平均 Rating" if len(groups) < 2 else "分组评分", 19, MUTED)
    c.rating(66, 222, summary.get("mean_rating"), sole["count"] if sole else 0)
    c.text(66, 276, "中位 " + number(summary.get("median_rating")), 16, MUTED)
    c.line(((330, 149), (330, 276)), "#e9eef5")
    decisions = summary.get("decisions")
    # The fraction is the same matches/total field used by the single-game card.
    matched = sum((r.get("metrics") or {}).get("matches", 0) for r in sole["rows"]) if sole else 0
    fraction = f"{matched} / {decisions}" if decisions else "—"
    for x, label, value, color in (
        (362, "AI 一致率", number(summary.get("agreement"), "%"), TEAL),
        (614, "≤5% 恶手率", number(summary.get("bad5_rate"), "%"), AMBER),
        (866, "≤10% 恶手率", number(summary.get("bad10_rate"), "%"), CORAL),
        (1118, "切牌次数", fraction, INK),
    ):
        c.text(x, 164, label, 18, MUTED)
        size = 26 if len(value) > 12 else 32
        c.text(x, 221, value, size, color, "number", width=246)
    c.text(1368, 276, f"失败 {data['failed']} 场" if data["failed"] else "", 16, CORAL, anchor="rm")

    c.panel((40, 324, 1400, 700))
    c.text(66, 355, "评分走势", 23, INK, "bold")
    c.line(((1024, 355), (1054, 355)), "#334e75", 3)
    c.text(1065, 355, "5场均线", 16, MUTED)
    c.dot(1193, 355, AMBER, 4)
    c.text(1205, 355, "90+", 16, MUTED)
    threshold = sole["threshold"] if sole else 94
    c.dot(1285, 355, PURPLE, 5, rainbow=True)
    c.text(1297, 355, f"{threshold}+", 16, MUTED)
    points = [p for group in groups.values() for p in group["points"]]
    values = [p[1] for p in points]
    low = max(0, math.floor(min(values) - 2)) if values else 70
    high = min(100, max(low + 6, math.ceil(max(values) + 2))) if values else 100
    plot = (84, 401, 1356, 644)
    px, py = coordinates(plot, len(rows), low, high)
    if sole:
        if high > 90:
            c.rect((plot[0], plot[1], plot[2], py(max(low, 90))), "#fffcf4")
        if high > threshold:
            c.rect((plot[0], plot[1], plot[2], py(max(low, threshold))), "#faf6ff")
    axes(c, plot, len(rows), low, high, range(0, 101, 5))
    for index, (key, group) in enumerate(groups.items()):
        color = PALETTE[index % len(PALETTE)]
        draw_series(c, rows, key, "rating", plot, low, high, color)
        moving = []
        for i in range(4, len(rows)):
            window = rows[i - 4 : i + 1]
            sample = [metric_value(r, "rating") if metric_group(r) == key else None for r in window]
            if all(value is not None for value in sample):
                moving.append((px(i + 1), py(sum(sample) / 5)))
            else:
                if len(moving) > 1:
                    c.line(moving, "#334e75" if sole else color, 3.5)
                moving = []
        if len(moving) > 1:
            c.line(moving, "#334e75" if sole else color, 3.5)
        for i, value, _ in group["points"]:
            special = rating_color(value, games=group["count"])
            c.dot(
                px(i),
                py(value),
                AMBER if special == GOLD_RATING else color,
                4.8 if special in {GOLD_RATING, RAINBOW_RATING} else 3.1,
                rainbow=special == RAINBOW_RATING,
            )
    if sole and sole["best"]:
        best, worst = sole["best"], sole["worst"]
        annotation(c, px(best[0]), py(best[1]), best[1], "最高", above=True)
        if worst[0] != best[0]:
            annotation(c, px(worst[0]), py(worst[1]), worst[1], "最低", above=False)
    elif not points:
        c.text(720, 515, "等待分析", 24, MUTED, anchor="mm")
    if len(groups) > 1:
        for i, key in enumerate(list(groups)[:4]):
            c.dot(86 + i * 312, 682, PALETTE[i % len(PALETTE)])
            c.text(98 + i * 312, 682, f"{key[0]} / T={key[1]}", 15, MUTED, width=285)

    c.panel((40, 720, 700, 930))
    c.panel((720, 720, 1400, 930))
    c.text(66, 750, "AI 一致率", 21, INK, "bold")
    c.text(746, 750, "恶手率", 21, INK, "bold")
    c.line(((1120, 750), (1144, 750)), AMBER if sole else INK, 2.5)
    c.text(1154, 750, "≤5%", 15, MUTED)
    c.line(((1254, 750), (1278, 750)), CORAL if sole else INK, 2.5, dashed=True)
    c.text(1288, 750, "≤10%", 15, MUTED)
    agree_box, bad_box = (84, 789, 660, 882), (766, 789, 1360, 882)
    agree_values = [v for r in rows if (v := metric_value(r, "agreement")) is not None]
    agree_low = max(0, math.floor((min(agree_values) - 5) / 10) * 10) if agree_values else 0
    agree_high = (
        min(100, max(agree_low + 10, math.ceil((max(agree_values) + 5) / 10) * 10))
        if agree_values
        else 100
    )
    bad_values = [
        v
        for r in rows
        for field in ("bad5_rate", "bad10_rate")
        if (v := metric_value(r, field)) is not None
    ]
    bad_high = min(100, max(5, math.ceil(max(bad_values) / 5) * 5)) if bad_values else 20
    axes(c, agree_box, len(rows), agree_low, agree_high, range(0, 101, 10))
    axes(c, bad_box, len(rows), 0, bad_high, range(0, 101, 5))
    for i, key in enumerate(groups):
        draw_series(
            c,
            rows,
            key,
            "agreement",
            agree_box,
            agree_low,
            agree_high,
            TEAL if sole else PALETTE[i % 4],
        )
        draw_series(
            c, rows, key, "bad5_rate", bad_box, 0, bad_high, AMBER if sole else PALETTE[i % 4]
        )
        draw_series(
            c,
            rows,
            key,
            "bad10_rate",
            bad_box,
            0,
            bad_high,
            CORAL if sole else PALETTE[i % 4],
            dashed=True,
        )

    for box in ((40, 950, 480, 1086), (500, 950, 940, 1086), (960, 950, 1400, 1086)):
        c.panel(box)
    c.text(66, 976, "近期变化", 18, MUTED)
    recent = sole["recent"] if sole else None
    if recent:
        shift = recent["mean_rating"]
        c.text(
            66,
            1016,
            f"近{recent['window']}场  {delta(shift)}",
            27,
            MUTED if abs(shift) < 0.005 else TEAL if shift > 0 else CORAL,
            "bold",
        )
        c.text(454, 976, f"较前{recent['window']}场", 15, MUTED, anchor="rm")
        c.text(
            66,
            1059,
            f"一致率 {delta(recent['agreement'])}pp  ·  ≤5% {delta(recent['bad5_rate'])}pp",
            15,
            MUTED,
            width=388,
        )
    else:
        c.text(66, 1016, "—" if len(groups) < 2 else "分组统计", 27, INK, "bold")
    c.text(526, 976, "高分占比", 18, MUTED)
    count = sole["count"] if sole else 0
    if count:
        c.text(526, 1016, f"90+  {sole['gold_count']} / {count}", 27, AMBER, "number")
        c.text(
            526,
            1059,
            f"{threshold}+  {sole['rainbow_count']}场  ·  {sole['rainbow_count'] / count:.0%}",
            16,
            MUTED,
        )
    else:
        c.text(526, 1016, "—", 27)
    c.text(986, 976, "评分波动", 18, MUTED)
    c.text(986, 1016, "标准差  " + number(sole["deviation"] if sole else None), 27, INK, "bold")
    c.text(986, 1059, "中位 " + number(summary.get("median_rating")), 16, MUTED)
    c.text(42, 1118, job.get("id", ""), 15, MUTED)
    return c.png()
