"""Confirm per-match achievements from exact, single-game Paipuya windows."""

import asyncio
import logging
import math
import sqlite3
from collections import Counter
from datetime import timedelta

from .ms_sample import number
from .ms_trait_tags import passerby_requirement
from .paipuya import PaipuyaError

logger = logging.getLogger(__name__)


def qualifies(stats):
    if stats is None or stats.games != 1:
        return None
    hands = number(stats.raw.get("_hand_count"))
    if hands is None or hands < 1 or not hands.is_integer():
        return None
    rates = [
        number(getattr(stats, key, None)) for key in ("win_rate", "deal_in_rate", "riichi_rate")
    ]
    if any(value is None or not 0 <= value <= 1 for value in rates):
        return None
    return all(value == 0 for value in rates)


def win_count(stats):
    if stats is None or stats.games != 1:
        return None
    hands = number(stats.raw.get("_hand_count"))
    rate = number(getattr(stats, "win_rate", None))
    if hands is None or hands < 1 or not hands.is_integer() or rate is None or not 0 <= rate <= 1:
        return None
    count = hands * rate
    # Full-precision API rates must reconstruct an integer; rounded display
    # percentages are insufficient evidence for an exact single win.
    return round(count) if math.isclose(count, round(count), rel_tol=0, abs_tol=1e-6) else None


async def passerby_games(db, client, sample, *, mode, account_id, scan=False):
    evidence = await match_achievement_games(
        db, client, sample, mode=mode, account_id=account_id, scan=scan, single_win=False
    )
    return evidence["passerby"]


async def match_achievement_games(
    db, client, sample, *, mode, account_id, scan=False, single_win=True
):
    evidence = {"passerby": (), "single_win_first": ()}
    if db is None or mode not in {8, 9, 11, 12, 15, 16}:
        return evidence
    known, wins = {}, {}
    required = passerby_requirement(len(sample))

    def first(game):
        return bool(game.ended_at and number((game.seat(account_id) or {}).get("rank")) == 1)

    def results():
        return {
            "passerby": tuple(uuid for uuid, value in known.items() if value),
            "single_win_first": tuple(
                uuid for uuid, count in wins.items() if count == 1 and first(sample[uuid])
            ),
        }

    def complete():
        found = results()
        return len(found["passerby"]) >= required and (
            not single_win or len(found["single_win_first"]) >= required
        )

    try:
        for uuid, started, qualified in await db.passerby_games(mode, account_id, sample):
            if uuid in sample and started == sample[uuid].started_at.timestamp():
                known[uuid] = bool(qualified)
        if single_win:
            for uuid, started, count in await db.game_win_counts(mode, account_id, sample):
                if uuid in sample and started == sample[uuid].started_at.timestamp():
                    wins[uuid] = count
                    if count > 0:
                        known[uuid] = False
        if complete() or not scan or not callable(getattr(client, "range_stats", None)):
            return results()
        stamps = Counter(game.started_at for game in sample.values())
        # Both achievements share this budget and the API's global 1 QPS gate.
        # Confirmed matches persist; later queries resume unexamined matches.
        async with asyncio.timeout(12):
            for game in sorted(sample.values(), key=lambda game: game.started_at, reverse=True):
                need_passive = len(results()["passerby"]) < required and game.uuid not in known
                need_single_win = (
                    single_win
                    and len(results()["single_win_first"]) < required
                    and first(game)
                    and game.uuid not in wins
                    and not known.get(game.uuid)
                )
                if (
                    not (need_passive or need_single_win)
                    or not game.ended_at
                    or game.mode != str(mode)
                    or not game.seat(account_id)
                    or stamps[game.started_at] != 1
                ):
                    continue
                stats = await client.range_stats(
                    account_id,
                    start=game.started_at,
                    end=game.started_at + timedelta(milliseconds=1),
                    modes=(mode,),
                )
                count = win_count(stats)
                if count is not None:
                    await db.save_game_win_count(game, account_id, count)
                    wins[game.uuid] = count
                result = qualifies(stats)
                if result is not None:
                    await db.save_passerby_game(game, account_id, result)
                    known[game.uuid] = result
                if complete():
                    break
    except (TimeoutError, PaipuyaError):
        pass
    except sqlite3.Error:
        logger.warning("雀魂逐场成就缓存暂不可用", exc_info=True)
    return results()
