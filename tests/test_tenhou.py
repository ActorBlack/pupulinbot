import copy
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebot.adapters.onebot.v11 import Message
from PIL import Image

from pupulinbot.db import Database
from pupulinbot.qq_delivery import send_verified_image
from pupulinbot.tenhou import (
    TenhouClient,
    TenhouError,
    format_result,
    grade_name,
    history_rows,
    table_scores,
)
from pupulinbot.tenhou_card import render_start
from pupulinbot.tenhou_monitor import TenhouMonitor, TenhouStartNotice, TenhouStore

FIXTURES = Path(__file__).parent / "fixtures/tenhou"


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def test_real_user_example_points_duration_timezone_and_link():
    row = fixture("result")
    assert table_scores(row) == [49200, 45600, 9700, -4500]
    assert format_result(row) == (
        "四鳳南喰赤\n2026-11-08 05:28, 对局时长: 42分钟\n"
        "applePie(49200)\nbipsd(45600)\nアフリカ(9700)\nてりうむ(-4500)\n\n"
        "https://tenhou.net/4/?log=2026110806gm-00a9-0000-f946a576"
    )


def test_unknown_or_special_rules_never_mislabel_result_points_as_table_scores():
    row = fixture("result")
    row["lobby"] = 12345
    assert table_scores(row) is None
    assert "结算点" in format_result(row) and "applePie(+59.2)" in format_result(row)
    row.pop("lobby")
    row["player1ptr"] = "100"
    assert table_scores(row) is None
    row["playernum"] = 3
    assert table_scores(row) is None


def test_public_history_validates_names_and_numeric_data():
    row = fixture("result")
    assert history_rows({"name": "applePie", "list": [row]}, "applePie") == [row]
    for data in [
        {"name": "wrong"},
        {"name": "applePie", "list": "bad"},
        {"name": "applePie", "list": [dict(row, player1ptr="nan")]},
    ]:
        with pytest.raises(TenhouError):
            history_rows(data, "applePie")


def test_card_has_full_and_missing_data_fallback_and_long_names():
    game = fixture("live")[0]
    profiles = copy.deepcopy(game["players"])
    profiles[0].update(stats=fixture("profile")["s4"], recent=[1, 3, 2, 4], count=2545)
    profiles[1]["name"] = "长名称" * 20
    image = Image.open(io.BytesIO(render_start(game, profiles)))
    assert image.size == (1080, 790)
    assert grade_name(18) == "九段" and grade_name(16) == "七段"


@pytest.mark.asyncio
async def test_client_uses_only_public_host_no_bearer_and_honors_cache():
    calls = []

    async def respond(request):
        calls.append(request)
        assert request.url.host in {"nodocchi.moe", "www.nodocchi.moe"}
        assert "authorization" not in request.headers and "x-cap-token" not in request.headers
        return httpx.Response(200, json=fixture("live"))

    client = TenhouClient(httpx.MockTransport(respond))
    assert await client.live() == await client.live()
    assert len(calls) == 1
    await client.close()


@pytest.mark.asyncio
async def test_stale_and_empty_live_sources_are_errors_not_no_game():
    for body, headers in [
        (b"", {}),
        (json.dumps(fixture("live")).encode(), {"last-modified": "Sun, 01 Nov 2026 00:00:00 GMT"}),
    ]:
        client = TenhouClient(
            httpx.MockTransport(
                lambda r, body=body, headers=headers: httpx.Response(
                    200, content=body, headers=headers
                )
            )
        )
        with pytest.raises(TenhouError):
            await client.live()
        await client.close()


