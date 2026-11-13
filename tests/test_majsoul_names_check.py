import asyncio
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from pupulinbot.batch_chart import render_batch_chart
from pupulinbot.batch_export import export_rows
from pupulinbot.batch_review import BatchReviews, seat_nickname
from pupulinbot.paipuya import PaipuyaClient, PaipuyaError
from pupulinbot.paipuya_auth import PaipuyaAccess
from pupulinbot.record_names import RecordNames
from pupulinbot.review import ReviewError

UUID = "261109-7fc25847-a3f7-4e56-b460-d234d2475904"
LINK = f"https://game.maj-soul.com/1/?paipu={UUID}"
NAMES = ["东家真名", "南家真名", "西家真名", "北家真名"]
START = int(datetime(2026, 11, 9, 1, tzinfo=UTC).timestamp())


def record(uuid=UUID, mode=12, start=START):
    return dict(
        uuid=uuid,
        modeId=mode,
        startTime=start,
        endTime=start + 1800,
        players=[
            dict(accountId=42 + i, seat=i, nickname=name, score=40000 - i * 10000)
            for i, name in enumerate(NAMES)
        ],
    )


def row(seat=0):
    return dict(uuid=UUID, source="majsoul", url=LINK, seat=seat)


def official_head(uuid=UUID):
    return {
        "uuid": uuid,
        "end_time": START + 1800,
        "result_seats": [2, 3, 0, 1],
        "accounts": [{"account_id": 42 + i, "seat": i, "nickname": NAMES[i]} for i in (3, 2, 1, 0)],
    }


async def test_official_header_is_shared_fallback_after_paipuya_misses():
    browser = SimpleNamespace(record_head=AsyncMock(return_value=official_head()))
    paipuya = SimpleNamespace(find_game=AsyncMock(return_value=None))
    resolver = RecordNames(clients={4: paipuya}, majsoul_browser=browser)
    assert await asyncio.gather(*(resolver(row(i)) for i in range(4))) == [NAMES] * 4
    browser.record_head.assert_awaited_once_with(LINK, UUID)
    assert resolver.name_check(row())["source"] == "majsoul_official"
    paipuya.find_game.assert_awaited_once_with(UUID)
    assert [await resolver.seat_result(row(i)) for i in range(4)] == [
        dict(name=name, rank=rank) for name, rank in zip(NAMES, [3, 4, 1, 2], strict=True)
    ]


@pytest.mark.parametrize(
    "head", [None, official_head("wrong"), {**official_head(), "accounts": []}]
)
async def test_verified_paipuya_skips_official_browser(head):
    browser = SimpleNamespace(record_head=AsyncMock(return_value=head))
    paipuya = SimpleNamespace(find_game=AsyncMock(return_value=PaipuyaClient._record(record())))
    resolver = RecordNames(clients={4: paipuya}, majsoul_browser=browser)
    assert await resolver(row()) == NAMES
    paipuya.find_game.assert_awaited_once_with(UUID)
    assert resolver.name_check(row())["source"] == "paipuya"
    browser.record_head.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [PaipuyaError("timeout"), TimeoutError(), ReviewError("incomplete")]
)
async def test_paipuya_failure_uses_official_fallback(failure):
    browser = SimpleNamespace(record_head=AsyncMock(return_value=official_head()))
    client = SimpleNamespace(find_game=AsyncMock(side_effect=failure))
    resolver = RecordNames(clients={4: client}, majsoul_browser=browser)
    assert await resolver(row()) == NAMES
    browser.record_head.assert_awaited_once_with(LINK, UUID)
    assert resolver.name_check(row())["source"] == "majsoul_official"


def masked_record():
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    number = (START << 32) | int("7fc25847", 16)
    identifier = ""
    while number:
        number, digit = divmod(number, 62)
        identifier = alphabet[digit] + identifier
    return {**record(), "_id": identifier, "uuid": identifier, "_masked": True}


@pytest.mark.asyncio
async def test_masked_uuid_uses_candidate_then_verifies_complete_redirect_without_following():
    masked = masked_record()
    access = PaipuyaAccess("test-key")
    access.gate.wait = AsyncMock()
    seen = []

    def reply(request):
        seen.append(request)
        assert request.url.host == "5-data.amae-koromo.com"
        assert request.headers["Authorization"] == "Bearer test-key"
        if len(seen) == 1:
            assert "/games/" in request.url.path
            return httpx.Response(200, json=[masked])
        assert request.url.path.endswith("/view_game/1/12/" + masked["_id"])
        return httpx.Response(302, headers={"Location": LINK + "_a255244981"})

    client = PaipuyaClient("https://5-data.amae-koromo.com/api/v2/pl4", access=access)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reply), follow_redirects=False
    ) as http:
        client._http = http
        found = await client.find_game(UUID)
    assert found.uuid == UUID and [p["nickname"] for p in found.players] == NAMES
    assert found.raw["_uuid_verified_by"] == "view_game"
    assert len(seen) == 2 and access.gate.wait.await_count == 2


