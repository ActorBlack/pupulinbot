"""Compact spectator cards with fixed numerals and source-aware statistics."""

import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .tenhou import CN, grade_name, rule_name
from .tenhou_profile import finite
from .typography import face

INK = "#1c2739"
MUTED = "#929dac"
COLORS = ("#e53027", "#2276ed", "#1d9344", "#8536da")

# The current badge and history strip use the same distinct dan palette.
RANK_THEMES = {
    15: ("#0f8274", "#e5f4ef"),
    16: ("#2e65cc", "#e8efff"),
    17: ("#8253c8", "#f0eafe"),
    18: ("#bf4162", "#fbe9ee"),
    19: ("#98660c", "#fff0d5"),
}


def rank_theme(grade):
    if finite(grade) and grade >= 20:
        return "#485574", "#eaedf5"
    return RANK_THEMES.get(grade, ("#5c718c", "#edf1f6"))


@lru_cache(maxsize=48)
def card_font(size, style="regular"):
    if style not in {"number", "number_regular"}:
        return face(size, style)
    from matplotlib import get_data_path

    filename = "DejaVuSans-Bold.ttf" if style == "number" else "DejaVuSans.ttf"
    return ImageFont.truetype(str(Path(get_data_path()) / "fonts" / "ttf" / filename), size)


@lru_cache(maxsize=32)
def title_art(value, size, top, bottom):
    scale, pad = 3, 12
    try:
        font = ImageFont.truetype(
            str(Path(__file__).parent / "assets" / "tenhou" / "title-brush.ttf"), size * scale
        )
    except OSError:
        font = face(size * scale, "title")
    bounds = font.getbbox(value)
    mask = Image.new("L", (bounds[2] - bounds[0] + pad * 2, bounds[3] - bounds[1] + pad * 2))
    ImageDraw.Draw(mask).text((pad - bounds[0], pad - bounds[1]), value, font=font, fill=255)
    outline = Image.new("RGBA", mask.size, "#26323d")
    outline.putalpha(mask.filter(ImageFilter.MaxFilter(7)))
    gradient = Image.new("RGBA", mask.size)
    draw = ImageDraw.Draw(gradient)
    start = tuple(int(top[i : i + 2], 16) for i in (1, 3, 5))
    end = tuple(int(bottom[i : i + 2], 16) for i in (1, 3, 5))
    for y in range(mask.height):
        ratio = min(1, max(0, (y - pad) / max(1, mask.height - 2 * pad)))
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(start, end, strict=True))
        draw.line((0, y, mask.width, y), fill=(*color, 255))
    gradient.putalpha(mask)
    outline.alpha_composite(gradient)
    return outline.resize(
        (round(outline.width / scale), round(outline.height / scale)), Image.Resampling.LANCZOS
    )