@pytest.fixture
async def setup(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.initialize()
    store = TenhouStore(db)
    await store.initialize()
    return db, store


@pytest.mark.asyncio
async def test_group_dedup_restart_retry_and_old_history_exclusion(setup):
    db, store = setup
    row = fixture("result")
    # This row finishes after subscribing, so it is eligible once it is indexed.
    row["starttime"] = int(time.time()) - 60
    old = dict(row, starttime=int(time.time()) - 10000)
    for group, owner in [("test", "a"), ("test", "b"), ("other", "a")]:
        await store.add(group, owner, "applePie")
    client = SimpleNamespace(
        live=AsyncMock(return_value=[]), history=AsyncMock(return_value=[old, row])
    )
    monitor = TenhouMonitor(store, client)
    send = AsyncMock(side_effect=[RuntimeError("network"), None, None])
    await monitor.poll(send)
    # a's failure is retried by b in same group, independent other-group delivery.
    assert send.await_count == 3
    send.reset_mock(side_effect=True)
    await TenhouMonitor(store, client).poll(send)
    send.assert_not_awaited()
    assert client.history.await_count == 2  # once per player per poll, never per group
    assert len(await store.subscriptions()) == 3
    assert await store.remove("test", "a") == 1
    assert len(await store.subscriptions("test")) == 1
    assert await db.subscriptions() == []


@pytest.mark.asyncio
async def test_live_failure_does_not_block_finish_and_history_failure_keeps_cursor(setup):
    _, store = setup
    await store.add("test", "a", "applePie")
    client = SimpleNamespace(
        live=AsyncMock(side_effect=TenhouError("live down")),
        history=AsyncMock(side_effect=TenhouError("history down")),
    )
    monitor = TenhouMonitor(store, client)
    original = (await store.subscriptions())[0]["cursor"]
    await monitor.poll(AsyncMock())
    sub = (await store.subscriptions())[0]
    assert sub["cursor"] == original and sub["error"] == "history down"
    row = fixture("result")
    row["starttime"] = int(time.time()) - 60
    client.history.side_effect = None
    client.history.return_value = [row]
    send = AsyncMock()
    await monitor.poll(send)
    send.assert_awaited_once()
    assert monitor.live_error == "live down"


@pytest.mark.asyncio
async def test_start_is_persisted_retried_after_disappearing_and_group_scoped(setup):
    _, store = setup
    game = fixture("live")[0]
    for owner in ["a", "b"]:
        await store.add("test", owner, game["players"][0]["name"])
    client = SimpleNamespace(
        live=AsyncMock(return_value=[game]), history=AsyncMock(return_value=[])
    )
    monitor = TenhouMonitor(store, client)
    monitor.start_message = AsyncMock(return_value=b"png")
    await monitor.poll(AsyncMock(side_effect=RuntimeError("send failed")))
    assert len(await store.pending()) == 1
    client.live.return_value = []
    send = AsyncMock()
    await monitor.poll(send)
    send.assert_awaited_once_with(
        "test",
        TenhouStartNotice(
            b"png",
            "四鳳南喰赤 · 对局开始\n" + " / ".join(p["name"] for p in game["players"]),
        ),
    )
    assert await store.pending() == []
    client.live.return_value = [game]
    await monitor.poll(send)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_disabled_subscriptions_never_receive_queued_start(setup):
    _, store = setup
    game = fixture("live")[0]
    await store.enqueue("test", game)
    await store.add("other", "a", "applePie")
    client = SimpleNamespace(live=AsyncMock(return_value=[]), history=AsyncMock(return_value=[]))
    monitor = TenhouMonitor(store, client)
    send = AsyncMock()
    await monitor.poll(send)
    send.assert_not_awaited()
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_empty_qq_ack_keeps_start_pending_until_image_is_verified(setup):
    db, store = setup
    game = fixture("live")[0]
    await store.add("42", "a", game["players"][0]["name"])
    client = SimpleNamespace(
        live=AsyncMock(return_value=[game]), history=AsyncMock(return_value=[])
    )
    monitor = TenhouMonitor(store, client)
    monitor.start_message = AsyncMock(return_value=b"png")
    bot = SimpleNamespace(
        send_group_msg=AsyncMock(return_value={"message_id": 123}),
        get_msg=AsyncMock(return_value={"message": []}),
    )

    async def send(group, notice):
        await send_verified_image(bot, group, Message())

    with patch("pupulinbot.qq_delivery.asyncio.sleep", new=AsyncMock()):
        await monitor.poll(send)
    assert len(await store.pending()) == 1
    async with db.connect() as conn:
        row = await (
            await conn.execute("SELECT COUNT(*) FROM game_events WHERE status='sent'")
        ).fetchone()
        assert row[0] == 0
    client.live.return_value = []
    bot.get_msg.return_value = {"message": [{"type": "image", "data": {"file": "image.png"}}]}
    await monitor.poll(send)
    assert await store.pending() == []
    await monitor.poll(send)
    assert bot.send_group_msg.await_count == 2


@pytest.mark.asyncio
async def test_crashed_start_lease_does_not_remove_pending_retry(setup):
    db, store = setup
    game = fixture("live")[0]
    await store.add("test", "a", game["players"][0]["name"])
    await store.enqueue("test", game)
    key = f"tenhou:wg:{game['info']['id']}:{game['info']['starttime']}"
    assert await db.claim_event(key, "tenhou:group:test", "start")
    client = SimpleNamespace(live=AsyncMock(return_value=[]), history=AsyncMock(return_value=[]))
    monitor = TenhouMonitor(store, client)
    monitor.start_message = AsyncMock(return_value=b"png")
    send = AsyncMock()
    await monitor.poll(send)
    send.assert_not_awaited()
    assert len(await store.pending()) == 1
    async with db.connect() as conn:
        await conn.execute("UPDATE game_events SET lease_until=0")
        await conn.commit()
    await monitor.poll(send)
    send.assert_awaited_once()
    assert await store.pending() == []


@pytest.mark.asyncio
async def test_transient_read_timeout_is_retried_once_without_auth():
    count = 0

    async def respond(request):
        nonlocal count
        count += 1
        if count == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, json=fixture("live"))

    client = TenhouClient(httpx.MockTransport(respond))
    assert await client.live() == fixture("live")
    assert count == 2
    await client.close()


