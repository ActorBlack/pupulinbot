"""Group standings ordered by current Mahjong Soul rank and points."""

import asyncio

from .ms_level import point_number, point_unit, point_value, rank_label, rank_progress
from .paipuya import PaipuyaError
from .rank_card import render_rank_pages


async def ranking_rows(client, bindings):
    limit = asyncio.Semaphore(4)

    async def load(row):
        try:
            async with limit:
                stats = await client.metadata(row["account_id"])
            return row, rank_progress((stats.raw or {}).get("level"))
        except PaipuyaError:
            return row, None

    loaded = await asyncio.gather(*(load(row) for row in bindings))
    available = [(row, rank) for row, rank in loaded if rank is not None]
    available.sort(
        key=lambda item: (item[1]["major"], item[1]["star"], item[1]["points"]),
        reverse=True,
    )
    standings = []
    for row, rank in available:
        unit = point_unit(rank)
        points = point_number(point_value(rank["points"], rank), unit)
        cap = "—" if rank["cap"] is None else point_number(point_value(rank["cap"], rank), unit)
        standings.append((row["nickname"], rank_label(rank), points, cap))
    return standings


async def group_ranking(client, bindings):
    standings = await ranking_rows(client, bindings)
    if not standings:
        return "牌谱屋暂时没有本群玩家的段位数据。"
    return await asyncio.to_thread(render_rank_pages, client.mode, standings)
