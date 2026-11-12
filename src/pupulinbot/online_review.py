"""Async coordinator for a single persistent SeleniumBase Chrome process."""

import asyncio
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

from .batch_review import (
    VERIFICATION_FAILURE_STATUSES,
    fetch_report,
    finish_file_write,
    report_metrics,
)
from .online_worker import TURNSTILE_TIMEOUT, progress_report_url
from .review import ReviewError

logger = logging.getLogger(__name__)
VERIFICATION_BROWSER_GRACE = 15


def paused_journal(profile, key):
    job_id, _, ordinal = key.partition(":")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id) or not re.fullmatch(r"[0-9]+", ordinal):
        raise ValueError("浏览器恢复记录的任务编号无效")
    return profile / "paused-submissions" / f"{job_id}:{ordinal}.json"


class OnlinePaused(ReviewError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class OnlineReviews:
    def __init__(
        self, profile: Path, model="4.1b", cooldown=20, timeout=600, *, options=None, gate=None
    ):
        self.gate = gate
        self.options = options or {}
        self.profile, self.model = profile, model
        self.cooldown, self.timeout = cooldown, timeout
        self.process = None
        self.lock = asyncio.Lock()
        self.pending = None
        self.pending_key = None
        self.progress = None
        self.last_submission = 0
        self.active_submitted_key = None

    async def submit(self, key, row, on_submitted):
        async with self.lock:
            if self.active_submitted_key not in {None, key}:
                raise ReviewError("前一场已提交但结果未核对，请先继续原任务或导入报告")
            if self.pending is not None and self.pending_key != key:
                raise ReviewError("另一个场次仍在浏览器处理中，请先继续该任务")
            if self.pending is None:
                self.pending_key = key
                self.pending = asyncio.create_task(self._submit(key, row, on_submitted))
            try:
                return await asyncio.shield(self.pending)
            finally:
                if self.pending.done():
                    self.pending = self.pending_key = None
                    self.progress = None

    async def recover(self, key, row, on_submitted, journal, *, journal_path=None):
        """Recover a known report without launching Chrome or submitting a review."""
        if journal.get("key") == key and journal.get("state") == "done":
            from .batch_review import report_url

            report_url(journal["url"])
            data = await self._read_native(journal["report_file"], row)
            await on_submitted(journal["url"])
            self.active_submitted_key = None
            return data, journal["url"]
        recovery_url = journal.get("remote_report_url")
        if not recovery_url and journal.get("remote_progress_url"):
            # A known task ID permits a read-only lookup, never another submission.
            recovery_url = progress_report_url(journal["remote_progress_url"])
        if journal.get("key") == key and journal.get("state") == "submitted" and recovery_url:
            from .native_report import read_native_report

            try:
                async with asyncio.timeout(90):
                    data = await asyncio.to_thread(
                        read_native_report, recovery_url, row["seat"], self.model
                    )
            except Exception as exc:
                raise OnlinePaused(
                    "uncertain", "已提交结果尚未读到，稍后只重试读取，不重复提交"
                ) from exc
            await self._store_completed(key, recovery_url, data, journal_path=journal_path)
            await on_submitted(recovery_url)
            self.active_submitted_key = None
            return data, recovery_url
        if row.get("online_report_url"):
            data = await self._fetch(row["online_report_url"], row)
            await self._store_completed(
                key, row["online_report_url"], data, journal_path=journal_path
            )
            self.active_submitted_key = None
            return data, row["online_report_url"]
        return None

    async def _submit(self, key, row, on_submitted):
        def read_journal():
            path = self.profile / "submission-journal.json"
            return json.loads(path.read_text()) if path.exists() else {}

        journal = await asyncio.to_thread(read_journal)
        recovered = await self.recover(key, row, on_submitted, journal)
        if recovered is not None:
            return recovered
        if self.process is None or self.process.returncode is not None:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pupulinbot.online_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True,
            )
        delay = self.last_submission + self.cooldown - asyncio.get_running_loop().time()
        if delay > 0:
            await asyncio.sleep(delay)
        request = {
            "key": key,
            "online_url": row["online_url"],
            "seat": row["seat"],
            "submitted": bool(row.get("online_submitted")),
            "need_game_result": not (type(row.get("rank")) is int and 1 <= row["rank"] <= 4),
            "profile": str(self.profile.resolve()),
            "model": self.model,
            "timeout": self.timeout,
            "backend": "batchmortal",
            "options": {**self.options, "cooldown": self.cooldown},
        }
        self.progress = "正在打开 Mortal 复盘页"
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        await self.process.stdin.drain()
        try:
            async with (
                asyncio.timeout(
                    self.timeout
                    + (self.options.get("retries", 2) + 1)
                    * (TURNSTILE_TIMEOUT + VERIFICATION_BROWSER_GRACE + 60)
                    + 120
                ),
                asyncio.timeout(None) as verification,
            ):
                while line := await self.process.stdout.readline():
                    if not line.startswith(b"PUPULIN_ONLINE "):
                        continue
                    event = json.loads(line[len(b"PUPULIN_ONLINE ") :])
                    status = event.get("status")
                    if status == "verification_wait":
                        self.progress = "等待 Mortal 网页验证，尚未提交"
                        # Allow browser interaction plus the worker's full token wait.
                        if self.options.get("controlled_gate") and not request["submitted"]:
                            verification.reschedule(
                                asyncio.get_running_loop().time()
                                + TURNSTILE_TIMEOUT
                                + VERIFICATION_BROWSER_GRACE
                            )
                        continue
                    verification.reschedule(None)
                    if status == "ready_to_submit":
                        self.progress = "验证已通过，等待提交"
                        allowed = await self.gate.acquire() if self.gate else True
                        self.process.stdin.write(
                            (
                                json.dumps({"action": "permit" if allowed else "abort", "key": key})
                                + "\n"
                            ).encode()
                        )
                        await self.process.stdin.drain()
                        continue
                    if status == "preparing_retry":
                        self.progress = event.get("message") or "正在重试打开复盘页"
                        continue
                    if status == "submitted":
                        self.progress = "已发起提交，等待报告"
                        self.active_submitted_key = key
                        self.last_submission = asyncio.get_running_loop().time()
                        await on_submitted()
                        continue
                    if status in {"captcha_rejected", "server_timeout"}:
                        self.active_submitted_key = None
                    if status in VERIFICATION_FAILURE_STATUSES:
                        if request["submitted"] or self.active_submitted_key is not None:
                            raise OnlinePaused(
                                "uncertain", "本场已有提交记录，稍后只找回报告，不重复提交"
                            )
                        # Reloading the form can leave the same Chrome session stuck.
                        await self._close_process()
                    if status != "done":
                        raise OnlinePaused(status, event.get("message", "在线批跑已暂停"))
                    from .batch_review import report_url

                    report_url(event["url"])
                    await on_submitted(event["url"])
                    if event.get("report_file"):
                        data = await self._read_native(event["report_file"], row)
                    else:
                        data = await self._fetch(event["url"], row)
                    self.active_submitted_key = None
                    return data, event["url"]
        except TimeoutError as exc:
            await self._terminate()
            if verification.expired():
                limit = TURNSTILE_TIMEOUT + VERIFICATION_BROWSER_GRACE
                logger.info("Mortal 验证流程超过 %s 秒，关闭 Chrome：%s", limit, key)
                raise OnlinePaused(
                    "verification_timeout",
                    f"Mortal 验证流程超过 {limit} 秒，尚未提交。",
                ) from exc
            raise OnlinePaused("uncertain", "浏览器响应超时，任务已暂停；不会自动重复提交") from exc
        raise ReviewError("浏览器进程已退出，请重启后继续")

    async def _store_completed(self, key, url, data, *, journal_path=None):
        import hashlib

        from .batchmortal_worker import atomic_json

        def record():
            artifact = (
                self.profile / "artifacts" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            )
            atomic_json(artifact, data)
            atomic_json(
                journal_path or self.profile / "submission-journal.json",
                {"key": key, "state": "done", "url": url, "report_file": str(artifact.resolve())},
            )

        await finish_file_write(record)

    async def _read_native(self, filename, row):
        def read():
            path = Path(filename).resolve()
            root = (self.profile / "artifacts").resolve()
            if path.parent != root or path.suffix != ".json":
                raise ReviewError("报告文件不在专用目录内")
            with path.open("rb") as stream:
                content = stream.read(10 * 1024 * 1024 + 1)
            if len(content) > 10 * 1024 * 1024:
                raise ReviewError("报告超过10MiB")
            return json.loads(content)

        data = await asyncio.to_thread(read)
        if not isinstance(data, dict) or data.get("player_id") != row["seat"]:
            raise ReviewError("在线报告座位不匹配，任务已暂停")
        if report_metrics(data)["model"] != self.model:
            raise ReviewError("在线报告模型不匹配，任务已暂停")
        return data

    async def _fetch(self, url, row):
        import httpx

        try:
            data = await fetch_report(url)
        except httpx.HTTPError as exc:
            raise ReviewError("报告已生成，但下载失败；稍后继续会重试下载，不重复提交") from exc
        if not isinstance(data, dict) or data.get("player_id") != row["seat"]:
            raise ReviewError("在线报告座位不匹配，任务已暂停")
        if report_metrics(data)["model"] != self.model:
            raise ReviewError("在线报告模型不匹配，任务已暂停")
        return data

    async def acknowledge_import(self, key, url, data):
        import hashlib

        from .batchmortal_worker import atomic_json

        if self.pending_key == key and self.pending:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = self.pending_key = None
            await self._terminate()

        def record():
            active = self.profile / "submission-journal.json"
            journals = []
            for journal in (active, paused_journal(self.profile, key)):
                if not journal.exists():
                    continue
                state = json.loads(journal.read_text())
                if state.get("key") == key and (
                    journal != active or state.get("state") == "submitted"
                ):
                    journals.append(journal)
            if not journals:
                return
            artifact = (
                self.profile / "artifacts" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            )
            atomic_json(artifact, data)
            for journal in journals:
                atomic_json(
                    journal,
                    {
                        "key": key,
                        "state": "done",
                        "url": url,
                        "report_file": str(artifact.resolve()),
                    },
                )

        await finish_file_write(record)
        if self.active_submitted_key == key:
            self.active_submitted_key = None

    async def release_jobs(self, job_ids, *, submitted_key=None):
        """Stop an owned browser operation before its journal can be moved."""

        def owned(key):
            return key and key.partition(":")[0] in job_ids

        if self.pending_key and not owned(self.pending_key):
            return
        if not any(
            owned(key) for key in (self.pending_key, self.active_submitted_key, submitted_key)
        ):
            return
        async with self.lock:
            if self.pending is not None:
                self.pending.cancel()
                await asyncio.gather(self.pending, return_exceptions=True)
            await self._terminate()
            self.pending = self.pending_key = self.active_submitted_key = self.progress = None

    async def _terminate(self):
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        self.process = None

    async def close(self):
        if self.pending:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
        await self._close_process()

    async def _close_process(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.stdin.write(b'{"action":"close"}\n')
                await self.process.stdin.drain()
                await asyncio.wait_for(self.process.wait(), 5)
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                await self._terminate()
        self.process = None
