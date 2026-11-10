"""Recent-game text summaries with explicit sampling and projection assumptions.

Rank point limits and the yonma south stable-level equation are checked against
SAPikachu/amae-koromo src/data/types/{level,metadata}.ts (MIT).
"""

import math
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .ms_level import format_level, rank_progress
from .ms_options import TZ
from .ms_sample import number, sample_summary
from .ms_style import style_lines


def ratio(value):
    value = number(value)
    if value is None:
        return None
    value = value / 100 if value > 1 else value
    return value if 0 <= value <= 1 else None


def percent(value):
    value = ratio(value)
    return "—" if value is None else f"{value * 100:.1f}%"


def scoring_metrics(stats):
    raw = stats.raw if stats else {}
    win_points, deal_points = (number(raw.get(key)) for key in ("平均打点", "平均铳点"))
    win_points = win_points if win_points is not None and win_points >= 0 else None
    deal_points = deal_points if deal_points is not None and deal_points >= 0 else None

    win = ratio(stats.win_rate if stats else None)
    deal = ratio(stats.deal_in_rate if stats else None)
    difference = (
        None
        if any(value is None for value in (win, deal, win_points, deal_points))
        else win * win_points - deal * deal_points
    )
    return win_points, deal_points, difference


def scoring_lines(stats):
    win_points, deal_points, difference = scoring_metrics(stats)

    def points(value):
        return "—" if value is None else f"{value:.1f}".removesuffix(".0")

    diff = "—" if difference is None else f"{difference:+.1f}"
    return [
        f"平均打点: {points(win_points)} | 平均铳点: {points(deal_points)}",
        f"和铳度差: {diff}",
    ]


def upstream_rates(stats, mode=4):
    """Read the selected API aggregate, without replacing it with record estimates."""
    if stats is None:
        return None
    raw = stats.raw.get("rank_rates") or stats.raw.get("rankRates")
    if not isinstance(raw, list) or len(raw) < mode:
        return None
    values = [number(n) for n in raw[:mode]]
    if any(n is None or n < 0 for n in values) or not sum(values):
        return None
    return [n / sum(values) for n in values]


def js_fixed(value, digits):
    """Match JS toFixed's half-away rounding on the actual binary number."""
    quantum = Decimal(1).scaleb(-digits)
    return format(
        Decimal.from_float(float(value)).quantize(quantum, rounding=ROUND_HALF_UP), f".{digits}f"
    )


def projection(stats, modes, level):
    """Use API rank averages and the upstream page's display/rounding rules.

    References: metadata.ts estimateStableLevel2/calculateExpectedGamePoint and
    components/playerDetails/estimatedStableLevel.tsx. No per-record substitute.
    """
    if stats is None:
        return "安定段: —"
    if len(modes) != 1 or next(iter(modes)) not in {"12", "16"}:
        return "安定段: 仅支持玉南/王座南"
    mode = next(iter(modes))
    sample_rank = rank_progress(stats.raw.get("level"))
    current = rank_progress(level)
    if not sample_rank:
        return "安定段: —"
    if sample_rank["major"] not in {4, 5} or (mode == "16" and sample_rank["major"] != 5):
        return "安定段: —"
    rates = upstream_rates(stats)
    means = stats.raw.get("rank_avg_score")
    if rates is None or not isinstance(means, list) or len(means) < 4:
        return "安定段: —"
    scores = [number(n) for n in means[:4]]
    if any(score is None and rates[i] > 0 for i, score in enumerate(scores)):
        return "安定段: —"
    bonus = (110, 55, 0, 0) if mode == "12" else (120, 60, 0, 0)
    base = sum(
        rates[i] * (math.ceil(((score or 0) - 25000) / 1000 + (15, 5, -5, -15)[i]) + bonus[i])
        for i, score in enumerate(scores)
    )
    index = (sample_rank["major"] - 4) * 3 + sample_rank["star"] - 1
    expected = base - rates[3] * (165, 180, 195, 210, 225, 240)[index]
    if not rates[3]:
        label = "暂无（样本无四位）"
    else:
        stable = base / (rates[3] * 15) - 10
        # Upstream formatStableLevel2 truncates, rather than rounding to 2 decimals.
        value = stable - 3 if stable >= 4 else stable
        label = ("圣" if stable >= 4 else "豪") + f"{math.trunc(value * 100) / 100:.2f}"
        if mode == "16" and stable > 7:
            # Page falls back to discrete allowed levels at the celestial boundary.
            celestial = sum(p * x for p, x in zip(rates, (50, 20, -20, -50), strict=True))
            if abs(celestial) < 0.001:
                label = "魂天Lv1 (0.0魂珠)"
            elif celestial > 0:
                label = f"魂天 {js_fixed(celestial / 100, 1)}魂珠/战"
            else:
                ev = base - rates[3] * 240
                label = f"圣3 ({js_fixed(ev, 2)})"
    if not current or current["major"] not in {4, 5} or (mode == "16" and current["major"] != 5):
        current = sample_rank
    raw_level = stats.raw["level"]
    raw_id = int(number(raw_level["id"])) % 10000
    same_level = current["major"] * 100 + current["star"] == raw_id
    points = (
        number(raw_level["score"]) + number(raw_level["delta"])
        if same_level
        else current["cap"] / 2
    )
    trend = ""
    if expected:
        remaining = (current["cap"] - points) / expected if expected > 0 else points / expected
        # Upstream toFixed(0) rounds a positive absolute count, and hides >=10000.
        if 0 < abs(remaining) < 10000:
            count = math.floor(abs(remaining) + 0.5)
            trend = f"；预计 {count} 战后{'升' if remaining > 0 else '掉'}段"
    return (
        f"安定段: {label}{trend}；分数期望 {'+' if expected >= 0 else ''}"
        f"{js_fixed(expected, 1)} PT/战"
    )