@pytest.mark.asyncio
async def test_prefix_collision_is_not_enough_to_claim_names():
    masked = masked_record()
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(side_effect=[[masked], LINK[:-1] + "5", [], [], [], [], [], []])
    assert await client.find_game(UUID) is None
    assert client._get.await_args_list[1].kwargs == {"redirect_location": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://evil.invalid/?paipu=" + UUID,
        "https://game.maj-soul.com/1/",
        "http://game.maj-soul.com/1/?paipu=" + UUID,
    ],
)
async def test_bad_redirect_does_not_verify_or_follow(location):
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(side_effect=[[masked_record()], location])
    with pytest.raises(PaipuyaError, match="跳转格式异常"):
        await client.find_game(UUID)


def test_masked_candidate_checks_timestamp_and_uuid_prefix():
    item = masked_record()
    assert PaipuyaClient._masked_candidate(PaipuyaClient._record(item), UUID)
    assert not PaipuyaClient._masked_candidate(
        PaipuyaClient._record({**item, "startTime": START + 1}), UUID
    )
    assert not PaipuyaClient._masked_candidate(
        PaipuyaClient._record(item), UUID.replace("7fc25847", "7fc25848")
    )


@pytest.mark.asyncio
async def test_game_listing_paginates_short_pages_and_checks_full_uuid():
    client = PaipuyaClient("https://example.invalid/api/v2/pl4")
    first = record(uuid=UUID[:-1] + "5", start=START + 3600)
    client._get = AsyncMock(side_effect=[[first], [], [], [], [], [], [record()]])
    found = await client.find_game(UUID)
    assert found.uuid == UUID
    calls = client._get.await_args_list
    assert [c.args[1]["mode"] for c in calls] == [12, 16, 9, 11, 15, 8, 12]
    assert calls[-1].args[0].split("/")[1] == str((START + 3600) * 1000)
    assert calls[0].args[1]["limit"] == 500


@pytest.mark.asyncio
async def test_only_exhausted_all_rooms_means_not_found():
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(return_value=[])
    assert await client.find_game(UUID) is None
    assert client._get.await_count == 6


@pytest.mark.asyncio
async def test_query_budget_is_unverified_and_next_attempt_continues_other_rooms():
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(side_effect=[[record(uuid=UUID[:-1] + "5")], [record(mode=16)]])
    with pytest.raises(PaipuyaError, match="尚未查完"):
        await client.find_game(UUID, max_pages=1)
    assert (await client.find_game(UUID, max_pages=1)).mode == "16"
    assert client._get.await_args_list[-1].args[1]["mode"] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response", [{"error": "bad"}, [record(mode=9)], [record(start=START + 86400)]]
)
async def test_bad_listing_is_not_treated_as_no_names(response):
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(return_value=response)
    with pytest.raises(PaipuyaError):
        await client.find_game(UUID)


@pytest.mark.asyncio
async def test_repeating_page_is_not_silently_skipped():
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock(
        side_effect=[[record(uuid="other")], [], [], [], [], [], [record(uuid="other")]]
    )
    with pytest.raises(PaipuyaError, match="时间范围异常"):
        await client.find_game(UUID)


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid", ["261332-abcdefgh", "no-date-abcdefgh", "261109-../../bad"])
async def test_bad_identifiers_are_not_sent_to_api(uuid):
    client = PaipuyaClient("https://example.invalid")
    client._get = AsyncMock()
    with pytest.raises(PaipuyaError, match="日期"):
        await client.find_game(uuid)
    client._get.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_perspective_table_loads_once_and_uses_original_seat_order():
    game = PaipuyaClient._record(record())
    game.players.reverse()
    client = SimpleNamespace(find_game=AsyncMock(return_value=game), query_history=AsyncMock())
    resolver = RecordNames(clients={4: client})
    assert await asyncio.gather(*(resolver(row(i)) for i in range(4))) == [NAMES] * 4
    client.find_game.assert_awaited_once_with(UUID)
    client.query_history.assert_not_awaited()
    assert [await resolver.seat_result(row(i)) for i in range(4)] == [
        dict(name=n, rank=i + 1) for i, n in enumerate(NAMES)
    ]


