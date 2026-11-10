import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.db import Database
from pupulinbot.models import GameRecord
from pupulinbot.paipuya import PaipuyaClient
from pupulinbot.promotion import majsoul_promotion, promotion_line
from pupulinbot.tenhou import TenhouClient
from pupulinbot.tenhou_monitor import TenhouMonitor, TenhouStore
from pupulinbot.tenhou_profile import rank_history


def test_majsoul_promotions_require_single_matching_game_and_real_rank_change():
    player = {"level": 10502, "gradingScore": 110}
    metadata = {"count": 1, "level": {"id": 10502, "score": 5900, "delta": 110}}
    assert majsoul_promotion(player, metadata) == "雀圣3星"
    for key, value in (("count", 2), ("count", 0), ("level", None)):
        assert majsoul_promotion(player, {**metadata, key: value}) is None
    for key, value in (("id", 10501), ("score", 100), ("score", float("nan")), ("delta", 100)):
        assert (
            majsoul_promotion(player, {**metadata, "level": {**metadata["level"], key: value}})
            is None
        )
    assert (
        majsoul_promotion(
            {"level": 10501, "gradingScore": -20},
            {"count": 1, "level": {"id": 10501, "score": 10, "delta": -20}},
        )
        is None
    )
    assert (
        majsoul_promotion(
            {"level": 10503, "gradingScore": 100},
            {"count": 1, "level": {"id": 10503, "score": 8990, "delta": 100}},
        )
        == "魂天Lv1"
    )


@pytest.mark.asyncio
async def test_majsoul_checks_exact_game_mode_and_timestamp_and_reuses_check():
    game = GameRecord(
        "promotion",
        datetime(2026, 11, 10, tzinfo=UTC),
        datetime.now(UTC),
        "12",
        [{"account_id": 42, "level": 10502, "gradingScore": 110}],
        {},
    )
    client = PaipuyaClient("https://example.invalid/api/v2/pl4")
    client._get = AsyncMock(
        return_value={"count": 1, "level": {"id": 10502, "score": 5900, "delta": 110}}
    )
    try:
        assert await client.promotion(game, 42) == "雀圣3星"
        assert await client.promotion(game, 42) == "雀圣3星"
        stamp = int(game.started_at.timestamp() * 1000)
        client._get.assert_awaited_once_with(f"player_stats/42/{stamp}/{stamp}", {"mode": "12"})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_optional_check_failure_or_timeout_does_not_prevent_result():
    client = SimpleNamespace(promotion=AsyncMock(side_effect=RuntimeError("offline")))
    assert await promotion_line(client, None, 42) == ""

    async def slow(*args):
        await asyncio.sleep(1)

    client.promotion = slow
    assert await promotion_line(client, None, 42, budget_seconds=0.001) == ""


@pytest.mark.asyncio
async def test_tenhou_congratulation_is_sent_once_and_failed_delivery_retries(tmp_path):
    db = Database(tmp_path / "bot.db")
    await db.initialize()
    store = TenhouStore(db)
    await store.initialize()
    client = SimpleNamespace(promotion=AsyncMock(return_value="七段"))
    monitor = TenhouMonitor(store, client)
    row = json.loads((Path(__file__).parent / "fixtures/tenhou/result.json").read_text())

    async def message():
        return await monitor.finish_message(row, "applePie")

    send = AsyncMock(side_effect=RuntimeError("offline"))
    with pytest.raises(RuntimeError):
        await monitor.deliver("test", "game", "finish", message, send)
    send.side_effect = None
    assert await monitor.deliver("test", "game", "finish", message, send)
    assert "applePie：欢迎新七段" in send.call_args.args[1]
    assert await TenhouMonitor(store, client).deliver("test", "game", "finish", message, send)
    assert send.await_count == 2
    assert client.promotion.await_count == 2


@pytest.mark.asyncio
async def test_tenhou_promotion_replay_is_tied_to_finished_game_and_ignores_later_games():
    cases = json.loads((Path(__file__).parent / "fixtures/tenhou/rank-oracle.json").read_text())
    case = cases[0]
    raw = copy.deepcopy(case["input"])
    result = rank_history(raw, "A", case["mode"], case["cutoff"], include_promotions=True)
    target = result["promotions"][-1]
    row = next(r for r in raw["list"] if r["starttime"] == target["starttime"])
    client = TenhouClient()
    client._profile_history = AsyncMock(return_value={**raw, "name": "A"})
    try:
        assert await client.promotion(row, "A") == "八段"
        assert await client.promotion(raw["list"][-1], "A") is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_tenhou_result_includes_promotions_for_each_monitored_tablemate():
    row = json.loads((Path(__file__).parent / "fixtures/tenhou/result.json").read_text())
    client = SimpleNamespace(
        promotion=AsyncMock(side_effect=lambda game, name: "七段" if name == "bipsd" else None)
    )
    monitor = TenhouMonitor(None, client)
    message = await monitor.finish_message(row, ["applePie", "bipsd", "bipsd"])
    assert "bipsd：欢迎新七段" in message
    assert "applePie：欢迎" not in message
    assert client.promotion.await_count == 2
