import asyncio
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from pupulinbot.models import GameRecord
from pupulinbot.monitor import GameMonitor
from pupulinbot.monitor_card import collect_profiles, rank_snapshot, rate, render_monitor_card


def game(count=4):
    now = datetime(2026, 11, 1, tzinfo=UTC)
    return GameRecord(
        "preview-test",
        now,
        now,
        "12",
        [
            {
                "account_id": i + 1,
                "nickname": "测试玩家" + str(i),
                "rank": i + 1,
                "score": 30000,
                "level": 10402,
                "gradingScore": -42,
            }
            for i in range(count)
        ],
        {},
    )


def test_card_renders_real_shapes_and_unknown_stats_for_sanma_and_yonma():
    assert rate(None) is None and rate(float("nan")) is None
    assert rate(21.5) == 0.215 and rate(0.215) == 0.215
    for count in (3, 4):
        for event in ("start", "finish"):
            png = render_monitor_card(game(count), event)
            image = Image.open(io.BytesIO(png))
            assert image.size == (1080, 706) and image.format == "PNG"


@pytest.mark.asyncio
async def test_optional_profiles_timeout_does_not_block_notification():
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    client = SimpleNamespace(stats=stalled, query_history=stalled)
    profiles = await collect_profiles(client, game(), budget_seconds=0.01)
    assert all(not p for p in profiles.values())


@pytest.mark.asyncio
async def test_recent_tiles_exclude_current_and_future_games():
    current = game()
    client = SimpleNamespace(
        stats=AsyncMock(side_effect=RuntimeError("offline")),
        query_history=AsyncMock(return_value=([current], False)),
    )
    profiles = await collect_profiles(client, current)
    assert all(p["ranks"] == [] for p in profiles.values())


@pytest.mark.asyncio
async def test_card_delivery_failure_releases_claim_for_retry():
    db = SimpleNamespace(
        claim_game_event=AsyncMock(return_value=True),
        complete_event=AsyncMock(),
        release_event=AsyncMock(),
    )
    build = AsyncMock(return_value="rendered image plus link")
    monitor = GameMonitor(db, None, None, notification_builder=build)
    sub = {"group_id": "1", "qq_id": "2", "account_id": 1}
    send = AsyncMock(side_effect=RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        await monitor._deliver(sub, game(), "finish", "text", send)
    db.release_event.assert_awaited_once()
    db.complete_event.assert_not_awaited()
    send.side_effect = None
    await monitor._deliver(sub, game(), "finish", "text", send)
    assert send.call_args.args[1] == "rendered image plus link"
    db.complete_event.assert_awaited_once()


def test_rank_pt_uses_upstream_rank_metadata_not_game_score_or_delta():
    player = {"level": 10502, "score": 40000, "gradingScore": 63}
    rank = rank_snapshot(player, None)
    assert rank["major"] == 5 and rank["star"] == 2
    assert rank["points"] is None and rank["cap"] is None
    stats = SimpleNamespace(raw={"level": {"id": 10402, "score": 1200, "delta": 95}})
    rank = rank_snapshot(player, stats)
    assert rank == {"major": 4, "star": 2, "points": 1295, "cap": 3200}
    assert rank_snapshot({"level": float("nan")}, None) is None


def test_rank_promotion_and_celestial_pt_scale_remain_upstream_compatible():
    stats = SimpleNamespace(raw={"level": {"id": 10502, "score": 5980, "delta": 30}})
    rank = rank_snapshot({}, stats)
    assert rank["star"] == 3 and rank["points"] == 4500 and rank["cap"] == 9000
    stats.raw["level"] = {"id": 10701, "score": 1000, "delta": 0}
    assert rank_snapshot({}, stats)["points"] == 1000


def test_card_renders_long_names_and_available_statistics():
    current = game()
    current.players[0]["nickname"] = "很长的中文玩家昵称Japaneseあいうえお" * 4
    stats = SimpleNamespace(
        raw={"level": {"id": 10502, "score": 3785, "delta": 0}},
        games=2193,
        win_rate=0.223,
        deal_in_rate=0.109,
        call_rate=0.254,
        riichi_rate=0.201,
    )
    profiles = {1: {"stats": stats, "ranks": [1, 3, 3, 1, 1, 4, 2, 2, 2, 4]}}
    image = Image.open(io.BytesIO(render_monitor_card(current, profiles=profiles)))
    assert image.mode == "RGB" and image.size == (1080, 706)


@pytest.mark.asyncio
async def test_slow_build_and_send_use_separate_deadlines_and_cancellation_releases_claim():
    db = SimpleNamespace(
        claim_game_event=AsyncMock(return_value=True),
        complete_event=AsyncMock(),
        release_event=AsyncMock(),
    )

    async def build(*args):
        await asyncio.sleep(0.04)
        return "image"

    async def send(*args):
        await asyncio.sleep(0.04)

    monitor = GameMonitor(
        db, None, None, notification_builder=build, build_timeout=0.07, send_timeout=0.07
    )
    sub = {"group_id": "1", "qq_id": "2", "account_id": 1}
    assert await monitor._deliver(sub, game(), "finish", "text", send)
    db.complete_event.assert_awaited_once()
    db.release_event.assert_not_awaited()
    monitor.notification_builder = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await monitor._deliver(sub, game(), "finish", "text", send)
    db.release_event.assert_awaited_once()
