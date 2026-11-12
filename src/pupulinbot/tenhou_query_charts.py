"""Tenhou activity and comparison graphics, based on the selected public records."""

from collections import Counter

from matplotlib.colors import LinearSegmentedColormap
from matplotlib.ticker import MaxNLocator

from .ms_charts import activity_counts, font, sessions
from .ms_extra import split_comparison
from .tenhou import CN, TenhouError
from .tenhou_queries import value
from .tenhou_query_card import (
    AMBER,
    BLUE,
    INK,
    MUTED,
    ROSE,
    TEAL,
    Canvas,
    chart_header,
    number_text,
    paint_plot,
    plot_card,
    stat_cards,
    style_axis,
)

WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def render_activity(command, view, games, options):
    if command == "thsession":
        return render_sessions(view, games, options.gap)
    stamps = [game.started_at.astimezone(CN) for game in games]
    c = Canvas((1600, 890))
    if command == "thheat":
        render_heat(c, view, stamps)
    else:
        render_counts(c, view, stamps, yearly=options.year)
    c.text(42, 861, "水表网 nodocchi", 18, MUTED)
    return c.png()


def render_heat(c, view, stamps):
    frequencies = Counter((stamp.weekday(), stamp.hour) for stamp in stamps)
    peak, busiest = max(
        frequencies.items(), key=lambda entry: (entry[1], -entry[0][0], -entry[0][1])
    )
    chart_header(c, view, "对局时间热力图")
    stat_cards(
        c,
        [
            ("样本场数", f"{len(stamps):,}", BLUE),
            ("活跃天数", f"{len({stamp.date() for stamp in stamps}):,}", TEAL),
            ("最多开局时段", f"{WEEK[peak[0]]} {peak[1]:02d}:00", AMBER),
        ],
    )
    plot_card(c, (40, 298, 1560, 831), "星期与小时", f"北京时间　峰值 {busiest:,} 场")

    def plot(fig):
        ax = fig.add_axes((0.055, 0.24, 0.923, 0.72))
        values = [[frequencies[(day, hour)] for hour in range(24)] for day in range(7)]
        ramp = LinearSegmentedColormap.from_list(
            "tenhou-activity", ("#eff4fa", "#a9c9f5", "#558cdb", "#214b9c")
        )
        image = ax.imshow(values, cmap=ramp, vmin=0, vmax=busiest, aspect="auto")
        ax.set_yticks(range(7), WEEK, fontproperties=font())
        ax.set_xticks(range(24), [f"{h:02d}" for h in range(24)])
        ax.set_xticks([h - 0.5 for h in range(1, 24)], minor=True)
        ax.set_yticks([d - 0.5 for d in range(1, 7)], minor=True)
        ax.grid(which="minor", color="white", linewidth=1)
        ax.tick_params(which="both", length=0, pad=7, labelsize=11, colors=MUTED)
        for spine in ax.spines.values():
            spine.set_visible(False)
        for (day, hour), count in frequencies.items():
            color = "white" if count / busiest >= 0.69 else INK
            ax.text(hour, day, f"{count:,}", ha="center", va="center", fontsize=10, color=color)
        legend = fig.add_axes((0.775, 0.085, 0.203, 0.028))
        scale = fig.colorbar(image, cax=legend, orientation="horizontal", ticks=[0, busiest])
        scale.outline.set_visible(False)
        legend.tick_params(length=0, labelsize=10, colors=MUTED)

    paint_plot(c, (53, 357, 1547, 816), plot)


def render_counts(c, view, stamps, *, yearly):
    labels, counts, period = activity_counts(stamps, yearly=yearly)
    maximum = max(counts)
    chart_header(c, view, "对局场数统计")
    stat_cards(
        c,
        [
            ("样本场数", f"{len(stamps):,}", BLUE),
            (f"活跃{period}数", f"{sum(n > 0 for n in counts):,}", TEAL),
            (f"单{period}最多", f"{maximum:,}", AMBER),
        ],
    )
    plot_card(c, (40, 298, 1560, 831), f"按{period}统计", f"共 {len(labels):,} 个统计周期")

    def plot(fig):
        ax = fig.add_axes((0.065, 0.19, 0.91, 0.74))
        style_axis(ax, ylabel="场数", integer=True)
        bars = ax.bar(
            range(len(labels)),
            counts,
            width=0.67,
            color=[AMBER if n == maximum else BLUE for n in counts],
        )
        if len(labels) <= 18:
            ax.bar_label(
                bars,
                labels=[f"{n:,}" if n else "" for n in counts],
                padding=5,
                fontsize=10,
                color=MUTED,
            )
        stride = max(1, (len(labels) + 11) // 12)
        ticks = list(range(0, len(labels), stride))
        ax.set_xticks(ticks, [labels[i] for i in ticks], rotation=25 if len(ticks) > 7 else 0)
        ax.set_ylim(0, maximum * 1.20)

    paint_plot(c, (53, 356, 1547, 816), plot)


def render_sessions(view, games, gap):
    lengths = [len(block) for block in sessions(games, gap)]
    c = Canvas((1600, 820))
    longest = max(lengths)
    chart_header(c, view, "连续对局统计")
    stat_cards(
        c,
        [
            ("连续时段数", f"{len(lengths):,}", BLUE),
            ("每段平均场数", f"{sum(lengths) / len(lengths):.1f}", TEAL),
            ("最长连续场数", f"{longest:,}", AMBER),
        ],
    )
    plot_card(c, (40, 298, 1008, 761), "连续场数", f"间隔超过 {gap} 分钟分组")
    plot_card(c, (1028, 298, 1560, 761), "时段长度分布")

    def sequence(fig):
        ax = fig.add_axes((0.09, 0.18, 0.87, 0.74))
        style_axis(ax, xlabel="时段序号", ylabel="场数", integer=True)
        ax.bar(
            range(1, len(lengths) + 1),
            lengths,
            width=0.73,
            color=[AMBER if n == longest else BLUE for n in lengths],
        )
        ax.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=True))
        ax.set_ylim(0, longest * 1.2)

    def distribution(fig):
        ax = fig.add_axes((0.19, 0.18, 0.73, 0.72))
        counts = Counter(min(n, 5) for n in lengths)
        values = [counts[i] for i in range(1, 6)]
        bars = ax.barh(range(5), values, color=TEAL, height=0.56)
        style_axis(ax, xlabel="时段数")
        ax.grid(False, axis="y")
        ax.grid(True, axis="x", linewidth=0.8, color="#e6edf5")
        ax.set_yticks(range(5), ["1 场", "2 场", "3 场", "4 场", "5 场以上"], fontproperties=font())
        ax.invert_yaxis()
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4, integer=True))
        ax.set_xlim(0, max(values) * 1.25)
        ax.bar_label(bars, labels=[f"{n:,}" for n in values], padding=4, color=MUTED)

    paint_plot(c, (54, 355, 991, 745), sequence)
    paint_plot(c, (1040, 355, 1544, 745), distribution)
    c.text(42, 791, "水表网 nodocchi", 18, MUTED)
    return c.png()


