"""Data-backed monitoring cards; unavailable fields stay visibly unavailable."""

import asyncio
import io
import math

from PIL import Image, ImageDraw, ImageFilter

from .ms_level import delta_text, point_text, point_unit, rank_identity, rank_label, rank_progress
from .ms_options import TZ
from .ms_sample_chart import MODE_NAMES
from .ms_style import analyze_style
from .rank_assets import point_run, rank_badge
from .rate_tags import rate_tag
from .typography import face

COLORS = ["#e53935", "#2575ed", "#229447", "#8835d9"]


def rank_snapshot(player, stats):
    """Only upstream rank metadata can establish current PT, never the table score."""
    raw = getattr(stats, "raw", {}) or {}
    current = rank_progress(raw.get("level"))
    if current:
        return current
    return rank_identity(player.get("level"))


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def rate(value):
    if not finite(value):
        return None
    value = value / 100 if abs(value) > 1 else value
    return value if 0 <= value <= 1 else None


async def collect_profiles(client, game, *, budget_seconds=120):
    """Optional context must never prevent a timely game notification."""
    profiles = {}
    gate = asyncio.Semaphore(2)

    accounts = []
    for player in game.players[:4]:
        try:
            account = int(player.get("account_id", -1))
        except (TypeError, ValueError):
            continue
        if account > 0 and account not in accounts:
            accounts.append(account)
            profiles[account] = {}

    async def stats(account):
        async with gate:
            try:
                profiles[account]["stats"] = await client.stats(account)
            except Exception:
                pass

    async def recent(account):
        async with gate:
            try:
                games, _ = await client.query_history(account, 10, end=game.started_at)
                profiles[account]["ranks"] = [
                    (g.seat(account) or {}).get("rank")
                    for g in sorted(games, key=lambda g: g.started_at)
                    if g.ended_at and g.started_at < game.started_at
                ][-10:]
            except Exception:
                pass

    try:
        async with asyncio.timeout(budget_seconds):
            # Prioritize statistics for every seat before optional history strips.
            await asyncio.gather(*(stats(a) for a in accounts), *(recent(a) for a in accounts))
    except TimeoutError:
        pass
    return profiles


