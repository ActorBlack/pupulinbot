"""Readable Tenhou profile, replay-list and trend images from nodocchi data."""

import io
import math
from datetime import datetime

from PIL import Image, ImageDraw

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_charts import font
from .ms_sample import number, rolling_ranks
from .profile_header import draw_profile_header
from .tenhou import CN, LOG_ID, grade_name, rule_name, table_scores
from .tenhou_card import rank_theme
from .tenhou_profile import history_rank
from .tenhou_queries import value
from .typography import profile_face as face

INK, MUTED = "#1d2c44", "#6c7b90"
COLORS = ("#4077df", "#189b91", "#dcab46", "#cc8296")
SIZE = (1600, 1350)


class Canvas:
    def __init__(self, size):
        self.image = Image.new("RGB", size, "#f2f5fa")
        self.draw = ImageDraw.Draw(self.image)

    def card(self, box):
        x, y, r, b = box
        self.draw.rounded_rectangle((x, y + 5, r, b + 5), radius=22, fill="#e8edf5")
        self.draw.rounded_rectangle(box, radius=22, fill="white", outline="#e6ecf4")

    def text(self, x, y, text, size=24, color=INK, style="regular", anchor="lm", width=None):
        text = str(text)
        if width:
            while size > 18 and self.draw.textlength(text, font=face(size, style)) > width:
                size -= 1
            while text and self.draw.textlength(text, font=face(size, style)) > width:
                text = text[:-2] + "…" if len(text) > 1 else ""
        self.draw.text((x, y), text, font=face(size, style), fill=color, anchor=anchor)

    def png(self):
        output = io.BytesIO()
        self.image.save(output, format="PNG", optimize=True)
        return output.getvalue()


def percentile_label(pct):
    pct = number(pct)
    if pct is None or not 0 <= pct <= 100:
        return "—"
    tail = pct if pct < 50 else 100 - pct
    rounded = max(1, math.floor(tail + 0.5))
    return f"{'低位' if pct < 50 else '高位'} {rounded}%"


def draw_percentile(c, x, y, pct, color):
    """Higher position means a larger metric value, regardless of desirability."""
    width = 294
    c.draw.rounded_rectangle((x, y - 6, x + width, y + 6), radius=6, fill="#edf1f6")
    pct = number(pct)
    if pct is not None and 0 <= pct <= 100:
        end = x + width * pct / 100
        pale = tuple(round(int(color[i : i + 2], 16) * 0.24 + 255 * 0.76) for i in (1, 3, 5))
        if end > x:
            c.draw.rounded_rectangle((x, y - 6, max(x + 6, end), y + 6), radius=6, fill=pale)
    else:
        end = None
    c.draw.line((x + width / 2, y - 8, x + width / 2, y + 8), fill="#a6b2c4", width=2)
    if end is not None:
        c.draw.ellipse((end - 6, y - 6, end + 6, y + 6), fill=color, outline="white", width=2)