@pytest.mark.asyncio
async def test_capped_or_stale_player_history_rechecks_global_list():
    game = PaipuyaClient._record(record())
    client = SimpleNamespace(
        query_history=AsyncMock(return_value=([], True)), find_game=AsyncMock(return_value=game)
    )
    resolver = RecordNames(clients={4: client})
    assert await resolver({**row(), "url": LINK + "_a255244981"}) == NAMES
    client.find_game.assert_awaited_once_with(UUID)


@pytest.mark.asyncio
async def test_checked_missing_names_can_render_but_network_errors_cannot(tmp_path):
    client = SimpleNamespace(find_game=AsyncMock(side_effect=PaipuyaError("timeout")))
    resolver = RecordNames(clients={4: client})
    manager = BatchReviews(
        tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)), name_resolver=resolver
    )
    job = await manager.create_links([LINK], "g", "u", table=True)
    for r in job["rows"]:
        r.update(
            status="done",
            metrics={"rating": 88},
            backend_artifacts={"playerNames": ["Aさん", "Bさん", "Cさん", "Dさん"]},
        )
    job["status"] = "done"
    manager.save(job)
    for render in (render_batch_chart, export_rows):
        with pytest.raises(ReviewError, match="牌谱屋复核"):
            render(job)
    assert seat_nickname(job["rows"][0]) == "昵称核验中"
    client.find_game.side_effect = None
    client.find_game.return_value = None
    checked = await manager.refresh_names(job["id"], "g", "u")
    assert checked["status"] == "done"
    assert all(r["name_check"]["status"] == "not_found" for r in checked["rows"])
    assert all(seat_nickname(r) == "" for r in checked["rows"])
    assert Image.open(io.BytesIO(render_batch_chart(checked))).format == "PNG"
    # A later timeout invalidates old negative proof; do not output blanks again.
    client.find_game.side_effect = PaipuyaError("timeout")
    with pytest.raises(ReviewError, match="牌谱屋复核"):
        await manager.refresh_names(job["id"], "g", "u")
    queued = manager.load(job["id"], "g", "u")
    assert queued["retry_phase"] == "names" and queued["status"] == "retry_wait"
    assert not any(r.get("name_check") for r in queued["rows"])


@pytest.mark.asyncio
async def test_done_task_repairs_names_without_resubmitting_or_changing_scores(tmp_path):
    manager = BatchReviews(
        tmp_path,
        None,
        SimpleNamespace(client=SimpleNamespace(url=None)),
        SimpleNamespace(submit=AsyncMock()),
    )
    job = await manager.create_links([LINK], "g", "u", table=True)
    for r in job["rows"]:
        r.update(
            status="done",
            metrics={"rating": 87 + r["seat"]},
            rank=4 - r["seat"],
            rank_source="mortal_mjai",
        )
    job["status"] = "done"
    manager.save(job)
    manager.name_resolver = RecordNames(
        clients={
            4: SimpleNamespace(find_game=AsyncMock(return_value=PaipuyaClient._record(record())))
        }
    )
    updated = await manager.refresh_names(job["id"], "g", "u")
    assert [seat_nickname(r) for r in updated["rows"]] == NAMES
    assert [r["metrics"]["rating"] for r in updated["rows"]] == [87, 88, 89, 90]
    assert [r["rank"] for r in updated["rows"]] == [4, 3, 2, 1]
    manager.online.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_source_names_allow_only_confirmed_missing_seat(tmp_path):
    game = PaipuyaClient._record(record())
    game.players[2]["nickname"] = " "
    manager = BatchReviews(
        tmp_path,
        None,
        SimpleNamespace(client=SimpleNamespace(url=None)),
        name_resolver=RecordNames(
            clients={4: SimpleNamespace(find_game=AsyncMock(return_value=game))}
        ),
    )
    job = await manager.create_links([LINK], "g", "u", table=True)
    assert [seat_nickname(r) for r in job["rows"]] == [*NAMES[:2], "", NAMES[3]]
    assert job["rows"][2]["name_check"]["status"] == "incomplete"
    assert Image.open(io.BytesIO(render_batch_chart(job))).format == "PNG"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_lookup():
    started, finish = asyncio.Event(), asyncio.Event()

    async def find(uuid):
        started.set()
        await finish.wait()
        return PaipuyaClient._record(record())

    resolver = RecordNames(clients={4: SimpleNamespace(find_game=AsyncMock(side_effect=find))})
    first = asyncio.create_task(resolver(row()))
    await started.wait()
    second = asyncio.create_task(resolver(row(1)))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    finish.set()
    assert await second == NAMES
