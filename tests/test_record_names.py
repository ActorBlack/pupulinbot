import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from test_batch_review import native_report

from pupulinbot.batch_chart import render_batch_chart
from pupulinbot.batch_review import BatchReviews, seat_nickname
from pupulinbot.record_names import RecordNames, tenhou_names
from pupulinbot.review import ReviewError

LOG = "2026111008gm-0029-0000-e1dc7359"
LINK = f"https://tenhou.net/4/?log={LOG}"
XML = (
    b'<mjloggm><UN n0="%E3%81%97%E3%81%BB%E3%81%AE%E3%83%91%E3%83%91" '
    b'n1="A+B" n2="%E4%B8%AD%E6%96%87" n3="%24bad%5Ccommand%24" /></mjloggm>'
)
NAMES = ["しほのパパ", "A+B", "中文", "$bad\\command$"]
ANONYMOUS = ["Aさん", "Bさん", "Cさん", "Dさん"]


def test_original_xml_decodes_percent_encoding_without_changing_plus():
    assert tenhou_names(XML) == NAMES
    for content in [b"<html/>", b"<!DOCTYPE x><mjloggm/>", b'<!ENTITY x "bad"><mjloggm/>']:
        with pytest.raises(ReviewError):
            tenhou_names(content)


@pytest.mark.asyncio
async def test_table_nicknames_are_loaded_once_and_original_names_survive_review_results(tmp_path):
    calls = []

    async def response(request):
        calls.append(request)
        assert str(request.url) == f"https://tenhou.net/0/log/?{LOG}"
        assert "authorization" not in request.headers
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=XML)

    resolver = RecordNames(transport=httpx.MockTransport(response))
    data = native_report()
    data["batchmortal_artifacts"] = {"playerNames": ANONYMOUS}
    online = SimpleNamespace(
        submit=AsyncMock(
            return_value=(
                data,
                "https://mjai.ekyu.moe/killerducky/?data=/report/abcdef0123456789.json",
            )
        )
    )
    batches = BatchReviews(
        tmp_path,
        None,
        SimpleNamespace(client=SimpleNamespace(url=None)),
        online,
        name_resolver=resolver,
    )
    job = await batches.create_links([LINK], "g", "u", table=True)
    assert [seat_nickname(r) for r in job["rows"]] == NAMES
    assert len(calls) == 1
    await batches._analyze_row(job["id"], "g", "u", 0, job["rows"][0])
    job = batches.load(job["id"], "g", "u")
    assert seat_nickname(job["rows"][0]) == NAMES[0]
    assert Image.open(io.BytesIO(render_batch_chart(job))).format == "PNG"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_refresh_existing_report_only_changes_names_not_results_or_submission(tmp_path):
    batches = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await batches.create_links([LINK], "g", "u", table=True)
    for i, row in enumerate(job["rows"]):
        row.update(
            player_name=ANONYMOUS[i],
            backend_artifacts={"playerNames": ANONYMOUS},
            status="done",
            metrics={"rating": 80 + i},
        )
        assert seat_nickname(row) == "昵称读取中"
    job["status"] = "done"
    batches.save(job)
    batches.name_resolver = AsyncMock(return_value=NAMES)
    updated = await batches.refresh_names(job["id"], "g", "u")
    assert [seat_nickname(r) for r in updated["rows"]] == NAMES
    assert [r["metrics"]["rating"] for r in updated["rows"]] == [80, 81, 82, 83]
    assert updated["status"] == "done"


@pytest.mark.asyncio
async def test_missing_tenhou_names_block_output_and_only_retry_metadata(tmp_path):
    from datetime import datetime

    from pupulinbot.batch_export import export_rows

    online = SimpleNamespace(submit=AsyncMock())
    batches = BatchReviews(
        tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)), online
    )
    job = await batches.create_links([LINK], "g", "u", table=True)
    for row in job["rows"]:
        row.update(status="done", metrics={"rating": 88})
    job["status"] = "done"
    batches.save(job)
    batches.name_resolver = AsyncMock(return_value=[])
    with pytest.raises(ReviewError, match="昵称"):
        await batches.refresh_names(job["id"], "g", "u")
    queued = batches.load(job["id"], "g", "u")
    assert queued["status"] == "retry_wait" and queued["retry_phase"] == "names"
    for render in (render_batch_chart, export_rows):
        with pytest.raises(ReviewError, match="昵称"):
            render(queued)
    batches.name_resolver.return_value = NAMES
    await batches.retry_due(datetime.fromisoformat(queued["next_retry_at"]))
    await batches.tasks[job["id"]]
    final = batches.load(job["id"], "g", "u")
    assert final["status"] == "done"
    assert [seat_nickname(r) for r in final["rows"]] == NAMES
    assert [r["metrics"]["rating"] for r in final["rows"]] == [88] * 4
    online.submit.assert_not_awaited()
    assert Image.open(io.BytesIO(render_batch_chart(final))).format == "PNG"


