import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_batch_review import game, native_report

from pupulinbot.batch_review import BatchReviews
from pupulinbot.models import Player
from pupulinbot.online_review import OnlinePaused, OnlineReviews
from pupulinbot.online_worker import trusted_result


def test_only_real_author_result_is_accepted():
    assert trusted_result("https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json")
    assert not trusted_result("https://mjai.ekyu.moe/review")
    assert not trusted_result("https://evil.example/report/abcdef0123456789.json")


@pytest.mark.asyncio
async def test_online_human_pause_resume_skips_done_and_saves_submitted(tmp_path):
    report = native_report()

    async def submit(key, row, on_submitted):
        await on_submitted()
        return report, "https://mjai.ekyu.moe/report/abcdef0123456789.json"

    online = SimpleNamespace(
        submit=AsyncMock(side_effect=OnlinePaused("waiting_human", "请验证")), close=AsyncMock()
    )
    records = SimpleNamespace(review_records=AsyncMock(return_value=[game(), game(1)]))
    reviews = SimpleNamespace(client=SimpleNamespace(url=None))
    notify = AsyncMock()
    batches = BatchReviews(tmp_path, records, reviews, online, notify)
    job = await batches.create(2, "g", "u", Player(42, "name"))
    assert job["status"] == "ready"
    job["rows"][0].update(status="done", metrics={"rating": 90})
    batches.save(job)
    await batches._run(job["id"], "g", "u")
    paused = batches.load(job["id"], "g", "u")
    assert paused["status"] == "waiting_human"
    assert paused["rows"][0]["metrics"]["rating"] == 90
    online.submit.side_effect = submit
    await batches._run(job["id"], "g", "u")
    finished = batches.load(job["id"], "g", "u")
    assert finished["status"] == "done"
    assert finished["rows"][1]["online_submitted"]
    assert finished["rows"][1]["metrics"]["rating"] == 87
    assert (tmp_path / job["id"] / "2.json").exists()
    assert online.submit.await_count == 2
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_pause_during_browser_does_not_repeat_submission(tmp_path):
    engine = OnlineReviews(tmp_path)
    started, finish = asyncio.Event(), asyncio.Event()

    async def submit(*args):
        started.set()
        await finish.wait()
        return {"report": True}, "url"

    engine._submit = AsyncMock(side_effect=submit)
    first = asyncio.create_task(engine.submit("job:0", {}, AsyncMock()))
    await started.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert engine.pending and not engine.pending.done()
    finish.set()
    assert await engine.submit("job:0", {}, AsyncMock()) == ({"report": True}, "url")
    assert engine._submit.await_count == 1
    await engine.close()


@pytest.mark.asyncio
async def test_unresolved_external_submission_blocks_another_job(tmp_path):
    from pupulinbot.review import ReviewError

    engine = OnlineReviews(tmp_path)
    engine.active_submitted_key = "older:0"
    with pytest.raises(ReviewError, match="已提交"):
        await engine.submit("new:0", {}, AsyncMock())
    engine._fetch = AsyncMock(return_value={"player_id": 0})
    result = await engine.submit(
        "older:0",
        {"online_report_url": "https://mjai.ekyu.moe/report/abcdef0123456789.json"},
        AsyncMock(),
    )
    assert result[0]["player_id"] == 0
    assert engine.active_submitted_key is None
