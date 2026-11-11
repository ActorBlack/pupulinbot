"""Data-backed monitoring cards; unavailable fields stay visibly unavailable."""

import asyncio
import io
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .ms_level import delta_text, point_text, point_unit, rank_identity, rank_label, rank_progress
from .ms_options import MODE_NAMES, TZ
from .rank_assets import rank_badge
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


@lru_cache(maxsize=32)
def monitor_number(size, bold=False):
    # Matplotlib ships the same openly licensed numerals on macOS and Linux.
    from matplotlib import get_data_path

    filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(Path(get_data_path()) / "fonts" / "ttf" / filename), size)


TITLE_ASSETS = {
    "铜之间": "room_bronze",
    "银之间": "room_silver",
    "金之间": "room_gold",
    "玉之间": "room_jade",
    "王座之间": "room_throne",
    "四人东": "mode_four_east",
    "四人南": "mode_four_south",
    "三人东": "mode_three_east",
    "三人南": "mode_three_south",
}


@lru_cache(maxsize=16)
def game_title(value):
    """Original client lettering/colors, with a fine edge for a white header."""
    name = TITLE_ASSETS.get(value)
    if not name:
        return None
    try:
        path = Path(__file__).parent / "assets" / "monitor" / (name + ".png")
        with Image.open(path) as source:
            original = source.convert("RGBA")
    except (OSError, ValueError):
        return None
    original = original.resize(
        (round(original.width * 0.55), round(original.height * 0.55)),
        Image.Resampling.LANCZOS,
    )
    # Preserve the colored pixels. Only put a dark keyline behind light strokes.
    # Work at 3x so it stays a thin edge rather than a heavy replacement font.
    scale = 3
    original = original.resize(
        (original.width * scale, original.height * scale), Image.Resampling.LANCZOS
    )
    mask = Image.new("L", (original.width + 8 * scale, original.height + 8 * scale))
    glyphs = original.getchannel("A").point(lambda a: 255 if a >= 110 else 0)
    mask.paste(glyphs, (4 * scale, 4 * scale))
    outline = Image.new("RGBA", mask.size, "#263239")
    outline.putalpha(mask.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(0.5)))
    outline.alpha_composite(original, (4 * scale, 4 * scale))
    return outline.resize(
        (outline.width // scale, outline.height // scale), Image.Resampling.LANCZOS
    )


def tag_palette(tag):
    if tag in {"自爆卡车", "纯送子", "小送子", "无谋野猪", "狂战士", "发癫了"}:
        return "#d73b39", "#ffebea"
    if tag in {"野性打法", "火龙果", "激进对攻", "标准野猪", "猪突猛进", "莽夫上头", "每战必至"}:
        return "#f17516", "#fff1df"
    if tag in {"佛系观战", "铁壁地藏", "谨慎人类", "保守人类"}:
        return "#3676da", "#edf3ff"
    return "#268248", "#eaf5ed"


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

    def font(size, style="regular"):
        return (
            monitor_number(size, style == "number_bold")
            if style.startswith("number")
            else face(size, style)
        )

    def text(x, y, value, size=16, color="#172033", style="regular", **kwargs):
        draw.text((x, y), str(value), font=font(size, style), fill=color, **kwargs)

    def width(value, size=16, style="regular"):
        return draw.textlength(str(value), font=font(size, style))

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
        text(right - length + 9, y + 12, value, 13, color, "bold", anchor="lm")
        return right - length - 7

    for bounds in panels:
        box(bounds, "white")
    room = MODE_NAMES.get(game.mode, str(game.mode))
    room_name = (
        "王座之间"
        if "王" in room
        else next((name + "之间" for name in ("玉", "金", "银", "铜") if name in room), room)
    )
    length = "东" if "东" in room else "南" if "南" in room else ""
    table_name = ("三人" if len(game.players) == 3 else "四人") + length
    room_art, mode_art = game_title(room_name), game_title(table_name)
    if room_art is not None and mode_art is not None:
        title_left = (1080 - room_art.width - mode_art.width - 52) // 2
        canvas.alpha_composite(room_art, (title_left, round(63 - room_art.height / 2)))
        canvas.alpha_composite(
            mode_art,
            (title_left + room_art.width + 52, round(63 - mode_art.height / 2)),
        )
    else:
        text(540, 63, f"{room_name}    {table_name}", 36, style="bold", anchor="mm")
    stamp = game.started_at.astimezone(TZ).strftime("%m-%d %H:%M")
    subtitle = f"{stamp} 开始"
    if event == "finish" and game.ended_at:
        subtitle += "　" + game.ended_at.astimezone(TZ).strftime("%H:%M") + " 结束"
    if preview:
        subtitle += "　历史牌谱预览"
    text(540, 114, subtitle, 13, "#98a0ae", anchor="mm")

    for i, player in enumerate(game.players[:4]):
        x, y = 24 + (i % 2) * 526, 142 + (i // 2) * 280
        draw.line((x + 3, y + 16, x + 3, y + 244), fill=COLORS[i], width=5)
        box((x + 26, y + 19, x + 58, y + 51), COLORS[i], 5)
        text(x + 42, y + 35, "东南西北"[i], 18, "white", "bold", anchor="mm")
        text(
            x + 72,
            y + 35,
            trim(player.get("nickname") or "未知玩家", 406),
            25,
            style="bold",
            anchor="lm",
        )
        try:
            profile = profiles.get(int(player.get("account_id", -1)), {})
        except (ValueError, TypeError):
            profile = {}
        stats = profile.get("stats")
        rank = rank_snapshot(player, stats)
        major = rank["major"] if rank else None
        icon = (
            rank_badge(major, rank["star"], 3 if len(game.players) == 3 else 4, 90)
            if rank
            else None
        )
        if icon is not None:
            canvas.alpha_composite(icon, (x + 6, y + 51))
        else:
            text(x + 51, y + 87, rank_label(rank) if rank else "—", 20, "#8793a4", anchor="mm")
        points, cap = (rank["points"], rank["cap"]) if rank else (None, None)
        value = point_text(rank, capacity=False)
        number_size = 28 if len(value) <= 5 else 23
        text(x + 98, y + 83, value, number_size, "#151d2d", "number_bold", anchor="lm")
        unit_x = x + 104 + width(value, number_size, "number_bold")
        unit = point_unit(rank)
        text(unit_x, y + 85, unit, 10, "#99a1af", anchor="lm")
        meta_x = unit_x + width(unit, 10) + 10
        games = getattr(stats, "games", None)
        count = "对战 —" if games is None else f"对战 {games}"
        # Keep the last ten placements in one row even with long PT/game counts.
        tiles_left = x + 480 - 18 * 10
        if meta_x + width(count, 10) <= tiles_left - 7:
            text(meta_x, y + 85, count, 10, "#99a1af", anchor="lm")
        ranks = profile.get("ranks", [])[-10:]
        if not ranks:
            text(x + 480, y + 85, "近10场 —", 11, "#a1a8b4", anchor="rm")
        for j, r in enumerate(ranks):
            bx = tiles_left + j * 18
            color = {1: "#14783c", 2: "#7bcb96", 3: "#d5d9df", 4: "#c9242c"}.get(r, "#edf0f3")
            box((bx, y + 75, bx + 16, y + 92), color, 4)
            text(
                bx + 8,
                y + 83,
                r if r in {1, 2, 3, 4} else "?",
                10,
                "white" if r in {1, 4} else "#465160",
                "number_bold",
                anchor="mm",
            )
        progress(
            x + 98, y + 111, 265, None if cap is None or points is None else points / cap, "#f4ab00"
        )
        capacity = point_text(rank, spaced=True, unit=major == 7) if points is not None else "—"
        text(x + 480, y + 115, capacity, 11, "#99a1af", anchor="rm")

        # Settle details stay available without competing with the PT headline.
        if event == "finish":
            placement, score = player.get("rank"), player.get("score")
            parts = []
            if placement in {1, 2, 3, 4}:
                parts.append(f"{placement}位")
            if finite(score):
                parts.append(f"{score:g}点")
            if parts:
                separator = "　"
                cursor = x + 480 - width(separator.join(parts), 11)
                for index, part in enumerate(parts):
                    text(cursor, y + 136, part, 11, "#8a94a4", anchor="lm")
                    cursor += width(part, 11)
                    if index < len(parts) - 1:
                        text(cursor, y + 136, separator, 11, "#b3bac5", anchor="lm")
                        cursor += width(separator, 11)

        metrics = [
            ("和", "win_rate", "#209241"),
            ("铳", "deal_in_rate", "#df302c"),
            ("副露", "call_rate", "#8834d2"),
            ("立直", "riichi_rate", "#2676ee"),
        ]
        for j, (label, field, color) in enumerate(metrics):
            xx, yy = x + 26 + (j % 2) * 240, y + 152 + (j // 2) * 27
            value = rate(getattr(stats, field, None))
            text(xx, yy, label, 13, color, anchor="lm")
            scale = 0.6 if field == "call_rate" else 0.3
            progress(xx + 40, yy - 4, 126, None if value is None else value / scale, color)
            label_value = "—" if value is None else f"{value * 100:.1f}%"
            text(xx + 214, yy, label_value, 13, "#292d34", "number_bold", anchor="rm")
        draw.line((x + 26, y + 204, x + 480, y + 204), fill="#eef0f3")
        win = rate(getattr(stats, "win_rate", None))
        deal = rate(getattr(stats, "deal_in_rate", None))
        diff = "—" if win is None or deal is None else f"{(win - deal) * 100:+.1f}"
        diff_color = (
            "#258344" if win is not None and deal is not None and win - deal >= 0.08 else "#8c7c13"
        )
        text(x + 26, y + 228, f"和铳差 {diff}", 13, diff_color, "bold", anchor="lm")
        if event == "finish":
            delta = delta_text(player)
            if delta != "—":
                positive = finite(player.get("gradingScore")) and player["gradingScore"] >= 0
                text(
                    x + 253,
                    y + 228,
                    delta,
                    14,
                    "#258344" if positive else "#cf4545",
                    "bold",
                    anchor="mm",
                )
        tag = rate_tag(win, deal, mode=len(game.players))
        if tag:
            color, bg = tag_palette(tag)
            pill(x + 480, y + 215, tag, color, bg)
        elif event != "finish":
            pill(x + 480, y + 215, "正在对局", "#24804b", "#e8f5ec")

    if len(game.players) == 3:
        text(803, 550, "三人麻雀", 20, "#b0b7c2", anchor="mm")
    result = io.BytesIO()
    canvas.convert("RGB").save(result, "PNG")
    return result.getvalue()