def render_profile(view):
    c = Canvas(SIZE)
    c.card((40, 32, 1560, 210))
    mode = "三麻" if view["mode"] == 3 else "四麻"
    subtitle = (
        f"{mode}　{view['scope']}最近 {view['count']:,} 场"
        if view["recent"]
        else f"{mode}　已收录 {view['count']:,} 场"
    )
    progress = view["progress"]
    current_grade, highest_grade = progress.get("estimated_grade"), progress.get("high_grade")
    pt, cap = progress.get("pt"), progress.get("pt_max")
    point_text = f"{pt:g} / {cap:g}" if pt is not None and cap is not None else "—"
    high_pt = progress.get("high_pt")
    color, _ = rank_theme(current_grade)
    high_color, _ = rank_theme(highest_grade)
    draw_profile_header(
        c.image,
        box=(40, 32, 1560, 210),
        name=view["name"],
        subtitle=subtitle,
        current_rank=grade_name(current_grade) if current_grade is not None else "—",
        highest_rank=grade_name(highest_grade) if highest_grade is not None else "—",
        current_points=point_text,
        highest_points=f"{high_pt:g}" if high_pt is not None else "—",
        progress=pt / cap if pt is not None and cap else None,
        current_color=color,
        highest_color=high_color,
        progress_color=color,
        show_rating=True,
        rating=value(view["rate"], decimals=0),
        highest_rating=value(view["highest_rate"], decimals=0),
    )
    hero = [
        ("平均顺位", f"{view['average']:.2f}"),
        ("连对率", value(view["top2"], percent=True)),
        ("一位率", value(view["counts"][0] / view["count"], percent=True)),
        ("末位率", value(view["counts"][-1] / view["count"], percent=True)),
    ]
    for index, (label, text) in enumerate(hero):
        x = 40 + 384 * index
        c.card((x, 230, x + 368, 370))
        c.text(x + 28, 269, label, 23, MUTED)
        c.text(x + 28, 324, text, 48, style="number", width=310)
    c.card((40, 390, 1560, 565))
    c.text(72, 428, "顺位分布", 25, style="bold")
    start = 72
    for index, count in enumerate(view["counts"]):
        end = 72 + round(sum(view["counts"][: index + 1]) / view["count"] * 1456)
        if end > start:
            c.draw.rectangle((start, 465, end - 1, 482), fill=COLORS[index])
        start = end
        x = 76 + index * 1456 / view["mode"]
        c.draw.rounded_rectangle((x, 511, x + 64, 547), radius=10, fill=COLORS[index])
        c.text(x + 32, 529, f"{index + 1}位", 20, "white", "bold", "mm")
        c.text(x + 84, 529, value(count / view["count"], percent=True), 30, style="number")
    p = view["phoenix"]
    caption = (
        f"凤凰桌统计　{view['phoenix_scope']}（{p.get('totalrecord', '—')} 战）"
        if p
        else "凤凰桌统计暂时不可用"
        if view["phoenix_error"]
        else "暂无凤凰桌统计"
    )
    c.text(64, 599, caption, 25, style="bold", width=880)
    positions = view.get("percentiles", {})
    legend = "同类百分位（中线为中位数）" if positions else "暂无可用对照分布"
    c.text(1534, 599, legend, 18, MUTED, anchor="rm", width=550)
    groups = [
        (
            "和牌与打点",
            "#289479",
            [
                ("和牌率", value(p.get("agariC"), percent=True), "agariC"),
                ("平均打点", value(p.get("agariVT"), decimals=0), "agariVT"),
                ("自摸占比", value(p.get("tsumoV"), percent=True), "tsumoV"),
                ("默和占比", value(p.get("damaV"), percent=True), "damaV"),
                ("和牌巡数", value(p.get("agariVJ"), decimals=2), "agariVJ"),
                ("一发率", value(p.get("ippatsuV"), percent=True), "ippatsuV"),
            ],
        ),
        (
            "防守与流局",
            "#bf805e",
            [
                ("放铳率", value(p.get("houjuuC"), percent=True), "houjuuC"),
                ("平均铳点", value(p.get("houjuuVT"), decimals=0), "houjuuVT"),
                ("流局听牌率", value(p.get("nagaretenpaiV"), percent=True), "nagaretenpaiV"),
                ("局收支", value(p.get("shuushiCT"), signed=True), "shuushiCT"),
                ("和铳度差", value(view["difference"], signed=True), "agari_minus_houjuu_CT"),
                ("击飞率", value(p.get("tobiZ"), percent=True), "tobiZ"),
            ],
        ),
        (
            "立直与副露",
            "#6376d9",
            [
                ("立直率", value(p.get("riichC"), percent=True), "riichC"),
                ("副露率", value(p.get("fuuroC"), percent=True), "fuuroC"),
                ("先制立直占比", value(p.get("riichsenV"), percent=True), "riichsenV"),
                ("立直成功率", value(p.get("riich_seikou_V"), percent=True), "riich_seikou_V"),
                (
                    "安定段",
                    value(p.get("stablerank_phoenix_X"), decimals=2),
                    "stablerank_phoenix_X",
                ),
                ("平均顺位", value(p.get("order_Z"), decimals=2), "order_Z"),
            ],
        ),
    ]
    for index, (title, color, rows) in enumerate(groups):
        x = 40 + 512 * index
        c.card((x, 628, x + 496, 1154))
        c.draw.rounded_rectangle((x + 28, 655, x + 33, 681), radius=2, fill=color)
        c.text(x + 48, 668, title, 26, style="bold")
        for i, (label, text, key) in enumerate(rows):
            y = 724 + i * 76
            c.text(x + 28, y, label, 23, MUTED)
            c.text(x + 468, y, text, 28, style="number", anchor="rm", width=180)
            pct = positions.get(key)
            draw_percentile(c, x + 28, y + 28, pct, color)
            c.text(x + 468, y + 28, percentile_label(pct), 18, MUTED, anchor="rm", width=135)
            if i < 5:
                c.draw.line((x + 28, y + 53, x + 468, y + 53), fill="#edf1f6")
    c.card((40, 1176, 1560, 1301))
    c.text(72, 1213, "近10场", 22, MUTED)
    for index, rank in enumerate(view["ranks"][-10:]):
        x = 178 + index * 39
        c.draw.rounded_rectangle((x, 1198, x + 31, 1229), radius=8, fill=COLORS[rank - 1])
        c.text(x + 15, 1213, rank, 20, "white", "number", "mm")
    c.text(650, 1213, "近1日 / 7日 / 30日    " + " / ".join(map(str, view["activity"])), 23, MUTED)
    c.text(
        72,
        1262,
        "平均结算点 "
        + value(view["average_result"], signed=True)
        + "    最近对战 "
        + datetime.fromtimestamp(view["latest"], CN).strftime("%m-%d %H:%M"),
        22,
        MUTED,
    )
    if view["tag"]:
        c.text(1500, 1262, "#" + view["tag"], 23, "#288772", "bold", "rm", width=370)
    c.text(64, 1326, "水表网 nodocchi　段位积分为公开历史推定", 18, "#8b99ae")
    return c.png()


