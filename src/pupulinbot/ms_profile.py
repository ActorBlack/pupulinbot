"""Readable player cards using existing API aggregates and local rank artwork."""

import io

from PIL import Image, ImageDraw, ImageFilter

from .ms_ids import account_to_friend
from .ms_level import SOUL_COLOR, point_text, rank_identity, rank_label
from .ms_options import level_label
from .ms_sample import number
from .ms_style import analyze_style
from .ms_summary import format_level, rank_progress, ratio, scoring_metrics
from .ms_tags import supplemental_tags
from .ms_trait_tags import is_named_yakuman_tag
from .profile_header import draw_profile_header
from .rank_assets import rank_badge
from .typography import profile_face as face

SIZE = (1600, 1148)
INK, MUTED = "#1d2c44", "#6c7b90"
RANK_COLORS = ("#4077df", "#189b91", "#dcab46", "#cc8296")
RANK_NAMES = {1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣", 7: "魂天"}


def metric(value, kind="rate"):
    value = ratio(value) if kind == "rate" else number(value)
    if value is None or (kind != "signed" and value < 0):
        return "—"
    if kind == "rate":
        return f"{value * 100:.2f}%"
    if kind == "points":
        return f"{value:,.0f}"
    if kind == "signed":
        return f"{value:+,.1f}"
    return f"{value:.2f}"


def profile_content(player, stats, *, scope=None, games=()):
    raw = stats.raw or {}
    mode = 3 if stats.mode == "三麻" else 4
    count = number(stats.games)
    valid_games = count is not None and count > 0
    ranks = [getattr(stats, name, None) for name in ("first_rate", "second_rate", "third_rate")]
    if mode == 4:
        ranks.append(getattr(stats, "fourth_rate", None))
    ranks = [ratio(v) for v in ranks]
    if not valid_games or any(v is None for v in ranks) or abs(sum(ranks) - 1) > 0.02:
        ranks = [None] * mode
    average = number(stats.average_rank)
    if not valid_games or average is None or not 1 <= average <= mode:
        average = None
    win_points, deal_points, difference = (
        scoring_metrics(stats) if valid_games else (None, None, None)
    )
    current = rank_progress(raw.get("level"))
    raw_level = raw.get("level")
    code = number(raw_level.get("id") if isinstance(raw_level, dict) else raw_level)
    major = current["major"] if current else int(code) % 10000 // 100 if code else None
    if major == 6:
        major = 7
    label = level_label(raw_level)
    progress = None
    if current:
        label = rank_label(current, full=True)
        point_text_value = point_text(current, spaced=True, unit=True)
        if current["cap"]:
            progress = min(1, max(0, current["points"] / current["cap"]))
    else:
        point_text_value = ""
    highest = raw.get("max_level")
    high = rank_progress(highest)
    highest_text = format_level(highest) if high else level_label(highest)
    if highest_text == "未知段位":
        highest_text = ""
    highest_rank = rank_label(high, full=True) if high else highest_text
    highest_points = point_text(high, spaced=True) if high else ""
    style = analyze_style(stats, mode)
    tag = "#" + style.label if style.x is not None else ""
    try:
        friend = str(account_to_friend(player.account_id))
    except ValueError:
        friend = "—"
    groups = [
        (
            "和牌与打点",
            "#289479",
            [
                ("和牌率", metric(stats.win_rate)),
                ("平均打点", metric(win_points, "points")),
                ("自摸占比", metric(raw.get("自摸率"))),
                ("默和占比", metric(raw.get("默听率"))),
                ("和牌巡数", metric(raw.get("和了巡数"), "number")),
                ("一发率", metric(raw.get("一发率"))),
            ],
        ),
        (
            "防守与流局",
            "#bf805e",
            [
                ("放铳率", metric(stats.deal_in_rate)),
                ("平均铳点", metric(deal_points, "points")),
                ("流局率", metric(raw.get("流局率"))),
                ("流局听牌率", metric(raw.get("流听率"))),
                ("立直后放铳率", metric(raw.get("立直后放铳率"))),
                ("副露后放铳率", metric(raw.get("副露后放铳率"))),
            ],
        ),
        (
            "立直与副露",
            "#6376d9",
            [
                ("立直率", metric(stats.riichi_rate)),
                ("副露率", metric(stats.call_rate)),
                ("立直巡目", metric(raw.get("立直巡目"), "number")),
                ("先制立直占比", metric(raw.get("先制率"))),
                ("追立占比", metric(raw.get("追立率"))),
                ("立直后和牌率", metric(raw.get("立直后和牌率"))),
            ],
        ),
    ]
    if not valid_games:
        groups = [
            (title, color, [(label, "—") for label, _ in rows]) for title, color, rows in groups
        ]
    return {
        "name": player.nickname,
        "mode": mode,
        "mode_label": stats.mode,
        "games": "—" if count is None or count < 0 else f"{count:,.0f}",
        "friend": friend,
        "rank": label,
        "rank_major": major,
        "point_text": point_text_value,
        "rank_data": current or rank_identity(raw_level),
        "highest_rank_data": high or rank_identity(highest),
        "progress": progress,
        "highest": highest_text,
        "highest_rank": highest_rank,
        "highest_points": highest_points,
        "tag": tag,
        "extra_tags": supplemental_tags(stats, games, player.account_id, mode=mode)
        if games
        else [],
        "scope": scope,
        "ranks": ranks,
        "hero": [
            ("平均顺位", metric(average, "number")),
            ("连对率", metric(None if ranks[0] is None else ranks[0] + ranks[1])),
            ("和铳度差", metric(difference, "signed")),
            ("击飞率", metric(stats.negative_rate if valid_games else None)),
        ],
        "groups": groups,
    }


def render_profile(player, stats, *, scope=None, games=()):
    content = profile_content(player, stats, scope=scope, games=games)
    tag_lines = []
    line = []
    line_width = 0
    tag_font = face(22)
    gap = tag_font.getlength(" ")
    for tag in content["extra_tags"]:
        label = f"#{tag}"
        width = tag_font.getlength(label)
        if line and line_width + width > 1472:
            tag_lines.append(line)
            line = []
            line_width = 0
        line.append((label, line_width, is_named_yakuman_tag(tag)))
        line_width += width + gap
    if line:
        tag_lines.append(line)
    size = (SIZE[0], SIZE[1] + (110 + 38 * max(0, len(tag_lines) - 2) if tag_lines else 0))
    image = Image.new("RGBA", size, "#f2f5fa")
    shadow = Image.new("RGBA", size)
    sd = ImageDraw.Draw(shadow)
    cards = [(40, 32, 1560, 240), (40, 436, 1560, 626)]
    cards += [(40 + 384 * i, 260, 408 + 384 * i, 416) for i in range(4)]
    cards += [(40 + 512 * i, 646, 536 + 512 * i, 1068) for i in range(3)]
    for x, y, right, bottom in cards:
        sd.rounded_rectangle((x, y + 6, right, bottom + 6), radius=24, fill=(30, 49, 80, 16))
    image = Image.alpha_composite(image, shadow.filter(ImageFilter.GaussianBlur(12)))
    draw = ImageDraw.Draw(image)
    for box in cards:
        draw.rounded_rectangle(box, radius=24, fill="white", outline="#e6ecf4")

    def text(x, y, value, size=24, color=INK, style="regular", anchor="lm"):
        draw.text((x, y), str(value), font=face(size, style), fill=color, anchor=anchor)

    def fitted(value, max_width, size, style="regular", minimum=20):
        while size > minimum and draw.textlength(value, font=face(size, style)) > max_width:
            size -= 1
        while value and draw.textlength(value, font=face(size, style)) > max_width:
            value = value[:-2].rstrip("…") + "…" if len(value) > 1 else ""
        return value, size

    draw_profile_header(
        image,
        box=cards[0],
        name=content["name"],
        subtitle=content["scope"] or f"{content['mode_label']}　已收录 {content['games']} 场",
        current_rank=content["rank"],
        highest_rank=content["highest_rank"],
        current_points=content["point_text"].removesuffix(" PT"),
        highest_points=content["highest_points"],
        progress=content["progress"],
        tag=content["tag"],
        icon=(
            rank_badge(
                content["rank_data"]["major"], content["rank_data"]["star"], content["mode"], 100
            )
            if content["rank_data"]
            else None
        ),
        rank_data=content["rank_data"],
        highest_rank_data=content["highest_rank_data"],
        rank_mode=content["mode"],
        current_color=SOUL_COLOR if content["rank_major"] == 7 else INK,
        highest_color=SOUL_COLOR
        if content["highest_rank_data"] and content["highest_rank_data"]["major"] == 7
        else "#75618c",
    )

    for i, (label, value) in enumerate(content["hero"]):
        x = 68 + 384 * i
        text(x, 300, label, 22, MUTED)
        color = "#288772" if i == 2 and not value.startswith(("—", "-")) else INK
        value, size = fitted(value, 312, 48, "number")
        text(x, 363, value, size, color, "number")

    text(72, 474, "顺位分布", 25, style="bold")
    draw.rounded_rectangle((72, 511, 1528, 529), radius=9, fill="#edf1f6")
    if content["ranks"][0] is not None:
        bar = Image.new("RGBA", (1456, 18))
        bd = ImageDraw.Draw(bar)
        position = 0
        total = sum(content["ranks"])
        for i, value in enumerate(content["ranks"]):
            end = round(position + value / total * 1456)
            if end > round(position):
                bd.rectangle((round(position), 0, end - 1, 17), fill=RANK_COLORS[i])
            position += value / total * 1456
        mask = Image.new("L", (1456, 18))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 1455, 17), radius=9, fill=255)
        image.paste(bar, (72, 511), mask)
    for i, rate in enumerate(content["ranks"]):
        x = 76 + i * 1456 / content["mode"]
        draw.rounded_rectangle((x, 559, x + 64, 595), radius=10, fill=RANK_COLORS[i])
        text(x + 32, 577, f"{i + 1}位", 20, "white", "bold", "mm")
        text(x + 84, 577, metric(rate), 30, INK, "number")

    for i, (title, color, rows) in enumerate(content["groups"]):
        x = 40 + 512 * i
        draw.rounded_rectangle((x + 28, 674, x + 33, 699), radius=2, fill=color)
        text(x + 48, 686, title, 26, style="bold")
        for index, (label, value) in enumerate(rows):
            y = 744 + index * 54
            text(x + 28, y, label, 23, MUTED)
            value, size = fitted(value, 180, 28, "number", minimum=18)
            text(x + 468, y, value, size, INK, "number", "rm")
            if index < len(rows) - 1:
                draw.line((x + 28, y + 26, x + 468, y + 26), fill="#edf1f6")
    text(64, 1108, "好友码 " + content["friend"], 20, "#8b99ae")
    for i, line in enumerate(tag_lines):
        for label, offset, gold in line:
            text(64 + offset, 1159 + i * 38, label, 22, "#986b1d" if gold else "#7561a8")
    output = io.BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
