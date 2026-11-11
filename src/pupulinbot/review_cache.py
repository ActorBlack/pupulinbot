"""Keep verified online reports independently of the tasks that created them."""

import hashlib
import json
import logging
from pathlib import Path

from .batch_review import report_metrics, report_url
from .batchmortal_worker import atomic_json
from .review import ReviewError

logger = logging.getLogger(__name__)
REPORT_LIMIT = 10 * 1024 * 1024


class ReviewCache:
    def __init__(self, directory: Path, *, batch_directory: Path | None = None):
        self.directory = directory.resolve()
        self.batch_directory = batch_directory.resolve() if batch_directory else None
        self.index = None
        self.memory = {}

    @staticmethod
    def key(row, model):
        return row.get("source", "majsoul"), row["uuid"], row["seat"], model

    def _path(self, key):
        digest = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
        return self.directory / f"{digest}.json"

    @staticmethod
    def _read(path, limit=REPORT_LIMIT):
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Mortal 报告超过缓存大小限制")
        return json.loads(content)

    @staticmethod
    def _valid(key, result):
        try:
            data, url = result
            report_url(url)
            return (
                isinstance(data, dict)
                and type(data.get("player_id")) is int
                and data["player_id"] == key[2]
                and key[3] not in {None, "", "未标注模型"}
                and report_metrics(data)["model"] == key[3]
            )
        except (ReviewError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def _load(self, key):
        try:
            saved = self._read(self._path(key), REPORT_LIMIT + 4096)
            if saved["key"] != list(key):
                return None
            result = saved["report"], saved["report_url"]
            return result if self._valid(key, result) else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _remember_memory(self, key, result):
        self.memory[key] = result
        if len(self.memory) > 200:
            self.memory.pop(next(iter(self.memory)))

    def _store(self, key, result):
        data, url = result
        atomic_json(self._path(key), {"key": key, "report": data, "report_url": url})
        self._remember_memory(key, result)

    def _legacy_index(self):
        if self.index is not None:
            return self.index
        self.index = {}
        if not self.batch_directory:
            return self.index
        for manifest in self.batch_directory.glob("*/job.json"):
            try:
                rows = self._read(manifest, 4 * 1024 * 1024)["rows"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if not isinstance(rows, list):
                continue
            for index, row in enumerate(rows, 1):
                try:
                    if row["status"] != "done" or not (
                        row.get("online_submitted") or row.get("review_cache_hit")
                    ):
                        continue
                    if row.get("source_verification", "").startswith("用户指定场次"):
                        continue
                    key = self.key(row, (row.get("metrics") or {}).get("model"))
                    path = (manifest.parent / f"{index}.json").resolve()
                    if path.is_relative_to(self.batch_directory):
                        self.index.setdefault(key, []).append((path, row.get("report_url")))
                except (OSError, KeyError, TypeError, AttributeError):
                    continue
        return self.index

    def _legacy_result(self, key, candidates):
        for path, url in candidates:
            try:
                result = self._read(path), url
            except (OSError, ValueError):
                continue
            if self._valid(key, result):
                return result
        return None

    def find(self, row, model):
        if row.get("online_submitted"):
            return None
        key = self.key(row, model)
        if key in self.memory:
            return self.memory[key]
        saved = self._load(key)
        if saved is not None:
            self._remember_memory(key, saved)
            return saved
        legacy = self._legacy_result(key, self._legacy_index().get(key, ()))
        if legacy is not None:
            try:
                self._store(key, legacy)
            except OSError:
                self._remember_memory(key, legacy)
                logger.warning("Mortal 旧报告无法写入缓存", exc_info=True)
        return legacy

    def remember(self, row, model, result):
        if row.get("source_verification", "").startswith("用户指定场次"):
            return
        key = self.key(row, model)
        if self._valid(key, result):
            self._store(key, result)

    def migrate_legacy(self):
        """Verify and copy reports before the corresponding jobs are removed."""
        self.index = None
        copied = existing = invalid = 0
        for key, candidates in self._legacy_index().items():
            if self._load(key) is not None:
                existing += 1
                continue
            result = self._legacy_result(key, candidates)
            if result is None:
                invalid += 1
                continue
            self._store(key, result)
            if self._load(key) is None:
                raise OSError("Mortal 缓存写入后核验失败")
            copied += 1
        return {"copied": copied, "existing": existing, "invalid": invalid}