def render_start(game, profiles):
    info = game["info"]
    image = Image.new("RGBA", (1080, 790), "#f3f4f6")
    shadow = Image.new("RGBA", image.size)
    sd = ImageDraw.Draw(shadow)
    panels = [(24, 20, 1056, 104)] + [
        (24 + i % 2 * 526, 122 + i // 2 * 332, 530 + i % 2 * 526, 434 + i // 2 * 332)
        for i in range(min(len(profiles), 4))
    ]
    for left, top, right, bottom in panels:
        sd.rounded_rectangle((left, top + 6, right, bottom + 6), 17, fill=(35, 45, 60, 20))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=14, color=INK, style="regular", anchor="lm"):
        draw.text((x, y), str(value), font=card_font(size, style), fill=color, anchor=anchor)

    def width(value, size=14, style="regular"):
        return draw.textlength(str(value), font=card_font(size, style))

    def fit(value, available, size=25):
        while width(value, size, "bold") > available:
            if size > 20:
                size -= 1
            else:
                value = value[:-2] + "…"
        return value, size

    def glyph(grade):
        if not finite(grade):
            return "—"
        grade = int(grade)
        return (
            "一二三四五六七八九十"[grade - 10]
            if 10 <= grade <= 19
            else "天"
            if grade >= 20
            else "級"
        )

    for panel in panels:
        draw.rounded_rectangle(panel, 17, fill="white")
    title = rule_name(info)
    palette = {
        0: ("#b6c2cd", "#65788e"),
        1: ("#95d3b3", "#378773"),
        2: ("#9cbedc", "#4d779e"),
        3: ("#f4d780", "#ad7934"),
    }
    color_top, color_bottom = palette.get(int(info["playerlevel"]), palette[0])
    main = title_art(title[:3], 52, color_top, color_bottom)
    rules = title_art(title[3:], 46, "#f1f1f2", "#aeb5c0") if title[3:] else None
    gap = 36 if rules is not None else 0
    left = round((1080 - main.width - (rules.width if rules else 0) - gap) / 2)
    image.alpha_composite(main, (left, round(54 - main.height / 2)))
    if rules is not None:
        image.alpha_composite(rules, (left + main.width + gap, round(54 - rules.height / 2)))
    stamp = datetime.fromtimestamp(float(info["starttime"]), CN).strftime("%m-%d %H:%M")
    text(540, 90, stamp + " 开始", 13, MUTED, anchor="mm")

    for index, player in enumerate(profiles[:4]):
        x, y = 24 + index % 2 * 526, 122 + index // 2 * 332
        c = COLORS[index]
        draw.rounded_rectangle((x, y + 10, x + 5, y + 302), 3, fill=c)
        draw.rounded_rectangle((x + 25, y + 18, x + 58, y + 51), 6, fill=c)
        text(x + 42, y + 35, "東南西北"[index], 19, "white", "bold", "mm")
        name, size = fit(player["name"], 300 if int(info["playerlevel"]) == 3 else 320)
        text(x + 72, y + 35, name, size, style="bold")
        badge_x = x + 80 + width(name, size, "bold")
        if int(info["playerlevel"]) == 3 and badge_x + 20 < x + 407:
            draw.rounded_rectangle(
                (badge_x, y + 24, badge_x + 20, y + 45), 4, fill="#fff7df", outline="#e6a516"
            )
            text(badge_x + 10, y + 35, "鳳", 12, "#b77b0e", "bold", "mm")
        rate = player.get("rate")
        rate_high = finite(rate) and rate >= 2200
        draw.rounded_rectangle(
            (x + 415, y + 20, x + 482, y + 49), 14, fill="#eee6ff" if rate_high else "#e4f3f1"
        )
        text(
            x + 449,
            y + 35,
            f"R{round(rate):g}" if finite(rate) else "R —",
            13,
            "#8546e8" if rate_high else "#00998e",
            "number",
            "mm",
        )
        grade = player.get("grade")
        grade_color, grade_background = rank_theme(grade)
        draw.rounded_rectangle((x + 25, y + 60, x + 77, y + 86), 13, fill=grade_background)
        text(
            x + 51,
            y + 73,
            grade_name(grade) if grade is not None else "—",
            14,
            grade_color,
            "bold",
            "mm",
        )
        pt = player.get("pt")
        good_form = "好调" in player.get("tags", [])
        pt_text = f"{pt:g}" if finite(pt) else "—"
        text(x + 85, y + 74, pt_text, 29, "#8546e8" if good_form else INK, "number")
        label_x = x + 92 + width(pt_text, 29, "number")
        text(label_x, y + 77, "PT", 11, MUTED, "number_regular")
        text(x + 25, y + 104, "近10场", 11, MUTED)
        recent = player.get("recent", [])[-10:]
        if not recent:
            text(x + 75, y + 104, "—", 11, MUTED)
        for j, rank in enumerate(recent):
            rx = x + 75 + j * 20
            known = finite(rank) and rank in (1, 2, 3, 4)
            rank = int(rank) if known else None
            rc = {1: "#19813c", 2: "#8bcf9c", 3: "#f2a59e", 4: "#cf2528"}.get(rank, "#e8edf2")
            draw.rounded_rectangle((rx, y + 95, rx + 17, y + 112), 4, fill=rc)
            text(
                rx + 8.5,
                y + 103.5,
                rank if known else "?",
                11,
                "white" if rank in (1, 4) else INK,
                "number",
                "mm",
            )
        high, high_pt = player.get("high_grade"), player.get("high_pt")
        high_text = f"最高 {grade_name(high)}" if high is not None else "最高 —"
        if high is not None and finite(high_pt):
            high_text += f" {high_pt:g}"
        text(x + 481, y + 104, high_text, 11, MUTED, anchor="rm")
        stats = player.get("stats") or {}
        total = player.get("ranked_count")
        dan_games = player.get("dan_games") if finite(pt) else None
        counts = (
            f"本段 {dan_games} / {total}"
            if dan_games is not None
            else f"段位战 {total}"
            if total is not None
            else "段位战 —"
        )
        phoenix = stats.get("totalrecord")
        counts += f" · 鳳桌 {phoenix} 战" if phoenix is not None else " · 鳳桌 —"
        count_size = 12
        while width(counts, count_size) > 249 and count_size > 10:
            count_size -= 1
        text(x + 25, y + 134, counts, count_size, MUTED)
        trail = player.get("trajectory", [])[-10:]
        if not trail:
            text(x + 481, y + 134, "段位轨迹 —", 11, MUTED, anchor="rm")
        for j, previous_grade in enumerate(trail):
            rx = x + 282 + j * 20
            color, _ = rank_theme(previous_grade)
            draw.rounded_rectangle((rx, y + 125, rx + 18, y + 143), 4, fill=color)
            text(rx + 9, y + 134, glyph(previous_grade), 12, "white", "bold", "mm")
        draw.rounded_rectangle((x + 25, y + 150, x + 481, y + 158), 4, fill="#eff1f4")
        pt_max = player.get("pt_max")
        if finite(pt) and finite(pt_max) and pt_max > 0:
            end = x + 25 + int(456 * min(1, max(0, pt / pt_max)))
            if end > x + 25:
                draw.rounded_rectangle(
                    (x + 25, y + 150, end, y + 158), 4, fill="#8546e8" if good_form else "#efac00"
                )
            text(x + 481, y + 171, f"{pt:g} / {pt_max:g}", 11, MUTED, "number_regular", "rm")
        fields = (
            ("和", "agariC", "#1f913e", True),
            ("銃", "houjuuC", "#df3029", True),
            ("副露", "fuuroC", "#8931d7", True),
            ("立直", "riichC", "#237bf5", True),
            ("局收支", "shuushiCT", "#129c91", False),
            ("安定段", "stablerank_phoenix_X", "#b35c14", False),
        )
        percentiles = player.get("percentiles", {})
        for j, (label, key, color, is_rate) in enumerate(fields):
            bx, by = x + 25 + j % 2 * 239, y + 195 + j // 2 * 27
            text(bx, by, label, 13, color)
            draw.rounded_rectangle((bx + 49, by - 8, bx + 151, by + 9), 8, fill="#eff1f4")
            value, pct = stats.get(key), percentiles.get(key)
            if finite(pct):
                filled = int(102 * min(1, max(0, pct / 100)))
                if filled:
                    draw.rounded_rectangle(
                        (bx + 49, by - 8, bx + 49 + filled, by + 9), 8, fill=color
                    )
                position = bx + 49 + filled / 2 if filled >= 32 else bx + 55 + filled
                text(
                    position,
                    by,
                    f"{int(pct + 0.5)}%",
                    10,
                    "white" if filled >= 32 else "#7d899a",
                    "number_regular",
                    "mm" if filled >= 32 else "lm",
                )
            else:
                text(bx + 100, by, "—", 11, MUTED, anchor="mm")
            result = (
                f"{value:.1%}"
                if finite(value) and is_rate
                else f"{value:+.1f}"
                if finite(value) and key == "shuushiCT"
                else f"{value:.2f}"
                if finite(value)
                else "—"
            )
            highlight = finite(pct) and (pct <= 10 or pct >= 90)
            text(bx + 216, by, result, 14, "#d59508" if highlight else INK, "number", "rm")
        draw.line((x + 25, y + 263, x + 481, y + 263), fill="#edf0f3")
        diff = stats.get("agari_minus_houjuu_C")
        text(
            x + 25,
            y + 289,
            f"和銃差 {diff * 100:+.1f}" if finite(diff) else "和銃差 —",
            14,
            "#268247",
            "bold",
        )
        right = x + 481
        for tag in reversed(player.get("tags", [])[:3]):
            length = width(tag, 13, "bold") + 18
            left = right - length
            if left < x + 175:
                break
            tag_color, tag_bg = (
                ("#ed7617", "#fff1df")
                if tag in {"火龙果", "野性打法", "激进对攻", "猪突猛进"}
                else ("#3676da", "#edf3ff")
                if tag in {"谨慎人类", "铁壁地藏", "佛系观战"}
                else ("#268247", "#e9f4ec")
            )
            draw.rounded_rectangle((left, y + 277, right, y + 302), 12, fill=tag_bg)
            text(left + 9, y + 289, tag, 13, tag_color, "bold")
            right = left - 7
        if index == 2 and len(profiles) == 3:
            text(803, y + 145, "三人麻雀", 24, MUTED, "title", "mm")
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
