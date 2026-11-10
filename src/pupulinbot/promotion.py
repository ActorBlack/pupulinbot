"""Conservative promotion checks tied to the completed game, not current profiles."""

import asyncio

from .ms_options import level_label
from .ms_sample import number
from .ms_summary import rank_progress


def majsoul_promotion(player, metadata):
    if not player or not isinstance(metadata, dict) or metadata.get("count") != 1:
        return None
    level = metadata.get("level")
    if not isinstance(level, dict):
        return None
    game_level = player.get("level")
    game_level = game_level.get("id") if isinstance(game_level, dict) else game_level
    code = number(level.get("id"))
    if code is None or not code.is_integer() or number(game_level) != code:
        return None
    delta = number(level.get("delta"))
    recorded_delta = number(player.get("gradingScore", player.get("grading_score")))
    if delta is None or delta <= 0 or (recorded_delta is not None and recorded_delta != delta):
        return None
    before = rank_progress({**level, "delta": 0})
    after = rank_progress(level)
    if before and after and (after["major"], after["star"]) > (before["major"], before["star"]):
        mode = int(code) // 10000
        return level_label(mode * 10000 + after["major"] * 100 + after["star"])
    return None


async def promotion_line(client, game, account_id, *, budget_seconds=20):
    try:
        async with asyncio.timeout(budget_seconds):
            level = await client.promotion(game, account_id)
        return "欢迎新" + level if level else ""
    except Exception:
        # Optional congratulations must never block the normal result notification.
        return ""
