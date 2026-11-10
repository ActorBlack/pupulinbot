"""Group standings ordered by current Mahjong Soul rank and points."""

import asyncio
import io

from PIL import Image, ImageDraw

from .ms_level import point_number, point_unit, point_value, rank_label, rank_progress
from .paipuya import PaipuyaError
from .rank_assets import game_number, rank_badge, ui_sprite
from .typography import face

RANK_COLORS = {
    "杰": "#e2b21c",
    "豪": "#ef8b2c",
    "圣": "#991f36",
    "魂": "#40b9e9",
}


async def ranking_rows(client, bindings):
    limit = asyncio.Semaphore(4)

    async def load(row):
        try:
            async with limit:
                stats = await client.metadata(row["account_id"])
            return row, rank_progress((stats.raw or {}).get("level"))
        except PaipuyaError:
            return row, None

    loaded = await asyncio.gather(*(load(row) for row in bindings))
    available = [(row, rank) for row, rank in loaded if rank is not None]
    available.sort(
        key=lambda item: (item[1]["major"], item[1]["star"], item[1]["points"]),
        reverse=True,
    )
    standings = []
    for row, rank in available[:20]:
        unit = point_unit(rank)
        points = point_number(point_value(rank["points"], rank), unit)
        cap = "—" if rank["cap"] is None else point_number(point_value(rank["cap"], rank), unit)
        standings.append((row["nickname"], rank_label(rank), points, cap))
    return standings


async def group_ranking(client, bindings):
    standings = await ranking_rows(client, bindings)
    if not standings:
        return "牌谱屋暂时没有本群玩家的段位数据。"
    return await asyncio.to_thread(render_ranking, client.mode, standings)


def render_ranking(mode, standings):
    height = 210 + len(standings) * 64
    image = Image.new("RGB", (680, height), "#f2f5fa")
    draw = ImageDraw.Draw(image)
    ink, muted = "#22324b", "#7f8da3"
    draw.rounded_rectangle((24, 40, 30, 82), radius=3, fill="#4479d7")
    draw.text((48, 60), f"本群{mode}排行", font=face(34, "title"), fill=ink, anchor="lm")
    draw.rounded_rectangle((24, 116, 656, height - 28), radius=20, fill="white")
    draw.text((48, 150), "游戏昵称", font=face(23, "bold"), fill=muted, anchor="lm")
    draw.text((302, 150), "段位", font=face(23, "bold"), fill=muted, anchor="lm")
    draw.text((500, 150), "PT / 魂珠", font=face(20, "bold"), fill=muted, anchor="rm")
    draw.text((632, 150), "上限", font=face(23, "bold"), fill=muted, anchor="rm")
    draw.line((48, 180, 632, 180), fill="#e6ecf4", width=2)
    for index, (name, level, points, cap) in enumerate(standings):
        y = 219 + index * 64
        if index % 2:
            draw.rounded_rectangle((36, y - 28, 644, y + 28), radius=10, fill="#f7f9fd")
        name_size = 28
        while name_size > 14 and draw.textlength(name, font=face(name_size, "bold")) > 200:
            name_size -= 1
        draw.text((48, y), name, font=face(name_size, "bold"), fill=ink, anchor="lm")
        color = RANK_COLORS.get(level[:1], "#3872c6")
        major = {"初": 1, "士": 2, "杰": 3, "豪": 4, "圣": 5, "魂": 7}.get(level[:1])
        minor = int(level.removeprefix("魂天Lv")) if major == 7 else int(level[1:])
        badge = rank_badge(major, minor, 3 if mode == "三麻" else 4, 50)
        if badge is not None:
            image.paste(badge, (250, y - 25), badge)
        size = 28
        while size > 17 and draw.textlength(level, font=face(size, "bold")) > 95:
            size -= 1
        draw.text((302, y), level, font=face(size, "bold"), fill=color, anchor="lm")
        number = game_number(points, 24)
        if number is not None:
            image.paste(number, (500 - number.width, y - 12), number)
            if major == 7:
                orb = ui_sprite("ui_hpoint_l")
                if orb is not None:
                    orb = orb.resize((14, 17), Image.Resampling.LANCZOS)
                    image.paste(orb, (480 - number.width, y - 8), orb)
        else:
            draw.text((500, y), points, font=face(28, "number"), fill=ink, anchor="rm")
        draw.text((526, y), "/", font=face(26, "number"), fill="#b1bdcd", anchor="mm")
        number = game_number(cap, 24, color="#8f9eac")
        if number is not None:
            image.paste(number, (632 - number.width, y - 12), number)
        else:
            draw.text((632, y), cap, font=face(28, "number"), fill=muted, anchor="rm")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
