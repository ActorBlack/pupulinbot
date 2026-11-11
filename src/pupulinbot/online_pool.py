"""Two independent browser processes, with one global submission clock."""

import asyncio
import json
from pathlib import Path

from .online_review import OnlinePaused, OnlineReviews
from .review import ReviewError
from .review_cache import ReviewCache


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
    def __init__(
        self,
        profile: Path,
        model="4.1b",
        cooldown=10,
        timeout=600,
        *,
        options=None,
        concurrency=2,
        cache_directory=None,
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 2:
            raise ValueError("在线浏览器并发数仅支持1–2")
        self.profile, self.concurrency = profile, concurrency
        self.cache = ReviewCache(cache_directory) if cache_directory else None
        self.cache_lock = asyncio.Lock()
        self.gate = SubmissionGate(cooldown)
        self.workers = [
            OnlineReviews(
                profile / f"worker-{i}",
                model,
                cooldown=0,
                timeout=timeout,
                options={
                    **(options or {}),
                    "controlled_gate": True,
                    "unique_debug_port": True,
                    "gui_lock_path": str((profile / "verification-gui.lock").resolve()),
                    "cooldown": 0,
                },
                gate=self.gate,
            )
            for i in range(concurrency)
        ]

    @property
    def pending_keys(self):
        return {w.pending_key for w in self.workers if w.pending_key}

    @property
    def pending_key(self):
        return next((w.pending_key for w in self.workers if w.pending_key), None)

    def job_progress(self, job_id):
        stages = []
        for worker in self.workers:
            if (
                worker.progress
                and worker.pending_key
                and worker.pending_key.startswith(job_id + ":")
            ):
                index = int(worker.pending_key.rsplit(":", 1)[1])
                stages.append((index, worker.progress))
        return [f"第 {index + 1} 项：{message}" for index, message in sorted(stages)]

    @property
    def recovery_slots(self):
        """Keep durable results pinned even if the parent died before saving its row."""
        slots = {}
        for path in [
            self.profile / "submission-journal.json",
            *sorted(self.profile.glob("worker-*/submission-journal.json")),
        ]:
            if not path.exists():
                continue
            state = json.loads(path.read_text())
            if not isinstance(state, dict):
                raise ValueError("浏览器恢复记录格式无效")
            if state.get("state") not in {"submitted", "done"} or not state.get("key"):
                continue
            slot = (
                0 if path.parent == self.profile else int(path.parent.name.removeprefix("worker-"))
            )
            slots[state["key"]] = slot
        return slots

    def begin(self):
        self.gate.blocked = False

    def halt(self):
        self.gate.blocked = True

    async def submit(self, key, row, on_submitted):
        slot = row.get("online_slot", int(key.rsplit(":", 1)[1]) % self.concurrency)
        if type(slot) is not int or not 0 <= slot < self.concurrency:
            raise ReviewError("原任务的浏览器槽位超出当前并发设置，请恢复原并发配置")
        own_journal = self.workers[slot].profile / "submission-journal.json"
        own_state = json.loads(own_journal.read_text()) if own_journal.exists() else {}
        recovering = own_state.get("key") == key and own_state.get("state") in {"submitted", "done"}
        # Keep one global unresolved-submission barrier even across restarts/config changes.
        for path in [
            self.profile / "submission-journal.json",
            *self.profile.glob("worker-*/submission-journal.json"),
        ]:
            if path.exists():
                state = json.loads(path.read_text())
                if state.get("state") == "submitted" and state.get("key") != key:
                    active = any(w.pending_key == state.get("key") for w in self.workers)
                    # Recovering an existing submission cannot create a fresh review.
                    # Two interrupted workers must not block each other's recovery.
                    if not active and not recovering:
                        self.halt()
                        raise OnlinePaused(
                            "uncertain", "另一个浏览器有已提交但未核对的场次，请先恢复原任务"
                        )
        if self.cache:
            async with self.cache_lock:
                cached = await asyncio.to_thread(self.cache.find, row, self.workers[slot].model)
            if cached:
                row["review_cache_hit"] = True
                return cached
        worker = self.workers[slot]
        try:
            result = await worker.submit(key, row, on_submitted)
            legacy = self.profile / "submission-journal.json"
            if legacy.exists():
                await OnlineReviews(self.profile).acknowledge_import(key, result[1], result[0])
            if self.cache:
                self.cache.remember(row, worker.model, result)
            return result
        except Exception:
            self.halt()
            raise

    async def acknowledge_import(self, key, url, data):
        await OnlineReviews(self.profile).acknowledge_import(key, url, data)
        for worker in self.workers:
            await worker.acknowledge_import(key, url, data)

    async def close(self):
        self.halt()
        await asyncio.gather(*(w.close() for w in self.workers))
