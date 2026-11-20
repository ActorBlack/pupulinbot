from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_majsoul_live import blob, integer, wrapped
from test_ms_tags import records, stats

from pupulinbot.db import Database
from pupulinbot.majsoul_rounds import replay_max_wind
from pupulinbot.ms_replay_tags import west_rounds
from pupulinbot.ms_tags import supplemental_tags


def test_west_tag_requires_sample_evidence_and_half_game():
    games = records(count=1)
    sample = stats(1, raw={"最大连庄": 6})
    tags = supplemental_tags(sample, games, 42)
    assert "独裁者" in tags and "鏖战" not in tags
    sample.raw["_replay_winds"] = {"outside-sample": 2, games[0].uuid: 1}
    assert "鏖战" not in supplemental_tags(sample, games, 42)
    sample.raw["_replay_winds"][games[0].uuid] = 2
    assert "鏖战" not in supplemental_tags(sample, games, 42)
    assert "鏖战" not in supplemental_tags(sample, [replace(games[0], mode="11")], 42)


def test_replay_round_wind_reads_both_replay_formats():
    for west in (False, True):
        rounds = [wrapped("RecordNewRound", integer(1, wind)) for wind in range(3 if west else 2)]
        legacy = wrapped("GameDetailRecords", b"".join(blob(1, r) for r in rounds))
        current = wrapped("GameDetailRecords", b"".join(blob(3, blob(3, r)) for r in rounds))
        assert replay_max_wind(legacy) == (2 if west else 1)
        assert replay_max_wind(current) == (2 if west else 1)
    assert replay_max_wind(wrapped("GameDetailRecords", b"")) is None


async def test_west_evidence_persists_and_reuses_confirmed_replay(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    game = records(count=1)[0]
    sample = {game.uuid: game}
    browser = SimpleNamespace(records_enabled=True, max_wind=AsyncMock(return_value=2))
    assert await west_rounds(db, browser, sample, mode=12) == {game.uuid: 2}
    assert await west_rounds(db, browser, sample, mode=12) == {game.uuid: 2}
    browser.max_wind.assert_awaited_once_with(game)
    assert await west_rounds(db, browser, sample, mode=11) == {}
    # Cached data must have the same replay start time.
    await db.save_replay_wind(replace(game, started_at=game.ended_at), 2)
    assert await west_rounds(db, None, sample, mode=12) == {}


async def test_unavailable_replay_does_not_become_a_negative_cache_entry(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    game = records(count=1)[0]
    sample = {game.uuid: game}
    browser = SimpleNamespace(records_enabled=True, max_wind=AsyncMock(return_value=None))
    assert await west_rounds(db, browser, sample, mode=12) == {}
    assert await db.replay_winds(12, sample) == []


def test_west_minimum_count_and_proportion_both_apply():
    from pupulinbot.ms_replay_tags import west_requirement

    for count, required in ((20, 2), (50, 2), (100, 2), (101, 3), (200, 4), (500, 10)):
        assert west_requirement(count) == required
        games = records(count=count)
        for hits, matches in ((required - 1, False), (required, True)):
            sample = stats(count, raw={"_replay_winds": {g.uuid: 2 for g in games[:hits]}})
            assert ("鏖战" in supplemental_tags(sample, games, 42)) is matches
