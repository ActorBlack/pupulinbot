"""One Chrome worker with read-only recovery of historical browser journals."""

import asyncio
import json
import logging
from pathlib import Path

from .batch_review import finish_file_write
from .online_review import OnlinePaused, OnlineReviews, paused_journal
from .review_cache import ReviewCache

logger = logging.getLogger(__name__)


class SubmissionGate:
    def __init__(self, interval=10):
        self.interval = interval
        self.next_at = 0.0
        self.lock = asyncio.Lock()
        self.blocked = False

    async def acquire(self):
        async with self.lock:
            if self.blocked:
                return False
            delay = self.next_at - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.blocked:
                return False
            self.next_at = asyncio.get_running_loop().time() + self.interval
            return True


class OnlinePool:
    concurrency = 1

    def __init__(
        self,
        profile: Path,
        model="4.1b",
        cooldown=10,
        timeout=600,
        *,
        options=None,
        cache: ReviewCache | None = None,
    ):
        self.profile = profile
        self.cache = cache
        self.cache_lock = asyncio.Lock()
        self.gate = SubmissionGate(cooldown)
        self.worker = OnlineReviews(
            profile / "worker-0",
            model,
            cooldown=0,
            timeout=timeout,
            options={
                **(options or {}),
                "controlled_gate": True,
                "unique_debug_port": True,
            },
            gate=self.gate,
        )
        self.legacy_profiles = (profile / "worker-1", profile)

    @property
    def pending_key(self):
        return self.worker.pending_key

    def job_progress(self, job_id):
        key, progress = self.pending_key, self.worker.progress
        if key and progress and key.startswith(job_id + ":"):
            index = int(key.rsplit(":", 1)[1])
            return [f"第 {index + 1} 项：{progress}"]
        return []

    @property
    def profiles(self):
        return (self.worker.profile, *self.legacy_profiles)

    @staticmethod
    def read_journal(path):
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or not isinstance(state.get("key", ""), str):
            raise ValueError("浏览器恢复记录格式无效")
        return state

    def _journals(self):
        for profile in self.profiles:
            path = profile / "submission-journal.json"
            if path.exists():
                yield profile, self.read_journal(path)

    def _paused_paths(self, profile, job_id):
        return (profile / "paused-submissions").glob(f"{job_id}:*.json")

    def recovery_slots_for(self, job_id):
        slots = {
            state["key"]: int(profile == self.legacy_profiles[0])
            for profile, state in self._journals()
            if state.get("state") in {"submitted", "done"} and state.get("key")
        }
        for profile in self.profiles:
            for path in self._paused_paths(profile, job_id):
                state = self.read_journal(path)
                if state.get("key") != path.stem or state.get("state") not in {"submitted", "done"}:
                    raise ValueError("暂停场次恢复记录格式无效")
                slots.setdefault(path.stem, int(profile == self.legacy_profiles[0]))
        return slots

    async def park_jobs(self, job_ids):
        current = self.worker.profile / "submission-journal.json"
        state = self.read_journal(current) if current.exists() else {}
        submitted_key = state.get("key") if state.get("state") == "submitted" else None
        await self.worker.release_jobs(job_ids, submitted_key=submitted_key)
        for profile, state in self._journals():
            key = state.get("key") or ""
            if key.partition(":")[0] not in job_ids or state.get("state") not in {
                "submitted",
                "done",
            }:
                continue
            if profile == self.worker.profile and self.worker.pending_key:
                continue
            destination = paused_journal(profile, key)
            destination.parent.mkdir(mode=0o700, exist_ok=True)
            (profile / "submission-journal.json").replace(destination)

    def forget_job(self, job_id):
        for profile in self.profiles:
            for path in self._paused_paths(profile, job_id):
                path.unlink(missing_ok=True)

    def begin(self):
        self.gate.blocked = False

    def halt(self):
        self.gate.blocked = True

    async def submit(self, key, row, on_submitted):
        journals = list(self._journals())
        matching = next(
            (
                (profile, state)
                for profile, state in journals
                if state.get("key") == key and state.get("state") in {"submitted", "done"}
            ),
            None,
        )
        if matching is None and any(state.get("state") == "submitted" for _, state in journals):
            self.halt()
            raise OnlinePaused("uncertain", "有已提交但未核对的场次，请先恢复原任务")
        parked = None
        if matching is None:
            for profile in self.profiles:
                path = paused_journal(profile, key)
                if path.exists():
                    state = self.read_journal(path)
                    if state.get("key") != key or state.get("state") not in {"submitted", "done"}:
                        raise ValueError("暂停场次恢复记录格式无效")
                    matching = (profile, state)
                    parked = path
                    break
        if self.cache and matching is None:
            async with self.cache_lock:
                cached = await finish_file_write(self.cache.find, row, self.worker.model)
            if cached:
                row["review_cache_hit"] = True
                return cached
        try:
            if matching and (parked or matching[0] != self.worker.profile):
                profile, journal = matching
                result = await OnlineReviews(profile, self.worker.model).recover(
                    key, row, on_submitted, journal, journal_path=parked
                )
                if result is None:
                    raise OnlinePaused(
                        "uncertain", "这场已提交，但未保存报告地址，请找回报告并导入"
                    )
            else:
                result = await self.worker.submit(key, row, on_submitted)
            if self.cache:
                try:
                    async with self.cache_lock:
                        await finish_file_write(self.cache.remember, row, self.worker.model, result)
                except OSError:
                    logger.warning("Mortal 报告无法写入独立缓存", exc_info=True)
            return result
        except Exception as exc:
            if not isinstance(exc, OnlinePaused) or exc.status != "verification_timeout":
                self.halt()
            raise

    async def acknowledge_import(self, key, url, data):
        await self.worker.acknowledge_import(key, url, data)
        for profile in self.legacy_profiles:
            await OnlineReviews(profile).acknowledge_import(key, url, data)

    async def close(self):
        self.halt()
        await self.worker.close()
