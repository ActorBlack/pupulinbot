"""Readable Tenhou profile, replay-list and trend images from nodocchi data."""

import io
import math
from datetime import datetime

from matplotlib.ticker import MaxNLocator, StrMethodFormatter
from PIL import Image, ImageDraw

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_charts import font
from .ms_sample import number, rolling_ranks
from .profile_header import draw_profile_header
from .tenhou import CN, LOG_ID, grade_name, rule_name, table_scores
from .tenhou_card import rank_theme
from .tenhou_options import ROOMS
from .tenhou_profile import history_rank
from .tenhou_queries import value
from .typography import profile_face as face

INK, MUTED = "#1d2c44", "#536982"
COLORS = ("#4077df", "#189b91", "#dcab46", "#cc8296")
SIZE = (1600, 1350)
STYLE_SIZE = (1600, 1040)
BLUE, TEAL, AMBER, ROSE = "#315fbd", "#167f74", "#b47622", "#b8556d"
GRID = "#e6edf5"


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


def period_label(rows):
    first, last = (
        datetime.fromtimestamp(float(row["starttime"]), CN).date() for row in (rows[0], rows[-1])
    )
    return str(first) if first == last else f"{first} 至 {last}"


def chart_header(c, view, title):
    c.card((40, 28, 1560, 148))
    c.text(72, 71, view["name"], 42, style="title", width=940)
    c.text(1528, 72, title, 33, BLUE, "bold", "rm", 420)
    mode = "三麻" if view["mode"] == 3 else "四麻"
    c.text(74, 116, f"{mode}{view['scope']}　{view['count']:,} 场", 22, MUTED)
    c.text(1528, 116, period_label(view["rows"]), 21, MUTED, anchor="rm")


def stat_cards(c, items, *, top=168, height=108):
    span = (1520 - 16 * (len(items) - 1)) / len(items)
    for i, (label, reading, accent) in enumerate(items):
        x = 40 + i * (span + 16)
        c.card((x, top, x + span, top + height))
        c.draw.rounded_rectangle((x + 24, top + 17, x + 53, top + 21), radius=2, fill=accent)
        c.text(x + 24, top + 39, label, 20, MUTED, width=span - 48)
        style = "bold" if any("\u3400" <= ch <= "\u9fff" for ch in str(reading)) else "number"
        c.text(x + 24, top + height * 0.72, reading, 39, accent, style, width=span - 48)


def plot_card(c, box, title, detail=""):
    c.card(box)
    left, top, right, _ = box
    c.text(left + 28, top + 36, title, 26, style="bold")
    c.text(right - 28, top + 36, detail, 19, MUTED, anchor="rm", width=right - left - 220)


def paint_plot(c, box, plot):
    """Draw a plot into a card while retaining the same typography and spacing."""
    x, y, right, bottom = box
    width, height = right - x, bottom - y
    with _RENDER_LOCK:
        figure = Figure(figsize=(width / 100, height / 100), dpi=100, facecolor="white")
        canvas = FigureCanvasAgg(figure)
        plot(figure)
        canvas.draw()
        bitmap = Image.frombuffer("RGBA", canvas.get_width_height(), canvas.buffer_rgba()).convert(
            "RGB"
        )
        c.image.paste(bitmap, (x, y))


def style_axis(ax, *, xlabel="", ylabel="", integer=False):
    ax.spines[["top", "right", "left"]].set_visible(False)
    ax.spines["bottom"].set_color("#cbd8e7")
    ax.set_axisbelow(True)
    ax.grid(axis="y", linewidth=0.8, color=GRID)
    ax.tick_params(length=0, pad=6, labelsize=11, colors=MUTED)
    if xlabel:
        ax.set_xlabel(xlabel, fontproperties=font(), fontsize=11, color=MUTED)
    if ylabel:
        ax.set_ylabel(ylabel, fontproperties=font(), fontsize=11, color=MUTED)
    if integer:
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
        ax.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))


