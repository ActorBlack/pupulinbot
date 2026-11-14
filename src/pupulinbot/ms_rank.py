"""Group standings ordered by current Mahjong Soul rank and points."""

import asyncio

from .ms_level import (
    point_number,
    point_unit,
    point_value,
    rank_identity,
    rank_label,
    rank_progress,
)
from .paipuya import PaipuyaError
from .rank_card import render_rank_pages


async def ranking_rows(client, bindings):
    limit = asyncio.Semaphore(4)

    async def load(row):
        try:
            async with limit:
                stats = await client.metadata(row["account_id"])
            raw = stats.raw or {}
            current = rank_progress(raw.get("level"))
            peak = rank_progress(raw.get("max_level")) or rank_identity(raw.get("max_level"))
            return row, current, peak
        except PaipuyaError:
            return row, None, None

    loaded = await asyncio.gather(*(load(row) for row in bindings))
    available = [item for item in loaded if item[1] is not None]
    available.sort(
        key=lambda item: (item[1]["major"], item[1]["star"], item[1]["points"]),
        reverse=True,
    )
    standings = []
    for row, rank, peak in available:
        unit = point_unit(rank)
        points = point_number(point_value(rank["points"], rank), unit)
        cap = "—" if rank["cap"] is None else point_number(point_value(rank["cap"], rank), unit)
        if peak is not None and (peak["major"], peak["star"]) < (rank["major"], rank["star"]):
            peak = rank
        standings.append(
            (row["nickname"], rank_label(rank), rank_label(peak) if peak else "—", points, cap)
        )
    return standings


async def group_ranking(client, bindings):
    standings = await ranking_rows(client, bindings)
    if not standings:
        return "牌谱屋暂时没有本群玩家的段位数据。"
    return await asyncio.to_thread(render_rank_pages, client.mode, standings)
