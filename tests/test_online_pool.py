import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from test_batch_review import game, native_report

from pupulinbot.batch_review import BatchReviews, report_metrics
from pupulinbot.batchmortal_worker import atomic_json, browser_options
from pupulinbot.models import Player
from pupulinbot.online_pool import OnlinePool, SubmissionGate
from pupulinbot.online_review import OnlinePaused, OnlineReviews
from pupulinbot.review_cache import ReviewCache

REPORT = "https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json"


def manager(tmp_path, online, count=4):
    return BatchReviews(
        tmp_path,
        SimpleNamespace(review_records=AsyncMock(return_value=[game(i) for i in range(count)])),
        SimpleNamespace(client=SimpleNamespace(url=None)),
        online,
    )


@pytest.mark.asyncio
async def test_two_consumers_are_bounded_and_finish_out_of_order_without_head_blocking(tmp_path):
    release = [asyncio.Event() for _ in range(4)]
    started = [asyncio.Event() for _ in range(4)]
    active = peak = 0

    async def submit(key, row, callback):
        nonlocal active, peak
        i = int(key.rsplit(":", 1)[1])
        active += 1
        peak = max(peak, active)
        started[i].set()
        await callback()
        await release[i].wait()
        data = native_report()
        data["review"]["rating"] = 0.8 + i / 100
        active -= 1
        return data, REPORT

    online = SimpleNamespace(concurrency=2, submit=submit, begin=lambda: None, halt=lambda: None)
    batches = manager(tmp_path, online)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    batches.start(job["id"], "g", "u")
    await asyncio.wait_for(asyncio.gather(started[0].wait(), started[1].wait()), 1)
    assert not started[2].is_set() and peak == 2
    release[1].set()
    await asyncio.wait_for(started[2].wait(), 1)
    assert not release[0].is_set()
    for event in release:
        event.set()
    await batches.tasks[job["id"]]
    result = batches.load(job["id"], "g", "u")
    assert result["status"] == "done" and peak == 2
    assert result["rows"][2]["online_slot"] == 1
    assert [round(r["metrics"]["rating"]) for r in result["rows"]] == [80, 81, 82, 83]
    assert all((tmp_path / job["id"] / f"{i}.json").exists() for i in range(1, 5))


@pytest.mark.asyncio
async def test_failure_stops_new_dispatch_but_collects_other_submitted_result(tmp_path):
    second = asyncio.Event()
    failed = asyncio.Event()
    called = []
    halted = []

    async def submit(key, row, callback):
        i = int(key.rsplit(":", 1)[1])
        called.append(i)
        if i == 0:
            await second.wait()
            failed.set()
            raise OnlinePaused("rate_limited", "limited")
        await callback()
        second.set()
        await failed.wait()
        await asyncio.sleep(0)
        return native_report(), REPORT

    online = SimpleNamespace(
        concurrency=2, submit=submit, begin=lambda: None, halt=lambda: halted.append(True)
    )
    batches = manager(tmp_path, online)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    await batches._run(job["id"], "g", "u")
    result = batches.load(job["id"], "g", "u")
    assert result["status"] == "rate_limited" and sorted(called) == [0, 1]
    assert result["rows"][1]["status"] == "done" and halted
    assert result["rows"][2]["status"] == "pending"


@pytest.mark.asyncio
async def test_pending_unknown_submission_is_recovered_before_fresh_work(tmp_path):
    called = []

    async def submit(key, row, callback):
        called.append(key)
        raise OnlinePaused("uncertain", "unknown")

    online = SimpleNamespace(concurrency=2, submit=submit, begin=lambda: None, halt=lambda: None)
    batches = manager(tmp_path, online)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    job["rows"][2]["online_submitted"] = True
    batches.save(job)
    await batches._run(job["id"], "g", "u")
    assert called == [job["id"] + ":2"]


