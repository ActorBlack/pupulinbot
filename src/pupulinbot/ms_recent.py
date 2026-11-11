"""Recent game record table for the mslast image option."""

from .batch_chart import _RENDER_LOCK, Figure, FigureCanvasAgg
from .ms_charts import font, save
from .ms_level import delta_text
from .ms_options import MODE_NAMES, TZ
from .ms_sample import number


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
            f"{player.nickname}　最近 {len(games)} 场（{'三麻' if mode == 3 else '四麻'}）",
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