@pytest.mark.asyncio
async def test_real_feed_clock_lead_and_ron2_tables_are_supported():
    game = fixture("live")[0]
    game["info"]["starttime"] = int(time.time()) + 300
    ron2 = copy.deepcopy(game)
    ron2["info"].update(ron2=1, playerlevel=9)
    client = TenhouClient(httpx.MockTransport(lambda r: httpx.Response(200, json=[game, ron2])))
    assert await client.live() == [game]
    await client.close()


def test_private_lobby_without_duration_or_link_remains_queryable():
    row = fixture("result")
    row.pop("during")
    row.pop("url")
    row.update(sctype="a", lobby="6337")
    assert history_rows({"name": "applePie", "list": [row]}, "applePie") == [row]
    text = format_result(row)
    assert "时长: 未提供" in text and "未提供牌谱链接" in text and "结算点" in text


def test_six_to_ten_dan_have_distinct_badge_and_history_backgrounds():
    game = fixture("live")[0]
    backgrounds = set()
    for grade in range(15, 20):
        profiles = copy.deepcopy(game["players"])
        profiles[0].update(grade=grade, trajectory=list(range(15, 20)))
        rendered = Image.open(io.BytesIO(render_start(game, profiles)))
        backgrounds.add(rendered.getpixel((24 + 27, 122 + 73)))
        tiles = [rendered.getpixel((24 + 282 + j * 20 + 1, 122 + 134)) for j in range(5)]
        assert len(set(tiles)) == 5
    assert len(backgrounds) == 5


@pytest.mark.asyncio
async def test_incomplete_card_is_refreshed_when_missing_history_arrives():
    game = fixture("live")[0]
    ready = False

    async def enrich(player, game, destination):
        destination.update(stats_loaded=True, scale_loaded=True)
        if ready:
            destination.update(history_loaded=True, recent=[1, 2, 3, 4], trajectory=[15, 16, 17])

    client = SimpleNamespace(profile=AsyncMock(side_effect=enrich))
    monitor = TenhouMonitor(None, client)
    first = await monitor.start_message(game)
    identity = (game["info"]["id"], game["info"]["starttime"])
    monitor._cards[identity] = (first, 0)
    ready = True
    second = await monitor.start_message(game)
    assert first != second and all(p["history_loaded"] for p in monitor.last_profiles)
    count = client.profile.await_count
    assert await monitor.start_message(game) == second
    assert client.profile.await_count == count


@pytest.mark.asyncio
async def test_only_configured_manager_can_change_tenhou_group_list(setup):
    from pupulinbot.tenhou_commands import manage_watch

    _, store = setup
    await store.add("test", "previous-member", "applePie")
    await store.add("other", "previous-member", "applePie")
    client = SimpleNamespace(history=AsyncMock(return_value=[fixture("result")]))
    original = await store.subscriptions()
    for caller in ("member", "admin", "owner"):
        for command in ("on bipsd", "off applePie", "off all"):
            with pytest.raises(TenhouError, match="负责人"):
                await manage_watch(store, client, command, caller, "test", manager="285843647")
    with pytest.raises(TenhouError, match="负责人"):
        await manage_watch(store, client, "off all", "285843647", "test")
    assert await store.subscriptions() == original
    client.history.assert_not_awaited()
    assert "已监控" in await manage_watch(
        store, client, "on applePie", "285843647", "test", manager="285843647"
    )
    assert await store.subscriptions() == original
    client.history.assert_not_awaited()
    assert "1 项" in await manage_watch(
        store, client, "off applePie", "285843647", "test", manager="285843647"
    )
    assert not await store.subscriptions("test")
    assert len(await store.subscriptions("other")) == 1
    await manage_watch(store, client, "on bipsd", "285843647", "test", manager="285843647")
    assert (await store.subscriptions("test"))[0]["owner_id"] == "285843647"
    await manage_watch(store, client, "off all", "285843647", "test", manager="285843647")
    assert not await store.subscriptions("test")
    assert len(await store.subscriptions("other")) == 1