@pytest.mark.asyncio
async def test_failed_recovery_does_not_block_other_submitted_result(tmp_path):
    called = []

    async def submit(key, row, callback):
        called.append(key)
        if key.endswith(":0"):
            raise OnlinePaused("uncertain", "unknown")
        return native_report(), REPORT

    online = SimpleNamespace(concurrency=2, submit=submit, begin=lambda: None, halt=lambda: None)
    batches = manager(tmp_path, online)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    for index in (0, 2):
        job["rows"][index]["online_submitted"] = True
    batches.save(job)
    await batches._run(job["id"], "g", "u")
    current = batches.load(job["id"], "g", "u")
    assert called == [job["id"] + ":0", job["id"] + ":2"]
    assert current["rows"][2]["status"] == "done"
    assert current["rows"][1]["status"] == "pending"
    assert current["rows"][3]["status"] == "pending"


@pytest.mark.asyncio
async def test_global_submission_gate_keeps_interval_and_halt_aborts_waiter():
    gate = SubmissionGate(0.025)
    assert await gate.acquire()
    start = asyncio.get_running_loop().time()
    assert await gate.acquire()
    assert asyncio.get_running_loop().time() - start >= 0.02
    pending = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    gate.blocked = True
    assert not await pending


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [False, True])
async def test_worker_cannot_submit_without_global_permit(tmp_path, monkeypatch, blocked):
    gate = SubmissionGate(0)
    gate.blocked = blocked
    events = asyncio.Queue()
    actions = []
    report = tmp_path / "artifacts/report.json"
    atomic_json(report, native_report())

    class Input:
        def write(self, line):
            value = json.loads(line)
            if "online_url" in value:
                events.put_nowait(b'PUPULIN_ONLINE {"status":"ready_to_submit"}\n')
            else:
                actions.append(value["action"])
                if value["action"] == "abort":
                    events.put_nowait(b'PUPULIN_ONLINE {"status":"paused"}\n')
                else:
                    events.put_nowait(b'PUPULIN_ONLINE {"status":"submitted"}\n')
                    done = {"status": "done", "url": REPORT, "report_file": str(report)}
                    events.put_nowait(("PUPULIN_ONLINE " + json.dumps(done) + "\n").encode())

        async def drain(self):
            pass

    process = SimpleNamespace(
        returncode=0, stdin=Input(), stdout=SimpleNamespace(readline=events.get)
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    engine = OnlineReviews(tmp_path, cooldown=0, gate=gate, options={"controlled_gate": True})
    callback = AsyncMock()
    if blocked:
        with pytest.raises(OnlinePaused):
            await engine.submit("job:0", {"online_url": "unused", "seat": 0}, callback)
        callback.assert_not_called()
        assert actions == ["abort"]
    else:
        data, _ = await engine.submit("job:0", {"online_url": "unused", "seat": 0}, callback)
        assert data["player_id"] == 0 and actions == ["permit"]
        assert callback.await_count == 2
    await engine.close()


@pytest.mark.asyncio
async def test_single_browser_honors_legacy_restart_barrier(tmp_path):
    pool = OnlinePool(tmp_path)
    assert pool.concurrency == 1 and pool.worker.profile.name == "worker-0"
    atomic_json(
        tmp_path / "worker-1/submission-journal.json", {"key": "old:1", "state": "submitted"}
    )
    pool.worker.submit = AsyncMock()
    with pytest.raises(OnlinePaused, match="未核对"):
        await pool.submit("new:0", {"uuid": "x", "seat": 0}, AsyncMock())
    with pytest.raises(OnlinePaused, match="找回报告"):
        await pool.submit("old:1", {"uuid": "x", "seat": 0}, AsyncMock())
    pool.worker.submit.assert_not_called()
    assert pool.gate.blocked
    await pool.close()


@pytest.mark.asyncio
async def test_both_old_journals_recover_without_starting_another_browser(tmp_path, monkeypatch):
    pool = OnlinePool(tmp_path)
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    read = MagicMock(return_value=native_report())
    monkeypatch.setattr("pupulinbot.native_report.read_native_report", read)
    for index in (0, 1):
        atomic_json(
            tmp_path / f"worker-{index}/submission-journal.json",
            {"key": f"job:{index}", "state": "submitted", "remote_report_url": REPORT},
        )
    pool.halt()
    await pool.submit("job:0", {"uuid": "x", "seat": 0}, AsyncMock())
    with pytest.raises(OnlinePaused, match="未核对"):
        await pool.submit("job:2", {"uuid": "x", "seat": 2}, AsyncMock())
    await pool.submit("job:1", {"uuid": "x", "seat": 0}, AsyncMock())
    assert pool.gate.blocked and read.call_count == 2
    launch.assert_not_called()
    for index in (0, 1):
        state = json.loads((tmp_path / f"worker-{index}/submission-journal.json").read_text())
        assert state["state"] == "done"
    await pool.close()


def test_parallel_browser_uses_explicit_unique_debug_port(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 43210)
    monkeypatch.setattr("pupulinbot.batchmortal_worker.socket.socket", lambda: connection)
    first = browser_options(Path("/tmp/a"), {"unique_debug_port": True})
    assert first["chromium_arg"].startswith("--remote-debugging-port=")
    assert int(first["chromium_arg"].split("=")[1]) > 1024


@pytest.mark.asyncio
async def test_cache_requires_exact_game_seat_model_and_automatic_origin(tmp_path):
    batches = manager(tmp_path, None, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    data = native_report()
    row = job["rows"][0]
    row.update(
        status="done", online_submitted=True, metrics=report_metrics(data), report_url=REPORT
    )
    batches.save(job)
    atomic_json(tmp_path / job["id"] / "1.json", data)
    cache = ReviewCache(tmp_path / "cache", batch_directory=tmp_path)
    query = {"uuid": row["uuid"], "seat": 0}
    assert cache.find(query, "4.1b")[0]["player_id"] == 0
    assert cache.find({**query, "seat": 1}, "4.1b") is None
    assert cache.find({**query, "source": "tenhou"}, "4.1b") is None
    assert cache.find(query, "other") is None
    assert cache.find({**query, "online_submitted": True}, "4.1b") is None
    row["source_verification"] = "用户指定场次；报告座位已核对"
    batches.save(job)
    assert (
        ReviewCache(tmp_path / "separate-cache", batch_directory=tmp_path).find(query, "4.1b")
        is None
    )


@pytest.mark.asyncio
async def test_cache_hit_does_not_launch_browser_or_call_submission_callback(tmp_path):
    archive = tmp_path / "batches"
    batches = manager(archive, None, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    data = native_report()
    row = job["rows"][0]
    row.update(
        status="done", online_submitted=True, metrics=report_metrics(data), report_url=REPORT
    )
    batches.save(job)
    atomic_json(archive / job["id"] / "1.json", data)
    pool = OnlinePool(
        tmp_path / "browser",
        cache=ReviewCache(tmp_path / "cache", batch_directory=archive),
    )
    pool.worker.submit = AsyncMock()
    callback = AsyncMock()
    request = {"uuid": row["uuid"], "seat": 0}
    result = await pool.submit("new:0", request, callback)
    assert result[0]["review"]["rating"] == 0.87 and request["review_cache_hit"]
    pool.worker.submit.assert_not_called()
    callback.assert_not_called()
    await pool.close()


@pytest.mark.asyncio
async def test_new_online_report_survives_restart_without_task_files(tmp_path):
    cache_dir = tmp_path / "cache"
    pool = OnlinePool(tmp_path / "browser", cache=ReviewCache(cache_dir))
    report = native_report()
    pool.worker.submit = AsyncMock(return_value=(report, REPORT))
    row = {"uuid": "original-game", "seat": 0}
    assert await pool.submit("first:0", row, AsyncMock()) == (report, REPORT)
    await pool.close()

    restarted = OnlinePool(tmp_path / "browser", cache=ReviewCache(cache_dir))
    restarted.worker.submit = AsyncMock()
    request = row.copy()
    assert await restarted.submit("second:0", request, AsyncMock()) == (report, REPORT)
    assert request["review_cache_hit"]
    restarted.worker.submit.assert_not_called()
    await restarted.close()


def test_deferred_submission_changes_only_the_regular_submit_script():
    from pupulinbot.batchmortal_worker import deferred_form_submit

    script = "form.requestSubmit(submit); submit.click(); return 'submitted';"
    result = deferred_form_submit(script)
    assert "setTimeout(() => form.requestSubmit(submit), 0)" in result
    assert "setTimeout(() => submit.click(), 0)" in result
    challenge = "return token.value;"
    assert deferred_form_submit(challenge) == challenge


@pytest.mark.asyncio
async def test_done_journal_resumes_without_browser_or_network(tmp_path, monkeypatch):
    data = native_report()
    path = tmp_path / "artifacts/completed.json"
    atomic_json(path, data)
    atomic_json(
        tmp_path / "submission-journal.json",
        {"key": "job:0", "state": "done", "url": REPORT, "report_file": str(path)},
    )
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    engine = OnlineReviews(tmp_path)
    result = await engine.submit("job:0", {"seat": 0}, AsyncMock())
    assert result[0] == data and engine.process is None
    launch.assert_not_called()
    await engine.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", ["", "worker-1"])
async def test_manual_import_releases_legacy_browser_journal(tmp_path, legacy):
    pool = OnlinePool(tmp_path)
    journal = tmp_path / legacy / "submission-journal.json"
    atomic_json(journal, {"key": "old:0", "state": "submitted"})
    await pool.acknowledge_import("old:0", REPORT, native_report())
    state = json.loads(journal.read_text())
    assert state["state"] == "done"
    await pool.close()


def test_worker_remembers_only_trusted_remote_task_urls(tmp_path):
    from pupulinbot.batchmortal_worker import BrowserWorker

    worker = BrowserWorker.__new__(BrowserWorker)
    worker.journal = {"key": "job:0", "state": "submitted"}
    worker.journal_path = tmp_path / "submission-journal.json"
    worker._remember_remote_url("https://evil.invalid/progress?task=abcdef0123456789")
    assert not worker.journal_path.exists()
    worker._remember_remote_url("https://mjai.ekyu.moe/progress?task=abcdef0123456789")
    assert worker.journal["remote_progress_url"].endswith("abcdef0123456789")
    worker._remember_remote_url(REPORT)
    assert worker.journal["remote_report_url"] == REPORT


def test_native_report_compaction_preserves_all_metrics():
    from pupulinbot.native_report import compact_report

    data = native_report()
    data["review"]["kyokus"][0]["entries"][0]["details"].append(
        {"prob": 0.97, "large_unused": [1] * 100}
    )
    compact = compact_report(data)
    assert report_metrics(compact) == report_metrics(data)
    assert compact["review"]["kyokus"][0]["entries"][0]["details"][1] is None


def test_native_report_reader_uses_only_existing_report_and_checks_seat_and_model():
    import httpx
    import respx

    from pupulinbot.native_report import read_native_report

    with respx.mock(assert_all_called=False) as mock:
        endpoint = mock.get("https://mjai.ekyu.moe/report/abcdef0123456789.json").mock(
            return_value=httpx.Response(200, json=native_report())
        )
        assert read_native_report(REPORT, 0, "4.1b")["player_id"] == 0
        from pupulinbot.review import ReviewError

        with pytest.raises(ReviewError, match="座位"):
            read_native_report(REPORT, 1, "4.1b")
        with pytest.raises(ReviewError, match="模型"):
            read_native_report(REPORT, 0, "other")
        assert endpoint.call_count == 3
        assert all(call.request.method == "GET" for call in endpoint.calls)


@pytest.mark.asyncio
async def test_known_generated_report_recovers_via_get_without_new_browser_or_post(
    tmp_path, monkeypatch
):
    atomic_json(
        tmp_path / "submission-journal.json",
        {"key": "job:0", "state": "submitted", "remote_report_url": REPORT},
    )
    reader = __import__("unittest.mock", fromlist=["Mock"]).Mock(return_value=native_report())
    monkeypatch.setattr("pupulinbot.native_report.read_native_report", reader)
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    engine = OnlineReviews(tmp_path)
    result = await engine.submit("job:0", {"seat": 0, "online_submitted": True}, AsyncMock())
    assert result[0]["player_id"] == 0
    assert json.loads((tmp_path / "submission-journal.json").read_text())["state"] == "done"
    reader.assert_called_once_with(REPORT, 0, "4.1b")
    launch.assert_not_called()
    await engine.close()


@pytest.mark.asyncio
async def test_progress_only_journal_recovers_without_browser_or_resubmission(
    tmp_path, monkeypatch
):
    atomic_json(
        tmp_path / "submission-journal.json",
        {
            "key": "job:0",
            "state": "submitted",
            "remote_progress_url": "https://mjai.ekyu.moe/progress?task=abcdef0123456789",
        },
    )
    reader = MagicMock(return_value=native_report())
    monkeypatch.setattr("pupulinbot.native_report.read_native_report", reader)
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    engine = OnlineReviews(tmp_path)
    data, url = await engine.submit("job:0", {"seat": 0, "online_submitted": True}, AsyncMock())
    assert data["player_id"] == 0 and url == REPORT
    reader.assert_called_once_with(REPORT, 0, "4.1b")
    launch.assert_not_called()
    assert json.loads((tmp_path / "submission-journal.json").read_text())["state"] == "done"
    await engine.close()


@pytest.mark.asyncio
async def test_transient_submission_failure_persists_retry_and_recovers_after_restart(tmp_path):
    from datetime import datetime

    submit = AsyncMock(side_effect=OnlinePaused("uncertain", "temporary report timeout"))
    online = SimpleNamespace(concurrency=2, submit=submit, begin=lambda: None, halt=lambda: None)
    batches = manager(tmp_path, online, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    await batches._run(job["id"], "g", "u")
    queued = batches.load(job["id"], "g", "u")
    assert queued["status"] == "retry_wait" and queued["retry_attempts"] == 1
    due = datetime.fromisoformat(queued["next_retry_at"])
    submit.side_effect = None
    submit.return_value = (native_report(), REPORT)
    restarted = manager(tmp_path, online, count=1)
    denied = AsyncMock(return_value=False)
    await restarted.retry_due(due, allowed=denied)
    denied.assert_awaited_once_with("g", "u")
    assert not restarted.tasks and submit.await_count == 1
    await restarted.retry_due(due)
    await restarted.tasks[job["id"]]
    final = restarted.load(job["id"], "g", "u")
    assert final["status"] == "done" and "next_retry_at" not in final
    assert submit.await_count == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_manual_pause_and_limits_do_not_loop(tmp_path):
    online = SimpleNamespace(concurrency=2)
    batches = manager(tmp_path, online, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    for attempt in range(5):
        job["status"] = "uncertain"
        batches.schedule_retry(job)
        assert job["status"] == "retry_wait" and job["retry_attempts"] == attempt + 1
    job["status"] = "uncertain"
    batches.schedule_retry(job)
    assert job["status"] == "uncertain" and "next_retry_at" not in job
    job["status"] = "retry_wait"
    job["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    batches.save(job)
    await batches.pause(job["id"], "g", "u")
    await batches.retry_due()
    assert not batches.tasks
    for status in (
        "rate_limited",
        "waiting_human",
        "captcha_rejected",
        "verification_failed",
        "paused",
    ):
        job["status"] = status
        batches.schedule_retry(job)
        assert job["status"] == status


@pytest.mark.asyncio
async def test_verification_timeout_reports_progress_and_reopens_chrome(tmp_path, monkeypatch):
    pool = OnlinePool(tmp_path)
    worker = pool.worker
    reading_failure, release_failure = asyncio.Event(), asyncio.Event()

    async def failure():
        reading_failure.set()
        await release_failure.wait()
        return b'PUPULIN_ONLINE {"status":"verification_timeout"}\n'

    responses = iter([b'PUPULIN_ONLINE {"status":"verification_wait"}\n', None])

    async def readline():
        return next(responses) or await failure()

    stdin = SimpleNamespace(write=Mock(), drain=AsyncMock())
    worker.process = SimpleNamespace(
        returncode=None,
        stdin=stdin,
        stdout=SimpleNamespace(readline=readline),
        wait=AsyncMock(return_value=0),
    )
    on_submitted = AsyncMock()
    pending = asyncio.create_task(
        worker.submit("job:3", {"seat": 3, "online_url": "test"}, on_submitted)
    )
    try:
        await asyncio.wait_for(reading_failure.wait(), 1)
        assert pool.job_progress("job") == ["第 4 项：等待 Mortal 网页验证，尚未提交"]
        assert pool.job_progress("other") == []
    finally:
        release_failure.set()
        with pytest.raises(OnlinePaused) as stopped:
            await pending
    assert stopped.value.status == "verification_timeout" and pool.job_progress("job") == []
    assert worker.process is None
    first_request, close = [call.args[0] for call in stdin.write.call_args_list]
    assert json.loads(close) == {"action": "close"}
    on_submitted.assert_not_awaited()

    fresh = SimpleNamespace(
        returncode=0,
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
        stdout=SimpleNamespace(
            readline=AsyncMock(return_value=b'PUPULIN_ONLINE {"status":"paused"}\n')
        ),
    )
    launch = AsyncMock(return_value=fresh)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(OnlinePaused) as stopped:
        await worker.submit("job:3", {"seat": 3, "online_url": "test"}, on_submitted)
    assert stopped.value.status == "paused"
    launch.assert_awaited_once()
    assert fresh.stdin.write.call_args.args[0] == first_request
    on_submitted.assert_not_awaited()


@pytest.mark.asyncio
async def test_verification_timeout_retry_skips_completed_rows_and_stops(tmp_path):
    from datetime import datetime

    submit = AsyncMock(side_effect=OnlinePaused("verification_timeout", "未取得验证令牌"))
    online = SimpleNamespace(concurrency=2, submit=submit)
    batches = manager(tmp_path, online)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    for index in (1, 2):
        job["rows"][index].update(status="done", metrics={"rating": 87})

    # Allow both browsers to start before either times out.
    async def time_out(*args):
        await asyncio.sleep(0)
        raise OnlinePaused("verification_timeout", "未取得验证令牌")

    submit.side_effect = time_out
    batches.save(job)
    job_id = job["id"]
    await batches._run(job_id, "g", "u")
    for attempt, delay in enumerate((60, 120), 1):
        queued = batches.load(job_id, "g", "u")
        assert queued["status"] == "retry_wait" and queued["retry_attempts"] == attempt
        assert f"{delay} 秒后" in queued["message"]
        await batches.retry_due(datetime.fromisoformat(queued["next_retry_at"]))
        await batches.tasks[job_id]
    final = batches.load(job_id, "g", "u")
    assert final["status"] == "verification_timeout" and "next_retry_at" not in final
    assert [final["rows"][i]["metrics"] for i in (1, 2)] == [{"rating": 87}] * 2
    assert all(not final["rows"][i].get("online_submitted") for i in (0, 3))
    assert [c.args[0] for c in submit.await_args_list] == [f"{job_id}:0", f"{job_id}:3"] * 3