def render_comparison(view, games):
    try:
        _, samples = split_comparison(games, 0, view["mode"])
    except ValueError as exc:
        raise TenhouError("前后半段对比至少需要 4 场已结算对局。") from exc
    old, new = samples
    c = Canvas((1600, 840))
    chart_header(c, view, "前后半段对比")

    def trend(label, field, kind, favorable=1):
        delta = new[field] - old[field]
        text = f"{delta * 100:+.1f} pp" if kind == "rate" else f"{delta:+.2f}"
        color = MUTED if not delta else TEAL if delta * favorable > 0 else ROSE
        return label, text, color

    stat_cards(
        c,
        [
            trend("均顺变化", "average_rank", "rank", -1),
            trend("一位率变化", "first_rate", "rate"),
            trend("连对率变化", "top2_rate", "rate"),
        ],
    )
    plot_card(c, (40, 298, 868, 782), "战绩对照")
    plot_card(c, (888, 298, 1560, 782), "顺位分布")
    columns = (75, 538, 834)
    headers = ("指标", f"前半 {old['games']} 场", f"后半 {new['games']} 场")
    c.draw.rounded_rectangle((58, 357, 850, 400), radius=9, fill="#e9eff8")
    for i, (x, label) in enumerate(zip(columns, headers, strict=True)):
        c.text(x, 379, label, 21, MUTED, "bold", "rm" if i else "lm")
    metrics = (
        ("平均顺位", "average_rank", False),
        ("一位率", "first_rate", True),
        ("连对率", "top2_rate", True),
        ("击飞率", "negative_rate", True),
        ("平均得点", "average_score", False),
    )
    rows = [
        (
            label,
            *(
                value(s[field], percent=pct, decimals=2 if field == "average_rank" else 0)
                for s in samples
            ),
        )
        for label, field, pct in metrics
    ]
    pts = [
        number_text(s["pt_delta"], signed=True)
        + (f" ({s['known_deltas']}/{s['games']})" if 0 < s["known_deltas"] < s["games"] else "")
        for s in samples
    ]
    rows.append(("推算 PT", *pts))
    for i, (label, a, b) in enumerate(rows):
        y = 433 + i * 55
        if i % 2 == 0:
            c.draw.rounded_rectangle((58, y - 24, 850, y + 24), radius=8, fill="#f4f7fb")
        c.text(columns[0], y, label, 22)
        c.text(columns[1], y, a, 24, MUTED, "number", "rm")
        c.text(columns[2], y, b, 24, BLUE, "number", "rm")

    def plot(fig):
        ax = fig.add_axes((0.13, 0.16, 0.83, 0.73))
        style_axis(ax, ylabel="占比（%）", integer=True)
        ranks = range(1, view["mode"] + 1)
        peak = 0
        for sample, offset, color, label in zip(
            samples, (-0.18, 0.18), ("#97add2", BLUE), ("前半", "后半"), strict=True
        ):
            rates = [count / sample["known_ranks"] * 100 for count in sample["rank_counts"]]
            peak = max(peak, *rates)
            bars = ax.bar([r + offset for r in ranks], rates, width=0.32, color=color, label=label)
            ax.bar_label(
                bars,
                labels=[f"{rate:.0f}%" if rate else "" for rate in rates],
                color=MUTED,
                fontsize=10,
                padding=4,
            )
        ax.set_xticks(ranks, [f"{r} 位" for r in ranks], fontproperties=font())
        ax.set_ylim(0, max(20, peak * 1.25))
        ax.legend(loc="upper right", prop=font(), frameon=False)

    paint_plot(c, (898, 358, 1547, 758), plot)
    c.text(42, 813, "水表网 nodocchi　PT 为公开历史推算", 18, MUTED)
    return c.png()
