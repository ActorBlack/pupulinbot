from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_ms_tags import records, stats

from pupulinbot.db import Database
from pupulinbot.models import Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_options import parse_options
from pupulinbot.ms_passerby import match_achievement_games, win_count
from pupulinbot.ms_tag_help import tag_help
from pupulinbot.ms_tags import supplemental_tags


@pytest.mark.parametrize(
    "games,hands,rate,expected",
    [
        (1, 12, 1 / 12, 1),
        (1, 12, 2 / 12, 2),
        (1, 12, 0, 0),
        (2, 12, 1 / 12, None),
        (1, 0, 0, None),
        (1, None, 0, None),
        (1, 12, 0.0833, None),
        (1, 12, None, None),
        (1, 12, True, None),
        (1, 12, float("nan"), None),
    ],
)
def test_single_game_win_count_requires_exact_integer(games, hands, rate, expected):
    value = SimpleNamespace(games=games, raw={"_hand_count": hands}, win_rate=rate)
    assert win_count(value) == expected


async def test_shared_scan_confirms_both_tags_and_reuses_cache(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(ranks=[2, 2, 1, 1])
    single = stats(1, win_rate=1 / 12)
    passive = stats(1, win_rate=0, deal_in_rate=0, riichi_rate=0)
    client = SimpleNamespace(range_stats=AsyncMock(side_effect=[single, single, passive, passive]))
    sample = {game.uuid: game for game in games}
    found = await match_achievement_games(db, client, sample, mode=12, account_id=42, scan=True)
    assert set(found["single_win_first"]) == {games[2].uuid, games[3].uuid}
    assert set(found["passerby"]) == {games[0].uuid, games[1].uuid}
    assert client.range_stats.await_count == 4
    reused = await match_achievement_games(db, client, sample, mode=12, account_id=42, scan=True)
    assert {key: set(value) for key, value in reused.items()} == {
        key: set(value) for key, value in found.items()
    }
    assert client.range_stats.await_count == 4
    assert not (await match_achievement_games(db, client, sample, mode=12, account_id=999))[
        "single_win_first"
    ]
    changed = {game.uuid: replace(game, started_at=game.ended_at) for game in games}
    assert not (await match_achievement_games(db, client, changed, mode=12, account_id=42))[
        "single_win_first"
    ]


async def test_single_win_without_first_or_two_wins_without_single_do_not_count(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(ranks=[2, 1])
    for game, count in zip(games, [1, 2], strict=True):
        await db.save_game_win_count(game, 42, count)
    found = await match_achievement_games(
        db, None, {g.uuid: g for g in games}, mode=12, account_id=42
    )
    assert found["single_win_first"] == ()


@pytest.mark.parametrize(
    "count,matches,expected",
    [
        (100, 1, False),
        (100, 2, True),
        (200, 2, False),
        (200, 4, True),
        (201, 4, False),
        (201, 5, True),
        (500, 10, True),
    ],
)
def test_single_win_first_requires_two_games_and_two_percent(count, matches, expected):
    games = records(count=count)
    sample = stats(count, raw={"_single_win_first_replays": [g.uuid for g in games[:matches]]})
    assert ("一鸣惊人" in supplemental_tags(sample, games, 42, maximum=100)) is expected
    assert "一鸣惊人" not in supplemental_tags(sample, games, 42, mode=3)
    sample.raw["_single_win_first_replays"] = ["outside"] * matches
    assert "一鸣惊人" not in supplemental_tags(sample, games, 42, maximum=100)


async def test_query_attaches_verified_single_win_evidence(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    games = records(ranks=[1, 1])
    client = SimpleNamespace(range_stats=AsyncMock(return_value=stats(1, win_rate=1 / 12)))
    service = MahjongQueries(db, client, None)
    original = stats(2)
    result = await service.with_yakuman_names(
        Player(42, "测试"), original, games, parse_options("-r 玉南"), client, backfill=True
    )
    assert "一鸣惊人" in supplemental_tags(result, games, 42)
    assert "_single_win_first_replays" not in original.raw
    assert "一次和牌" in tag_help("一鸣惊人")
