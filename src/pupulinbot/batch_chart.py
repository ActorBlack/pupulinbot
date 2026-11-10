"""Render batch results as an inline QQ chart, preserving gaps and model boundaries."""

import io
import math
import os
import threading
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path("data/chart-cache").resolve()))

from matplotlib import font_manager as font_manager  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure as Figure  # noqa: E402

_RENDER_LOCK = threading.Lock()


def chart_series(job: dict) -> tuple[list[dict], dict[tuple, list[float]]]:
    rows = sorted(job["rows"], key=lambda row: row.get("started_at") or "")
    groups = {}
    for row in rows:
        metric = row.get("metrics") or {}
        if row["status"] == "done":
            key = (metric.get("model", "未标注模型"), metric.get("temperature"))
            groups.setdefault(key, [math.nan] * len(rows))
    for index, row in enumerate(rows):
        metric = row.get("metrics") or {}
        value = metric.get("rating")
        if row["status"] != "done" or value is None:
            continue
        value = float(value)
        if math.isfinite(value):
            key = (metric.get("model", "未标注模型"), metric.get("temperature"))
            groups[key][index] = value
    return rows, groups


def render_batch_chart(job: dict) -> bytes:
    from .batch_review import require_tenhou_names

    require_tenhou_names(job)
    with _RENDER_LOCK:
        if job.get("kind") == "table":
            return _render_table_chart(job)
        if len(job["rows"]) == 1:
            return _render_single_card(job)
        return _render_batch_chart(job)


def _render_single_card(job: dict) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    from .batch_review import seat_nickname
    from .typography import face

    row = job["rows"][0]
    metric = (row.get("metrics") or {}) if row.get("status") == "done" else {}
    image = Image.new("RGBA", (1000, 280), "#f2f5fa")
    shadow = Image.new("RGBA", image.size)
    painter = ImageDraw.Draw(shadow)
    for i in range(5):
        x = 28 + 192 * i
        painter.rounded_rectangle((x, 109, x + 176, 229), 18, fill=(34, 50, 75, 14))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(image)
    ink, muted = "#22324b", "#7f8da3"
    draw.rounded_rectangle((28, 29, 33, 65), 2, fill="#4479d7")
    draw.text((48, 46), "Mortal · 复盘", font=face(32, "title"), fill=ink, anchor="lm")
    name = seat_nickname(row)
    if name in {"—", "昵称读取中"}:
        name = job.get("nickname", "")
        if name in {"导入牌谱", "同桌四人"}:
            name = ""
    name_font = face(24, "bold")
    while draw.textlength(name, font=name_font) > 530:
        name = name[:-2] + "…"
    draw.text((972, 46), name, font=name_font, fill=ink, anchor="rm")
    cards = (
        ("Rating", "rating", ink),
        ("AI 一致率", "agreement", "#138b83"),
        ("≤5% 恶手率", "bad5_rate", "#d9952e"),
        ("≤10% 恶手率", "bad10_rate", "#d57773"),
        ("切牌次数", "matches", ink),
    )
    for i, (label, key, color) in enumerate(cards):
        x = 28 + 192 * i
        draw.rounded_rectangle((x, 104, x + 176, 224), 18, fill="white")
        draw.text((x + 16, 130), label, font=face(18), fill=muted, anchor="lm")
        value = metric.get(key)
        formatted = "—"
        if key == "matches":
            total = metric.get("decisions")
            if type(value) is int and type(total) is int and 0 <= value <= total and total > 0:
                formatted = f"{value} / {total}"
        elif type(value) in (int, float) and math.isfinite(value):
            formatted = f"{value:.2f}" + ("%" if key != "rating" else "")
        if key == "rating" and rating_color(value) in {GOLD_RATING, RAINBOW_RATING}:
            _draw_shiny_rating(
                image, x + 88, 177, formatted, rainbow=rating_color(value) == RAINBOW_RATING
            )
        else:
            size = 32
            while draw.textlength(formatted, font=face(size, "number")) > 144 and size > 18:
                size -= 1
            draw.text((x + 88, 177), formatted, font=face(size, "number"), fill=color, anchor="mm")
    model = metric.get("model")
    if model:
        draw.text((32, 256), str(model), font=face(16), fill=muted, anchor="lm")
    draw.text((968, 256), job.get("id", ""), font=face(16), fill=muted, anchor="rm")
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def _render_batch_chart(job: dict) -> bytes:
    from .batch_dashboard import render_dashboard

    return render_dashboard(job)


