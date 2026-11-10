"""Compact recent-results cards and a descriptive sample dashboard for QQ."""

import io
import math

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_charts import font
from .ms_level import delta_text, totals_text
from .ms_options import TZ
from .ms_sample import number, rolling_ranks, sample_summary
from .ms_style import style_lines

MODE_NAMES = {
    "8": "金东",
    "9": "金南",
    "11": "玉东",
    "12": "玉南",
    "15": "王座东",
    "16": "王座南",
    "21": "三麻金东",
    "22": "三麻金南",
    "23": "三麻玉东",
    "24": "三麻玉南",
    "25": "三麻王东",
    "26": "三麻王南",
}


def save(figure):
    result = io.BytesIO()
    figure.savefig(result, format="png", facecolor=figure.get_facecolor())
    return result.getvalue()


def render_sample(player, games, truncated, options, stats=None):
    summary = sample_summary(games, player.account_id, options.mode)
    if not summary["games"]:
        raise ValueError("筛选范围内没有已结算的收录对局")
    with _RENDER_LOCK:
        fp = font()
        figure = Figure(figsize=(12, 9), dpi=140, facecolor="#f4f7fc")
        FigureCanvasAgg(figure)
        figure.text(
            0.08,
            0.94,
            f"{player.nickname} · 最近战绩汇总",
            fontproperties=font("bold"),
            fontsize=24,
        )
        lo, hi = [summary[k].astimezone(TZ) for k in ("first_at", "last_at")]
        figure.text(
            0.08,
            0.89,
            f"{'三麻' if options.mode == 3 else '四麻'} · 已结算 {summary['games']} 场 · "
            f"{lo:%Y-%m-%d} 至 {hi:%Y-%m-%d}",
            fontproperties=fp,
            fontsize=12,
            color="#52627c",
        )

        def value(key, percent=False):
            n = summary[key]
            return "—" if n is None else f"{n * 100:.1f}%" if percent else f"{n:.2f}"

        metrics = [
            ("平均顺位", value("average_rank")),
            ("一位率", value("first_rate", True)),
            ("连对率", value("top2_rate", True)),
            ("击飞率", value("negative_rate", True)),
            ("平均得点", value("average_score")),
            ("积分净变化", totals_text(summary["point_totals"])),
            (
                "已知最长连一",
                "—" if summary["best_first_streak"] is None else str(summary["best_first_streak"]),
            ),
            (
                "样本末尾连一",
                "—"
                if summary["current_first_streak"] is None
                else str(summary["current_first_streak"]),
            ),
        ]
        for i, (label, text) in enumerate(metrics):
            x, y = 0.08 + (i % 4) * 0.225, 0.80 - (i // 4) * 0.115
            figure.text(x, y, label, fontproperties=fp, fontsize=11, color="#64748b")
            figure.text(
                x,
                y - 0.047,
                text,
                fontproperties=font("bold" if any(ord(c) > 127 for c in text) else "number"),
                fontsize=21,
                color="#172033",
            )
        ranks = figure.add_axes((0.09, 0.22, 0.52, 0.33))
        ranks.set_facecolor("white")
        ranks.plot(
            range(1, summary["games"] + 1),
            [r if r is not None else math.nan for r in summary["ranks"]],
            color="#2563eb",
            marker="o" if summary["games"] <= 100 else None,
            markersize=3,
            linewidth=1,
            alpha=0.35 if summary["games"] >= 10 else 1,
        )
        moving = rolling_ranks(summary["ranks"])
        if any(math.isfinite(v) for v in moving):
            ranks.plot(
                range(1, summary["games"] + 1),
                moving,
                color="#d97706",
                linewidth=2.4,
                label="10场平均顺位",
            )
            ranks.legend(prop=fp, fontsize=10)
        ranks.set_yticks(range(1, options.mode + 1))
        ranks.set_ylim(options.mode + 0.3, 0.7)
        ranks.set_xlabel("场次（由旧到新；未知顺位留空）", fontproperties=fp)
        ranks.set_ylabel("最终顺位", fontproperties=fp)
        ranks.grid(axis="y", alpha=0.15)
        distribution = figure.add_axes((0.70, 0.22, 0.25, 0.33))
        distribution.set_facecolor("white")
        distribution.bar(
            range(1, options.mode + 1),
            summary["rank_counts"],
            color=["#2563eb", "#0f766e", "#b45309", "#be185d"][: options.mode],
        )
        distribution.set_xticks(
            range(1, options.mode + 1),
            [f"{i}位" for i in range(1, options.mode + 1)],
            fontproperties=fp,
        )
        distribution.set_title("顺位分布（场）", fontproperties=font("bold"), fontsize=12)
        for i, count in enumerate(summary["rank_counts"], 1):
            distribution.text(i, count, str(count), ha="center", va="bottom", fontsize=10)
        distribution.set_ylim(0, max(summary["rank_counts"] + [1]) * 1.15)
        for ax in (ranks, distribution):
            ax.spines[["top", "right"]].set_visible(False)
        labels = style_lines(
            stats if stats is not None and stats.games == summary["games"] else None,
            mode=options.mode,
            games=games,
            account_id=player.account_id,
        )[0].removeprefix("牌风: ")
        # Keep supplementary tags readable on mobile without stretching the chart.
        parts = labels.split(" #")
        for index, line in enumerate((" #".join(parts[:3]), " #".join(parts[3:]))):
            if line:
                if index and not line.startswith("#"):
                    line = "#" + line
                figure.text(
                    0.08,
                    0.125 - index * 0.035,
                    ("样本牌风 · " if not index else "") + line,
                    fontproperties=fp,
                    fontsize=12,
                    color="#7561a8",
                )
        return save(figure)


def render_recent(player, games, mode=4):
    if not games:
        raise ValueError("筛选范围内没有已收录对局")
    if len(games) > 20:
        raise ValueError("牌谱图片最多展示 20 场")
    with _RENDER_LOCK:
        fp = font()
        figure = Figure(figsize=(11, 2.7 + 0.35 * len(games)), dpi=140, facecolor="#f4f7fc")
        FigureCanvasAgg(figure)
        figure.text(
            0.07,
            0.94,
            f"{player.nickname} · 最近{len(games)}场（{'三麻' if mode == 3 else '四麻'}）",
            fontproperties=font("bold"),
            fontsize=21,
            weight="bold",
        )
        ax = figure.add_axes((0.06, 0.15, 0.88, 0.67))
        ax.axis("off")
        rows = []
        for game in games:
            seat = game.seat(player.account_id) or {}
            score = number(seat.get("score"))
            rows.append(
                [
                    game.started_at.astimezone(TZ).strftime("%m-%d %H:%M"),
                    MODE_NAMES.get(game.mode, game.mode),
                    str(seat.get("rank") or "—"),
                    "—" if score is None else f"{score:g}",
                    delta_text(seat),
                ]
            )
        table = ax.table(
            cellText=rows,
            colLabels=["开始时间（北京时间）", "房间", "顺位", "得点", "PT / 魂珠增减"],
            cellLoc="center",
            loc="center",
            bbox=(0, 0, 1, 1),
        )
        for (row, _), cell in table.get_celld().items():
            cell.get_text().set_fontproperties(fp)
            cell.get_text().set_fontsize(11)
            cell.set_edgecolor("#dde5ef")
            cell.set_facecolor("#dfeafb" if row == 0 else "white" if row % 2 else "#f1f5fa")
        figure.text(
            0.07,
            0.065,
            "牌谱链接：mslast",
            fontproperties=fp,
            fontsize=10,
            color="#64748b",
        )
        return save(figure)
