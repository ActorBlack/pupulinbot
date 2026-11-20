import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from pupulinbot.tenhou import TenhouBackoff, TenhouClient, TenhouError
from pupulinbot.tenhou_monitor import TenhouMonitor


def expire_cooldown(client):
    client._retry_until = 0
    client.next_request = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 503])
async def test_challenge_stops_all_queued_players_and_does_not_retry_other_host(status):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"cf-mitigated": "challenge"}, text="challenge")

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        results = await asyncio.gather(
            *(client._get("/api/listuser.php", {"name": str(i), "start": "1"}) for i in range(32)),
            return_exceptions=True,
        )
        assert all(isinstance(r, TenhouBackoff) for r in results)
        assert len(requests) == 1
        assert client.cooldown_seconds == 300
        assert "访问验证" in client.source_status
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_repeated_challenges_back_off_and_success_resets_delay():
    challenged = True
    calls = []

    def respond(request):
        calls.append(request)
        return (
            httpx.Response(403, headers={"cf-mitigated": "challenge"})
            if challenged
            else httpx.Response(200, json={"ok": True})
        )

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        for expected in (300, 600, 1200, 1800, 1800):
            expire_cooldown(client)
            with pytest.raises(TenhouBackoff):
                await client._get("/probe", ttl=0)
            assert client.cooldown_seconds == expected
        assert len(calls) == 5
        challenged = False
        expire_cooldown(client)
        assert await client._get("/probe", ttl=0) == {"ok": True}
        assert not client.source_status and client._backoff_attempts == 0
        challenged = True
        client.next_request = 0
        with pytest.raises(TenhouBackoff):
            await client._get("/probe", ttl=0)
        assert client.cooldown_seconds == 300
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_expired_cooldown_allows_only_one_recovery_probe():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        if request.url.path == "/blocked":
            return httpx.Response(403, headers={"cf-mitigated": "challenge"})
        if request.url.path == "/probe":
            entered.set()
            await release.wait()
        return httpx.Response(200, json={"ok": True})

    client = TenhouClient(httpx.MockTransport(respond))
    probe = None
    try:
        with pytest.raises(TenhouBackoff):
            await client._get("/blocked")
        expire_cooldown(client)
        probe = asyncio.create_task(client._get("/probe"))
        await asyncio.wait_for(entered.wait(), 2)
        results = await asyncio.gather(
            *(client._get(f"/other/{i}") for i in range(20)), return_exceptions=True
        )
        assert all(isinstance(r, TenhouBackoff) for r in results)
        assert len(calls) == 2 and client._probe_inflight
        release.set()
        assert await probe == {"ok": True}
        assert not client._probe_inflight and not client.source_status
        client.next_request = 0
        assert await client._get("/recovered") == {"ok": True}
        assert len(calls) == 3
    finally:
        release.set()
        if probe:
            await asyncio.gather(probe, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_late_success_from_pre_challenge_request_does_not_clear_cooldown():
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if request.url.path == "/late":
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    client = TenhouClient(httpx.MockTransport(respond))
    late = asyncio.create_task(client._get("/late"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        client.next_request = 0
        with pytest.raises(TenhouBackoff):
            await client._get("/blocked")
        release.set()
        assert await late == {"ok": True}
        assert client.cooldown_seconds == 300 and client._backoff_attempts == 1
        assert await client._get("/late") == {"ok": True}  # Fresh cache is usable.
    finally:
        release.set()
        await asyncio.gather(late, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_access_limits_honor_retry_after_without_host_retry(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"retry-after": "900"})

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        with pytest.raises(TenhouBackoff):
            await client._get("/limited")
        assert client.cooldown_seconds == 900
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_repeated_server_failures_pause_globally_after_one_failover():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503)

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        with pytest.raises(TenhouBackoff):
            await client._get("/unavailable")
        assert len(calls) == 2
        assert client.cooldown_seconds == 60
        with pytest.raises(TenhouBackoff):
            await client._get("/another-player")
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_generating_record_is_negatively_cached_without_blocking_other_players():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200, json={"retry": True} if request.url.path == "/pending" else {"ok": True}
        )

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        for _ in range(3):
            with pytest.raises(TenhouError, match="正在生成"):
                await client._get("/pending")
        assert len(calls) == 1 and not client.cooldown_seconds
        client.next_request = 0
        assert await client._get("/another-player") == {"ok": True}
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_overlapping_history_queries_share_one_backwards_rounded_window():
    calls = []

    def respond(request):
        calls.append(request)
        name = request.url.params["name"]
        return httpx.Response(200, json={"name": name, "list": []})

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        assert await client.history("A", 12345) == []
        assert await client.history("A", 12390) == []
        assert len(calls) == 1
        assert calls[0].url.params["start"] == "12300"
        client.next_request = 0
        await client._get("/api/listuser.php", {"name": "A", "start": "1"})
        assert calls[-1].url.params["start"] == "1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_request_starts_are_spaced_and_jittered(monkeypatch):
    now = [1000.0]
    real_sleep = asyncio.sleep
    calls = []

    async def sleep(delay):
        now[0] += delay
        await real_sleep(0)

    def respond(request):
        calls.append(now[0])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("pupulinbot.tenhou.asyncio.sleep", sleep)
    monkeypatch.setattr("pupulinbot.tenhou.uniform", lambda a, b: 0.2)
    client = TenhouClient(httpx.MockTransport(respond))
    client._clock = lambda: now[0]
    try:
        for i in range(3):
            await client._get(f"/data/{i}")
        assert calls == pytest.approx([1000, 1002, 1004])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_monitor_does_not_launch_player_jobs_during_shared_cooldown():
    store = SimpleNamespace(subscriptions=AsyncMock())
    monitor = TenhouMonitor(store, SimpleNamespace(cooldown_seconds=300))
    await monitor.poll(AsyncMock(), wait=False)
    store.subscriptions.assert_not_awaited()
    assert not monitor._jobs and not monitor._histories
    await monitor.close()


@pytest.mark.asyncio
async def test_waiting_paced_request_rechecks_gate_after_challenge(monkeypatch):
    first_entered, allow_challenge = asyncio.Event(), asyncio.Event()
    waiting, release_waiter = asyncio.Event(), asyncio.Event()
    real_sleep = asyncio.sleep
    calls = []

    async def sleep(delay):
        if delay > 0:
            waiting.set()
            await release_waiter.wait()
        await real_sleep(0)

    async def respond(request):
        calls.append(request)
        first_entered.set()
        await allow_challenge.wait()
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    monkeypatch.setattr("pupulinbot.tenhou.asyncio.sleep", sleep)
    client = TenhouClient(httpx.MockTransport(respond))
    first = asyncio.create_task(client._get("/first"))
    second = None
    try:
        await asyncio.wait_for(first_entered.wait(), 2)
        second = asyncio.create_task(client._get("/second"))
        await asyncio.wait_for(waiting.wait(), 2)
        allow_challenge.set()
        with pytest.raises(TenhouBackoff):
            await first
        release_waiter.set()
        with pytest.raises(TenhouBackoff):
            await second
        assert len(calls) == 1
    finally:
        allow_challenge.set()
        release_waiter.set()
        await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
        await client.close()


def test_retry_after_supports_http_dates_and_ignores_invalid_values():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    date = format_datetime(datetime.now(UTC) + timedelta(seconds=600), usegmt=True)
    assert (
        598 <= TenhouClient._retry_after(httpx.Response(429, headers={"retry-after": date})) <= 600
    )
    for value in ("invalid", "nan", "-3", ""):
        assert TenhouClient._retry_after(httpx.Response(429, headers={"retry-after": value})) == 0