GOLD_RATING = "#f2ba28"
RAINBOW_RATING = "#a855f7"

TABLE_HEADERS = ("游戏昵称", "最终名次", "Rating", "一致率", "≤5% 恶手率", "≤10% 恶手率")


def rating_color(value, *, games=1):
    from .batch_insights import rainbow_threshold

    if type(value) in (int, float) and math.isfinite(value) and value >= rainbow_threshold(games):
        return RAINBOW_RATING
    if type(value) in (int, float) and math.isfinite(value) and value >= 90:
        return GOLD_RATING
    return "#22324b"


def table_rows(job):
    from .batch_review import seat_nickname

    def rank(row):
        value = row.get("rank")
        return value if type(value) is int and 1 <= value <= 4 else 5

    result = []
    for row in sorted(job["rows"], key=lambda r: (rank(r), r["seat"])):
        metrics = row.get("metrics") or {}
        values = []
        for key in ("rating", "agreement", "bad5_rate", "bad10_rate"):
            value = metrics.get(key) if row["status"] == "done" else None
            values.append(value if type(value) in (int, float) and math.isfinite(value) else None)
        result.append(
            {
                "name": seat_nickname(row),
                "rank": rank(row) if rank(row) < 5 else None,
                "values": values,
            }
        )
    return result


def render_batch_animation(job: dict) -> bytes:
    """Preview a gentle sweep of light across high-rating table cells."""
    from PIL import Image

    from .batch_review import require_tenhou_names

    require_tenhou_names(job)
    with _RENDER_LOCK:
        frames = [
            Image.open(io.BytesIO(_render_table_chart(job, shimmer_phase=i / 23))).convert("RGB")
            for i in range(24)
        ]
        palette = frames[12].quantize(colors=256)
        frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
        output = io.BytesIO()
        frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=[360, *([65] * 22), 360],
            loop=0,
            disposal=1,
            optimize=True,
        )
        return output.getvalue()


