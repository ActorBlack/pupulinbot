import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from test_batch_review import game, native_report
from test_review_metadata import reviewed_game

from pupulinbot.batch_review import BatchReviews, report_metrics
from pupulinbot.batchmortal_worker import atomic_json
from pupulinbot.models import Player
from pupulinbot.native_report import compact_report
from pupulinbot.online_pool import OnlinePool
from pupulinbot.online_review import OnlineReviews
from pupulinbot.record_names import RecordNames
from pupulinbot.review import MortalClient, ReviewError, ReviewResult
from pupulinbot.review_cache import ReviewCache

REPORT = "https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json"
NAMES = ["东家", "南家", "西家", "北家"]


def manager(path, online, *, count=4, names=None, reviews=None):
    return BatchReviews(
        path,
        SimpleNamespace(review_records=AsyncMock(return_value=[game(i) for i in range(count)])),
        reviews or SimpleNamespace(client=SimpleNamespace(url=None)),
        online,
        name_resolver=names,
    )


@pytest.mark.asyncio
async def test_slow_metadata_does_not_occupy_review_workers_or_lose_results(tmp_path):
    names_release = asyncio.Event()
    both_names_active = asyncio.Event()
    all_analyzed = asyncio.Event()
    analyzed = 0
    metadata_active = metadata_peak = 0

    async def names(row):
        nonlocal metadata_active, metadata_peak
        metadata_active += 1
        metadata_peak = max(metadata_peak, metadata_active)
        if metadata_active == 2:
            both_names_active.set()
        try:
            await names_release.wait()
            return NAMES
        finally:
            metadata_active -= 1

    async def submit(key, row, callback):
        nonlocal analyzed
        await callback()
        analyzed += 1
        if analyzed == 4:
            all_analyzed.set()
        return native_report(), REPORT

    online = SimpleNamespace(concurrency=2, submit=submit)
    batches = manager(tmp_path, online, names=names)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    for row in job["rows"]:
        row["player_name"] = None
    batches.save(job)
    batches.start(job["id"], "g", "u")
    task = batches.tasks[job["id"]]
    try:
        await asyncio.wait_for(all_analyzed.wait(), 2)
        await asyncio.wait_for(both_names_active.wait(), 2)

        async def persisted():
            while True:
                current = batches.load(job["id"], "g", "u")
                if all(r["status"] == "done" for r in current["rows"]):
                    return current
                await asyncio.sleep(0)

        current = await asyncio.wait_for(persisted(), 2)
        assert not task.done() and not names_release.is_set()
        assert all(r["metrics"]["rating"] == 87 for r in current["rows"])
        assert all((tmp_path / job["id"] / f"{i}.json").exists() for i in range(1, 5))
    finally:
        names_release.set()
        await task
    final = batches.load(job["id"], "g", "u")
    assert metadata_peak == 2 and final["status"] == "done"
    assert all(r["player_name"] == NAMES[0] for r in final["rows"])
    assert all("metadata_seconds" in r["timings"] for r in final["rows"])
    assert "平均 Rating 87.00" in (tmp_path / job["id"] / "report.html").read_text()


@pytest.mark.asyncio
async def test_unsubmitted_rows_from_old_second_browser_run_sequentially(tmp_path):
    pool = OnlinePool(tmp_path / "browser")
    release = asyncio.Event()
    first = asyncio.Event()
    entered = []

    async def submit(key, row, callback):
        entered.append((int(key.rsplit(":", 1)[1]), row["online_slot"]))
        first.set()
        await release.wait()
        await callback()
        return native_report(), REPORT

    pool.worker.submit = submit
    batches = manager(tmp_path / "jobs", pool)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    for row in job["rows"]:
        row["online_slot"] = 1
    for index in (1, 2):
        job["rows"][index].update(status="done", metrics={"rating": 87})
    batches.save(job)
    batches.start(job["id"], "g", "u")
    task = batches.tasks[job["id"]]
    try:
        await asyncio.wait_for(first.wait(), 2)
        assert entered == [(0, 0)]
    finally:
        release.set()
        await task
    assert entered == [(0, 0), (3, 0)]
    assert batches.load(job["id"], "g", "u")["status"] == "done"
    await pool.close()


