"""Cross-process API pacing: one shared SQLite clock for all local callers."""

import asyncio
import sqlite3
import time
from pathlib import Path


class PaipuyaGate:
    # Slight margin above 1 second; never allow bursts of accumulated capacity.
    interval = 1.05

    def __init__(self, path=Path("data/paipuya-rate.sqlite")):
        self.path = Path(path)

    def _claim(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as db:
            db.execute("CREATE TABLE IF NOT EXISTS pacing (id INTEGER PRIMARY KEY, at REAL)")
            db.execute("BEGIN IMMEDIATE")
            now = time.monotonic()
            row = db.execute("SELECT at FROM pacing WHERE id=1").fetchone()
            last = row[0] if row else None
            # A reboot resets the monotonic clock. Never reserve future slots.
            delay = self.interval - (now - last) if last is not None and now >= last else 0
            if delay > 0:
                return delay
            db.execute(
                "INSERT INTO pacing VALUES(1,?) ON CONFLICT(id) DO UPDATE SET at=?", (now, now)
            )
            return 0

    async def wait(self):
        while True:
            delay = await asyncio.to_thread(self._claim)
            if delay <= 0:
                return
            await asyncio.sleep(delay)