def render_monitor_card(game, event="finish", profiles=None, *, preview=False):
    profiles = profiles or {}
    canvas = Image.new("RGB", (1080, 706), "#f3f4f6")
    shadow = Image.new("RGBA", canvas.size)
    sd = ImageDraw.Draw(shadow)
    panels = [(24, 20, 1056, 124)] + [
        (24 + i % 2 * 526, 142 + i // 2 * 280, 530 + i % 2 * 526, 402 + i // 2 * 280)
        for i in range(min(len(game.players), 4))
    ]
    for x1, y1, x2, y2 in panels:
        sd.rounded_rectangle((x1, y1 + 7, x2, y2 + 7), 16, fill=(35, 45, 60, 22))
    canvas = Image.alpha_composite(
        canvas.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(9))
    )
    draw = ImageDraw.Draw(canvas)

    def text(x, y, value, size=16, color="#172033", style="regular", **kwargs):
        draw.text((x, y), str(value), font=face(size, style), fill=color, **kwargs)

    def width(value, size=16, style="regular"):
        return draw.textlength(str(value), font=face(size, style))

    def trim(value, available, size=25):
        value = str(value)
        if width(value, size, "bold") <= available:
            return value
        while value and width(value + "…", size, "bold") > available:
            value = value[:-1]
        return value + "…"

    def box(bounds, fill, radius=14):
        draw.rounded_rectangle(bounds, radius, fill=fill)

    def progress(x, y, length, value, color):
        box((x, y, x + length, y + 8), "#eff1f3", 4)
        if value is not None and value > 0:
            box((x, y, x + max(8, length * min(value, 1)), y + 8), color, 4)

    def pill(right, y, value, color, bg):
        length = width(value, 13, "bold") + 18
        box((right - length, y, right, y + 25), bg, 12)
        text(right - length + 9, y + 4, value, 13, color, "bold")
        return right - length - 7

    for bounds in panels:
        box(bounds, "white")
    room = MODE_NAMES.get(game.mode, str(game.mode))
    room_name = (
        "王座之间"
        if "王座" in room
        else ("玉之间" if "玉" in room else "金之间" if "金" in room else room)
    )
    length = "东" if "东" in room else "南" if "南" in room else ""
    table_name = ("三人" if len(game.players) == 3 else "四人") + length
    title_width = width(room_name, 34, "title")
    mode_width = width(table_name, 21, "bold") + 30
    left = (1080 - title_width - mode_width - 22) / 2
    eyebrow = "MAHJONG SOUL  /  MATCH REPORT"
    text((1080 - width(eyebrow, 11, "number")) / 2, 30, eyebrow, 11, "#869b94", "number")
    text(left, 50, room_name, 34, "#214b3e", "title")
    badge_x = left + title_width + 22
    box((badge_x, 55, badge_x + mode_width, 91), "#edf5f0", 10)
    text(badge_x + 15, 59, table_name, 21, "#437962", "bold")
    stamp = game.started_at.astimezone(TZ).strftime("%m-%d %H:%M")
    subtitle = f"{stamp} 开始 · {'对局结算' if event == 'finish' else '实时开局'}"
    if preview:
        subtitle += " · 历史牌谱预览"
    text((1080 - width(subtitle, 13)) / 2, 101, subtitle, 13, "#839087")

    for i, player in enumerate(game.players[:4]):
        x, y = 24 + (i % 2) * 526, 142 + (i // 2) * 280
        # A clipped accent keeps the panel's outer corners smooth.
        draw.line((x + 3, y + 16, x + 3, y + 244), fill=COLORS[i], width=5)
        box((x + 26, y + 19, x + 58, y + 51), COLORS[i], 5)
        text(x + 34, y + 24, "东南西北"[i], 18, "white", "bold")
        text(x + 72, y + 17, trim(player.get("nickname") or "未知玩家", 406), 25, style="bold")
        try:
            profile = profiles.get(int(player.get("account_id", -1)), {})
        except (ValueError, TypeError):
            profile = {}
        stats = profile.get("stats")
        rank = rank_snapshot(player, stats)
        major = rank["major"] if rank else None
        cx, cy = x + 51, y + 87
        icon = (
            rank_badge(major, rank["star"], 3 if len(game.players) == 3 else 4, 90)
            if rank
            else None
        )
        if icon is not None:
            canvas.alpha_composite(icon, (round(cx - icon.width / 2), y + 51))
        else:
            label = rank_label(rank) if rank else "—"
            text(cx - width(label, 20) / 2, cy - 12, label, 20, "#8793a4")
        points, cap = (rank["points"], rank["cap"]) if rank else (None, None)
        value = point_text(rank, capacity=False)
        run = point_run(rank, 30) if points is not None else None
        if run is not None:
            if run.width > 175:
                run.thumbnail((175, 34), Image.Resampling.LANCZOS)
            canvas.alpha_composite(run, (x + 98, y + 66))
        else:
            text(x + 98, y + 63, value, 32, style="number")
            text(x + 103 + width(value, 32, "number"), y + 79, point_unit(rank), 11, "#919aa8")
        games = getattr(stats, "games", None)
        text(x + 280, y + 56, "对战 —" if games is None else f"对战 {games}", 11, "#919aa8")
        ranks = profile.get("ranks", [])
        if not ranks:
            text(x + 280, y + 79, "此前顺位暂无数据", 11, "#a1a8b4")
        for j, r in enumerate(ranks[-10:]):
            bx = x + 280 + j * 19
            color = {1: "#14783c", 2: "#7bcb96", 3: "#f2a29b", 4: "#c9242c"}.get(r, "#e5e7eb")
            box((bx, y + 75, bx + 17, y + 92), color, 4)
            text(
                bx + 5,
                y + 77,
                r if r in {1, 2, 3, 4} else "?",
                11,
                "white" if r in {1, 4} else "#374151",
                "number",
            )
        progress(
            x + 98, y + 111, 265, None if cap is None or points is None else points / cap, "#f4ab00"
        )
        capacity = point_text(rank, spaced=True, unit=True) if points is not None else "—"
        text(x + 480 - width(capacity, 11), y + 109, capacity, 11, "#8793a2")
        style = analyze_style(stats, 3 if len(game.players) == 3 else 4)
        label = (
            style.label
            if style.x is not None
            else (f"牌风指标 {len(style.metrics)}/12" if style.supported else "三麻不套用四麻牌风")
        )
        tag = rate_tag(
            rate(getattr(stats, "win_rate", None)),
            rate(getattr(stats, "deal_in_rate", None)),
            mode=len(game.players),
        )
        available = 382
        if tag:
            chip_width = width(tag, 11, "bold") + 18
            chip_left = x + 480 - chip_width
            box((chip_left, y + 125, x + 480, y + 143), "#edf5f0", 8)
            text(chip_left + 9, y + 126, tag, 11, "#268247", "bold")
            available -= chip_width + 10
        text(x + 98, y + 128, trim(label, available, 11), 11, "#7c69ad")
        metrics = [
            ("和", "win_rate", "#209241"),
            ("铳", "deal_in_rate", "#df302c"),
            ("副露", "call_rate", "#8834d2"),
            ("立直", "riichi_rate", "#2676ee"),
        ]
        for j, (label, field, color) in enumerate(metrics):
            xx, yy = x + 26 + (j % 2) * 240, y + 148 + (j // 2) * 27
            value = rate(getattr(stats, field, None))
            text(xx, yy - 3, label, 14, color)
            # The visual scale saturates at 30% (win/deal-in/riichi) or 60% (calls).
            scale = 0.6 if field == "call_rate" else 0.3
            progress(xx + 40, yy + 5, 124, None if value is None else value / scale, color)
            label_value = "—" if value is None else f"{value * 100:.1f}%"
            text(xx + 168, yy - 3, label_value, 15, "#292d34", "number")
        draw.line((x + 26, y + 204, x + 480, y + 204), fill="#eef0f3")
        win, deal = (
            rate(getattr(stats, "win_rate", None)),
            rate(getattr(stats, "deal_in_rate", None)),
        )
        diff = "—" if win is None or deal is None else f"{(win - deal) * 100:+.1f}"
        text(x + 26, y + 219, f"和铳差 {diff}", 14, "#27804a", "bold")
        right = x + 480
        if event == "finish":
            delta, placement, score = (
                player.get("gradingScore"),
                player.get("rank"),
                player.get("score"),
            )
            delta_label = delta_text(player)
            if finite(delta) and delta_label != "—":
                right = pill(
                    right,
                    y + 215,
                    delta_label,
                    "#24804b" if delta >= 0 else "#ca4144",
                    "#e8f5ec" if delta >= 0 else "#fff0ef",
                )
            if finite(score):
                right = pill(right, y + 215, f"{score:g} 点", "#976b24", "#fff5e5")
            if placement in {1, 2, 3, 4}:
                pill(right, y + 215, f"{placement} 位", "#4e68af", "#edf1ff")
        else:
            pill(right, y + 215, "正在对局", "#24804b", "#e8f5ec")

    if len(game.players) == 3:
        text(672, 535, "三人麻雀 · 三个座位", 19, "#a2aab5")
    result = io.BytesIO()
    canvas.convert("RGB").save(result, "PNG")
    return result.getvalue()