@pytest.mark.asyncio
async def test_durable_done_result_recovers_its_actual_slot_without_parent_flag_or_new_post(
    tmp_path,
):
    pool = OnlinePool(tmp_path / "browser")
    batches = manager(tmp_path / "jobs", pool, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    job["rows"][0]["online_slot"] = 0
    batches.save(job)
    legacy = pool.profile / "worker-1"
    path = legacy / "artifacts/result.json"
    atomic_json(path, native_report())
    atomic_json(
        legacy / "submission-journal.json",
        {
            "key": job["id"] + ":0",
            "state": "done",
            "url": REPORT,
            "report_file": str(path),
        },
    )
    with patch(
        "pupulinbot.online_review.asyncio.create_subprocess_exec", new=AsyncMock()
    ) as launch:
        await batches._run(job["id"], "g", "u")
        launch.assert_not_awaited()
    result = batches.load(job["id"], "g", "u")
    assert result["status"] == "done" and result["rows"][0]["online_slot"] == 1
    await pool.close()


@pytest.mark.asyncio
async def test_independent_outstanding_results_recover_in_parallel(tmp_path):
    both = asyncio.Event()
    active = peak = 0

    async def submit(key, row, callback):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        active -= 1
        return native_report(), REPORT

    batches = manager(tmp_path, SimpleNamespace(concurrency=2, submit=submit), count=2)
    job = await batches.create(2, "g", "u", Player(42, "player"))
    for row in job["rows"]:
        row["online_submitted"] = True
    batches.save(job)
    await batches._run(job["id"], "g", "u")
    assert peak == 2 and batches.load(job["id"], "g", "u")["status"] == "done"


@pytest.mark.asyncio
async def test_bridge_batch_uses_configured_review_queue_parallelism(tmp_path):
    both = asyncio.Event()
    active = peak = 0

    async def submit(url, seat):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        active -= 1
        return ReviewResult(87, "", REPORT, native_report())

    reviews = SimpleNamespace(
        client=SimpleNamespace(url="https://self-hosted.example"), workers=2, submit=submit
    )
    batches = manager(tmp_path, None, reviews=reviews)
    job = await batches.create(4, "g", "u", Player(42, "player"))
    await batches._run(job["id"], "g", "u")
    assert peak == 2 and batches.load(job["id"], "g", "u")["status"] == "done"


@pytest.mark.asyncio
async def test_generated_rank_skips_redundant_original_result_lookup(tmp_path):
    class Names:
        __call__ = AsyncMock()
        seat_result = AsyncMock(return_value={"rank": 4})

    resolver = Names()
    online = SimpleNamespace(
        submit=AsyncMock(return_value=(compact_report(reviewed_game()), REPORT))
    )
    batches = manager(tmp_path, online, count=1, names=resolver)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    row = job["rows"][0]
    row.update(player_name="原始昵称", player_name_source="original", rank=None)
    await batches._analyze_row(job["id"], "g", "u", 0, row)
    resolver.seat_result.assert_not_awaited()
    resolver.__call__.assert_not_awaited()
    assert batches.load(job["id"], "g", "u")["rows"][0]["rank"] == 2


@pytest.mark.asyncio
async def test_cancelled_name_waiter_does_not_cancel_other_seats_shared_request():
    started, release = asyncio.Event(), asyncio.Event()

    async def load(log):
        started.set()
        await release.wait()
        return NAMES

    resolver = RecordNames()
    resolver._load = AsyncMock(side_effect=load)
    row = {
        "url": "https://tenhou.net/0/?log=2026111008gm-0029-0000-e1dc7359",
        "uuid": "2026111008gm-0029-0000-e1dc7359",
        "seat": 0,
    }
    first = asyncio.create_task(resolver(row))
    second = asyncio.create_task(resolver({**row, "seat": 1}))
    await started.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    release.set()
    assert await second == NAMES
    resolver._load.assert_awaited_once()


@pytest.mark.asyncio
async def test_bridge_reuses_http_client_and_closes_it():
    original = httpx.AsyncClient
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "done", "rating": 87})

    factory = Mock(side_effect=lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    bridge = MortalClient("https://self-hosted.example", "test-key")
    with patch("pupulinbot.review.httpx.AsyncClient", new=factory):
        await asyncio.gather(*(bridge.review("record", 0) for _ in range(4)))
        connection = bridge._http
        assert factory.call_count == 1 and len(requests) == 4
        assert all(r.headers["authorization"] == "Bearer test-key" for r in requests)
        await bridge.close()
        assert connection.is_closed and bridge._http is None


@pytest.mark.asyncio
async def test_wrong_model_local_report_cannot_be_reused(tmp_path):
    data = native_report()
    data["review"]["model_tag"] = "other"
    path = tmp_path / "artifacts/result.json"
    atomic_json(path, data)
    engine = OnlineReviews(tmp_path)
    with pytest.raises(ReviewError, match="模型"):
        await engine._read_native(str(path), {"seat": 0})
    await engine.close()


@pytest.mark.asyncio
async def test_migrated_cache_survives_task_removal_and_restart(tmp_path):
    batches = manager(tmp_path, None, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    row = job["rows"][0]
    row.update(
        status="done",
        online_submitted=True,
        metrics=report_metrics(native_report()),
        report_url=REPORT,
    )
    batches.save(job)
    path = tmp_path / job["id"] / "1.json"
    atomic_json(path, native_report())
    cache_dir = tmp_path / "cache"
    cache = ReviewCache(cache_dir, batch_directory=tmp_path)
    assert cache.migrate_legacy() == {"copied": 1, "existing": 0, "invalid": 0}
    assert cache.migrate_legacy() == {"copied": 0, "existing": 1, "invalid": 0}
    path.unlink()
    (path.parent / "job.json").unlink()
    result = ReviewCache(cache_dir).find(row | {"online_submitted": False}, "4.1b")
    assert result[0] == native_report()


@pytest.mark.asyncio
async def test_link_task_can_be_enqueued_without_waiting_for_metadata(tmp_path):
    names = AsyncMock(return_value=NAMES)
    batches = manager(tmp_path, None, names=names)
    job = await batches.create_links(
        ["https://game.maj-soul.com/1/?paipu=abcdefgh"], "g", "u", resolve_names=False
    )
    assert len(job["rows"]) == 1
    names.assert_not_awaited()


def test_known_original_rank_does_not_download_the_report_again(tmp_path):
    from test_batchmortal_backend import fake_worker

    worker = fake_worker(tmp_path)
    worker.seat, worker.key, worker.need_game_result = 0, "speed:0", False
    worker.report_reader = SimpleNamespace(submit=Mock(side_effect=AssertionError("extra GET")))
    worker.report_future = None
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker._done(REPORT)
    worker.report_reader.submit.assert_not_called()
    assert emit.call_args.kwargs["status"] == "done"


def test_worker_rejects_a_different_model_before_journaling_done(tmp_path):
    from test_batchmortal_backend import fake_worker

    worker = fake_worker(tmp_path)
    worker.seat, worker.key, worker.model = 0, "speed:0", "other"
    with pytest.raises(RuntimeError, match="模型"):
        worker._done(REPORT)
    assert not worker.journal_path.exists()


def test_memory_cache_never_promotes_manual_or_wrong_seat_associations(tmp_path):
    cache = ReviewCache(tmp_path)
    row = {"uuid": "example", "seat": 0}
    cache.remember(
        {**row, "source_verification": "用户指定场次；报告座位已核对"},
        "4.1b",
        (native_report(), REPORT),
    )
    cache.remember({**row, "seat": 1}, "4.1b", (native_report(), REPORT))
    cache.remember(row, "other", (native_report(), REPORT))
    assert cache.memory == {}


@pytest.mark.asyncio
async def test_malformed_durable_state_cannot_start_a_fresh_submission(tmp_path):
    pool = OnlinePool(tmp_path / "browser")
    batches = manager(tmp_path / "jobs", pool, count=1)
    job = await batches.create(1, "g", "u", Player(42, "player"))
    atomic_json(pool.profile / "worker-1/submission-journal.json", [])
    with patch(
        "pupulinbot.online_review.asyncio.create_subprocess_exec", new=AsyncMock()
    ) as launch:
        await batches._run(job["id"], "g", "u")
        launch.assert_not_awaited()
    result = batches.load(job["id"], "g", "u")
    assert result["status"] == "retry_wait" and pool.gate.blocked
    assert result["rows"][0]["status"] == "pending"
    await pool.close()


def test_accepted_progress_page_does_not_wait_for_a_final_viewer_url():
    from pupulinbot.batchmortal_worker import wait_for_submission_departure
    from pupulinbot.online_worker import progress_report_url

    url = "https://mjai.ekyu.moe/progress?task=abcdef0123456789"
    state = Mock(return_value={"url": url, "page_text": "queued"})
    with patch("pupulinbot.batchmortal_worker.time.sleep") as sleep:
        wait_for_submission_departure(state, "speed:0", 1)
    state.assert_called_once()
    sleep.assert_not_called()
    assert progress_report_url(url) == REPORT
    assert progress_report_url(url.replace("mjai.ekyu.moe", "evil.invalid")) is None
    assert progress_report_url(url + "&task=0123456789abcdef") is None
