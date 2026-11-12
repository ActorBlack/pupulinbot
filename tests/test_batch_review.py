import asyncio
import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from pupulinbot.batch_review import (
    BatchReviews,
    render_report,
    report_metrics,
    report_url,
    summaries,
    upload_report,
)
from pupulinbot.models import GameRecord, Player
from pupulinbot.paipuya import PaipuyaClient
from pupulinbot.review import ReviewError, ReviewResult


def native_report():
    return {
        "engine": "Mortal",
        "player_id": 0,
        "review": {
            "model_tag": "4.1b",
            "temperature": 0.1,
            "rating": 0.87,
            "total_reviewed": 10,
            "total_matches": 8,
            "kyokus": [
                {
                    "entries": [
                        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.03}]},
                        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.08}]},
                        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.8}]},
                        {"is_equal": False, "details": [{"prob": 0.01}]},
                    ]
                }
            ],
        },
    }


def game(index=0):
    return GameRecord(
        f"game-{index}",
        datetime(2026, 1, index + 1, tzinfo=UTC),
        datetime(2026, 1, index + 1, 1, tzinfo=UTC),
        "12",
        [{"account_id": 42, "rank": 1, "nickname": "name"}],
        {},
    )


def manager(tmp_path, configured=False):
    records = SimpleNamespace(review_records=AsyncMock(return_value=[game(), game(1)]))
    reviews = SimpleNamespace(
        client=SimpleNamespace(url="https://bridge" if configured else None),
        submit=AsyncMock(return_value=ReviewResult(87, "--", None, {})),
    )
    return BatchReviews(tmp_path, records, reviews)


def test_metrics_scale_and_weighted_aggregation_exclude_missing_and_failed():
    metric = report_metrics(native_report())
    assert metric["rating"] == 87
    assert metric["agreement"] == 80
    assert metric["badmove_decisions"] == 10
    assert metric["bad5"] == 1
    assert metric["bad10"] == 2
    other = {**metric, "rating": None, "decisions": 90, "matches": 90}
    results = summaries(
        [
            {"status": "done", "metrics": metric},
            {"status": "done", "metrics": other},
            {"status": "failed", "metrics": {"rating": 0}},
        ]
    )
    assert results[0]["mean_rating"] == 87
    assert results[0]["agreement"] == 98
    assert results[0]["rated_games"] == 1
    assert (
        len(
            summaries(
                [
                    {"status": "done", "metrics": metric},
                    {"status": "done", "metrics": {**metric, "model": "3.0"}},
                ]
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/report/abc.json",
        "https://mjai.ekyu.moe.evil/report/abcdef0123456789.json",
        "https://user@mjai.ekyu.moe/report/abcdef0123456789.json",
    ],
)
def test_only_trusted_report_urls(url):
    with pytest.raises(ReviewError):
        report_url(url)


@pytest.mark.asyncio
async def test_manifest_ownership_bounds_restart_resume_and_html_escaping(tmp_path):
    batches = manager(tmp_path)
    for count in [0, 101]:
        with pytest.raises(ReviewError):
            await batches.create(count, "group", "owner", Player(42, "name"))
    job = await batches.create(2, "group", "owner", Player(42, "<script>"))
    assert job["status"] == "waiting_service"
    assert "&lt;script&gt;" in render_report(job)
    with pytest.raises(ReviewError):
        batches.load(job["id"], "other-group", "owner")
    with pytest.raises(ReviewError):
        batches.load(job["id"], "group", "another-owner")
    job["status"] = "running"
    batches.save(job)
    assert batches.load(job["id"], "group", "owner")["status"] == "paused"
    assert BatchReviews(tmp_path, None, None).load(job["id"], "group", "owner")["rows"]


@pytest.mark.asyncio
async def test_import_requires_matching_seat_and_updates_real_metrics(tmp_path):
    batches = manager(tmp_path)
    job = await batches.create(2, "group", "owner", Player(42, "name"))
    report = native_report()
    with patch("pupulinbot.batch_review.fetch_report", AsyncMock(return_value=report)):
        updated = await batches.import_report(
            job["id"], "group", "owner", 1, "https://mjai.ekyu.moe/report/abcdef0123456789.json"
        )
        assert updated["status"] == "partial"
        assert updated["rows"][0]["metrics"]["rating"] == 87
        report["player_id"] = 3
        with pytest.raises(ReviewError, match="座位"):
            await batches.import_report(job["id"], "group", "owner", 2, "url")


@pytest.mark.asyncio
async def test_batch_failure_stops_and_resume_skips_success(tmp_path):
    batches = manager(tmp_path, True)
    job = await batches.create(2, "group", "owner", Player(42, "name"))
    job["rows"][0].update(status="done", metrics={"rating": 88, "model": "4.1b"})
    batches.save(job)
    batches.reviews.submit.side_effect = ReviewError("unavailable")
    await batches._run(job["id"], "group", "owner")
    assert batches.reviews.submit.await_count == 1
    assert batches.load(job["id"], "group", "owner")["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_keeps_completed_results_and_checks_owner(tmp_path):
    batches = manager(tmp_path)
    job = await batches.create(2, "group", "owner", Player(42, "name"))
    job["rows"][0].update(status="done", metrics={"rating": 87})
    batches.save(job)
    with pytest.raises(ReviewError):
        await batches.pause(job["id"], "group", "another-owner")
    await batches.pause(job["id"], "group", "owner")
    stored = batches.load(job["id"], "group", "owner")
    assert stored["status"] == "paused"
    assert stored["rows"][0]["metrics"]["rating"] == 87


@pytest.mark.asyncio
async def test_pause_prevents_an_already_scanning_retry_from_restarting_task(tmp_path):
    batches = manager(tmp_path, True)
    job = await batches.create(1, "group", "owner", Player(42, "name"))
    job.update(status="retry_wait", next_retry_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat())
    batches.save(job)
    checking, release = asyncio.Event(), asyncio.Event()

    async def allowed(group_id, owner):
        checking.set()
        await release.wait()
        return True

    retry = asyncio.create_task(
        batches.retry_due(datetime(2026, 1, 2, tzinfo=UTC), allowed=allowed)
    )
    try:
        await asyncio.wait_for(checking.wait(), 2)
        await batches.pause(job["id"], "group", "owner")
    finally:
        release.set()
        await retry
    assert not batches.tasks
    assert batches.load(job["id"], "group", "owner")["status"] == "paused"
    batches.reviews.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_hanchan_pagination_deduplicates_inclusive_boundary():
    real_client = httpx.AsyncClient
    pages = [
        [
            {
                "uuid": f"game-{i}",
                "startTime": 1700000000 + i,
                "endTime": 1700000010 + i,
                "modeId": 12,
                "players": [{"accountId": 42, "score": 40000}],
            }
            for i in [5, 4, 3]
        ],
        [
            {
                "uuid": f"game-{i}",
                "startTime": 1700000000 + i,
                "endTime": 1700000010 + i,
                "modeId": 12,
                "players": [{"accountId": 42, "score": 40000}],
            }
            for i in [3, 2, 1]
        ],
    ]

    def handle(request):
        assert request.url.params["mode"] == "16.12.9"
        return httpx.Response(200, json=pages.pop(0))

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw),
    ):
        result = await PaipuyaClient("https://api.example").review_records(42, 5)
    assert [r.uuid for r in result] == [f"game-{i}" for i in [5, 4, 3, 2, 1]]


