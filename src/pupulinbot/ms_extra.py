"""Comparisons of indexed Mahjong Soul records; no inferred hand statistics."""

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_charts import font, save
from .ms_level import delta_totals, seat_delta, totals_text
from .ms_options import TZ
from .ms_sample import number, sample_summary


def finished_records(games, account):
    unique = {g.uuid: g for g in games if g.ended_at and g.seat(account)}
    return sorted(unique.values(), key=lambda g: (g.started_at, g.uuid))


def matchup(player, opponent, games, options, truncated):
    finished = finished_records(games, player.account_id)
    common = [g for g in finished if g.seat(opponent.account_id)]
    title = f"{player.nickname} vs {opponent.nickname}（{'三麻' if options.mode == 3 else '四麻'}）"
    lines = [
        title,
        f"本人筛选窗口读取 {len(games)} 场，已结算 {len(finished)} 场；找到同桌 {len(common)} 场。",
    ]
    if not common:
        lines.append("未找到同桌记录。")
        return "\n".join(lines)
    opponent.nickname = str(
        (common[-1].seat(opponent.account_id) or {}).get("nickname") or opponent.nickname
    )
    lines[0] = (
        f"{player.nickname} vs {opponent.nickname}（{'三麻' if options.mode == 3 else '四麻'}）"
    )
    ahead = behind = tied = unknown = 0
    margins = []
    own_ranks, other_ranks = [], []
    for g in common:
        seats = [g.seat(p.account_id) for p in (player, opponent)]
        ranks = [number(p.get("rank")) for p in seats]
        valid = [r is not None and r.is_integer() and 1 <= r <= options.mode for r in ranks]
        if all(valid):
            ahead += ranks[0] < ranks[1]
            behind += ranks[0] > ranks[1]
            tied += ranks[0] == ranks[1]
            own_ranks.append(ranks[0])
            other_ranks.append(ranks[1])
        else:
            unknown += 1
        scores = [number(p.get("score")) for p in seats]
        if all(s is not None for s in scores):
            margins.append(scores[0] - scores[1])
    lines.append(f"相对顺位：领先 {ahead} / 落后 {behind} / 同顺位 {tied} / 顺位缺失 {unknown}")
    if own_ranks:
        lines.append(
            f"本人/对手均顺：{sum(own_ranks) / len(own_ranks):.2f} / "
            f"{sum(other_ranks) / len(other_ranks):.2f}（双方顺位有效 {len(own_ranks)} 场）"
        )
    else:
        lines.append("本人/对手均顺：— / —")
    margin = f"{sum(margins) / len(margins):+g}" if margins else "—"
    lines.append(f"平均得点差（本人−对手）：{margin} 点（有效 {len(margins)}/{len(common)}）")
    pt = []
    for p in (player, opponent):
        total = delta_totals(common, p.account_id)
        known = sum(seat_delta(g.seat(p.account_id) or {})[0] is not None for g in common)
        pt.append(
            f"{totals_text(total)}（已知 {known}/{len(common)}）" if total else "—（暂无积分）"
        )
    lines.append(f"同桌积分净变化，本人/对手：{pt[0]} / {pt[1]}")
    lines.append("最近同桌（最多5场）：")
    for g in reversed(common[-5:]):
        own, other = g.seat(player.account_id), g.seat(opponent.account_id)
        lines.append(
            f"{g.started_at.astimezone(TZ):%m-%d %H:%M}　"
            f"本人{own.get('rank') or '?'}位 / 对手{other.get('rank') or '?'}位\n{g.url}"
        )
    return "\n".join(lines)


def split_comparison(games, account, mode=4):
    finished = finished_records(games, account)
    if len(finished) < 4:
        raise ValueError("前后半段对比至少需要4场已结算且含该玩家的牌谱")
    split = len(finished) // 2
    halves = [finished[:split], finished[split:]]
    return halves, [sample_summary(h, account, mode) for h in halves]


def render_comparison(player, games, truncated, options, *, scope=None):
    halves, summaries = split_comparison(games, player.account_id, options.mode)
    with _RENDER_LOCK:
        fp = font()
        fig = Figure(figsize=(12, 7), dpi=130, facecolor="#f4f7fc")
        FigureCanvasAgg(fig)
        fig.text(
            0.06,
            0.91,
            f"{player.nickname}　前后半段战绩对比",
            fontproperties=font("bold"),
            fontsize=23,
        )
        lo, hi = halves[0][0].started_at.astimezone(TZ), halves[1][-1].started_at.astimezone(TZ)
        fig.text(
            0.06,
            0.84,
            f"{'三麻' if options.mode == 3 else '四麻'}{scope or ''}　"
            f"{lo:%Y-%m-%d} 至 {hi:%Y-%m-%d}",
            fontproperties=fp,
            fontsize=12,
            color="#64748b",
        )

        def value(s, key, pct=False):
            n = s[key]
            return "—" if n is None else f"{n * 100:.1f}%" if pct else f"{n:.2f}"

        rows = []
        for label, key, pct in [
            ("平均顺位", "average_rank", False),
            ("一位率", "first_rate", True),
            ("连对率", "top2_rate", True),
            ("击飞率", "negative_rate", True),
            ("平均得点", "average_score", False),
        ]:
            rows.append([label] + [value(s, key, pct) for s in summaries])
        rows.append(["积分净变化"] + [totals_text(s["point_totals"]) for s in summaries])
        rows.append(
            ["有效顺位 / 积分"] + [f"{s['known_ranks']} / {s['known_deltas']}" for s in summaries]
        )
        ax = fig.add_axes((0.06, 0.25, 0.49, 0.49))
        ax.axis("off")
        labels = [f"前半 {len(halves[0])} 场", f"后半 {len(halves[1])} 场"]
        table = ax.table(
            cellText=rows,
            colLabels=["指标"] + labels,
            cellLoc="center",
            colWidths=[0.4, 0.3, 0.3],
            bbox=(0, 0, 1, 1),
        )
        for (row, _), cell in table.get_celld().items():
            cell.get_text().set_fontproperties(fp)
            cell.get_text().set_fontsize(12)
            cell.set_facecolor("#dfeafb" if row == 0 else "white")
            cell.set_edgecolor("#e2e8f0")
        ranks = fig.add_axes((0.64, 0.30, 0.31, 0.40))
        for i, (s, color) in enumerate(zip(summaries, ("#879aba", "#2476da"), strict=True)):
            ys = [
                n / s["known_ranks"] * 100 if s["known_ranks"] else float("nan")
                for n in s["rank_counts"]
            ]
            xs = [r + (-0.18 if i == 0 else 0.18) for r in range(1, options.mode + 1)]
            ranks.bar(xs, ys, width=0.34, label=labels[i], color=color)
        ranks.set_xticks(
            range(1, options.mode + 1),
            [f"{r}位" for r in range(1, options.mode + 1)],
            fontproperties=fp,
        )
        ranks.set_ylabel("顺位占比（%）", fontproperties=fp)
        ranks.set_title("两段顺位分布", fontproperties=fp)
        ranks.spines[["top", "right"]].set_visible(False)
        ranks.legend(prop=fp)
        ranks.grid(axis="y", alpha=0.15)
        return save(fig)
