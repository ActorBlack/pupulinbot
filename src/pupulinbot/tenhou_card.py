"""Compact spectator cards with source-aware PT, quantiles and descriptive tags."""

import io
from datetime import datetime

from PIL import Image, ImageDraw

from .tenhou import CN, grade_name, rule_name
from .tenhou_profile import finite
from .typography import face

INK = "#1c2739"
MUTED = "#7c889b"
COLORS = ("#e53027", "#2276ed", "#1d9344", "#8536da")


# Shared by the current-rank badge and history tiles: six through ten dan
# must remain visually distinct, including pale badge backgrounds.
RANK_THEMES = {
    15: ("#0f8274", "#e5f4ef"),  # six: teal
    16: ("#2e65cc", "#e8efff"),  # seven: blue
    17: ("#8253c8", "#f0eafe"),  # eight: violet
    18: ("#bf4162", "#fbe9ee"),  # nine: rose
    19: ("#98660c", "#fff0d5"),  # ten: amber
}


def rank_theme(grade):
    if finite(grade) and grade >= 20:
        return "#485574", "#eaedf5"
    return RANK_THEMES.get(grade, ("#5c718c", "#edf1f6"))


def render_start(game, profiles):
    info = game["info"]
    image = Image.new("RGB", (1080, 790), "#f3f5f7")
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=20, color=INK, style="regular"):
        draw.text((x, y), str(value), font=face(size, style), fill=color)

    def fit(value, width, size=29):
        while draw.textlength(value, font=face(size, "bold")) > width:
            if size > 20:
                size -= 1
            else:
                value = value[:-2] + "…"
        return value, size

    def glyph(grade):
        return (
            "一二三四五六七八九十"[grade - 10]
            if 10 <= grade <= 19
            else "天"
            if grade >= 20
            else "級"
        )

    draw.rounded_rectangle((24, 20, 1056, 104), 16, fill="white")
    draw.text((540, 34), rule_name(info), anchor="mt", font=face(34, "title"), fill=INK)
    stamp = datetime.fromtimestamp(float(info["starttime"]), CN).strftime("%m-%d %H:%M")
    draw.text((540, 77), stamp, anchor="mt", font=face(14), fill=MUTED)
    for index, player in enumerate(profiles):
        x, y = 24 + index % 2 * 526, 122 + index // 2 * 332
        c = COLORS[index]
        draw.rounded_rectangle((x, y + 4, x + 506, y + 312), 16, fill="#e7ebf1")
        draw.rounded_rectangle((x, y, x + 506, y + 312), 18, fill="white", outline="#e9edf3")
        draw.rounded_rectangle((x, y + 10, x + 5, y + 302), 3, fill=c)
        draw.rounded_rectangle((x + 25, y + 18, x + 58, y + 51), 6, fill=c)
        text(x + 33, y + 21, "東南西北"[index], 21, "white", "bold")
        name, size = fit(player["name"], 300 if int(info["playerlevel"]) == 3 else 320)
        text(x + 72, y + 18, name, size, style="bold")
        badge_x = x + 80 + draw.textlength(name, font=face(size, "bold"))
        if int(info["playerlevel"]) == 3 and badge_x + 20 < x + 407:
            draw.rounded_rectangle(
                (badge_x, y + 24, badge_x + 20, y + 45), 4, fill="#fff7df", outline="#e6a516"
            )
            text(badge_x + 3, y + 27, "鳳", 13, "#b77b0e", "bold")
        rate = player.get("rate")
        rate_high = finite(rate) and rate >= 2200
        draw.rounded_rectangle(
            (x + 415, y + 20, x + 482, y + 49), 14, fill="#eee6ff" if rate_high else "#e4f3f1"
        )
        text(
            x + 425,
            y + 25,
            f"R{round(rate):g}" if finite(rate) else "R —",
            15,
            "#8546e8" if rate_high else "#00998e",
            "number",
        )
        grade = player.get("grade")
        grade_color, grade_background = rank_theme(grade)
        draw.rounded_rectangle((x + 25, y + 60, x + 77, y + 86), 13, fill=grade_background)
        draw.text(
            (x + 51, y + 73),
            grade_name(grade) if grade is not None else "—",
            anchor="mm",
            font=face(15, "bold"),
            fill=grade_color,
        )
        pt = player.get("pt")
        good_form = "好调" in player.get("tags", [])
        text(
            x + 84,
            y + 53,
            f"{pt:g}" if finite(pt) else "—",
            30,
            "#8546e8" if good_form else INK,
            "number",
        )
        pt_text = f"{pt:g}" if finite(pt) else "—"
        label_x = x + 89 + draw.textlength(pt_text, font=face(30, "number"))
        text(label_x, y + 68, "PT≈", 11, MUTED)
        text(x + 25, y + 98, "近10场", 11, MUTED)
        if not player.get("recent"):
            text(x + 75, y + 98, "—", 12, MUTED)
        for j, rank in enumerate(player.get("recent", [])[-10:]):
            rx = x + 75 + j * 20
            rc = ("#19813c", "#8bcf9c", "#f2a59e", "#cf2528")[min(rank, 4) - 1]
            draw.rounded_rectangle((rx, y + 95, rx + 17, y + 112), 4, fill=rc)
            text(rx + 5, y + 96, rank, 11, "white" if rank in (1, 4) else INK, "number")
        high = player.get("high_grade")
        high_text = (
            f"最高 {grade_name(high)} {player['high_pt']:g}" if high is not None else "最高 —"
        )
        draw.text((x + 481, y + 99), high_text, anchor="rt", font=face(12), fill=MUTED)
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
        count_size = 13
        while draw.textlength(counts, font=face(count_size)) > 249 and count_size > 10:
            count_size -= 1
        text(x + 25, y + 126, counts, count_size, MUTED)
        trail = player.get("trajectory", [])[-10:]
        if not trail:
            draw.text((x + 481, y + 126), "段位轨迹 —", anchor="rt", font=face(12), fill=MUTED)
        for j, previous_grade in enumerate(trail):
            rx = x + 282 + j * 20
            color, _ = rank_theme(previous_grade)
            draw.rounded_rectangle((rx, y + 125, rx + 18, y + 143), 4, fill=color)
            draw.text(
                (rx + 9, y + 134),
                glyph(previous_grade),
                anchor="mm",
                font=face(12, "bold"),
                fill="white",
            )
        draw.rounded_rectangle((x + 25, y + 150, x + 481, y + 158), 4, fill="#eff1f4")
        pt_max = player.get("pt_max")
        if finite(pt) and finite(pt_max) and pt_max > 0:
            end = x + 25 + int(456 * min(1, max(0, pt / pt_max)))
            if end > x + 25:
                draw.rounded_rectangle(
                    (x + 25, y + 150, end, y + 158), 4, fill="#8546e8" if good_form else "#efac00"
                )
            draw.text(
                (x + 481, y + 163),
                f"{pt:g} / {pt_max:g}",
                anchor="rt",
                font=face(12, "number"),
                fill=MUTED,
            )
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
            bx, by = x + 25 + j % 2 * 239, y + 182 + j // 2 * 27
            text(bx, by, label, 15, color)
            draw.rounded_rectangle((bx + 49, by + 1, bx + 151, by + 18), 8, fill="#eff1f4")
            value, pct = stats.get(key), percentiles.get(key)
            if finite(pct):
                width = int(102 * min(1, max(0, pct / 100)))
                if width:
                    draw.rounded_rectangle(
                        (bx + 49, by + 1, bx + 49 + width, by + 18), 8, fill=color
                    )
                position = bx + 49 + width / 2 if width >= 27 else bx + 52 + width
                draw.text(
                    (position, by + 3),
                    f"{int(pct + 0.5)}%",
                    anchor="mt" if width >= 27 else "lt",
                    font=face(11, "number"),
                    fill="white" if width >= 27 else MUTED,
                )
            else:
                draw.text((bx + 100, by + 3), "—", anchor="mt", font=face(11), fill=MUTED)
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
            text(bx + 160, by - 1, result, 17, "#d59508" if highlight else INK, "number")
        draw.line((x + 25, y + 263, x + 481, y + 263), fill="#edf0f3")
        diff = stats.get("agari_minus_houjuu_C")
        text(
            x + 25,
            y + 279,
            f"和銃差 {diff * 100:+.1f}" if finite(diff) else "和銃差 —",
            16,
            "#268247",
            "bold",
        )
        right = x + 481
        for tag in reversed(player.get("tags", [])[:3]):
            width = draw.textlength(tag, font=face(13, "bold")) + 18
            left = right - width
            if left < x + 175:
                break
            draw.rounded_rectangle((left, y + 277, right, y + 302), 12, fill="#e9f4ec")
            text(left + 9, y + 281, tag, 13, "#268247", "bold")
            right = left - 7
        if index == 2 and len(profiles) == 3:
            text(650, y + 105, "三人麻雀", 30, MUTED, "title")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
