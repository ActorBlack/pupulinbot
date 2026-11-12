"""Mahjong Soul sample charts; missing observations remain missing."""

import io
import math
from collections import Counter
from datetime import timedelta
from functools import partial

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_level import point_number, point_unit, rank_identity, seat_delta
from .ms_options import MODE_NAMES, TZ
from .ms_profile import render_profile as render_profile_card
from .typography import mpl_font


def font(style="regular"):
    return mpl_font(style)


def save(figure):
    output = io.BytesIO()
    figure.savefig(output, format="png", facecolor=figure.get_facecolor())
    return output.getvalue()


def pt_series(games, account_id, unit=None):
    ordered = sorted(games, key=lambda g: g.started_at)
    if unit is None:
        units = {seat_delta(g.seat(account_id) or {})[1] for g in ordered} - {None}
        if len(units) > 1:
            return [math.nan] * len(ordered)
    total = 0.0
    values = []
    for game in ordered:
        seat = game.seat(account_id) or {}
        delta, actual_unit = seat_delta(seat)
        if delta is None:
            rank = rank_identity(seat.get("level"))
            known_unit = point_unit(rank) if rank else None
            if unit is None or known_unit in {None, unit}:
                total = math.nan
        elif unit is None or actual_unit == unit:
            total += delta
        values.append(total)
    return values


def sessions(games, gap=60):
    groups = []
    previous_end = None
    for game in sorted(games, key=lambda g: g.started_at):
        # Missing end times cannot establish a continuous-play interval.
        if previous_end is None or game.started_at - previous_end > timedelta(minutes=gap):
            groups.append([])
        groups[-1].append(game)
        previous_end = (
            max(previous_end, game.ended_at) if previous_end and game.ended_at else game.ended_at
        )
    return groups


def activity_counts(stamps, *, yearly=False):
    lo, hi = min(stamps), max(stamps)
    daily = not yearly and (hi.date() - lo.date()).days < 31
    pattern = "%Y" if yearly else "%m-%d" if daily else "%Y-%m"
    counts = Counter(s.strftime(pattern) for s in stamps)
    labels = []
    cursor = lo.replace(hour=0, minute=0, second=0, microsecond=0)
    if not daily:
        cursor = cursor.replace(day=1, month=1 if yearly else cursor.month)
    while cursor <= hi:
        labels.append(cursor.strftime(pattern))
        if daily:
            cursor += timedelta(days=1)
        elif yearly:
            cursor = cursor.replace(year=cursor.year + 1)
        else:
            cursor = cursor.replace(
                year=cursor.year + (cursor.month == 12), month=cursor.month % 12 + 1
            )
    return labels, [counts[k] for k in labels], "年" if yearly else "日" if daily else "月"


def render_profile(player, stats, *, scope=None, games=()):
    with _RENDER_LOCK:
        return render_profile_card(player, stats, scope=scope, games=games)


def render_history(kind, player, games, truncated, options, *, scope=None):
    if not games:
        raise ValueError("筛选范围内没有牌谱屋已收录对局")
    with _RENDER_LOCK:
        return _render_history(kind, player, games, truncated, options, scope=scope)


