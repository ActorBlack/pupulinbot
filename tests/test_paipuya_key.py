import asyncio
import sys
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from test_cap import credentials, mock_http

from pupulinbot.paipuya import PaipuyaClient, PaipuyaError
from pupulinbot.paipuya_auth import PaipuyaAccess

BASE = "https://5-data.amae-koromo.com/api/v2/pl4"


@pytest.mark.asyncio
async def test_valid_key_uses_only_standard_bearer_header_and_no_solver():
    access = PaipuyaAccess("test-key", auto_cap=True)
    access._run_solver = AsyncMock()

    def handle(request):
        assert request.url.host == "5-data.amae-koromo.com"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert "X-Cap-Token" not in request.headers
        assert "cap_token_refreshed" not in request.url.params
        return httpx.Response(200, json=[])

    with mock_http(handle):
        client = PaipuyaClient(BASE, access=access)
        await client.records(42)
        assert await access.refresh(None) == "test-key"
        await client.close()
    access._run_solver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500, 502, 503, 504])
async def test_denial_limit_and_server_errors_never_activate_key_fallback(status):
    access = PaipuyaAccess("test-key", auto_cap=True)
    access._run_solver = AsyncMock()
    with mock_http(lambda req: httpx.Response(status, json={"error": "ip_not_allowed"})):
        with pytest.raises(PaipuyaError):
            await PaipuyaClient(BASE, access=access).records(42)
    assert access.using_api_key and not access.key_invalid
    access._run_solver.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_does_not_switch_authentication():
    access = PaipuyaAccess("test-key", auto_cap=True)
    access._run_solver = AsyncMock()

    def timeout(req):
        raise httpx.ReadTimeout("timeout", request=req)

    with mock_http(timeout), pytest.raises(PaipuyaError):
        await PaipuyaClient(BASE, access=access).records(42)
    access._run_solver.assert_not_called()
    assert access.using_api_key


@pytest.mark.asyncio
@pytest.mark.parametrize("code,body", [(401, {}), (403, {"error": "expired_api_key"})])
async def test_only_confirmed_invalid_key_activates_backup(code, body):
    access = PaipuyaAccess("test-key", auto_cap=True)
    access._run_solver = AsyncMock(return_value=credentials("backup"))
    requests = []

    def handle(req):
        requests.append(req)
        return (
            httpx.Response(code, json=body) if len(requests) == 1 else httpx.Response(200, json=[])
        )

    with mock_http(handle):
        assert await PaipuyaClient(BASE, access=access).records(42) == []
    assert access.key_invalid and not access.using_api_key
    assert requests[0].headers["Authorization"] == "Bearer test-key"
    assert "X-Cap-Token" not in requests[0].headers
    assert requests[1].headers["Authorization"] == "Bearer backup"
    access._run_solver.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://5-data.amae-koromo.com/api/v2/pl4",
        "https://example.com/api/v2/pl4",
        "https://5-data.amae-koromo.com.evil/api/v2/pl4",
        "https://5-data.amae-koromo.com:4433/api/v2/pl4",
    ],
)
async def test_key_cannot_be_sent_to_other_origins(url):
    access = PaipuyaAccess("test-key", auto_cap=True)
    with patch("httpx.AsyncClient") as http, pytest.raises(PaipuyaError):
        await PaipuyaClient(url, access=access).records(42)
    http.assert_not_called()


@pytest.mark.asyncio
async def test_redirect_does_not_forward_credentials():
    requests = []

    def handle(req):
        requests.append(req)
        return httpx.Response(302, headers={"Location": "https://example.com"})

    with mock_http(handle), pytest.raises(PaipuyaError):
        await PaipuyaClient(BASE, access=PaipuyaAccess("test-key")).records(42)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_every_retry_passes_the_shared_gate_and_rechecks_cooldown():
    access = PaipuyaAccess("test-key")
    count = 0

    def handle(req):
        nonlocal count
        count += 1
        return httpx.Response(503) if count == 1 else httpx.Response(200, json=[])

    access.gate.wait = AsyncMock()
    with mock_http(handle):
        await PaipuyaClient(BASE, access=access).records(42)
    assert access.gate.wait.await_count == 2

    async def limited():
        access.cooldown(60)

    access.gate.wait = limited
    with mock_http(handle), pytest.raises(PaipuyaError, match="访问暂缓"):
        await PaipuyaClient(BASE, access=access).records(42)
    assert count == 2


@pytest.mark.paipuya_pacing
@pytest.mark.asyncio
async def test_two_clients_share_one_qps_without_bursts(tmp_path):
    path = tmp_path / "pacing.db"
    times = []

    async def handle(req):
        times.append(time.monotonic())
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        clients = [
            PaipuyaClient(BASE, access=PaipuyaAccess("test-key", rate_limit_file=path)),
            PaipuyaClient(
                BASE.replace("pl4", "pl3"),
                mode="三麻",
                access=PaipuyaAccess("test-key", rate_limit_file=path),
            ),
        ]
        for c in clients:
            c._http = http
        await asyncio.gather(*(clients[i % 2].records(i) for i in range(4)))
    assert all(b - a >= 1 for a, b in zip(times, times[1:], strict=False))


@pytest.mark.paipuya_pacing
@pytest.mark.asyncio
async def test_independent_processes_use_the_same_api_clock(tmp_path):
    script = """import asyncio,sys,time
from pathlib import Path
from pupulinbot.paipuya_gate import PaipuyaGate
async def main():
 await PaipuyaGate(Path(sys.argv[1])).wait()
 print(time.monotonic())
asyncio.run(main())"""
    processes = [
        await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(tmp_path / "shared.db"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = await asyncio.gather(*(p.communicate() for p in processes))
    assert all(p.returncode == 0 for p in processes), [r[1].decode() for r in results]
    times = sorted(float(r[0]) for r in results)
    assert times[1] - times[0] >= 1


def test_nested_error_payload_is_not_misclassified_as_invalid_key():
    assert not PaipuyaClient._invalid_api_key(httpx.Response(403, json={"error": {"code": 403}}))


@pytest.mark.asyncio
async def test_slow_read_retry_preserves_key_gate_and_long_read_deadline():
    access = PaipuyaAccess("test-key", auto_cap=True)
    access._run_solver = AsyncMock()
    access.gate.wait = AsyncMock()
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert "X-Cap-Token" not in request.headers
        assert request.extensions["timeout"]["read"] == 60
        assert request.extensions["timeout"]["connect"] == 20
        if len(requests) == 1:
            raise httpx.ReadTimeout("slow upstream", request=request)
        return httpx.Response(200, json=[])

    with mock_http(handle):
        client = PaipuyaClient(BASE, access=access)
        assert await client.records(42) == []
        assert client.last_error is None
        await client.close()
    assert len(requests) == 2
    assert access.gate.wait.await_count == 2
    assert access.using_api_key
    access._run_solver.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_slow_query_is_not_retried():
    access = PaipuyaAccess("test-key")
    access.gate.wait = AsyncMock()

    async def handle(request):
        raise asyncio.CancelledError

    with mock_http(handle), pytest.raises(asyncio.CancelledError):
        await PaipuyaClient(BASE, access=access).records(42)
    assert access.gate.wait.await_count == 1
