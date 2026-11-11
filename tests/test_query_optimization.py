import asyncio
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pupulinbot.db import Database
from pupulinbot.models import GameRecord
from pupulinbot.monitor import GameMonitor
from pupulinbot.ms_sample import rolling_ranks
from pupulinbot.paipuya import PaipuyaClient, PaipuyaError
from pupulinbot.query_cache import QueryCache


@pytest.mark.asyncio
async def test_cache_merges_requests_and_protects_against_mutation():
    cache = QueryCache(2)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def load():
        entered.set()
        await finish.wait()
        return {"values": [1]}

    loader = AsyncMock(side_effect=load)
    first = asyncio.create_task(cache.get("k", 30, loader))
    await entered.wait()
    second = asyncio.create_task(cache.get("k", 30, loader))
    finish.set()
    a, b = await asyncio.gather(first, second)
    a["values"].append(2)
    assert b["values"] == [1] and (await cache.get("k", 30, loader))["values"] == [1]
    assert loader.await_count == 1
    await cache.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_fetch():
    cache = QueryCache()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def load():
        entered.set()
        await finish.wait()
        return 42

    first = asyncio.create_task(cache.get("k", 30, load))
    await entered.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    finish.set()
    assert await cache.get("k", 30, load) == 42
    await cache.close()


@pytest.mark.asyncio
async def test_errors_expiry_and_capacity_do_not_return_stale_results():
    cache = QueryCache(2)
    loader = AsyncMock(side_effect=[RuntimeError("offline"), 1, 2, 3, 4])
    with pytest.raises(RuntimeError):
        await cache.get("a", 30, loader)
    assert await cache.get("a", 0, loader) == 1
    assert await cache.get("a", 30, loader) == 2
    await cache.get("b", 30, loader)
    await cache.get("c", 30, loader)
    assert len(cache.entries) == 2 and "a" not in cache.entries
    await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,requests", [(503, 2), (403, 1), (429, 1)])
async def test_only_transient_server_failures_are_retried(status, requests):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(status, headers={"Retry-After": "1"})

    actual = httpx.AsyncClient
    with (
        patch(
            "httpx.AsyncClient",
            side_effect=lambda **kw: actual(transport=httpx.MockTransport(handler), **kw),
        ),
        patch("pupulinbot.paipuya.asyncio.sleep", AsyncMock()),
    ):
        with pytest.raises(PaipuyaError):
            await PaipuyaClient("https://example.invalid").records(42)
    assert count == requests


@pytest.mark.asyncio
async def test_timeout_has_useful_message_and_is_not_cached_as_zero():
    def handler(request):
        raise httpx.ReadTimeout("")

    actual = httpx.AsyncClient
    client = PaipuyaClient("https://example.invalid")
    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kw: actual(transport=httpx.MockTransport(handler), **kw),
    ):
        with pytest.raises(PaipuyaError, match="超时"):
            await client.stats(42)
    assert not client.cache.entries and "超时" in client.last_error
    await client.close()


@pytest.mark.asyncio
async def test_monitor_history_is_fresh_while_queries_share_cache():
    client = PaipuyaClient("https://example.invalid")
    client.history = AsyncMock(return_value=([], False))
    await client.query_history(42, 100)
    await client.query_history(42, 100)
    assert client.history.await_count == 1
    await client.history(42, 100)
    assert client.history.await_count == 2
    await client.query_history(42, 201)
    await client.query_history(42, 201)
    assert client.history.await_count == 4
    await client.close()


def record(i):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)
    return GameRecord(
        str(i),
        start,
        start + timedelta(minutes=30),
        "12",
        [{"account_id": 42, "rank": 1, "score": 40000}],
        {},
    )


@pytest.mark.asyncio
async def test_outage_backfill_includes_game_in_progress_at_activation(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.initialize()
    await db.bind("u", 42, "name")
    await db.subscribe("g", "u")
    games = [record(i) for i in range(30)]
    cutoff = games[0].started_at + timedelta(minutes=10)
    async with db.connect() as c:
        await c.execute("UPDATE subscriptions SET created_at=?", (cutoff.isoformat(),))
        await c.commit()
    client = SimpleNamespace(
        records=AsyncMock(return_value=games[-20:]),
        history=AsyncMock(side_effect=[(games[1:], False), ([games[0]], False)]),
    )
    monitor = GameMonitor(db, client, SimpleNamespace(enabled=False))
    send = AsyncMock()
    rows = await db.subscriptions()
    await monitor.poll(rows, send)
    assert [call.args[1].splitlines()[-1].split("=")[-1] for call in send.call_args_list] == [
        str(i) for i in range(30)
    ]
    assert send.await_count == 30 and client.history.await_count == 2
    client.history.assert_any_await(42, 1, end=cutoff)
    await monitor.poll(rows, send)
    assert send.await_count == 30 and client.history.await_count == 2
    saved = await db.monitor_health(rows[0]["subscriber"])
    assert saved["last_success"] and not saved["error"] and not saved["capped"]


@pytest.mark.asyncio
async def test_backfill_cap_is_visible_and_failure_does_not_advance_anchor(tmp_path):
    db = Database(tmp_path / "m.db")
    await db.initialize()
    await db.bind("u", 42, "name")
    await db.subscribe("g", "u")
    async with db.connect() as c:
        await c.execute("UPDATE subscriptions SET created_at='2025-01-01'")
        await c.commit()
    games = [record(i) for i in range(22)]
    client = SimpleNamespace(
        records=AsyncMock(return_value=games[-20:]), history=AsyncMock(return_value=(games, True))
    )
    monitor = GameMonitor(db, client, SimpleNamespace(enabled=False))
    rows = await db.subscriptions()
    await monitor.poll(rows, AsyncMock())
    assert (await db.monitor_health(rows[0]["subscriber"]))["capped"] == 1
    assert not monitor.anchors
    client.history.return_value = (games, False)
    await db.release_event("0", rows[0]["subscriber"], "finish")
    # A fresh UUID fails delivery; retrying must re-check the whole backfill window.
    client.history.return_value = ([record(30), *games], False)
    send = AsyncMock(side_effect=RuntimeError("QQ offline"))
    await monitor.poll(rows, send)
    assert not monitor.anchors
    assert (await db.monitor_health(rows[0]["subscriber"]))["error"]


def test_rolling_average_preserves_missing_windows():
    ranks = [1] * 10 + [None] + [2] * 10
    values = rolling_ranks(ranks)
    assert values[9] == 1 and values[-1] == 2
    assert all(math.isnan(v) for v in values[10:20])
