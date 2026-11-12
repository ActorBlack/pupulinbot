"""Shared Mahjong Soul and Tenhou group ranking cards."""

import io

from PIL import Image, ImageDraw

from .rank_assets import game_number, rank_badge, ui_sprite
from .tenhou import grade_name
from .tenhou_card import rank_theme
from .typography import face

RANK_COLORS = {
    "杰": "#e2b21c",
    "豪": "#ef8b2c",
    "圣": "#991f36",
    "魂": "#40b9e9",
}
TENHOU_COLORS = {grade_name(grade): rank_theme(grade)[0] for grade in range(21)}
PAGE_SIZE = 20


def render_rank_pages(mode, standings, *, tenhou=False):
    total = (len(standings) + PAGE_SIZE - 1) // PAGE_SIZE
    return [
        render_ranking(
            mode,
            standings[start : start + PAGE_SIZE],
            page=f"{index} / {total}" if total > 1 else None,
            tenhou=tenhou,
        )
        for index, start in enumerate(range(0, len(standings), PAGE_SIZE), start=1)
    ]


def render_ranking(mode, standings, *, page=None, tenhou=False):
    height = 210 + len(standings) * 64
    image = Image.new("RGB", (680, height), "#f2f5fa")
    draw = ImageDraw.Draw(image)
    ink, muted = "#22324b", "#7f8da3"
    draw.rounded_rectangle((24, 40, 30, 82), radius=3, fill="#4479d7")
    title = f"本群{'天凤' if tenhou else ''}{mode}排行"
    draw.text((48, 60), title, font=face(34, "title"), fill=ink, anchor="lm")
    if page:
        draw.text((632, 60), page, font=face(23, "number"), fill=muted, anchor="rm")
    draw.rounded_rectangle((24, 116, 656, height - 28), radius=20, fill="white")
    draw.text((48, 150), "游戏昵称", font=face(23, "bold"), fill=muted, anchor="lm")
    draw.text((302, 150), "段位", font=face(23, "bold"), fill=muted, anchor="lm")
    draw.text(
        (500, 150),
        "推算积分" if tenhou else "PT / 魂珠",
        font=face(20, "bold"),
        fill=muted,
        anchor="rm",
    )
    draw.text((632, 150), "上限", font=face(23, "bold"), fill=muted, anchor="rm")
    draw.line((48, 180, 632, 180), fill="#e6ecf4", width=2)
    for index, (name, level, points, cap) in enumerate(standings):
        y = 219 + index * 64
        if index % 2:
            draw.rounded_rectangle((36, y - 28, 644, y + 28), radius=10, fill="#f7f9fd")
        name_size = 28
        name_width = 246 if tenhou else 200
        while name_size > 14 and draw.textlength(name, font=face(name_size, "bold")) > name_width:
            name_size -= 1
        name_font = face(name_size, "bold")
        if draw.textlength(name, font=name_font) > name_width:
            while draw.textlength(name + "…", font=name_font) > name_width:
                name = name[:-1]
            name += "…"
        draw.text((48, y), name, font=name_font, fill=ink, anchor="lm")
        major = None
        if tenhou:
            color = TENHOU_COLORS.get(level.split("+", 1)[0], muted)
        else:
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
        number = None if tenhou else game_number(points, 24)
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
        number = None if tenhou else game_number(cap, 24, color="#8f9eac")
        if number is not None:
            image.paste(number, (632 - number.width, y - 12), number)
        else:
            draw.text((632, y), cap, font=face(28, "number"), fill=muted, anchor="rm")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
