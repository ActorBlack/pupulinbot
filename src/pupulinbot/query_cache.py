"""Bounded fresh-only cache; concurrent identical reads share one request."""

import asyncio
import copy
import time
from collections import OrderedDict


class QueryCache:
    def __init__(self, capacity=64):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.pending = {}
        self.hits = self.misses = 0

    async def get(self, key, ttl, load):
        now = time.monotonic()
        for expired in [k for k, (until, _) in self.entries.items() if until <= now]:
            self.entries.pop(expired, None)
        if key in self.entries:
            self.hits += 1
            self.entries.move_to_end(key)
            return copy.deepcopy(self.entries[key][1])
        task = self.pending.get(key)
        if task is None:
            self.misses += 1
            task = asyncio.create_task(self._load(key, ttl, load))
            self.pending[key] = task
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        else:
            self.hits += 1
        return copy.deepcopy(await asyncio.shield(task))

    async def _load(self, key, ttl, load):
        try:
            result = await load()
            self.entries[key] = (time.monotonic() + ttl, copy.deepcopy(result))
            self.entries.move_to_end(key)
            while len(self.entries) > self.capacity:
                self.entries.popitem(last=False)
            return result
        finally:
            self.pending.pop(key, None)

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