@pytest.mark.asyncio
async def test_tenhou_xml_transient_download_is_retried_before_returning_names():
    calls = []

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("temporary")
        return httpx.Response(200, content=XML)

    resolver = RecordNames(transport=httpx.MockTransport(respond))
    assert await resolver({"uuid": LOG, "url": LINK, "seat": 0}) == NAMES
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_empty_success_response_retries_and_uses_identifiable_user_agent():
    calls = []

    async def respond(request):
        calls.append(request)
        assert request.headers["user-agent"] == "PupulinBot/1.0"
        return httpx.Response(200, content=b"" if len(calls) == 1 else XML)

    resolver = RecordNames(transport=httpx.MockTransport(respond))
    assert await resolver({"uuid": LOG, "url": LINK, "seat": 0}) == NAMES
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_verified_archived_names_survive_restart_without_network(tmp_path):
    import json

    archive = tmp_path / "archive" / "old"
    archive.mkdir(parents=True)
    rows = [
        {
            "uuid": LOG,
            "source": "tenhou",
            "seat": i,
            "player_name": name,
            "player_name_source": "original",
        }
        for i, name in enumerate(NAMES)
    ]
    (archive / "job.json").write_text(json.dumps({"rows": rows}))

    def offline(request):
        raise AssertionError("Verified historical nicknames should avoid HTTP")

    resolver = RecordNames(archive_directory=archive.parent, transport=httpx.MockTransport(offline))
    assert await resolver({"uuid": LOG, "url": LINK, "seat": 3}) == NAMES


@pytest.mark.asyncio
async def test_filling_names_preserves_user_paused_state_and_does_not_restart_analysis(tmp_path):
    batches = BatchReviews(tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)))
    job = await batches.create_links([LINK], "g", "u", table=True)
    job.update(status="paused", retry_phase="names")
    for row in job["rows"]:
        row.update(status="done", metrics={"rating": 88})
    batches.save(job)
    batches.name_resolver = AsyncMock(return_value=NAMES)
    repaired = await batches.refresh_names(job["id"], "g", "u")
    assert repaired["status"] == "paused"
    assert "next_retry_at" not in repaired and not batches.tasks
    assert [seat_nickname(r) for r in repaired["rows"]] == NAMES


@pytest.mark.asyncio
async def test_majsoul_link_recovers_original_names_once_via_shared_api_client():
    from datetime import UTC, datetime, timedelta

    from pupulinbot.models import GameRecord

    game_id = "261109-e1da5f5a-f5ff-4a3c-a11b-351b5660a489"
    link = f"https://game.maj-soul.com/1/?paipu={game_id}_a255244981"
    names = ["带眼", "真名二", "真名三", "真名四"]
    players = [dict(account_id=23822448 + i, seat=i, nickname=n) for i, n in enumerate(names)]
    game = GameRecord(
        game_id,
        datetime(2026, 11, 9, tzinfo=UTC),
        datetime(2026, 11, 9, 1, tzinfo=UTC),
        "12",
        list(reversed(players)),
        {},
    )
    client = SimpleNamespace(query_history=AsyncMock(return_value=([game], False)))
    resolver = RecordNames(clients={4: client})
    assert (
        await asyncio.gather(*(resolver(dict(uuid=game_id, url=link, seat=i)) for i in range(4)))
        == [names] * 4
    )
    day = datetime(2026, 11, 9, tzinfo=UTC)
    client.query_history.assert_awaited_once_with(
        23822448, 200, start=day - timedelta(days=1), end=day + timedelta(days=2)
    )


@pytest.mark.asyncio
async def test_majsoul_name_lookup_rejects_wrong_game_and_invalid_perspective():
    from datetime import UTC, datetime

    from pupulinbot.models import GameRecord

    game_id = "261109-e1da5f5a-f5ff-4a3c-a11b-351b5660a489"
    other = GameRecord("another-game", datetime.now(UTC), datetime.now(UTC), "12", [], {})
    client = SimpleNamespace(query_history=AsyncMock(return_value=([other], False)))
    resolver = RecordNames(clients={4: client})
    row = dict(uuid=game_id, url=f"https://game.maj-soul.com/1/?paipu={game_id}_a255244981", seat=0)
    with pytest.raises(ReviewError, match="未找到该局"):
        await resolver(row)
    client.query_history.reset_mock()
    with pytest.raises(ValueError, match="视角"):
        await resolver({**row, "url": row["url"].replace("255244981", "255244982")})
    client.query_history.assert_not_awaited()


def test_majsoul_perspective_encoding_round_trips_and_is_distinct_from_friend_code():
    from pupulinbot.ms_ids import account_to_perspective, perspective_to_account

    assert perspective_to_account(255244981) == 23822448
    for account in (1, 9934016, 23822448, 2147483647):
        assert perspective_to_account(account_to_perspective(account)) == account
    for invalid in (0, -1, True, 1 << 35):
        with pytest.raises(ValueError):
            perspective_to_account(invalid)


@pytest.mark.asyncio
async def test_original_tenhou_final_scores_supply_placement_independent_of_rating():
    content = XML.replace(
        b"</mjloggm>", b'<AGARI owari="200,0,400,40,200,-30,200,-10" /></mjloggm>'
    )
    calls = []

    async def response(request):
        calls.append(request)
        return httpx.Response(200, content=content)

    resolver = RecordNames(transport=httpx.MockTransport(response))
    row = dict(url=LINK, uuid=LOG, seat=0)
    assert await resolver(row) == NAMES
    results = await asyncio.gather(*(resolver.seat_result({**row, "seat": i}) for i in range(4)))
    assert [r["rank"] for r in results] == [2, 1, 4, 3]
    assert len(calls) == 1
