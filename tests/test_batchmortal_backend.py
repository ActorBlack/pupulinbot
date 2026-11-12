import csv
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from test_batch_review import native_report

from pupulinbot.batch_chart import render_batch_chart
from pupulinbot.batch_export import write_export
from pupulinbot.batch_review import BatchReviews, render_report, report_metrics
from pupulinbot.batchmortal_worker import (
    BrowserWorker,
    VerificationRejected,
    VerificationTimeout,
    atomic_json,
    browser_options,
    validate_request,
)
from pupulinbot.online_review import OnlineReviews
from pupulinbot.review import ReviewError

LINK = "https://game.maj-soul.com/1/?paipu=261029-b5c0d5be-b902-42c5-a4e0-e7cf2dbfd4da"
REPORT = "https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json"


def request(key="job:0"):
    from urllib.parse import urlencode

    return {
        "key": key,
        "online_url": "https://mjai.ekyu.moe/?" + urlencode({"url": LINK}),
        "seat": 0,
    }


def fake_worker(tmp_path):
    w = BrowserWorker.__new__(BrowserWorker)
    w.profile = tmp_path
    w.journal_path = tmp_path / "submission-journal.json"
    w.journal = {}
    w.key = None
    w.submitted = False
    w.model = "4.1b"
    w.timeout = 30
    w.options = {"retries": 1}
    w.sb = SimpleNamespace(save_screenshot=lambda p: None, get_current_url=lambda: REPORT)
    w.automator = SimpleNamespace(_extract_killerducky_data=lambda *a, **kw: native_report())
    return w


def test_local_snapshot_is_pinned_and_metrics_match_inclusive_upstream_definition():
    root = Path(__file__).parents[1] / "src/pupulinbot/_batchmortal"
    meta = json.loads((root / "ORIGIN.json").read_text())
    assert (
        hashlib.sha256((root / "browser.py").read_bytes()).hexdigest()
        == meta["hashes"]["batchmortal/browser.py"]
    )
    data = native_report()
    data["review"]["kyokus"][0]["entries"] = [
        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.05}]},
        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.10}]},
        {"is_equal": True, "actual_index": 0, "details": [{"prob": 0.001}]},
        {"is_equal": False, "details": []},
    ]
    metric = report_metrics(data)
    assert metric["bad5"] == 1 and metric["bad10"] == 2
    assert metric["bad5_rate"] == 10 and metric["bad10_rate"] == 20
    assert metric["badmove_decisions"] == 10 and metric["badmove_unparsed"] == 1


def test_headless_options_follow_local_backend_platform_policy():
    mac = browser_options(Path("/tmp/profile"), {"headless": True}, platform="darwin")
    assert mac["uc"] and mac["headless"] and not mac.get("xvfb")
    linux = browser_options(Path("/tmp/profile"), {"headless": True}, platform="linux")
    assert linux["uc"] and not linux["headless"] and linux["xvfb"]
    assert not browser_options(Path("/tmp/p"), {"headless": False}, platform="darwin")["headless"]


def test_request_validation_rejects_untrusted_targets_before_submission():
    assert validate_request(request()) == LINK
    bad = request()
    bad["online_url"] = "https://evil.invalid/?url=" + LINK
    with pytest.raises(ValueError):
        validate_request(bad)
    bad = request()
    bad["seat"] = True
    with pytest.raises(ValueError):
        validate_request(bad)


def test_bounded_presubmit_retry_journal_and_done_replay(tmp_path):
    worker = fake_worker(tmp_path)
    attempts = []

    def analyze(sb, task):
        attempts.append(task)
        if len(attempts) == 1:
            raise RuntimeError("token timeout")
        worker._record_submission()
        return {"resultUrl": REPORT, "playerNames": ["测试"]}

    worker.automator.analyze_single = analyze
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker.run(request())
        statuses = [c.kwargs["status"] for c in emit.call_args_list]
        assert statuses == ["preparing_retry", "submitted", "done"]
        result = emit.call_args.kwargs
        assert Path(result["report_file"]).is_file()
        assert Path(result["report_file"]).stat().st_mode & 0o777 == 0o600
        worker.run(request())
        assert len(attempts) == 2 and emit.call_args.kwargs["status"] == "done"
    assert json.loads(worker.journal_path.read_text())["state"] == "done"