def number_text(value, *, signed=False, precision=0):
    return "—" if value is None else f"{value:{'+' if signed else ''},.{precision}f}"


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
    phoenix_room = view["room"] == 3
    c = Canvas(SIZE if phoenix_room else (1600, 765))
    c.card((40, 32, 1560, 210))
    mode = "三麻" if view["mode"] == 3 else "四麻"
    window = "所选日期" if view["period_filtered"] else "最近" if view["recent"] else "已收录"
    subtitle = f"{mode}{view['scope']}　{window} {view['count']:,} 场"
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
    stat_cards(
        c,
        [
            ("平均顺位", f"{view['average']:.2f}", BLUE),
            ("连对率", value(view["top2"], percent=True), TEAL),
            ("一位率", value(view["counts"][0] / view["count"], percent=True), AMBER),
            ("末位率", value(view["counts"][-1] / view["count"], percent=True), ROSE),
        ],
        top=230,
        height=140,
    )
    c.card((40, 390, 1560, 565))
    c.text(72, 428, "顺位分布", 25, style="bold")
    c.text(1528, 428, f"{view['count']:,} 场", 20, MUTED, "number", "rm")
    start = 72
    for index, count in enumerate(view["counts"]):
        end = 72 + round(sum(view["counts"][: index + 1]) / view["count"] * 1456)
        if end > start:
            c.draw.rectangle((start, 465, end - 1, 482), fill=COLORS[index])
        start = end
        x = 76 + index * 1456 / view["mode"]
        c.draw.rounded_rectangle((x, 511, x + 64, 547), radius=10, fill=COLORS[index])
        c.text(x + 32, 529, f"{index + 1}位", 20, "white", "bold", "mm")
        c.text(x + 84, 529, value(count / view["count"], percent=True), 29, style="number")
        c.text(x + 210, 529, f"{count:,} 场", 18, MUTED, width=135)
    if phoenix_room:
        p = view["phoenix"]
        caption = (
            f"凤凰桌累计　{view['phoenix_scope']}（{p.get('totalrecord', '—')} 战）"
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
    foot = 1176 if phoenix_room else 591
    c.card((40, foot, 1560, foot + 125))
    c.text(72, foot + 37, "近10场", 22, MUTED)
    for index, rank in enumerate(view["ranks"][-10:]):
        x = 178 + index * 39
        c.draw.rounded_rectangle((x, foot + 22, x + 31, foot + 53), radius=8, fill=COLORS[rank - 1])
        c.text(x + 15, foot + 37, rank, 20, "white", "number", "mm")
    c.text(
        650, foot + 37, "近1日 / 7日 / 30日    " + " / ".join(map(str, view["activity"])), 23, MUTED
    )
    c.text(
        72,
        foot + 86,
        "平均结算点 "
        + value(view["average_result"], signed=True)
        + "    最近对战 "
        + datetime.fromtimestamp(view["latest"], CN).strftime("%m-%d %H:%M"),
        22,
        MUTED,
    )
    if view["tag"]:
        c.text(1500, foot + 86, "#" + view["tag"], 23, "#288772", "bold", "rm", width=370)
    c.text(64, foot + 150, "水表网 nodocchi　段位积分为公开历史推定", 18, "#8b99ae")
    return c.png()


def render_recent(name, rows, mode, *, pt_deltas=None):
    last = rows[-1]
    room = ROOMS[int(last["playerlevel"])] + ("东" if int(last["playlength"]) == 1 else "南")
    c = Canvas((1600, 286 + 78 * len(rows)))
    chart_header(c, dict(name=name, rows=rows, count=len(rows), scope=room, mode=mode), "最近牌谱")
    c.card((40, 168, 1560, 234 + 78 * len(rows)))
    c.draw.rounded_rectangle((57, 185, 1543, 230), radius=10, fill="#e9eff8")
    labels = (
        (76, "时间"),
        (285, "规则"),
        (572, "顺位"),
        (806, "得点"),
        (968, "PT（推算）"),
        (1022, "同桌玩家"),
    )
    for index, (x, label) in enumerate(labels):
        anchor = "mm" if index == 2 else "rm" if index in (3, 4) else "lm"
        c.text(x, 207, label, 20, MUTED, "bold", anchor)
    for i, row in enumerate(reversed(rows)):
        y = 265 + i * 78
        if i % 2 == 0:
            c.draw.rounded_rectangle((57, y - 29, 1543, y + 42), radius=10, fill="#f3f6fb")
        rank = history_rank(row, name)
        scores = table_scores(row)
        score = number_text(scores[rank - 1]) if scores else f"{float(row[f'player{rank}ptr']):+g}"
        stamp = datetime.fromtimestamp(float(row["starttime"]), CN)
        c.text(76, y, stamp.strftime("%m-%d %H:%M"), 24, style="number")
        c.text(285, y, rule_name(row), 23, width=240)
        c.draw.rounded_rectangle((540, y - 15, 604, y + 15), radius=8, fill=COLORS[rank - 1])
        c.text(572, y, f"{rank}位", 18, "white", "bold", "mm")
        c.text(806, y, score, 25, style="number", anchor="rm")
        if scores is None:
            c.text(806, y + 25, "结算点", 15, MUTED, anchor="rm")
        delta = pt_deltas[-1 - i] if pt_deltas is not None else None
        color = MUTED if not delta else TEAL if delta > 0 else ROSE
        c.text(968, y, number_text(delta, signed=True), 24, color, "number", "rm")
        names = " / ".join(row[f"player{j}"] for j in range(1, mode + 1) if j != rank)
        c.text(1022, y, names, 22, width=492)
        match = LOG_ID.search(row.get("url") or "")
        if match:
            c.text(285, y + 26, match[1], 16, MUTED, "number", width=500)
    c.text(42, c.image.height - 26, "水表网 nodocchi", 18, MUTED)
    return c.png()


def render_life(view):
    deltas = view["pt_deltas"]
    known = [d for d in deltas if d is not None]
    values = [number(r.get("rate")) for r in view["rows"]]
    c = Canvas((1600, 1390 if known else 870))
    chart_header(c, view, "段位积分走势" if known else "战绩走势")
    xs = list(range(1, view["count"] + 1))
    bounds = (0, len(xs) + 0.45)

    def setup(ax, label="", *, integer=True):
        style_axis(ax, ylabel=label, integer=integer)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=10, integer=True))
        ax.set_xlim(bounds)

    if known:
        curve = [0]
        peak = drawdown = 0
        peak_game = 0
        trough = None
        for i, change in enumerate(deltas, 1):
            current = curve[-1] + change if change is not None else math.nan
            curve.append(current)
            if current >= peak:
                peak, peak_game = current, i
            elif peak - current > drawdown:
                drawdown, trough = peak - current, (peak_game, i)
        complete = len(known) == len(deltas)
        stat_cards(
            c,
            [
                (
                    "净变化" if complete else f"已知净变化 {len(known)}/{len(deltas)} 场",
                    number_text(sum(known), signed=True),
                    BLUE,
                ),
                ("总升分", number_text(sum(d for d in known if d > 0), signed=True), TEAL),
                ("总掉分", number_text(sum(d for d in known if d < 0), signed=True), ROSE),
                ("最大回撤", number_text(drawdown if complete else None), AMBER),
            ],
        )
        note = (
            f"单场最高 {number_text(max(known), signed=True)}　"
            f"最低 {number_text(min(known), signed=True)}"
        )
        plot_card(c, (40, 298, 1560, 795), "段位积分", note)

        def points(fig):
            line = fig.add_axes((0.070, 0.53, 0.895, 0.43))
            bars = fig.add_axes((0.070, 0.14, 0.895, 0.28), sharex=line)
            setup(line, "累计 PT")
            setup(bars, "单场 PT")
            if complete and trough:
                line.axvspan(*trough, color="#f9eaf0", zorder=0)
            line.axhline(0, color="#b7c5d7", linewidth=0.9)
            line.plot([0, *xs], curve, color=BLUE, linewidth=2.35, zorder=4)
            line.fill_between([0, *xs], 0, curve, color=BLUE, alpha=0.075)
            line.tick_params(labelbottom=False)
            bars.axhline(0, color="#aebed2", linewidth=0.9)
            bars.bar(
                xs,
                [d if d is not None else math.nan for d in deltas],
                color=[TEAL if d and d > 0 else ROSE for d in deltas],
                width=0.77,
                alpha=0.82,
            )
            if not complete:
                line.text(
                    0.99,
                    0.92,
                    "累计线止于首个缺失值",
                    transform=line.transAxes,
                    ha="right",
                    va="top",
                    fontproperties=font(),
                    color=MUTED,
                )

        paint_plot(c, (52, 356, 1548, 779), points)
        rank_top, rating_top = 815, 1080
    else:
        stat_cards(
            c,
            [
                ("平均顺位", f"{view['average']:.2f}", BLUE),
                ("连对率", value(view["top2"], percent=True), TEAL),
                ("推算 PT", "—", MUTED),
            ],
        )
        rank_top, rating_top = 298, 563

    plot_card(
        c,
        (40, rank_top, 1560, rank_top + 245),
        "顺位走势",
        f"均顺 {view['average']:.2f}　连对率 {value(view['top2'], percent=True)}",
    )

    def ranks_plot(fig):
        ax = fig.add_axes((0.07, 0.21, 0.895, 0.72))
        setup(ax, "顺位")
        ax.plot(
            xs, view["ranks"], color="#a1b9e8", linewidth=1.15, marker="." if len(xs) < 15 else ""
        )
        moving = rolling_ranks(view["ranks"])
        if any(math.isfinite(n) for n in moving):
            ax.plot(xs, moving, color=AMBER, linewidth=2.25, label="10 场平均顺位")
            ax.legend(loc="lower left", prop=font(), frameon=False)
        ax.set_yticks(range(1, view["mode"] + 1))
        ax.set_ylim(view["mode"] + 0.28, 0.72)

    paint_plot(c, (52, rank_top + 55, 1548, rank_top + 232), ranks_plot)
    rating_values = [r for r in values if r is not None]
    detail = (
        f"最高 {number_text(max(rating_values))}　最低 {number_text(min(rating_values))}"
        if rating_values
        else ""
    )
    plot_card(c, (40, rating_top, 1560, rating_top + 255), "R 值走势", detail)

    def rate_plot(fig):
        ax = fig.add_axes((0.07, 0.28, 0.895, 0.66))
        setup(ax, "R 值")
        ax.set_xlabel("对局序号", fontproperties=font(), color=MUTED)
        if rating_values:
            ax.plot(
                xs,
                [r if r is not None else math.nan for r in values],
                color=TEAL,
                linewidth=2.2,
                marker="." if len(xs) < 15 else "",
            )
        else:
            ax.set_yticks([])
            ax.text(
                0.5,
                0.5,
                "该样本暂无 R 值",
                transform=ax.transAxes,
                ha="center",
                fontproperties=font(),
                color=MUTED,
            )

    paint_plot(c, (52, rating_top + 57, 1548, rating_top + 242), rate_plot)
    c.text(
        42,
        c.image.height - 27,
        "水表网 nodocchi　PT 为公开历史推算" if known else "水表网 nodocchi",
        18,
        MUTED,
    )
    return c.png()


