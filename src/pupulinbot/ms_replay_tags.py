"""Sample-scoped replay evidence for the West-round achievement."""

import asyncio
import logging
import math
import sqlite3

logger = logging.getLogger(__name__)
WEST_MIN_GAMES = 2
WEST_MIN_RATE = 0.02


def west_requirement(count):
    return max(WEST_MIN_GAMES, math.ceil(count * WEST_MIN_RATE))


async def west_rounds(db, browser, sample, *, mode):
    if mode not in {9, 12, 16}:
        return {}
    confirmed = {}
    if db is not None:
        try:
            for uuid, started, wind in await db.replay_winds(mode, sample):
                if (
                    uuid in sample
                    and abs(started - sample[uuid].started_at.timestamp()) <= 1
                    and type(wind) is int
                    and wind in range(4)
                ):
                    confirmed[uuid] = wind
        except (sqlite3.Error, ValueError):
            logger.warning("雀魂西入牌谱存档暂不可读", exc_info=True)
    if sum(wind >= 2 for wind in confirmed.values()) >= west_requirement(len(sample)):
        return confirmed
    if browser is None or not browser.records_enabled:
        return confirmed
    try:
        # Queries resume with unexamined games after this budget is exhausted.
        async with asyncio.timeout(25):
            for game in sorted(sample.values(), key=lambda g: g.started_at, reverse=True):
                if game.uuid in confirmed:
                    continue
                wind = await browser.max_wind(game)
                if wind is None:
                    break
                if type(wind) is not int or wind not in range(4):
                    continue
                confirmed[game.uuid] = wind
                if db is not None:
                    await db.save_replay_wind(game, wind)
                if sum(value >= 2 for value in confirmed.values()) >= west_requirement(len(sample)):
                    break
                await asyncio.sleep(1.05)
    except TimeoutError:
        pass
    except sqlite3.Error:
        logger.warning("雀魂西入牌谱存档暂不可写", exc_info=True)
    return confirmed