def render_recent(name, rows, mode):
    c = Canvas((1600, 220 + 94 * len(rows)))
    c.card((40, 32, 1560, 166 + 94 * len(rows)))
    c.text(76, 86, f"{name}　最近 {len(rows)} 场", 40, style="title", width=1100)
    labels = [(76, "时间"), (315, "规则"), (565, "顺位"), (700, "得点"), (920, "同桌玩家")]
    for x, label in labels:
        c.text(x, 143, label, 21, MUTED)
    for index, row in enumerate(reversed(rows)):
        y = 192 + index * 94
        rank = history_rank(row, name)
        scores = table_scores(row)
        score = str(scores[rank - 1]) if scores else f"结算 {float(row[f'player{rank}ptr']):+g}"
        c.text(
            76,
            y,
            datetime.fromtimestamp(float(row["starttime"]), CN).strftime("%m-%d %H:%M"),
            25,
            style="number",
        )
        c.text(315, y, rule_name(row), 23, width=220)
        c.text(565, y, f"{rank}位", 25, COLORS[rank - 1], "bold")
        c.text(700, y, score, 25, style="number" if scores else "regular", width=195)
        names = " / ".join(row[f"player{i}"] for i in range(1, mode + 1))
        c.text(920, y, names, 22, width=580)
        match = LOG_ID.search(row.get("url", ""))
        c.text(315, y + 31, match[1] if match else "该记录未提供牌谱链接", 17, MUTED)
        c.draw.line((76, y + 56, 1524, y + 56), fill="#edf1f6")
    c.text(64, c.image.height - 28, "水表网 nodocchi", 18, "#8b99ae")
    return c.png()


def render_life(view):
    with _RENDER_LOCK:
        fig = Figure(figsize=(13, 8), dpi=130, facecolor="#f2f5fa")
        FigureCanvasAgg(fig)
        fig.text(
            0.07,
            0.94,
            f"{view['name']}　最近 {view['count']} 场",
            fontproperties=font("bold"),
            fontsize=25,
            color=INK,
        )
        fig.text(
            0.07,
            0.89,
            f"{'三麻' if view['mode'] == 3 else '四麻'}　{view['scope']}　"
            f"均顺 {view['average']:.2f}　连对率 {view['top2'] * 100:.1f}%",
            fontproperties=font(),
            fontsize=13,
            color=MUTED,
        )
        xs = list(range(1, view["count"] + 1))
        ranks = fig.add_axes((0.09, 0.51, 0.85, 0.29), facecolor="white")
        ranks.plot(xs, view["ranks"], color="#8ca8df", linewidth=1, alpha=0.65)
        moving = rolling_ranks(view["ranks"])
        if any(math.isfinite(n) for n in moving):
            ranks.plot(xs, moving, color="#d58c32", linewidth=2.4, label="10场平均顺位")
            ranks.legend(prop=font(), frameon=False)
        ranks.set_yticks(range(1, view["mode"] + 1))
        ranks.set_ylim(view["mode"] + 0.3, 0.7)
        ranks.set_ylabel("顺位", fontproperties=font())
        rating = fig.add_axes((0.09, 0.13, 0.85, 0.25), facecolor="white")
        values = [number(r.get("rate")) for r in view["rows"]]
        if any(v is not None for v in values):
            rating.plot(
                xs, [v if v is not None else math.nan for v in values], color="#328d83", linewidth=2
            )
        else:
            rating.text(
                0.5,
                0.5,
                "该样本未提供 R 值",
                transform=rating.transAxes,
                ha="center",
                fontproperties=font(),
                color=MUTED,
            )
        rating.set_ylabel("R值", fontproperties=font())
        rating.set_xlabel("场次", fontproperties=font())
        for ax in (ranks, rating):
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=0.15)
            ax.tick_params(colors=MUTED)
        fig.text(0.07, 0.04, "水表网 nodocchi", fontproperties=font(), fontsize=10, color=MUTED)
        output = io.BytesIO()
        fig.savefig(output, format="png", facecolor=fig.get_facecolor())
        return output.getvalue()
