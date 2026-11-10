import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from test_review_metadata import reviewed_game

from pupulinbot.batch_chart import table_rows
from pupulinbot.batch_review import BatchReviews
from pupulinbot.batchmortal_worker import fill_riichi_city_form, validate_request
from pupulinbot.native_report import compact_report
from pupulinbot.record_names import RecordNames
from pupulinbot.review import ReviewError, parse_review_link

ID = "dboqr0kj4ak3gig8n0tg"
NAMES = ["玩家一", "玩家二", "Player Three", "玩家四"]


@pytest.mark.parametrize("seat", range(4))
def test_city_id_preserves_seat_and_worker_uses_separate_input_type(seat):
    log = parse_review_link(f"{ID}@{seat}")
    assert (log.source, log.game_id, log.seat) == ("riichi_city", ID, seat)
    assert validate_request({"online_url": log.online_url, "seat": seat}) == f"{ID}@{seat}"
    assert parse_review_link(log.url, 0).url == f"{ID}@0"
    assert parse_review_link(ID).seat == 0


@pytest.mark.parametrize(
    "value", [ID + "@4", ID + "@-1", ID + "@00", "../" + ID + "@0", "https://evil.invalid/" + ID]
)
def test_invalid_city_ids_and_urls_are_rejected(value):
    with pytest.raises(ReviewError):
        parse_review_link(value)


def test_city_form_sets_id_and_target_without_touching_submission_or_verification():
    browser = SimpleNamespace(execute_script=Mock(return_value=True))
    fill_riichi_city_form(browser, f"{ID}@2")
    assert browser.execute_script.call_args.args[1:] == (f"{ID}@2", 2)
    browser.execute_script.return_value = False
    with pytest.raises(RuntimeError, match="一番街"):
        fill_riichi_city_form(browser, f"{ID}@2")


@pytest.mark.asyncio
async def test_city_table_loads_original_names_once_and_keeps_rank_from_review(tmp_path):
    requests = []

    async def response(request):
        requests.append(request)
        assert request.method == "POST" and str(request.url) == "https://rc.honk.li/api/log"
        assert json.loads(request.content) == {"log_id": ID}
        assert "authorization" not in request.headers
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"ref": ID, "ratingc": "PF4", "name": NAMES})

    resolver = RecordNames(transport=httpx.MockTransport(response))

    async def submit(key, row, on_submitted):
        data = reviewed_game()
        data["player_id"] = row["seat"]
        return compact_report(data), "https://mjai.ekyu.moe/report/abcdef0123456789.json"

    online = SimpleNamespace(submit=AsyncMock(side_effect=submit))
    batches = BatchReviews(
        tmp_path,
        None,
        SimpleNamespace(client=SimpleNamespace(url=None)),
        online,
        name_resolver=resolver,
    )
    job = await batches.create_links([f"{ID}@0"], "g", "u", table=True)
    assert [row["source"] for row in job["rows"]] == ["riichi_city"] * 4
    assert [row["player_name"] for row in job["rows"]] == NAMES
    assert len(requests) == 1
    for i, row in enumerate(job["rows"]):
        assert validate_request({"online_url": row["online_url"], "seat": i}) == f"{ID}@{i}"
        await batches._analyze_row(job["id"], "g", "u", i, row)
    saved = batches.load(job["id"], "g", "u")
    assert [row["rank"] for row in saved["rows"]] == [2, 4, 1, 3]
    assert [row["name"] for row in table_rows(saved)] == [NAMES[2], NAMES[0], NAMES[3], NAMES[1]]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_city_rejects_unmatched_or_three_player_metadata():
    log = parse_review_link(ID)
    for payload in (
        {"ref": "anotherid", "ratingc": "PF4", "name": NAMES},
        {"ref": ID, "ratingc": "PF3", "name": NAMES[:3]},
    ):
        resolver = RecordNames(
            transport=httpx.MockTransport(
                lambda request, payload=payload: httpx.Response(200, json=payload)
            )
        )
        with pytest.raises(ReviewError, match="一番街"):
            await resolver({"url": log.url, "uuid": ID, "seat": 0})
