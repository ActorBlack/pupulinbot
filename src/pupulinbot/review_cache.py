"""Reuse verified local results; never silently cache manually assigned reports."""

import json
from pathlib import Path

from .batch_review import report_metrics, report_url
from .review import ReviewError


class ReviewCache:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.index = None
        self.memory = {}

    @staticmethod
    def key(row, model):
        return row.get("source", "majsoul"), row["uuid"], row["seat"], model

    def find(self, row, model):
        if row.get("online_submitted"):
            return None
        if self.key(row, model) in self.memory:
            return self.memory[self.key(row, model)]
        if self.index is None:
            self.index = {}
            for manifest in self.directory.glob("*/job.json"):
                try:
                    job = json.loads(manifest.read_text())
                    for i, saved in enumerate(job["rows"], 1):
                        metric = saved.get("metrics") or {}
                        if saved["status"] != "done" or not saved.get("online_submitted"):
                            continue
                        if saved.get("source_verification", "").startswith("用户指定场次"):
                            continue
                        url = saved.get("report_url")
                        report_url(url)
                        path = (manifest.parent / f"{i}.json").resolve()
                        if not path.is_relative_to(self.directory):
                            continue
                        self.index.setdefault(self.key(saved, metric.get("model")), []).append(
                            (path, url)
                        )
                except (ValueError, KeyError, TypeError, OSError, ReviewError):
                    continue
        for path, url in self.index.get(self.key(row, model), []):
            try:
                with path.open("rb") as stream:
                    content = stream.read(10 * 1024 * 1024 + 1)
                if len(content) > 10 * 1024 * 1024:
                    continue
                data = json.loads(content)
                if (
                    not isinstance(data, dict)
                    or data.get("player_id") != row["seat"]
                    or report_metrics(data)["model"] != model
                ):
                    continue
                self.remember(row, model, (data, url))
                return data, url
            except (OSError, ValueError, KeyError, TypeError, ReviewError):
                continue
        return None

    def remember(self, row, model, result):
        if row.get("source_verification", "").startswith("用户指定场次"):
            return
        try:
            data, url = result
            report_url(url)
            if (
                not isinstance(data, dict)
                or data.get("player_id") != row["seat"]
                or report_metrics(data)["model"] != model
            ):
                return
        except (ReviewError, ValueError, KeyError, TypeError):
            return
        self.memory[self.key(row, model)] = result
        if len(self.memory) > 200:
            self.memory.pop(next(iter(self.memory)))