def test_postsubmit_failure_and_restart_cannot_duplicate_post(tmp_path):
    worker = fake_worker(tmp_path)
    worker.sb.get_current_url = lambda: "https://mjai.ekyu.moe/review"

    def analyze(sb, task):
        worker._record_submission()
        raise RuntimeError("network lost after submit")

    worker.automator.analyze_single = analyze
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker.run(request())
        assert emit.call_args.kwargs["status"] == "uncertain"
        worker.key = None
        worker.automator.analyze_single = AsyncMock()
        worker.run(request())
        worker.run(request("other:0"))
        worker.automator.analyze_single.assert_not_called()
        assert emit.call_args.kwargs["status"] == "uncertain"


@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (VerificationTimeout, "verification_timeout"),
        (VerificationRejected, "verification_rejected"),
    ],
)
def test_verification_failure_returns_to_batch_scheduler_without_submitting(
    tmp_path, failure, status
):
    worker = fake_worker(tmp_path)
    worker.options["turnstile_timeout"] = 120
    with (
        patch.object(
            worker.automator,
            "analyze_single",
            create=True,
            side_effect=failure("test"),
        ) as analyze,
        patch("pupulinbot.batchmortal_worker.emit") as emit,
    ):
        worker.run(request())
    analyze.assert_called_once()
    assert analyze.call_args.args[1]["turnstile_timeout"] == 30
    assert [call.kwargs["status"] for call in emit.call_args_list] == [status]
    assert not worker.submitted and not worker.journal_path.exists()


@pytest.mark.asyncio
async def test_native_report_file_is_confined_and_report_seat_verified(tmp_path):
    engine = OnlineReviews(tmp_path)
    path = tmp_path / "artifacts/report.json"
    atomic_json(path, native_report())
    data = await engine._read_native(str(path), {"seat": 0})
    assert data["player_id"] == 0
    with pytest.raises(ReviewError, match="座位"):
        await engine._read_native(str(path), {"seat": 1})
    outside = tmp_path / "other.json"
    atomic_json(outside, native_report())
    with pytest.raises(ReviewError, match="专用目录"):
        await engine._read_native(str(outside), {"seat": 0})
    await engine.close()


@pytest.mark.asyncio
async def test_manual_import_acknowledges_durable_submission(tmp_path):
    engine = OnlineReviews(tmp_path)
    engine.active_submitted_key = "job:0"
    atomic_json(tmp_path / "submission-journal.json", {"key": "job:0", "state": "submitted"})
    await engine.acknowledge_import("job:0", REPORT, native_report())
    state = json.loads((tmp_path / "submission-journal.json").read_text())
    assert state["state"] == "done" and state["url"] == REPORT
    assert engine.active_submitted_key is None
    assert (await engine._read_native(state["report_file"], {"seat": 0}))["engine"] == "Mortal"
    await engine.close()


@pytest.mark.asyncio
async def test_four_seat_and_link_manifests_preserve_unknown_metadata_and_ownership(tmp_path):
    manager = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await manager.create_links(["「" + LINK + "」"], "g", "u", table=True)
    assert [r["seat"] for r in job["rows"]] == [0, 1, 2, 3]
    assert all(r["started_at"] is None and r["mode"] == "file" for r in job["rows"])
    assert "未知（导入链接）" in render_report(job)
    with pytest.raises(ReviewError, match="牌谱屋复核"):
        render_batch_chart(job)
    for row in job["rows"]:
        row.update(player_name="原始牌谱昵称", player_name_source="original")
    assert Image.open(io.BytesIO(render_batch_chart(job))).format == "PNG"
    assert manager.load(job["id"], "g", "u")["kind"] == "table"
    with pytest.raises(ReviewError):
        manager.load(job["id"], "other", "u")
    links = await manager.create_links([LINK, LINK], "g", "u", seat=2)
    assert len(links["rows"]) == 1 and links["rows"][0]["seat"] == 2
    links["rows"][0].update(player_name="原始牌谱昵称", player_name_source="original")
    assert Image.open(io.BytesIO(render_batch_chart(links))).format == "PNG"
    with pytest.raises(ReviewError):
        await manager.create_links(["https://evil.invalid/?paipu=abcdefgh"], "g", "u")
    with pytest.raises(ReviewError):
        await manager.create_links([LINK, LINK], "g", "u", table=True)


@pytest.mark.asyncio
async def test_csv_and_xlsx_export_keep_blank_metadata_and_escape_spreadsheet_formulas(tmp_path):
    manager = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await manager.create_links([LINK], "g", "u")
    job["nickname"] = '=HYPERLINK("bad")'
    job["rows"][0].update(player_name=job["nickname"], player_name_source="original")
    job["rows"][0].update(status="done", metrics=report_metrics(native_report()))
    target = tmp_path / "result.csv"
    write_export(job, target)
    with target.open(encoding="utf-8-sig") as stream:
        row = next(csv.DictReader(stream))
    assert row["nickname"].startswith("'=") and row["startTime"] == "" and row["timestamp"] == ""
    assert row["rating"] == "87.0" and row["badMoveDenominator"] == "10"
    openpyxl = pytest.importorskip("openpyxl")
    target = tmp_path / "result.xlsx"
    write_export(job, target)
    book = openpyxl.load_workbook(target)
    assert book.active["A2"].data_type == "s" and book.active["F2"].value is None
    book.close()


