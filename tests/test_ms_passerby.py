from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ms_tags import records, stats

from pupulinbot.db import Database
from pupulinbot.models import Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_options import parse_options
from pupulinbot.ms_passerby import passerby_games, qualifies
from pupulinbot.ms_tag_help import TAG_DESCRIPTIONS
from pupulinbot.ms_tags import supplemental_tags
from pupulinbot.ms_trait_tags import passerby_requirement
from pupulinbot.paipuya import PaipuyaError


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, True),
        ({"win_rate": 0.01}, False),
        ({"deal_in_rate": 0.01}, False),
        ({"riichi_rate": 0.01}, False),
        ({"win_rate": None}, None),
        ({"win_rate": float("nan")}, None),
        ({"deal_in_rate": -1}, None),
        ({"riichi_rate": True}, None),
        ({"games": 2}, None),
        ({"raw": {"_hand_count": 0}}, None),
    ],
)
def test_zero_events_require_complete_single_game(changes, expected):
    values = dict(games=1, raw={"_hand_count": 12}, win_rate=0, deal_in_rate=0, riichi_rate=0)
    values.update(changes)
    assert qualifies(SimpleNamespace(**values)) is expected


async def test_persists_positive_and_negative_scoped_results(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(count=2)
    sample = {game.uuid: game for game in games}
    client = SimpleNamespace(
        range_stats=AsyncMock(
            side_effect=[stats(1), stats(1, win_rate=0, deal_in_rate=0, riichi_rate=0)]
        )
    )
    assert await passerby_games(db, client, sample, mode=12, account_id=42, scan=True) == (
        games[0].uuid,
    )
    assert client.range_stats.await_count == 2
    first = client.range_stats.await_args_list[0]
    assert first.kwargs == dict(
        start=games[1].started_at, end=games[1].started_at + timedelta(milliseconds=1), modes=(12,)
    )
    assert len(await db.passerby_games(12, 42, sample)) == 2
    assert await passerby_games(db, client, sample, mode=12, account_id=42) == (games[0].uuid,)
    assert client.range_stats.await_count == 2
    assert await passerby_games(db, client, sample, mode=12, account_id=999) == ()
    assert await passerby_games(db, client, sample, mode=9, account_id=42) == ()
    changed = {games[0].uuid: replace(games[0], started_at=games[0].ended_at)}
    assert await passerby_games(db, client, changed, mode=12, account_id=42) == ()


async def test_missing_data_and_errors_remain_unconfirmed(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(count=1)
    sample = {game.uuid: game for game in games}
    client = SimpleNamespace(
        range_stats=AsyncMock(
            side_effect=[
                stats(2),
                PaipuyaError("timeout"),
                stats(1, win_rate=0, deal_in_rate=0, riichi_rate=0),
            ]
        )
    )
    for _ in range(2):
        assert await passerby_games(db, client, sample, mode=12, account_id=42, scan=True) == ()
        assert await db.passerby_games(12, 42, sample) == []
    assert await passerby_games(db, client, sample, mode=12, account_id=42, scan=True) == (
        games[0].uuid,
    )


async def test_duplicate_timestamps_are_not_inferred(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(count=2)
    games[1] = replace(games[1], started_at=games[0].started_at)
    client = SimpleNamespace(range_stats=AsyncMock())
    assert (
        await passerby_games(
            db, client, {g.uuid: g for g in games}, mode=12, account_id=42, scan=True
        )
        == ()
    )
    client.range_stats.assert_not_awaited()


async def test_query_enriches_tag_and_reuses_cache(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(count=2)
    client = SimpleNamespace(
        range_stats=AsyncMock(return_value=stats(1, win_rate=0, deal_in_rate=0, riichi_rate=0))
    )
    service = MahjongQueries(db, client, None)
    original = stats(2)
    enriched = await service.with_yakuman_names(
        Player(42, "测试"), original, games, parse_options("-r 玉南"), client, backfill=True
    )
    assert "吃瓜群众" in supplemental_tags(enriched, games, 42)
    reused = await service.with_yakuman_names(
        Player(42, "测试"), original, games, parse_options("-r 玉南"), client
    )
    assert "吃瓜群众" in supplemental_tags(reused, games, 42)
    assert client.range_stats.await_count == 2
    assert "_passerby_replays" not in original.raw
    assert "吃瓜群众" not in supplemental_tags(enriched, records(count=2)[1:], 42)
    assert "吃瓜群众" not in supplemental_tags(enriched, games, 42, mode=3)
    assert "均为0" in TAG_DESCRIPTIONS["吃瓜群众"]


@pytest.mark.parametrize(
    "count,required", [(1, 2), (50, 2), (100, 2), (101, 3), (150, 3), (200, 4), (500, 10)]
)
def test_passerby_minimum_and_percentage(count, required):
    assert passerby_requirement(count) == required
    games = records(count=count)
    sample = stats(count)
    sample.raw["_passerby_replays"] = [game.uuid for game in games[: required - 1]]
    assert "吃瓜群众" not in supplemental_tags(sample, games, 42, maximum=100)
    sample.raw["_passerby_replays"] = [game.uuid for game in games[:required]]
    assert ("吃瓜群众" in supplemental_tags(sample, games, 42, maximum=100)) is (count >= required)


async def test_one_cached_match_does_not_stop_remaining_scan(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(count=100)
    await db.save_passerby_game(games[0], 42, True)
    client = SimpleNamespace(
        range_stats=AsyncMock(return_value=stats(1, win_rate=0, deal_in_rate=0, riichi_rate=0))
    )
    found = await passerby_games(
        db, client, {game.uuid: game for game in games}, mode=12, account_id=42, scan=True
    )
    assert set(found) == {games[0].uuid, games[-1].uuid}
    client.range_stats.assert_awaited_once()