def _draw_shiny_rating(image, x, y, value, *, rainbow=False, shimmer_phase=None):
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    from .typography import face

    draw = ImageDraw.Draw(image)
    font = face(32, "number")
    width = math.ceil(draw.textlength(value, font=font))
    size = (width + 90, 76) if rainbow else (width + 44, 66)
    center = (size[0] / 2, size[1] / 2)
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).text(center, value, font=font, fill=255, anchor="mm")
    bounds = mask.getbbox()
    if bounds is None:
        return
    glow = Image.new("RGBA", size, "#bc92ff" if rainbow else "#ffcd40")
    glow.putalpha(mask.filter(ImageFilter.GaussianBlur(3)).point(lambda alpha: int(alpha * 0.42)))
    outline = Image.new("RGBA", size)
    ImageDraw.Draw(outline).text(
        center,
        value,
        font=font,
        fill="#9571c6" if rainbow else "#d39412",
        stroke_width=1,
        stroke_fill="#ffffff" if rainbow else "#cf941c",
        anchor="mm",
    )
    stops = (
        (0.0, (224, 154, 18)),
        (0.26, (255, 211, 60)),
        (0.43, (255, 244, 170)),
        (0.57, (255, 205, 45)),
        (0.82, (242, 173, 18)),
        (1.0, (216, 145, 11)),
    )
    if rainbow:
        stops = (
            (0.0, (255, 128, 218)),
            (0.17, (199, 151, 255)),
            (0.34, (101, 190, 255)),
            (0.50, (37, 211, 235)),
            (0.66, (62, 217, 166)),
            (0.83, (255, 189, 76)),
            (1.0, (255, 160, 120)),
        )
    gradient = Image.new("RGBA", size)
    painter = ImageDraw.Draw(gradient)
    axis = 0 if rainbow else 1
    for line in range(size[axis]):
        position = min(
            1, max(0, (line - bounds[axis]) / max(1, bounds[axis + 2] - bounds[axis] - 1))
        )
        for (start, first), (end, last) in zip(stops, stops[1:], strict=False):
            if start <= position <= end:
                weight = (position - start) / (end - start)
                color = tuple(round(a + (b - a) * weight) for a, b in zip(first, last, strict=True))
                segment = (line, 0, line, size[1]) if rainbow else (0, line, size[0], line)
                painter.line(segment, fill=(*color, 255))
                break
    if rainbow:
        # Keep the colored halo and ornaments inside this rating's table cell.
        halo = gradient.copy()
        halo.putalpha(mask.filter(ImageFilter.GaussianBlur(6)))
        glow = halo
        decoration = Image.new("RGBA", size)
        ornament = ImageDraw.Draw(decoration)
        cx, cy = center
        ornament.arc((18, 13, size[0] - 18, size[1] - 13), 30, 90, fill="#a7deef", width=1)

        def star(sx, sy, radius, color):
            points = []
            for point in range(8):
                angle = math.pi * point / 4 - math.pi / 2
                length = radius if point % 2 == 0 else radius * 0.22
                points.append((sx + math.cos(angle) * length, sy + math.sin(angle) * length))
            ornament.polygon(points, fill=color)
            ornament.ellipse((sx - 1, sy - 1, sx + 1, sy + 1), fill="white")

        for sx, sy, radius, color in (
            (cx - width / 2 - 17, cy + 7, 5, "#db9cf3"),
            (cx + width / 2 + 17, cy - 7, 6, "#a6acff"),
            (cx - 13, cy - 22, 3, "#efaddb"),
        ):
            star(sx, sy, radius, color)
        for dx, dy, color in ((19, -20, "#99dce9"), (-26, 22, "#cbb4f5")):
            ornament.ellipse((cx + dx - 1, cy + dy - 1, cx + dx + 1, cy + dy + 1), fill=color)
        star_glow = Image.new("RGBA", size, "#ce8bff")
        star_glow.putalpha(
            decoration.getchannel("A")
            .filter(ImageFilter.GaussianBlur(3))
            .point(lambda alpha: round(alpha * 0.65))
        )
        glow.alpha_composite(star_glow)
        glow.alpha_composite(decoration)
        sheen = Image.new("RGBA", size)
        shine = ImageDraw.Draw(sheen)
        # An even pearly highlight avoids washing out one side of the rainbow.
        highlight_y = bounds[1] + (bounds[3] - bounds[1]) * 0.3
        shine.rectangle(
            (0, highlight_y - 1, size[0], highlight_y + 2),
            fill=(255, 255, 255, 40),
        )
        gradient = Image.alpha_composite(gradient, sheen.filter(ImageFilter.GaussianBlur(2)))
        # The moving highlight is clipped to the digits, leaving the table still.
        if shimmer_phase is not None:
            sweep = Image.new("RGBA", size)
            sweep_draw = ImageDraw.Draw(sweep)
            sweep_x = bounds[0] - 36 + shimmer_phase * (bounds[2] - bounds[0] + 72)
            for dx in range(-15, 16):
                alpha = round(195 * math.exp(-((dx / 7) ** 2)))
                sweep_draw.line(
                    (sweep_x + dx - 12, 0, sweep_x + dx + 12, size[1]),
                    fill=(255, 255, 255, alpha),
                )
            gradient = Image.alpha_composite(gradient, sweep)
        # A colored rim and a shallow extrusion keep the original type size
        # while giving the rainbow the same raised finish as the gold score.
        rim_mask = mask.filter(ImageFilter.MaxFilter(3))
        outline = gradient.convert("RGB").point(lambda channel: round(channel * 0.9 + 8))
        outline = outline.convert("RGBA")
        outline.putalpha(rim_mask)
        depth = gradient.convert("RGB").point(lambda channel: round(channel * 0.85))
        depth = depth.convert("RGBA")
        depth.putalpha(rim_mask.point(lambda alpha: round(alpha * 0.35)))
        glow.alpha_composite(depth, (1, 1))
    gradient.putalpha(mask)
    glow.alpha_composite(outline)
    glow.alpha_composite(gradient)
    if rainbow:
        top_edge = ImageChops.subtract(mask, ImageChops.offset(mask, 0, 1))
        highlight = Image.new("RGBA", size, "#ffffff")
        highlight.putalpha(top_edge.point(lambda alpha: round(alpha * 0.8)))
        glow.alpha_composite(highlight)
        bottom_edge = ImageChops.subtract(mask, ImageChops.offset(mask, 0, -1))
        bevel = gradient.convert("RGB").point(lambda channel: round(channel * 0.85))
        bevel = bevel.convert("RGBA")
        bevel.putalpha(bottom_edge.point(lambda alpha: round(alpha * 0.18)))
        glow.alpha_composite(bevel)
    image.alpha_composite(glow, (round(x - size[0] / 2), round(y - size[1] / 2)))
    if rainbow:
        return
    for sx, sy, radius in ((x + width / 2 + 12, y - 10, 5), (x - width / 2 - 10, y + 10, 3)):
        draw.polygon(
            [
                (sx, sy - radius),
                (sx + radius * 0.28, sy - radius * 0.28),
                (sx + radius, sy),
                (sx + radius * 0.28, sy + radius * 0.28),
                (sx, sy + radius),
                (sx - radius * 0.28, sy + radius * 0.28),
                (sx - radius, sy),
                (sx - radius * 0.28, sy - radius * 0.28),
            ],
            fill="#ab7dea" if rainbow else "#f3c841",
        )