@pytest.mark.asyncio
async def test_report_upload_transfers_bytes_without_requiring_shared_filesystem(tmp_path):
    directory = tmp_path / "abcdef012345"
    directory.mkdir()
    path = directory / "report.html"
    data = "<html>喵杰统计表</html>".encode()
    path.write_bytes(data)
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"file_id": "uploaded"}))
    result = await upload_report(bot, 1131357545, path)
    assert result["file_id"] == "uploaded"
    args, kwargs = bot.call_api.call_args
    assert args == ("upload_group_file",)
    assert kwargs["group_id"] == 1131357545
    assert kwargs["name"] == "mortal-abcdef012345.html"
    assert kwargs["file"].startswith("base64://")
    assert base64.b64decode(kwargs["file"][9:]) == data
    assert str(path) not in kwargs["file"]


@pytest.mark.asyncio
async def test_oversized_report_does_not_call_upload_api(tmp_path):
    path = tmp_path / "report.html"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    bot = SimpleNamespace(call_api=AsyncMock())
    with pytest.raises(ReviewError, match="2 MiB"):
        await upload_report(bot, 1131357545, path)
    bot.call_api.assert_not_called()


@pytest.mark.asyncio
async def test_superuser_manages_other_members_tasks_across_groups(tmp_path):
    batches = manager(tmp_path)
    batches.superusers = frozenset({"285843647"})
    job = await batches.create(1, "group", "creator", Player(42, "name"))
    for caller, group in (("stranger", "group"), ("creator", "other-group")):
        with pytest.raises(ReviewError, match="本人"):
            batches.load(job["id"], group, caller)
    privileged = batches.load(job["id"], "other-group", "285843647")
    assert privileged["owner"] == "creator" and privileged["group_id"] == "group"
    await batches.pause(job["id"], "other-group", "285843647")
    assert batches.load(job["id"], "group", "creator")["status"] == "paused"
