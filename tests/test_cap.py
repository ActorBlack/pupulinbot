import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pupulinbot.paipuya import PaipuyaClient, PaipuyaError
from pupulinbot.paipuya_auth import PaipuyaAccess

HTTP_CLIENT = httpx.AsyncClient


def mock_http(handler):
    return patch(
        "httpx.AsyncClient",
        side_effect=lambda **kwargs: HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )


def credentials(token="temporary"):
    return {"token": token, "expires": (time.time() + 600) * 1000}


@pytest.mark.asyncio
async def test_cap_challenge_then_retry_with_credentials_and_cache_buster():
    access = PaipuyaAccess(auto_cap=True)
    access._run_solver = AsyncMock(return_value=credentials())
    requests = []

    def handle(request):
        requests.append(request)
        if "Authorization" not in request.headers:
            return httpx.Response(429, text="x-cap-token-required")
        assert request.headers["Authorization"] == "Bearer temporary"
        assert request.headers["X-Cap-Token"] == "temporary"
        assert request.headers["Cache-Control"] == "no-cache"
        assert request.url.params["cap_token_refreshed"]
        return httpx.Response(200, json=[])

    with mock_http(handle):
        assert await PaipuyaClient("https://api.example", access=access).records(42) == []
    assert len(requests) == 2
    access._run_solver.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_concurrent_refresh_shared_across_three_and_four_player_clients():
    access = PaipuyaAccess(auto_cap=True)

    async def solve(rejected):
        await asyncio.sleep(0.01)
        return credentials()

    access._run_solver = AsyncMock(side_effect=solve)

    def handle(request):
        if request.headers.get("Authorization") != "Bearer temporary":
            return httpx.Response(429, text="x-cap-token-required")
        return httpx.Response(200, json=[])

    with mock_http(handle):
        await asyncio.gather(
            PaipuyaClient("https://api.example/pl3", access=access).records(1),
            PaipuyaClient("https://api.example/pl4", access=access).records(2),
        )
    assert access._run_solver.await_count == 1


@pytest.mark.asyncio
async def test_plain_rate_limit_respects_cooldown_without_solving():
    access = PaipuyaAccess(auto_cap=True)
    access._run_solver = AsyncMock()
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(429, headers={"Retry-After": "3600"}, text="rate limited")

    with mock_http(handle):
        client = PaipuyaClient("https://api.example", access=access)
        with pytest.raises(PaipuyaError, match="Retry-After"):
            await client.records(42)
        with pytest.raises(PaipuyaError, match="暂缓"):
            await client.records(42)
    assert count == 1
    access._run_solver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 401])
async def test_denied_requests_do_not_trigger_solver(status):
    access = PaipuyaAccess(auto_cap=True)
    access._run_solver = AsyncMock()
    with mock_http(lambda request: httpx.Response(status)):
        with pytest.raises(PaipuyaError):
            await PaipuyaClient("https://api.example", access=access).records(42)
    access._run_solver.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_key_is_used_and_never_replaced_by_solver():
    access = PaipuyaAccess("authorized-key", auto_cap=True)
    access._run_solver = AsyncMock()

    def handle(request):
        assert request.headers["Authorization"] == "Bearer authorized-key"
        return httpx.Response(429, text="x-cap-token-required")

    with mock_http(handle), pytest.raises(PaipuyaError, match="有效 API Key"):
        await PaipuyaClient("https://5-data.amae-koromo.com/api/v2/pl4", access=access).records(42)
    access._run_solver.assert_not_called()


@pytest.mark.asyncio
async def test_solver_failure_is_bounded_and_puts_subsequent_requests_on_cooldown():
    access = PaipuyaAccess(auto_cap=True)
    access._run_solver = AsyncMock(side_effect=TimeoutError())
    with mock_http(lambda request: httpx.Response(429, text="x-cap-token-required")):
        client = PaipuyaClient("https://api.example", access=access)
        with pytest.raises(PaipuyaError, match="暂停重试"):
            await client.records(42)
        with pytest.raises(PaipuyaError, match="暂缓"):
            await client.records(42)
    assert access._run_solver.await_count == 1


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_worker_group_and_reaps_parent():
    access = PaipuyaAccess(auto_cap=True, timeout=0.01)

    async def hang(data):
        await asyncio.sleep(10)

    proc = AsyncMock()
    proc.pid = 12345
    proc.communicate.side_effect = hang
    proc.wait.return_value = -9
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("os.killpg") as kill,
        pytest.raises(TimeoutError),
    ):
        await access._run_solver(None)
    kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_subprocess_cancellation_also_reaps_worker():
    access = PaipuyaAccess(auto_cap=True)
    proc = AsyncMock()
    proc.pid = 12345
    proc.communicate.side_effect = asyncio.CancelledError()
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("os.killpg") as kill,
        pytest.raises(asyncio.CancelledError),
    ):
        await access._run_solver(None)
    kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_solver_credentials_never_returned_as_user_facing_errors():
    access = PaipuyaAccess(auto_cap=True)
    access._run_solver = AsyncMock(side_effect=ValueError(json.dumps(credentials())))
    with pytest.raises(Exception, match="CAP 验证失败") as error:
        await access.refresh(None)
    assert "temporary" not in str(error.value)