def _render_table_chart(job, *, shimmer_phase=None):
    from PIL import Image, ImageDraw, ImageFilter

    from .typography import face

    rows = table_rows(job)
    height = 660 + (40 if job.get("preview_note") else 0)
    image = Image.new("RGB", (1600, height), "#f2f5fa")
    shadow = Image.new("RGBA", image.size)
    ImageDraw.Draw(shadow).rounded_rectangle((40, 164, 1560, 592), radius=24, fill=(26, 47, 80, 20))
    image = Image.alpha_composite(
        image.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(13))
    )
    draw = ImageDraw.Draw(image)
    ink, muted = "#22324b", "#7f8da3"

    def text(x, y, value, size=26, color=ink, style="regular", anchor="mm"):
        draw.text((x, y), str(value), font=face(size, style), fill=color, anchor=anchor)

    def fit(value, width, size=28):
        while draw.textlength(value, font=face(size, "bold")) > width and size > 20:
            size -= 1
        while draw.textlength(value, font=face(size, "bold")) > width:
            value = value[:-2] + "…"
        return value, size

    draw.rounded_rectangle((64, 42, 71, 92), radius=3, fill="#4479d7")
    text(92, 69, "Mortal · 同桌复盘", 44, style="title", anchor="lm")
    sources = {r.get("source", "majsoul") for r in job["rows"]}
    source = (
        {"tenhou": "天凤", "majsoul": "雀魂", "riichi_city": "一番街"}.get(
            next(iter(sources)), "全桌"
        )
        if len(sources) == 1
        else "全桌"
    )
    text(1536, 75, source, 24, muted, anchor="rm")
    draw.rounded_rectangle((40, 156, 1560, 584), radius=24, fill="white")
    widths = (380, 152, 225, 225, 235, 235)
    edges = [72]
    for width in widths:
        edges.append(edges[-1] + width)
    centers = [(edges[i] + edges[i + 1]) / 2 for i in range(len(widths))]
    for i, label in enumerate(TABLE_HEADERS):
        text(
            88 if i == 0 else centers[i],
            192,
            label,
            23,
            "#63738c",
            "bold",
            "lm" if i == 0 else "mm",
        )
    draw.line((72, 225, 1524, 225), fill="#e6ecf4", width=2)
    themes = {
        1: ("#ae781c", "#fff3d8"),
        2: ("#607da9", "#ecf2fc"),
        3: ("#a17658", "#f9eee5"),
        4: ("#7b889b", "#eff2f6"),
    }
    for index, row in enumerate(rows):
        y = 269 + index * 80
        if index % 2:
            draw.rounded_rectangle((56, y - 37, 1544, y + 37), radius=12, fill="#f7f9fd")
        name, size = fit(row["name"], 340)
        text(88, y, name, size, style="bold", anchor="lm")
        rank = row["rank"]
        color, background = themes.get(rank, (muted, "#f0f3f7"))
        draw.rounded_rectangle(
            (centers[1] - 51, y - 23, centers[1] + 51, y + 23), radius=14, fill=background
        )
        text(centers[1], y, f"{rank} 位" if rank else "待确认", 25, color, "bold")
        for i, value in enumerate(row["values"]):
            label = "—" if value is None else f"{value:.2f}" + ("%" if i else "")
            if i == 0 and rating_color(value) in {GOLD_RATING, RAINBOW_RATING}:
                _draw_shiny_rating(
                    image,
                    centers[i + 2],
                    y,
                    label,
                    rainbow=rating_color(value) == RAINBOW_RATING,
                    shimmer_phase=shimmer_phase,
                )
            else:
                text(
                    centers[i + 2],
                    y,
                    label,
                    32 if i == 0 else 28,
                    rating_color(value) if i == 0 else ink,
                    "number",
                )
    if job.get("id"):
        text(80, 618, job["id"], 20, "#9aa6b8", anchor="lm")
    if job.get("preview_note"):
        text(80, 661, job["preview_note"], 18, "#b45309", anchor="lm")
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