def render_style(view):
    c = Canvas(STYLE_SIZE)
    p, positions = view["phoenix"], view["percentiles"]
    groups = (
        ("进攻", False, (("和牌率", "agariC", "%"), ("自摸占比", "tsumoV", "%"))),
        ("防守", True, (("放铳率", "houjuuC", "%"), ("平均铳点", "houjuuVT", "点"))),
        (
            "立直",
            False,
            (
                ("立直率", "riichC", "%"),
                ("先制立直", "riichsenV", "%"),
                ("立直成功率", "riich_seikou_V", "%"),
            ),
        ),
        ("速度", True, (("和牌巡数", "agariVJ", "巡"),)),
        ("打点", False, (("平均打点", "agariVT", "点"), ("局收支", "shuushiCT", "±点"))),
        ("副露", False, (("副露率", "fuuroC", "%"),)),
    )
    c.card((40, 28, 1560, 148))
    c.text(72, 71, view["name"], 42, style="title", width=940)
    c.text(1528, 72, "牌风雷达", 33, BLUE, "bold", "rm")
    c.text(
        74, 116, f"{view['phoenix_scope']}　凤凰桌累计 {p.get('totalrecord', '—')} 场", 22, MUTED
    )
    c.text(
        1528,
        116,
        f"安定段 {value(p.get('stablerank_phoenix_X'), decimals=2)}",
        22,
        MUTED,
        anchor="rm",
    )
    plot_card(c, (40, 168, 684, 981), "同类百分位", "中位数 = 50")
    plot_card(c, (704, 168, 1560, 981), "各维度指标")
    c.draw.rounded_rectangle((722, 230, 1542, 275), radius=10, fill="#e9eff8")
    for x, label in ((745, "维度"), (914, "指标"), (1300, "本人"), (1523, "同类百分位")):
        c.text(x, 252, label, 20, MUTED, "bold", "rm" if x > 1000 else "lm")

    cx, cy, radius = 362, 560, 202
    angles = [i * math.tau / 6 - math.pi / 2 for i in range(6)]

    def vertices(factors):
        return [
            (cx + radius * math.cos(a) * f, cy + radius * math.sin(a) * f)
            for a, f in zip(angles, factors, strict=True)
        ]

    scores = []
    for _, reverse, metrics in groups:
        pct = number(positions.get(metrics[0][1]))
        scores.append(
            (100 - pct if reverse else pct) if pct is not None and 0 <= pct <= 100 else None
        )
    points = vertices([(score or 0) / 100 for score in scores])
    if all(score is not None for score in scores):
        c.draw.polygon(points, fill="#deebff")
    for tick in (25, 50, 75, 100):
        ring = vertices([tick / 100] * 6)
        c.draw.line([*ring, ring[0]], fill="#a4b6cd" if tick == 50 else "#dce6f3", width=2)
        c.text(cx + 9, cy - radius * tick / 100 + 8, tick, 14, MUTED, "number")
    for i, ((x, y), score) in enumerate(zip(points, scores, strict=True)):
        prev = (i - 1) % 6
        if score is not None and scores[prev] is not None:
            c.draw.line((points[prev], (x, y)), fill=BLUE, width=4)
        if score is not None:
            c.draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=BLUE, outline="white")

    top = 287
    for i, ((axis, _, metrics), (x, y), score) in enumerate(
        zip(groups, vertices([1.25] * 6), scores, strict=True)
    ):
        c.text(x, y - 9, axis, 23, style="bold", anchor="mm")
        c.text(x, y + 23, value(score, decimals=0), 23, BLUE, "number", "mm")
        height = 51 * len(metrics) + 10
        center = top + height / 2
        if i % 2 == 0:
            c.draw.rounded_rectangle((722, top, 1542, top + height - 2), radius=10, fill="#f4f7fb")
        c.text(745, center, axis, 22, style="bold")
        if score is not None:
            c.draw.rounded_rectangle((805, center - 15, 859, center + 15), radius=8, fill="#e5effe")
            c.text(832, center, f"{score:.0f}", 19, BLUE, "number", "mm")
        for j, (label, key, unit) in enumerate(metrics):
            row_y = top + 30 + j * 51
            c.text(914, row_y, label, 22, MUTED)
            text = (
                value(p.get(key), percent=True)
                if unit == "%"
                else number_text(
                    number(p.get(key)), signed=unit == "±点", precision=1 if unit == "巡" else 0
                )
            )
            c.text(1300, row_y, text, 25, BLUE, "number", "rm")
            c.text(1523, row_y, percentile_label(positions.get(key)), 19, MUTED, anchor="rm")
        top += height

    c.text(
        362,
        895,
        f"和铳度差  {number_text(view['difference'], signed=True, precision=1)}",
        23,
        TEAL,
        "bold",
        "mm",
    )
    c.text(362, 934, "防守和速度按较低放铳率、较早和牌计分", 18, MUTED, anchor="mm")
    c.text(42, 1012, "水表网 nodocchi", 18, MUTED)
    return c.png()
