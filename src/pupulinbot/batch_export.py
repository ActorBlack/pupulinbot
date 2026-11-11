"""Optional batchmortal-compatible export, separate from default QQ PNG replies."""

import csv
import os
import tempfile
from pathlib import Path

from ._batchmortal.export_schema import CSV_COLUMNS
from .batch_review import require_record_names, seat_nickname
from .review import ReviewError


def export_rows(job):
    require_record_names(job)
    result = []
    for row in job["rows"]:
        metric = row.get("metrics") or {}
        artifacts = row.get("backend_artifacts") or {}
        result.append(
            dict(
                zip(
                    CSV_COLUMNS,
                    [
                        (
                            job["nickname"]
                            if job.get("account_id") is not None
                            else seat_nickname(row)
                            if job.get("kind") == "table" or seat_nickname(row) != "—"
                            else job["nickname"]
                        ),
                        row.get("source", "majsoul"),
                        row["mode"],
                        row["uuid"],
                        row["url"],
                        row.get("started_at"),
                        row.get("ended_at"),
                        row.get("report_url"),
                        artifacts.get("localPaipuPath"),
                        metric.get("model"),
                        metric.get("rating"),
                        metric.get("agreement"),
                        metric.get("matches"),
                        metric.get("decisions"),
                        metric.get("temperature"),
                        metric.get("game_length"),
                        row["seat"],
                        metric.get("review_duration"),
                        artifacts.get("screenshotPath"),
                        row.get("completed_at"),
                        metric.get("bad5_rate"),
                        metric.get("bad5"),
                        metric.get("bad10_rate"),
                        metric.get("bad10"),
                        metric.get("badmove_decisions"),
                    ],
                    strict=True,
                )
            )
        )
    return result


def literal(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def write_export(job, path):
    if path.suffix not in {".csv", ".xlsx"}:
        raise ReviewError("导出格式只支持csv或xlsx")
    rows = export_rows(job)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".export-", suffix=".tmp", delete=False
    ) as temporary:
        temp = Path(temporary.name)
    try:
        if path.suffix == ".csv":
            with temp.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerows({k: literal(v) for k, v in row.items()} for row in rows)
        else:
            try:
                from openpyxl import Workbook
            except ImportError as exc:
                raise ReviewError("XLSX导出需要安装online依赖openpyxl") from exc
            wb = Workbook()
            ws = wb.active
            ws.title = "Mortal"
            ws.append(CSV_COLUMNS)
            for row in rows:
                ws.append([literal(row.get(k)) for k in CSV_COLUMNS])
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            wb.save(temp)
            wb.close()
        temp.chmod(0o600)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path
