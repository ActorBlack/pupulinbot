"""One Chrome worker with read-only recovery of historical browser journals."""

import asyncio
import json
import logging
from pathlib import Path

from .online_review import OnlinePaused, OnlineReviews
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

    def _journals(self):
        for profile in (self.worker.profile, *self.legacy_profiles):
            path = profile / "submission-journal.json"
            if not path.exists():
                continue
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                raise ValueError("浏览器恢复记录格式无效")
            yield profile, state

    @property
    def recovery_slots(self):
        return {
            state["key"]: int(profile == self.legacy_profiles[0])
            for profile, state in self._journals()
            if state.get("state") in {"submitted", "done"} and state.get("key")
        }

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
        if self.cache and matching is None:
            async with self.cache_lock:
                cached = await asyncio.to_thread(self.cache.find, row, self.worker.model)
            if cached:
                row["review_cache_hit"] = True
                return cached
        try:
            if matching and matching[0] != self.worker.profile:
                profile, journal = matching
                result = await OnlineReviews(profile, self.worker.model).recover(
                    key, row, on_submitted, journal
                )
                if result is None:
                    raise OnlinePaused(
                        "uncertain", "旧浏览器已提交，但未保存报告地址，请找回报告并导入"
                    )
            else:
                result = await self.worker.submit(key, row, on_submitted)
            if self.cache:
                try:
                    async with self.cache_lock:
                        await asyncio.to_thread(self.cache.remember, row, self.worker.model, result)
                except OSError:
                    logger.warning("Mortal 报告无法写入独立缓存", exc_info=True)
            return result
        except Exception:
            self.halt()
            raise

    async def acknowledge_import(self, key, url, data):
        await self.worker.acknowledge_import(key, url, data)
        for profile in self.legacy_profiles:
            await OnlineReviews(profile).acknowledge_import(key, url, data)

    async def close(self):
        self.halt()
        await self.worker.close()
