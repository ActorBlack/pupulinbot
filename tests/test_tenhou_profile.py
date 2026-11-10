import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from pupulinbot.tenhou import TenhouClient
from pupulinbot.tenhou_profile import describe, percentile, rank_history

FIXTURES = Path(__file__).parent / "fixtures/tenhou"
CASES = json.loads((FIXTURES / "rank-oracle.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["label"])
def test_rank_reconstruction_matches_public_site_js_oracle(case):
    result = rank_history(case["input"], "A", case["mode"], case["cutoff"])
    assert {k: result.get(k) for k in case["expected"]} == case["expected"]


def test_grade_mismatch_suppresses_pt_and_high_rank():
    case = CASES[0]
    result = rank_history(case["input"], "A", case["mode"], case["cutoff"], live_grade=1)
    assert "pt" not in result and "high_grade" not in result
    assert "不一致" in result["pt_unavailable"]
    assert result["recent"]


def test_percentile_numeric_strings_ties_and_unavailable_data():
    distribution = {"data": ["1", "2", "2", "4"]}
    assert percentile(distribution, 2) == 50
    assert percentile(distribution, 0) == 0
    assert percentile(distribution, 5) == 75
    assert percentile(distribution, float("nan")) is None
    assert percentile({"data": ["bad"]}, 2) is None
    assert percentile({"data": [2, 1]}, 2) is None


def test_tags_have_minimum_samples_and_no_four_player_model_assumption():
    assert describe({"recent": [1]}) == []
    assert describe({"mode": 3, "recent": [1] * 10}) == ["好调"]
    assert "铁壁地藏" in describe({"stats": dict(agariC=0.205, houjuuC=0.10)})
    assert describe({"mode": 3, "stats": dict(agariC=0.205, houjuuC=0.10)}) == []
    assert "段位起伏" not in describe({"trajectory": [16, 17, 16, 17, 16]})
    assert "段位起伏" in describe({"trajectory": [16, 17, 16, 17, 16], "grade_changes_30d": 3})


@pytest.mark.asyncio
async def test_concurrent_fetches_share_requests_but_keep_one_qps_start_spacing():
    starts = []
    completed = []

    async def respond(request):
        starts.append(time.monotonic())
        await asyncio.sleep(1.2)
        completed.append(time.monotonic())
        return httpx.Response(200, json={"ok": True})

    client = TenhouClient(httpx.MockTransport(respond))
    try:
        await asyncio.gather(client._get("/one"), client._get("/two"), client._get("/one"))
        assert len(starts) == 2
        assert starts[1] - starts[0] >= 1.05
        assert starts[1] < completed[0]  # overlap slow responses, no extra QPS
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_profile_uses_matching_hanchan_stats_and_percentile_population():
    game = json.loads((FIXTURES / "live.json").read_text())[0]
    profile = {}
    client = TenhouClient(httpx.MockTransport(lambda r: httpx.Response(200)))

    async def get(path, params=None, **kwargs):
        if path == "/api/phoenix_status.php":
            return {
                "s4": {"agariC": 0.9, "totalrecord": 999},
                "s4_2": {"agariC": 0.2, "houjuuC": 0.15, "totalrecord": 312},
            }
        if path == "/s/phoenix_scale_all.js":
            return {
                "s4": {"agariC": {"min": "500", "data": [".5", ".9"]}},
                "s4_2": {
                    "agariC": {"min": "500", "data": [".1", ".3"]},
                    "houjuuC": {"min": "1000", "data": [".1", ".3"]},
                },
            }
        return {"name": game["players"][0]["name"], "list": []}

    client._get = get
    try:
        await client.profile(game["players"][0], game, destination=profile)
        assert profile["stats"]["agariC"] == 0.2
        assert profile["percentiles"]["agariC"] == 0
        assert profile["percentiles"]["houjuuC"] == 0  # Cohort min is not a player gate.
        assert profile["stats_scope"] == "s4_2"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_disk_snapshot_bootstraps_once_and_then_merges_incremental_rows(tmp_path):
    import copy

    original = json.loads((FIXTURES / "result.json").read_text())
    name = "applePie"
    first = {"name": name, "list": [original]}
    client = TenhouClient(history_directory=tmp_path)
    calls = []

    async def bootstrap(path, params, **kwargs):
        calls.append(params)
        return first

    client._get = bootstrap
    try:
        assert len(await client.history(name)) == 1
        assert calls == [{"name": name, "start": "1"}]
    finally:
        await client.close()
    fresh = TenhouClient(history_directory=tmp_path)
    second = copy.deepcopy(original)
    second["starttime"] += 3600
    second["url"] = second["url"].replace("f946a576", "f946a577")
    changed = dict(original, during=43)

    async def incremental(path, params, **kwargs):
        assert params["start"] == str(original["starttime"] - 7200)
        return {"name": name, "list": [changed, second]}

    fresh._get = incremental
    try:
        rows = await fresh.history(name)
        assert len(rows) == 2 and rows[0]["during"] == 43
        assert rows[1]["starttime"] == second["starttime"]
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_card_cancellation_keeps_background_history_bootstrap(tmp_path):
    row = json.loads((FIXTURES / "result.json").read_text())
    started, release = asyncio.Event(), asyncio.Event()
    client = TenhouClient(history_directory=tmp_path)

    async def get(path, params, **kwargs):
        started.set()
        await release.wait()
        return {"name": "applePie", "list": [row]}

    client._get = get
    caller = asyncio.create_task(client.history("applePie"))
    try:
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert "applePie" in client._history_tasks
        release.set()
        await asyncio.wait_for(client._history_tasks["applePie"], 1)
        assert client._snapshot_path("applePie").exists()
        assert len(client._history_snapshots["applePie"]["list"]) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bootstrap_download_cannot_starve_normal_stats_requests():
    started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        if request.url.path == "/api/listuser.php":
            started.set()
            await release.wait()
        return httpx.Response(200, json={"ok": True})

    client = TenhouClient(httpx.MockTransport(respond))
    task = asyncio.create_task(
        client._get("/api/listuser.php", {"start": "1", "name": "A"}, budget=180)
    )
    second = asyncio.create_task(
        client._get("/api/listuser.php", {"start": "1", "name": "B"}, budget=180)
    )
    try:
        await started.wait()
        normal = await asyncio.wait_for(
            asyncio.gather(client._get("/stat-one"), client._get("/stat-two")), 4
        )
        assert normal == [{"ok": True}, {"ok": True}] and not task.done()
    finally:
        release.set()
        await asyncio.gather(task, second)
        await client.close()


@pytest.mark.asyncio
async def test_public_stats_survive_restart_but_expired_data_is_refetched(tmp_path):
    calls = []

    async def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"s4_2": {"totalrecord": 500}})

    for _ in range(2):
        client = TenhouClient(httpx.MockTransport(respond), history_directory=tmp_path)
        await client._get("/api/phoenix_status.php", {"username": "A", "all": "1"}, ttl=900)
        await client.close()
    assert len(calls) == 1
    cached = next((tmp_path / "public").glob("*.json"))
    data = json.loads(cached.read_text())
    data["fetched_at"] = time.time() - 901
    cached.write_text(json.dumps(data))
    client = TenhouClient(httpx.MockTransport(respond), history_directory=tmp_path)
    try:
        await client._get("/api/phoenix_status.php", {"username": "A", "all": "1"}, ttl=900)
        assert len(calls) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_warm_history_and_first_card_share_one_download(tmp_path):
    from unittest.mock import AsyncMock

    data = {"name": "applePie", "list": []}
    client = TenhouClient(history_directory=tmp_path)
    client._get = AsyncMock(return_value=data)
    try:
        client.warm_history("applePie")
        client.warm_history("applePie")
        assert await client._profile_history("applePie") == data
        client._get.assert_awaited_once()
    finally:
        await client.close()
