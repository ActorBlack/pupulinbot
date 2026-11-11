"""Mahjong Soul sample charts; missing observations remain missing."""

import io
import math
from collections import Counter
from datetime import timedelta

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_level import point_unit, rank_identity, seat_delta
from .ms_options import TZ
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
    for game in sorted(games, key=lambda g: g.started_at):
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


def render_profile(player, stats, *, scope=None, games=()):
    with _RENDER_LOCK:
        return render_profile_card(player, stats, scope=scope, games=games)


def render_history(kind, player, games, truncated, options):
    if not games:
        raise ValueError("筛选范围内没有牌谱屋已收录对局")
    with _RENDER_LOCK:
        return _render_history(kind, player, games, truncated, options)


def _render_history(kind, player, games, truncated, options):
    games = sorted(games, key=lambda g: g.started_at)
    fp = font()
    titles = {
        "mslife": "段位积分变化",
        "msheat": "对局时间热力图",
        "msstat": "对局数量统计",
        "mssession": "连续对局统计",
    }
    figure = Figure(figsize=(12, 8), dpi=140, facecolor="#f4f7fc")
    FigureCanvasAgg(figure)
    figure.text(
        0.08, 0.93, f"{player.nickname} · {titles[kind]}", fontproperties=font("bold"), fontsize=24
    )
    lo, hi = [g.started_at.astimezone(TZ) for g in (games[0], games[-1])]
    figure.text(
        0.08,
        0.875,
        f"{options.mode}麻 · 样本 {len(games)} 场 · {lo:%Y-%m-%d} 至 {hi:%Y-%m-%d}（北京时间）",
        fontproperties=fp,
        fontsize=12,
    )
    ax = figure.add_axes((0.09, 0.20, 0.85, 0.58))
    ax.set_facecolor("white")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.15)
    if kind == "mslife":
        units = list(
            dict.fromkeys(
                unit for g in games if (unit := seat_delta(g.seat(player.account_id) or {})[1])
            )
        )
        if len(units) > 2:
            ax.set_position((0.09, 0.20, 0.72, 0.58))
        values = []
        for i, unit in enumerate(units):
            series = pt_series(games, player.account_id, unit)
            values.extend(series)
            target = ax if i == 0 else ax.twinx()
            if i > 1:
                target.spines["right"].set_position(("outward", (i - 1) * 55))
            color = {"PT": "#2563eb", "魂珠": "#1ca6bc", "积分": "#a57837"}[unit]
            target.plot(range(1, len(games) + 1), series, color=color, linewidth=2, label=unit)
            target.set_ylabel(unit + " 净变化", fontproperties=fp, color=color)
            target.spines["top"].set_visible(False)
            target.legend(prop=fp, loc=("upper left", "upper right", "lower right")[i])
        ax.axhline(0, color="#94a3b8", linewidth=1)
        ax.set_xlabel("样本场次（由旧到新）", fontproperties=fp)
        note = "PT 与魂珠分别累计" if len(units) > 1 else ""
        if not any(math.isfinite(v) for v in values):
            ax.text(
                0.5,
                0.5,
                "样本缺少段位积分变化数据",
                transform=ax.transAxes,
                ha="center",
                fontproperties=fp,
            )
    elif kind == "msheat":
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
        yearly = options.year
        daily = not yearly and (hi.date() - lo.date()).days < 31
        pattern = "%Y" if yearly else "%m-%d" if daily else "%Y-%m"
        keys = Counter(s.strftime(pattern) for s in stamps)
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
        ax.bar(range(len(labels)), [keys[k] for k in labels], color="#2563eb")
        stride = max(1, len(labels) // 15)
        ax.set_xticks(range(0, len(labels), stride), labels[::stride], rotation=35, ha="right")
        ax.set_ylabel("样本对局数", fontproperties=fp)
        note = "按年统计" if yearly else "按日统计（跨度不超过 31 天）" if daily else "按月统计"
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