def _render_life(player, games, options):
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyBboxPatch, Patch
    from matplotlib.ticker import MaxNLocator

    moves = [seat_delta(game.seat(player.account_id) or {}) for game in games]
    units = [unit for unit in ("PT", "魂珠", "积分") if any(u == unit for _, u in moves)]
    height = 1.5 + max(1, len(units)) * 5.35
    figure = Figure(figsize=(10.8, height), dpi=150, facecolor="#eef3f8")
    FigureCanvasAgg(figure)
    ink, muted = "#192d45", "#566a82"
    up, down, flat = "#148772", "#c04f69", "#7b8b9f"
    fp, bold = font(), font("bold")

    def text(x, top, label, size, color=ink, *, strong=False, align="left"):
        figure.text(
            x,
            1 - top / height,
            label,
            va="top",
            ha=align,
            color=color,
            fontsize=size,
            fontproperties=bold if strong else fp,
            parse_math=False,
        )

    def axes(top, depth):
        ax = figure.add_axes((0.111, 1 - (top + depth) / height, 0.82, depth / height))
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color("#cdd8e5")
        ax.set_axisbelow(True)
        ax.grid(axis="y", color="#e2e9f2", linewidth=0.8)
        ax.tick_params(length=0, pad=6, labelsize=10, colors=muted)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=10, steps=[1, 2, 2.5, 5, 10], integer=True))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
        ax.set_xlim(-0.7, len(games) + 0.7)
        ax.axhline(0, color="#a8b7c9", linewidth=1, zorder=1)
        return ax

    lo, hi = (g.started_at.astimezone(TZ) for g in (games[0], games[-1]))
    rooms = {str(game.mode) for game in games}
    room = MODE_NAMES.get(next(iter(rooms)), "").removeprefix("三麻") if len(rooms) == 1 else ""
    mode = "三麻" if options.mode == 3 else "四麻"
    text(0.065, 0.27, "段位积分走势", 26, strong=True)
    text(0.066, 0.79, f"{player.nickname[:30]}　{mode}{room}　{len(games):,} 场", 14, muted)
    period = f"{lo:%Y-%m-%d}" if lo.date() == hi.date() else f"{lo:%Y-%m-%d} 至 {hi:%Y-%m-%d}"
    text(0.937, 0.84, period, 11, muted, align="right")
    if not units:
        text(0.50, 2.55, "这段对局没有段位积分变化记录", 18, muted, align="center")
        return save(figure)

    for i, unit in enumerate(units):
        top = 1.18 + i * 5.35
        color = {"PT": "#315fbd", "魂珠": "#157e91", "积分": "#94642f"}[unit]
        known = [
            (n, value) for n, (value, u) in enumerate(moves, 1) if u == unit and value is not None
        ]
        gains = [(n, value) for n, value in known if value > 0]
        losses = [(n, value) for n, value in known if value < 0]
        neutral = [n for n, value in known if value == 0]
        earned, spent = sum(v for _, v in gains), sum(v for _, v in losses)
        curve = [0, *pt_series(games, player.account_id, unit)]
        complete = all(math.isfinite(v) for v in curve)
        peak, peak_index, drawdown = 0, 0, 0
        drawdown_span = None
        if complete:
            for n, value in enumerate(curve):
                if value >= peak:
                    peak, peak_index = value, n
                elif peak - value > drawdown:
                    drawdown = peak - value
                    drawdown_span = (peak_index, n)

        signed = partial(point_number, unit=unit, signed=True)

        tiles = (
            ("净变化" if complete else "已记录净变化", signed(earned + spent), color),
            ("总升分", signed(earned), up),
            ("总掉分", signed(spent), down),
            ("最大回撤", point_number(drawdown, unit) if complete else "—", ink),
        )
        for j, (label, value, accent) in enumerate(tiles):
            x = 0.063 + j * 0.222
            figure.add_artist(
                FancyBboxPatch(
                    (x, 1 - (top + 0.84) / height),
                    0.205,
                    0.84 / height,
                    boxstyle="round,pad=0.004,rounding_size=0.012",
                    facecolor="white",
                    edgecolor="none",
                    transform=figure.transFigure,
                )
            )
            text(x + 0.015, top + 0.13, label, 11, muted)
            text(x + 0.015, top + 0.36, value if known else "—", 22, accent, strong=True)
            text(x + 0.187, top + 0.49, unit, 10, muted, align="right")

        text(0.067, top + 1.02, "累计走势", 15, strong=True)
        if complete and known:
            text(
                0.21, top + 1.06, f"峰值 {signed(max(curve))}　谷值 {signed(min(curve))}", 11, muted
            )
        line = axes(top + 1.38, 1.55)
        line.set_ylabel(unit, color=muted, fontproperties=fp)
        line.tick_params(labelbottom=False)
        line_handles = [Line2D([], [], color=color, linewidth=2.2, label=f"累计 {unit}")]
        if drawdown_span and drawdown > 1e-8:
            highlight = "#f6e8ee"
            line.axvspan(*drawdown_span, color=highlight, linewidth=0, zorder=0)
            line_handles.append(Patch(facecolor=highlight, label="最大回撤区间"))
        line.plot(range(len(curve)), curve, color=color, linewidth=2.2, zorder=3)
        line.fill_between(range(len(curve)), 0, curve, color=color, alpha=0.09)
        if not complete:
            line.text(
                0.98,
                0.08,
                "累计线止于首个数据缺口",
                transform=line.transAxes,
                ha="right",
                color=muted,
                fontproperties=fp,
                fontsize=10,
            )
        line.legend(
            handles=line_handles,
            ncol=len(line_handles),
            loc="lower right",
            bbox_to_anchor=(1, 1.02),
            frameon=False,
            prop=fp,
        )

        text(0.067, top + 3.13, "单场涨跌", 15, strong=True)
        bars = axes(top + 3.47, 1.02)
        bars.set_ylabel(unit, color=muted, fontproperties=fp)
        bars.set_xlabel("对局场次", color=muted, fontproperties=fp, labelpad=3)
        for entries, accent in ((gains, up), (losses, down)):
            if entries:
                xs, ys = zip(*entries, strict=True)
                bars.bar(xs, ys, width=0.77, color=accent, zorder=2)
        if neutral:
            bars.scatter(neutral, [0] * len(neutral), color=flat, s=9, zorder=3)
        handles = [Patch(color=up, label="升分"), Patch(color=down, label="掉分")]
        if neutral:
            handles.append(Line2D([], [], color=flat, marker="o", linestyle="none", label="持平"))
        bars.legend(
            handles=handles,
            ncol=len(handles),
            loc="lower right",
            bbox_to_anchor=(1, 1.02),
            frameon=False,
            prop=fp,
        )

        average = f"{(earned + spent) / len(known):+,.2f} {unit}" if known else "—"
        coverage = f"有效 {len(known)}/{len(games)} 场　" if len(known) < len(games) else ""
        best = max(gains, key=lambda entry: entry[1], default=None)
        worst = min(losses, key=lambda entry: entry[1], default=None)
        best_text = f"{signed(best[1])}（第 {best[0]} 场）" if best else "—"
        worst_text = f"{signed(worst[1])}（第 {worst[0]} 场）" if worst else "—"
        text(
            0.066,
            top + 4.90,
            f"升分 {len(gains)} 场　掉分 {len(losses)} 场　持平 {len(neutral)} 场",
            10.5,
            muted,
        )
        text(
            0.937,
            top + 4.90,
            f"{coverage}场均 {average}",
            10.5,
            muted,
            align="right",
        )
        text(0.066, top + 5.16, f"单场最高升分 {best_text}　最大掉分 {worst_text}", 10.5, muted)
        if drawdown_span and drawdown > 1e-8:
            first, last = drawdown_span
            start_label = f"第 {first} 场" if first else "起点"
            text(
                0.937,
                top + 5.16,
                f"回撤区间 {start_label} 至第 {last} 场",
                10.5,
                muted,
                align="right",
            )
    return save(figure)