def test_optional_artifact_failure_recovers_report_without_second_submission(tmp_path):
    worker = fake_worker(tmp_path)
    attempts = []

    def analyze(sb, task):
        attempts.append(task)
        worker._record_submission()
        raise OSError("optional screenshot could not be saved")

    worker.automator.analyze_single = analyze
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker.run(request())
        assert emit.call_args.kwargs["status"] == "done"
        worker.run(request())
        assert emit.call_args.kwargs["status"] == "done"
    assert len(attempts) == 1


def test_completed_report_recovers_missing_scores_without_another_submission(tmp_path):
    from concurrent.futures import Future
    from unittest.mock import Mock

    worker = fake_worker(tmp_path)
    worker.seat, worker.key = 0, "metadata:0"
    recovered = native_report()
    recovered["game_result"] = {"scores": [36400, 23600, 21500, 18500], "ranks": [1, 2, 3, 4]}
    future = Future()
    future.set_result(recovered)
    worker.report_reader = SimpleNamespace(submit=Mock(return_value=future))
    worker.report_future = None
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker._done(REPORT)
    saved = json.loads(Path(emit.call_args.kwargs["report_file"]).read_text())
    assert saved["game_result"] == recovered["game_result"]
    assert report_metrics(saved) == report_metrics(native_report())
    worker.report_reader.submit.assert_called_once()
    # Recovering the same result reuses the completed GET, without another GET or POST.
    with patch("pupulinbot.batchmortal_worker.emit"):
        worker._done(REPORT)
    worker.report_reader.submit.assert_called_once()


@pytest.mark.asyncio
async def test_table_report_does_not_average_different_players(tmp_path):
    manager = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await manager.create_links([LINK], "g", "u", table=True)
    job["rows"][0].update(
        status="done",
        metrics=report_metrics(native_report()),
        player_name="昵称<&>",
        player_name_source="original",
    )
    report = render_report(job)
    assert "不合并为同一玩家均值" in report
    assert "平均 Rating" not in report
    assert "起始座位" in report and "东起" in report
    assert "<th>游戏昵称</th>" in report and "<th>状态</th>" not in report
    assert "昵称&lt;&amp;&gt;" in report


def test_explicit_server_captcha_rejection_is_not_uncertain(tmp_path):
    worker = fake_worker(tmp_path)
    worker.sb.get_current_url = lambda: "https://mjai.ekyu.moe/review"
    worker.automator._read_review_state = lambda sb: {"page_text": "invalid captcha response"}

    def analyze(sb, task):
        worker._record_submission()
        raise RuntimeError("Turnstile token was rejected")

    worker.automator.analyze_single = analyze
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker.run(request())
        assert emit.call_args.kwargs["status"] == "captcha_rejected"
    assert json.loads(worker.journal_path.read_text())["state"] == "rejected"
    assert not worker.submitted


def test_explicit_backend_task_timeout_can_be_retried(tmp_path):
    worker = fake_worker(tmp_path)
    worker.sb.get_current_url = lambda: "https://mjai.ekyu.moe/review"
    worker.automator._read_review_state = lambda sb: {
        "page_text": "task timeout, been running for 29.999983718s"
    }

    def analyze(sb, task):
        worker._record_submission()
        raise RuntimeError("Review submission never left the form page")

    worker.automator.analyze_single = analyze
    with patch("pupulinbot.batchmortal_worker.emit") as emit:
        worker.run(request())
        assert emit.call_args.kwargs["status"] == "server_timeout"
    assert json.loads(worker.journal_path.read_text())["state"] == "rejected"


@pytest.mark.asyncio
async def test_export_keeps_known_account_nickname_instead_of_anonymized_report_name(tmp_path):
    from pupulinbot.batch_export import export_rows

    manager = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await manager.create_links([LINK], "g", "u")
    job.update(account_id=12342734, nickname="y024")
    job["rows"][0]["player_name"] = "Aさん"
    job["rows"][0]["name_check"] = {
        "source": "paipuya",
        "uuid": job["rows"][0]["uuid"],
        "status": "not_found",
        "checked_at": "2026-11-11T00:00:00+00:00",
    }
    assert export_rows(job)[0]["nickname"] == "y024"
