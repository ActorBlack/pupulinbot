"""Offline comparison against a Git revision; no author-service submissions.

Run with PYTHONPATH=src; use --output artifacts/mortal-speed-audit.json to save results.
All inference and metadata delays are synthetic. The HTTP server is loopback-only.
"""

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pupulinbot
from pupulinbot.batch_review import BatchReviews, report_metrics
from pupulinbot.models import GameRecord, Player
from pupulinbot.review import MortalClient, ReviewResult

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(pupulinbot.__file__).resolve().parent
REPORT = "https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json"


def previous_module(ref, name):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:src/pupulinbot/{name}.py"], cwd=ROOT, text=True
    )
    module = types.ModuleType("pupulinbot._bench_before_" + name)
    module.__package__ = "pupulinbot"
    sys.modules[module.__name__] = module
    exec(compile(source, module.__name__, "exec"), module.__dict__)
    return module


def native():
    return {
        "engine": "Mortal",
        "player_id": 0,
        "review": {
            "model_tag": "4.1b",
            "temperature": 0.1,
            "rating": 0.87,
            "total_matches": 8,
            "total_reviewed": 10,
            "kyokus": [
                {
                    "entries": [
                        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.03}]},
                        {"is_equal": False, "actual_index": 0, "details": [{"prob": 0.08}]},
                    ]
                }
            ],
        },
    }


async def pipeline(cls, count, latency, *, bridge=False):
    data = native()
    active = peak = names_active = names_peak = 0

    async def names(row):
        nonlocal names_active, names_peak
        names_active += 1
        names_peak = max(names_peak, names_active)
        await asyncio.sleep(latency)
        names_active -= 1
        return ["测试甲", "测试乙", "测试丙", "测试丁"]

    async def analyze():
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(latency)
        active -= 1

    async def online_submit(key, row, callback):
        await callback()
        await analyze()
        return data, REPORT

    async def bridge_submit(url, seat):
        await analyze()
        return ReviewResult(87, "", REPORT, data)

    async def records(account, n):
        return [
            GameRecord(
                f"bench-{i}",
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
                datetime(2026, 1, 1, 1, tzinfo=UTC) + timedelta(days=i),
                "12",
                [{"account_id": 42, "rank": 1, **({"nickname": "测试甲"} if bridge else {})}],
                {},
            )
            for i in range(n)
        ]

    with tempfile.TemporaryDirectory(prefix="pupulin-mortal-bench-") as directory:
        reviews = SimpleNamespace(
            client=SimpleNamespace(url="http://loopback-only.invalid" if bridge else None),
            workers=4 if bridge else 1,
            submit=bridge_submit,
        )
        online = None if bridge else SimpleNamespace(concurrency=2, submit=online_submit)
        manager = cls(
            Path(directory),
            SimpleNamespace(review_records=records),
            reviews,
            online,
            name_resolver=names if not bridge else None,
        )
        module = sys.modules[cls.__module__]
        with patch.object(module, "render_report", wraps=module.render_report) as renderer:
            job = await manager.create(count, "benchmark", "benchmark", Player(42, "测试"))
            started = time.perf_counter()
            await manager._run(job["id"], "benchmark", "benchmark")
            elapsed = time.perf_counter() - started
            renders = renderer.call_count
        result = manager.load(job["id"], "benchmark", "benchmark")
        assert result["status"] == "done"
        assert all(r["metrics"] == report_metrics(data) for r in result["rows"])
        if not bridge:
            assert all(r.get("player_name") == "测试甲" for r in result["rows"])
        return {
            "seconds": elapsed,
            "analysis_peak": peak,
            "metadata_peak": names_peak,
            "html_renders": renders,
        }


async def http_pool(cls, count):
    connections = requests = 0
    handlers = set()
    body = b'{"status":"done","rating":87}'

    async def handle(reader, writer):
        nonlocal connections, requests
        handlers.add(asyncio.current_task())
        connections += 1
        try:
            while True:
                header = await reader.readuntil(b"\r\n\r\n")
                fields = dict(
                    line.split(b":", 1) for line in header.split(b"\r\n")[1:] if b":" in line
                )
                length = next(
                    (v for k, v in fields.items() if k.lower() == b"content-length"), b"0"
                )
                await reader.readexactly(int(length))
                requests += 1
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = cls(f"http://127.0.0.1:{port}", None)
    started = time.perf_counter()
    try:
        for _ in range(count):
            assert (await client.review("offline-benchmark", 0)).rating == 87
    finally:
        if close := getattr(client, "close", None):
            await close()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*list(handlers))
    return {
        "seconds": time.perf_counter() - started,
        "connections": connections,
        "requests": requests,
    }


async def run(args):
    before_batch = (
        await asyncio.to_thread(previous_module, args.baseline_ref, "batch_review")
    ).BatchReviews
    before_http = (
        await asyncio.to_thread(previous_module, args.baseline_ref, "review")
    ).MortalClient
    baseline_ref = (
        await asyncio.to_thread(
            subprocess.check_output, ["git", "rev-parse", args.baseline_ref], cwd=ROOT, text=True
        )
    ).strip()
    output = {
        "benchmark_kind": "synthetic_offline_not_live_throughput",
        "baseline_ref": baseline_ref,
        "count": args.count,
        "stage_latency_ms": args.stage_ms,
        "rounds": args.rounds,
        "new_mortal_submissions": 0,
        "external_http_requests": 0,
        "submission_interval_unchanged_seconds": 10,
    }
    for name, bridge in (("metadata_pipeline", False), ("self_hosted_bridge_dispatch", True)):
        before, after = [], []
        for _ in range(args.rounds):
            before.append(
                await pipeline(before_batch, args.count, args.stage_ms / 1000, bridge=bridge)
            )
            after.append(
                await pipeline(BatchReviews, args.count, args.stage_ms / 1000, bridge=bridge)
            )
        baseline = statistics.median(r["seconds"] for r in before)
        optimized = statistics.median(r["seconds"] for r in after)
        output[name] = {
            "baseline_seconds": round(baseline, 4),
            "optimized_seconds": round(optimized, 4),
            "speedup": round(baseline / optimized, 2),
            "baseline_peaks": before[-1],
            "optimized_peaks": after[-1],
            "metrics_and_names_unchanged": True,
        }
    # Ensure a process-wide proxy cannot send this loopback benchmark externally.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
    output["http_connections"] = {
        "baseline": await http_pool(before_http, args.count),
        "optimized": await http_pool(MortalClient, args.count),
    }
    changed = (
        "batch_review",
        "batchmortal_worker",
        "online_pool",
        "online_review",
        "plugin",
        "record_names",
        "review",
        "review_cache",
    )
    output["working_source_sha256"] = {
        name: hashlib.sha256((SOURCE_ROOT / f"{name}.py").read_bytes()).hexdigest()
        for name in changed
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="b6314c4")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--stage-ms", type=float, default=20)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path)
    options = parser.parse_args()
    if not 1 <= options.count <= 100 or not 1 <= options.rounds <= 10 or options.stage_ms < 0:
        parser.error("count: 1–100; rounds: 1–10; stage-ms: non-negative")
    asyncio.run(run(options))