def _render_history(kind, player, games, truncated, options, *, scope=None):
    games = sorted(games, key=lambda g: g.started_at)
    if kind == "mslife":
        return _render_life(player, games, options)
    fp = font()
    titles = {
        "msheat": "对局时间热力图",
        "msstat": "对局数量统计",
        "mssession": "连续对局统计",
    }
    figure = Figure(figsize=(12, 8), dpi=140, facecolor="#f4f7fc")
    FigureCanvasAgg(figure)
    figure.text(
        0.08, 0.93, f"{player.nickname}　{titles[kind]}", fontproperties=font("bold"), fontsize=24
    )
    lo, hi = [g.started_at.astimezone(TZ) for g in (games[0], games[-1])]
    mode = "三麻" if options.mode == 3 else "四麻"
    figure.text(
        0.08,
        0.875,
        f"{mode}{scope or ''}  {len(games)} 场　{lo:%Y-%m-%d} 至 {hi:%Y-%m-%d}",
        fontproperties=fp,
        fontsize=12,
    )
    ax = figure.add_axes((0.09, 0.20, 0.85, 0.58))
    ax.set_facecolor("white")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.15)
    if kind == "msheat":
        grid = [[0] * 24 for _ in range(7)]
        for game in games:
            stamp = game.started_at.astimezone(TZ)
            grid[stamp.weekday()][stamp.hour] += 1
        image = ax.imshow(grid, cmap="Blues", aspect="auto")
        ax.set_yticks(
            range(7), ["周一", "周二", "周三", "周四", "周五", "周六", "周日"], fontproperties=fp
        )
        ax.set_xticks(range(0, 24, 2))
        ax.set_xlabel("开始小时（北京时间）", fontproperties=fp)
        figure.colorbar(image, ax=ax, label="Games", fraction=0.03)
        note = "按对局开始时间计数；颜色表示样本内对局数量。"
    elif kind == "msstat":
        stamps = [g.started_at.astimezone(TZ) for g in games]
        labels, counts, period = activity_counts(stamps, yearly=options.year)
        ax.bar(range(len(labels)), counts, color="#2563eb")
        stride = max(1, len(labels) // 15)
        ax.set_xticks(range(0, len(labels), stride), labels[::stride], rotation=35, ha="right")
        ax.set_ylabel("样本对局数", fontproperties=fp)
        note = "按日统计（跨度不超过 31 天）" if period == "日" else f"按{period}统计"
    else:
        groups = sessions(games, options.gap)
        ax.bar(range(1, len(groups) + 1), [len(g) for g in groups], color="#0f766e")
        ax.set_ylabel("连续对局场数", fontproperties=fp)
        ax.set_xlabel("会话编号", fontproperties=fp)
        note = f"对局间隔 {options.gap} 分钟分组"
    figure.text(0.08, 0.10, note, fontproperties=fp, fontsize=11, color="#52627c")
    output = io.BytesIO()
    figure.savefig(output, format="png")
    return output.getvalue()
