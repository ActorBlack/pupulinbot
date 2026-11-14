import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.exception import IgnoredException
from test_batch_review import native_report

from pupulinbot import capsolver
from pupulinbot.batch_review import BatchReviews
from pupulinbot.batchmortal_worker import BrowserWorker
from pupulinbot.db import Database
from pupulinbot.group_policy import check_group_command
from pupulinbot.online_review import OnlinePaused
from pupulinbot.review import ReviewError

LINK = "https://game.maj-soul.com/1/?paipu=261110-306acb95-44c1-4b6a-86df-b8a92b950cd1"
REPORT = "https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json"
WIDGET = {
    "url": "https://mjai.ekyu.moe/zh-cn.html?url=private-game",
    "sitekey": "0x4TestWidget123",
    "action": "review",
}


def test_turnstile_uses_one_create_and_polls_the_same_task(monkeypatch):
    monkeypatch.setattr(capsolver.time, "sleep", lambda _: None)
    requests = []
    responses = iter(
        [
            {"errorId": 0, "status": "idle", "taskId": "abc"},
            {"errorId": 0, "status": "processing"},
            {"errorId": 0, "status": "ready", "solution": {"token": "verified-token"}},
        ]
    )

    def handle(request):
        requests.append((request.url.host, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=next(responses))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert (
            capsolver.solve_turnstile(client, "test-key", capsolver.review_task(WIDGET))
            == "verified-token"
        )
    assert [path for _, path, _ in requests] == ["/createTask", "/getTaskResult", "/getTaskResult"]
    assert {host for host, _, _ in requests} == {"api.capsolver.com"}
    assert all(body["clientKey"] == "test-key" for _, _, body in requests)
    assert requests[0][2]["task"] == {
        "type": "AntiTurnstileTaskProxyLess",
        "websiteURL": "https://mjai.ekyu.moe/zh-cn.html",
        "websiteKey": WIDGET["sitekey"],
        "metadata": {"action": "review"},
    }
    assert [body["taskId"] for _, _, body in requests[1:]] == ["abc", "abc"]
    assert "private-game" not in str(requests)


def test_provider_error_never_returns_secret_or_recreates_paid_task():
    handler = Mock(
        return_value=httpx.Response(
            200,
            json={
                "errorId": 1,
                "errorCode": "ERROR_ZERO_BALANCE",
                "errorDescription": "test-key",
            },
        )
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(capsolver.CapSolverError, match="ERROR_ZERO_BALANCE") as error:
            capsolver.solve_turnstile(client, "test-key", capsolver.review_task(WIDGET))
    assert "test-key" not in str(error.value)
    assert handler.call_count == 1


def test_poll_network_failure_reuses_existing_paid_task(monkeypatch):
    monkeypatch.setattr(capsolver.time, "sleep", lambda _: None)
    requests = []

    def handle(request):
        requests.append((request.url.path, json.loads(request.content)))
        if len(requests) == 1:
            return httpx.Response(
                200, json={"errorId": 0, "status": "processing", "taskId": "same"}
            )
        if len(requests) == 2:
            raise httpx.ReadTimeout("temporary")
        return httpx.Response(
            200, json={"errorId": 0, "status": "ready", "solution": {"token": "ok"}}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert capsolver.solve_turnstile(client, "test-key", {}) == "ok"
    assert [path for path, _ in requests] == ["/createTask", "/getTaskResult", "/getTaskResult"]
    assert requests[1][1]["taskId"] == requests[2][1]["taskId"] == "same"


@pytest.mark.parametrize("recover", [True, False])
def test_definitive_solve_failure_retries_only_once(monkeypatch, recover):
    clock = [0.0]
    monkeypatch.setattr(capsolver.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(capsolver.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    requests = []
    created = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(request.url.path)
        if request.url.path == "/createTask":
            created.append(str(len(created)))
            data = {"errorId": 0, "status": "processing", "taskId": created[-1]}
        elif recover and body["taskId"] == "1":
            data = {"errorId": 0, "status": "ready", "solution": {"token": "ok"}}
        else:
            data = {"errorId": 1, "errorCode": "ERROR_CAPTCHA_SOLVE_FAILED"}
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        if recover:
            assert capsolver.solve_turnstile(client, "test-key", {}, timeout=60) == "ok"
        else:
            with pytest.raises(capsolver.CapSolverError, match="ERROR_CAPTCHA_SOLVE_FAILED"):
                capsolver.solve_turnstile(client, "test-key", {}, timeout=60)
    assert requests == ["/createTask", "/getTaskResult", "/createTask", "/getTaskResult"]
    assert clock[0] == 11


def test_ambiguous_creation_timeout_never_creates_again():
    handler = Mock(side_effect=httpx.ReadTimeout("unknown creation outcome"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(capsolver.CapSolverTransportError):
            capsolver.solve_turnstile(client, "test-key", {}, timeout=60)
    assert handler.call_count == 1


@pytest.mark.parametrize(
    ("code", "timeout"), [("ERROR_CAPTCHA_SOLVE_FAILED", 20), ("ERROR_ZERO_BALANCE", 60)]
)
def test_solver_does_not_retry_without_budget_or_for_account_error(monkeypatch, code, timeout):
    clock = [0.0]
    monkeypatch.setattr(capsolver.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(capsolver.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))
    responses = iter(
        [
            {"errorId": 0, "status": "processing", "taskId": "same"},
            {"errorId": 1, "errorCode": code},
        ]
    )
    handler = Mock(side_effect=lambda _: httpx.Response(200, json=next(responses)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(capsolver.CapSolverError, match=code):
            capsolver.solve_turnstile(client, "test-key", {}, timeout=timeout)
    assert handler.call_count == 2


def test_untrusted_site_or_ready_browser_token_never_calls_paid_api(monkeypatch):
    request = Mock(side_effect=AssertionError("paid call"))
    monkeypatch.setattr(capsolver, "solve_turnstile", request)
    browser = SimpleNamespace(execute_script=Mock(return_value={**WIDGET, "hasToken": True}))
    capsolver.solve_review_form(browser, "test-key")
    browser.execute_script.return_value = {**WIDGET, "url": "https://wrong.example/"}
    with pytest.raises(capsolver.CapSolverError, match="官方复盘表单"):
        capsolver.solve_review_form(browser, "test-key")
    request.assert_not_called()


def test_solver_reuses_supplied_http_client_across_seats(monkeypatch):
    client = Mock()
    solve = Mock(return_value="token")
    monkeypatch.setattr(capsolver, "solve_turnstile", solve)
    browser = SimpleNamespace(execute_script=Mock(side_effect=[WIDGET, True, WIDGET, True]))
    for _ in range(2):
        capsolver.solve_review_form(browser, "test-key", client=client)
    assert solve.call_count == 2
    assert all(call.args[0] is client for call in solve.call_args_list)
    client.close.assert_not_called()


@pytest.mark.parametrize("command", ["exmortal", "extable"])
def test_only_granted_users_can_use_capsolver_in_monitor_only_groups(command):
    restricted = {"456"}
    event = GroupMessageEvent.model_construct(group_id=456, user_id=123)
    state = {"_prefix": {"command": (command,)}}
    with pytest.raises(IgnoredException):
        check_group_command(event, state, restricted, vip2=True)
    check_group_command(event, state, restricted, capsolver_users={"123"})
    with pytest.raises(IgnoredException):
        check_group_command(
            event, {"_prefix": {"command": ("mortal",)}}, restricted, capsolver_users={"123"}
        )


@pytest.mark.asyncio
async def test_batch_route_is_persisted_authorized_and_rechecked_on_retry(tmp_path):
    bridge = SimpleNamespace(client=SimpleNamespace(url="https://bridge.test"), submit=AsyncMock())
    data = native_report()
    online = SimpleNamespace(concurrency=1, submit=AsyncMock(return_value=(data, REPORT)))
    manager = BatchReviews(
        tmp_path, None, bridge, online, capsolver_enabled=True, capsolver_users={"u"}
    )
    normal = await manager.create_links([LINK], "g", "other")
    assert normal["verification"] == "browser"
    with pytest.raises(ReviewError, match="未开通"):
        await manager.create_links([LINK], "g", "other", verification="capsolver")
    job = await manager.create_links([LINK], "g", "u", verification="capsolver")
    assert manager.load(job["id"], "g", "u")["verification"] == "capsolver"
    job.update(status="retry_wait", next_retry_at=datetime.now(UTC).isoformat())
    manager.save(job)
    manager.capsolver_users = frozenset()
    await manager.retry_due()
    assert not manager.tasks
    with pytest.raises(ReviewError, match="未开通"):
        manager.start(job["id"], "g", "u")
    online.submit.assert_not_awaited()
    manager.capsolver_users = {"u"}
    manager.start(job["id"], "g", "u")
    await manager.tasks[job["id"]]
    assert online.submit.await_args.args[1]["verification"] == "capsolver"
    assert manager.load(job["id"], "g", "u")["rows"][0]["status"] == "done"
    bridge.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verification", ["browser", "capsolver"])
@pytest.mark.parametrize("failure", ["uncertain", "server_timeout"])
async def test_automatic_retry_preserves_verification_and_submission_state(
    tmp_path, verification, failure
):
    bridge = SimpleNamespace(client=SimpleNamespace(url=None))
    online = SimpleNamespace(concurrency=1)
    manager = BatchReviews(
        tmp_path, None, bridge, online, capsolver_enabled=True, capsolver_users={"u"}
    )

    async def submit_then_fail(key, row, on_submitted):
        await on_submitted()
        raise OnlinePaused(failure, "Mortal result unavailable")

    online.submit = AsyncMock(side_effect=submit_then_fail)
    job = await manager.create_links([LINK], "g", "u", verification=verification)
    job_id = job["id"]
    manager.start(job_id, "g", "u")
    await manager.tasks[job_id]
    waiting = manager.load(job_id, "g", "u")
    assert waiting["status"] == "retry_wait"
    assert waiting["verification"] == verification

    online.submit = AsyncMock(return_value=(native_report(), REPORT))
    await manager.retry_due(datetime.fromisoformat(waiting["next_retry_at"]) + timedelta(seconds=1))
    await manager.tasks[job_id]
    online.submit.assert_awaited_once()
    key, row, _ = online.submit.await_args.args
    assert key == f"{job_id}:0"
    assert row["verification"] == verification
    assert row["online_submitted"] == (failure == "uncertain")
    result = manager.load(job_id, "g", "u")["rows"][0]
    assert result["status"] == "done"
    assert result["report_url"] == REPORT


def test_paid_solver_failure_stops_worker_without_retry_or_submission(tmp_path, monkeypatch):
    from pupulinbot import batchmortal_worker
    from pupulinbot.review import parse_review_link

    emitted = []
    monkeypatch.setattr(batchmortal_worker, "emit", lambda **event: emitted.append(event))
    worker = object.__new__(BrowserWorker)
    worker.profile = tmp_path
    worker.journal_path = tmp_path / "submission-journal.json"
    worker.journal = {}
    worker.options = {"retries": 2}
    worker.model = "4.1b"
    worker.sb = SimpleNamespace(save_screenshot=Mock())
    worker.automator = SimpleNamespace(
        analyze_single=Mock(side_effect=capsolver.CapSolverError("CapSolver timed out"))
    )
    worker.run(
        {
            "key": "example:0",
            "online_url": parse_review_link(LINK, 0).online_url,
            "seat": 0,
            "verification": "capsolver",
        }
    )
    assert emitted == [{"status": "capsolver_failed", "message": "CapSolver timed out"}]
    assert worker.automator.analyze_single.call_count == 1
    assert not worker.journal_path.exists()


@pytest.mark.asyncio
async def test_access_list_persists_separately_from_vip2(tmp_path):
    path = tmp_path / "bot.db"
    db = Database(path)
    await db.initialize()
    await db.set_vip2("123456", True)
    assert await db.set_capsolver_member("987654", True)
    assert not await db.set_capsolver_member("987654", True)
    reopened = Database(path)
    await reopened.initialize()
    assert await reopened.capsolver_members() == ["987654"]
    assert await reopened.vip2_members() == ["123456"]
    assert await reopened.set_capsolver_member("987654", False)
    assert not await db.capsolver_members()


@pytest.mark.asyncio
async def test_revoking_access_stops_the_next_paid_seat(tmp_path):
    bridge = SimpleNamespace(client=SimpleNamespace(url=None))
    online = SimpleNamespace(concurrency=1)
    manager = BatchReviews(
        tmp_path, None, bridge, online, capsolver_enabled=True, capsolver_users={"u"}
    )

    async def finish_first_seat(key, row, on_submitted):
        manager.capsolver_users = frozenset()
        return native_report(), REPORT

    online.submit = AsyncMock(side_effect=finish_first_seat)
    job = await manager.create_links([LINK], "g", "u", table=True, verification="capsolver")
    manager.start(job["id"], "g", "u")
    await manager.tasks[job["id"]]
    online.submit.assert_awaited_once()
    current = manager.load(job["id"], "g", "u")
    assert current["rows"][0]["status"] == "done"
    assert "未开通" in current["rows"][1]["error"]
    assert not current.get("next_retry_at")
