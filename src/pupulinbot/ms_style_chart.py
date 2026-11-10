"""Standalone, auditable four-player style report with all twelve inputs."""

import io
import math

from PIL import Image, ImageDraw

from .monitor_card import face
from .ms_options import TZ
from .ms_style import FEATURES, analyze_style


def render_style(player, stats, games, options):
    result = analyze_style(stats, options.mode)
    image = Image.new("RGB", (1200, 815), "#f3f5f8")
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size=18, color="#293447", style="regular"):
        draw.text((x, y), str(value), fill=color, font=face(size, style))

    def fit(value, maximum, size=29):
        value = str(value)
        while value and draw.textlength(value, font=face(size, "bold")) > maximum:
            value = value[:-1]
        return value

    finished = [g for g in games if g.ended_at and g.seat(player.account_id)]
    text(42, 26, fit(player.nickname + " · 四麻牌风分析", 1100), 29, style="bold")
    count = len(finished)
    if finished:
        lo = min(g.started_at for g in finished).astimezone(TZ)
        hi = max(g.started_at for g in finished).astimezone(TZ)
        text(42, 78, f"最近 {count} 战 · {lo:%Y-%m-%d %H:%M} — {hi:%Y-%m-%d %H:%M}", 16, "#78859a")
    tag = result.label if result.supported else "仅支持四麻"
    text(42, 119, "#" + tag, 25, "#5171b8", "bold")
    draw.rounded_rectangle((32, 211, 610, 791), 18, fill="white")
    draw.rounded_rectangle((630, 211, 1168, 791), 18, fill="white")
    text(658, 228, "牌风指标", 21, style="bold")
    text(902, 234, "本次样本", 14, "#78859a")
    text(1042, 234, "参考均值", 14, "#78859a")
    for i, (key, label, pct, mean, *_) in enumerate(FEATURES):
        y = 278 + 40 * i
        value = result.metrics.get(key)
        show = "—" if value is None else f"{value * 100:.2f}%" if pct else f"{value:.2f}"
        base = f"{mean * 100:.2f}%" if pct else f"{mean:.2f}"
        text(658, y, label, 16)
        text(908, y, show, 16, "#375a97", "number")
        text(1045, y, base, 14, "#a0a9b5", "number")
        draw.line((658, y + 32, 1140, y + 32), fill="#f0f2f5")
    cx, cy, half = 321, 500, 235
    bound = max(25, math.ceil(max(abs(result.x or 0), abs(result.y or 0)) / 5) * 5 + 5)

    def point(x, y):
        return cx + x / bound * half, cy - y / bound * half

    draw.line((cx - half, cy, cx + half, cy), fill="#b8c2cf", width=2)
    draw.line((cx, cy - half, cx, cy + half), fill="#b8c2cf", width=2)
    for v in range(-2, 3):
        x, y = point(v * bound / 2, v * bound / 2)
        text(x - 8, cy + 8, f"{v * bound / 2:g}", 11, "#929dab")
        if v:
            text(cx + 6, y - 7, f"{v * bound / 2:g}", 11, "#929dab")
    for slope in (1, -0.35):
        for i in range(0, 100, 2):
            x1, x2 = -bound + i / 100 * bound * 2, -bound + (i + 1) / 100 * bound * 2
            draw.line((*point(x1, slope * x1), *point(x2, slope * x2)), fill="#bfd0bf", width=2)
    for label, x, y in [
        ("后手反击", 0.5, 0.82),
        ("门前打点", 0.62, 0.12),
        ("铁壁地藏", 0.42, -0.78),
        ("先手闪避", -0.8, -0.82),
        ("副露速度", -0.88, 0.1),
        ("全局参与", -0.82, 0.82),
    ]:
        px, py = point(x * bound, y * bound)
        text(px - 28, py, label, 15, "#8b9aab")
    if result.x is not None:
        x, y = point(result.x, result.y)
        draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill="#e05f72", outline="white", width=3)
    else:
        text(164, 476, f"数据不足 {len(result.metrics)}/12", 23, "#9aa5b3", "bold")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()
