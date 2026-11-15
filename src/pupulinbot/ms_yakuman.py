"""Recover missing historical yakuman labels from official replays."""

import asyncio
import logging
import sqlite3

from .paipuya import PaipuyaError

logger = logging.getLogger(__name__)


async def backfill_yakuman(db, client, browser, sample, *, mode, account_id, total, archived):
    # One archived game proves at least one win. An official replay can prove several.
    known = {uuid: 1 for uuid, winner, _ in archived if winner == account_id}
    if sum(known.values()) >= total:
        return {}
    games = sorted(sample.values(), key=lambda game: game.started_at)
    if len({game.started_at for game in games}) != len(games):
        return {}  # The stats API cannot distinguish games at identical timestamps.

    verified = {}
    try:
        async with asyncio.timeout(145):
            for uuid, started, count, name in await db.yakuman_replay_wins(
                mode, sample, account_id
            ):
                if abs(started - sample[uuid].started_at.timestamp()) <= 1:
                    known[uuid] = count
                    names = verified.setdefault(uuid, [])
                    if name is not None:
                        names.append(name)

            pending = [(games, total)]
            ranges_left, replays_left = 64, 8
            while pending:
                rows, count = pending.pop()
                if count <= sum(known.get(game.uuid, 0) for game in rows):
                    continue
                if len(rows) == 1:
                    if not replays_left:
                        break
                    replays_left -= 1
                    game = rows[0]
                    result = await browser.yakuman_wins(game, account_id)
                    if result is not None and result[0] == count:
                        await db.save_yakuman_replay(
                            mode, game.uuid, game.started_at.timestamp(), account_id, *result
                        )
                        verified[game.uuid] = list(result[1])
                    continue
                if not ranges_left:
                    break
                ranges_left -= 1
                middle = len(rows) // 2
                left, right = rows[:middle], rows[middle:]
                left_count = await client.yakuman_count(
                    account_id, left[0].started_at, left[-1].started_at, mode=mode
                )
                right_count = count - left_count
                if not (
                    left_count >= sum(known.get(game.uuid, 0) for game in left)
                    and right_count >= sum(known.get(game.uuid, 0) for game in right)
                ):
                    continue  # An inconsistent subdivision cannot identify the game.
                pending.extend(((left, left_count), (right, right_count)))
    except (PaipuyaError, TimeoutError):
        pass  # Return the verified portion if either service is unavailable.
    except sqlite3.Error:
        logger.warning("雀魂历史役满存档暂不可读写", exc_info=True)
    return verified