def format_summary(player, games, truncated, options, context, *, now=None):
    now = now or datetime.now(TZ)
    finished = sorted(
        (g for g in games if g.ended_at and g.seat(player.account_id)), key=lambda g: g.started_at
    )
    if not finished:
        return f"{player.nickname}: 筛选范围内暂无已结算收录对局"
    summary = sample_summary(finished, player.account_id, options.mode)
    total, sample = context.get("total"), context.get("sample")
    # A range query may include additional records at a timestamp boundary.
    if sample is not None and sample.games != len(finished):
        sample = None
    modes = {g.mode for g in finished}
    room_names = {
        "8": "金东",
        "9": "金南",
        "11": "玉东",
        "12": "玉南",
        "15": "王座东",
        "16": "王座南",
        "21": "金东",
        "22": "金南",
        "23": "玉东",
        "24": "玉南",
        "25": "王座东",
        "26": "王座南",
    }
    scope = "/".join(dict.fromkeys(room_names.get(m, "所选房间") for m in sorted(modes)))
    raw = total.raw if total else {}
    header = f"{player.nickname}({format_level(raw.get('level'))}) - {scope}最近{len(finished)}战"
    ranks = "".join(str(r) if r is not None else "?" for r in summary["ranks"][-10:])
    lines = [
        header,
        "最高段位: " + format_level(raw.get("max_level")),
        f"最近战绩: [{ranks}]",
    ]
    activity = context.get("activity")
    if activity is None:
        counts, ago = "—/—/—", "暂无数据"
    else:
        active, capped = activity
        active = [
            g for g in active if g.ended_at and g.started_at <= now and g.seat(player.account_id)
        ]
        counts = "/".join(
            ("≥" if capped else "")
            + str(sum(g.started_at >= now - timedelta(days=d) for g in active))
            for d in (1, 7, 30)
        )
        if active:
            seconds = max(0, (now - max(g.started_at for g in active)).total_seconds())
            ago = (
                f"{int(seconds // 86400)}天前"
                if seconds >= 86400
                else (f"{int(seconds // 3600)}小时前" if seconds >= 3600 else "1小时内")
            )
        elif not options.start and not options.end:
            latest = finished[-1].started_at
            ago = f"{int(max(0, (now - latest).total_seconds()) // 86400)}天前"
        else:
            ago = "近30日暂无收录对局"
    total_count = str(total.games) if total else "—"
    lines.append(f"对战数: {total_count}; 近1日/7日/30日: {counts}; 最近对战: {ago}")
    rates = upstream_rates(sample, options.mode)
    if rates is None:
        rank_text, avg = " / ".join(["—"] * options.mode), "—"
    else:
        rank_text = ", ".join(f"{p * 100:.1f}%" for p in rates)
        average = number(sample.raw.get("avg_rank", sample.raw.get("average_rank")))
        if average is None:
            average = sum((i + 1) * p for i, p in enumerate(rates))
        avg = f"{average:.2f}"
    lines.append(f"1~{options.mode}位: {rank_text} (均顺{avg})")
    lines.append(
        f"和/铳: {percent(sample.win_rate if sample else None)} / "
        f"{percent(sample.deal_in_rate if sample else None)} | 副露/立直: "
        f"{percent(sample.call_rate if sample else None)} / "
        f"{percent(sample.riichi_rate if sample else None)}"
    )
    lines.extend(scoring_lines(sample))
    lines.append(projection(sample, modes, raw.get("level")) if options.mode == 4 else "安定段: —")
    lines.extend(
        style_lines(
            sample,
            mode=options.mode,
            scope=f"最近{len(finished)}战",
            games=finished,
            account_id=player.account_id,
        )
    )
    return "\n".join(lines)
