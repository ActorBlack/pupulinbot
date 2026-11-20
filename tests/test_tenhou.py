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
    format_start_players,
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
        "applePie(49200)\nbipsd(45600)\nアフリカ(9700)\nてりうむ(-4500)\n"
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
    failed = False

    async def fail_test_group_once(group, message):
        nonlocal failed
        if group == "test" and not failed:
            failed = True
            raise RuntimeError("network")

    send = AsyncMock(side_effect=fail_test_group_once)
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
@pytest.mark.parametrize(("level", "room"), [(0, "般"), (1, "上"), (2, "特"), (3, "鳳")])
async def test_start_is_persisted_retried_after_disappearing_and_group_scoped(setup, level, room):
    _, store = setup
    game = fixture("live")[0]
    game["info"]["playerlevel"] = level
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
    text = (
        f"四{room}南喰赤 对局开始\n"
        + format_start_players(game)
        + "\n观战：https://tenhou.net/4/?wg=F2821E7D\n逐局播报：thlive F2821E7D"
    )
    if level == 3:
        send.assert_awaited_once_with("test", TenhouStartNotice(b"png", text))
    else:
        send.assert_awaited_once_with("test", text)
        monitor.start_message.assert_not_awaited()
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


@pytest.mark.parametrize("link_fields", [{}, {"url": ""}, {"url": None}])
def test_private_lobby_without_duration_or_link_remains_queryable(link_fields):
    row = fixture("result")
    row.pop("during")
    row.pop("url")
    row.update(sctype="a", lobby="6337", **link_fields)
    assert history_rows({"name": "applePie", "list": [row]}, "applePie") == [row]
    text = format_result(row)
    assert "时长: 未提供" in text and "结算点" in text
    assert "未提供牌谱链接" not in text
    assert text.endswith("てりうむ(-54.5)")


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


@pytest.mark.asyncio
async def test_tenhou_watch_accepts_more_than_ten_without_duplicate_entries(setup):
    from pupulinbot.tenhou_commands import manage_watch

    _, store = setup
    client = SimpleNamespace(history=AsyncMock(return_value=[fixture("result")]))
    for i in range(12):
        assert "已添加" in await manage_watch(
            store, client, f"on player{i}", "owner", "test", manager="owner"
        )
    assert len(await store.subscriptions("test")) == 12
    client.history.reset_mock()
    assert "已监控" in await manage_watch(
        store, client, "on player11", "owner", "test", manager="owner"
    )
    client.history.assert_not_awaited()
    assert len(await store.subscriptions("test")) == 12


@pytest.mark.asyncio
async def test_slow_group_send_does_not_block_other_groups_or_next_tick(setup):
    import asyncio

    db, store = setup
    row = dict(fixture("result"), starttime=int(time.time()) - 60)
    for group in ("slow", "fast"):
        await store.add(group, "owner", "applePie")
    client = SimpleNamespace(live=AsyncMock(return_value=[]), history=AsyncMock(return_value=[row]))
    monitor = TenhouMonitor(store, client)
    blocked, fast_sent, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def send(group, message):
        if group == "slow":
            blocked.set()
            await release.wait()
        else:
            fast_sent.set()

    try:
        await monitor.poll(send, wait=False)
        await asyncio.wait_for(blocked.wait(), 2)
        await asyncio.wait_for(fast_sent.wait(), 2)
        # Finish the fast worker's cursor update before simulating the next tick.
        await monitor._jobs[("finish", "fast")]
        slow_worker = monitor._jobs[("finish", "slow")]
        await monitor.poll(send, wait=False)
        await monitor._jobs[("finish", "fast")]
        assert monitor._jobs[("finish", "slow")] is slow_worker
        assert not slow_worker.done()
        assert client.history.await_count == 2  # shared across groups on the first tick
        await monitor._jobs[("live",)]
        # Shutdown cancels the blocked send and releases its durable delivery lease.
        await monitor.close()
        async with db.connect() as conn:
            rows = await (await conn.execute("SELECT status FROM game_events")).fetchall()
        assert sum(row["status"] == "sent" for row in rows) == 1
    finally:
        release.set()
        await monitor.close()


@pytest.mark.asyncio
async def test_slow_history_and_live_discovery_do_not_block_ready_result(setup):
    import asyncio

    _, store = setup
    row = dict(fixture("result"), starttime=int(time.time()) - 60)
    await store.add("slow", "owner", "slowPlayer")
    await store.add("fast", "owner", "applePie")
    release, delivered = asyncio.Event(), asyncio.Event()

    async def history(name, start):
        if name == "slowPlayer":
            await release.wait()
            return []
        return [row]

    async def live():
        await release.wait()
        return []

    async def send(group, message):
        assert group == "fast"
        delivered.set()

    monitor = TenhouMonitor(store, SimpleNamespace(live=live, history=history))
    try:
        await monitor.poll(send, wait=False)
        await asyncio.wait_for(delivered.wait(), 2)
        await monitor._jobs[("finish", "fast")]
        assert not monitor._jobs[("live",)].done()
        assert not monitor._jobs[("finish", "slow")].done()
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_large_group_queries_interleave_with_small_group_and_bound_concurrency(setup):
    import asyncio

    _, store = setup
    for i in range(12):
        await store.add("large", "owner", f"player{i}")
    await store.add("small", "owner", "applePie")
    started, release = [], asyncio.Event()

    async def history(name, start):
        started.append(name)
        await release.wait()
        return []

    monitor = TenhouMonitor(
        store, SimpleNamespace(live=AsyncMock(return_value=[]), history=history)
    )
    try:
        await monitor.poll(AsyncMock(), wait=False)
        for _ in range(100):
            if len(started) == 4:
                break
            await asyncio.sleep(0.001)
        assert started[:2] == ["player0", "applePie"]
        assert len(started) == 4
        release.set()
        await monitor.drain()
        assert len(started) == 13
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_same_start_card_is_built_once_across_groups(setup):
    import asyncio

    _, store = setup
    monitor = TenhouMonitor(store, SimpleNamespace())
    entered, release = asyncio.Event(), asyncio.Event()

    async def build(game):
        entered.set()
        await release.wait()
        return b"picture"

    monitor._build_start_message = AsyncMock(side_effect=build)
    game = fixture("live")[0]
    tasks = [asyncio.create_task(monitor.start_message(game)) for _ in range(3)]
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        assert await asyncio.gather(*tasks) == [b"picture"] * 3
        monitor._build_start_message.assert_awaited_once()
    finally:
        await monitor.close()


@pytest.mark.asyncio
async def test_slow_start_image_does_not_hold_up_finish_in_same_group(setup):
    import asyncio

    _, store = setup
    game = fixture("live")[0]
    await store.add("test", "owner", game["players"][0]["name"])
    await store.add("test", "owner", "applePie")
    row = dict(fixture("result"), starttime=int(time.time()) - 60)
    client = SimpleNamespace(
        live=AsyncMock(return_value=[game]), history=AsyncMock(return_value=[row])
    )
    monitor = TenhouMonitor(store, client)
    building, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def card(game):
        building.set()
        await release.wait()
        return b"png"

    async def send(group, message):
        if isinstance(message, str) and "f946a576" in message:
            delivered.set()

    monitor.start_message = card
    try:
        await monitor.poll(send, wait=False)
        await asyncio.wait_for(building.wait(), 2)
        await asyncio.wait_for(delivered.wait(), 2)
        assert not monitor._jobs[("start", "test")].done()
    finally:
        release.set()
        await monitor.drain()
        await monitor.close()


@pytest.mark.asyncio
async def test_start_card_retries_incomplete_player_data_before_rendering():
    game = fixture("live")[0]
    calls = {}

    async def enrich(player, game, destination):
        name = player["name"]
        calls[name] = calls.get(name, 0) + 1
        destination["stats_loaded"] = True
        if calls[name] == 2:
            destination.update(scale_loaded=True, history_loaded=True)

    client = SimpleNamespace(profile=AsyncMock(side_effect=enrich))
    monitor = TenhouMonitor(None, client)
    with patch("pupulinbot.tenhou_monitor.asyncio.sleep", new=AsyncMock()):
        await monitor.start_message(game)
    assert all(calls[player["name"]] == 2 for player in game["players"])
    assert all(p["history_loaded"] and p["scale_loaded"] for p in monitor.last_profiles)


@pytest.mark.asyncio
async def test_start_card_with_no_data_is_retained_for_retry_instead_of_sent_blank():
    monitor = TenhouMonitor(None, SimpleNamespace(profile=AsyncMock()))
    with patch("pupulinbot.tenhou_monitor.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(TenhouError, match="保留任务"):
            await monitor.start_message(fixture("live")[0])
    assert not monitor._cards


def test_lower_room_start_lists_rank_rating_before_each_name():
    game = fixture("live")[0]
    game["info"]["playerlevel"] = 2
    game["players"] = [
        {"name": "彗星D", "grade": 13, "rate": 1899},
        {"name": "源太", "grade": 13, "rate": 1886},
        {"name": "Rina0w0", "grade": 15, "rate": 2030},
        {"name": "【頓頓頓頓】", "grade": 14, "rate": 1965},
    ]
    assert format_start_players(game) == (
        "四段R1899  彗星D\n四段R1886  源太\n六段R2030  Rina0w0\n五段R1965  【頓頓頓頓】"
    )
    game["info"]["playerlevel"] = 3
    assert format_start_players(game) == "彗星D / 源太 / Rina0w0 / 【頓頓頓頓】"


def test_lower_room_missing_fields_do_not_block_start_notice():
    game = fixture("live")[0]
    game["info"].update(playerlevel=0, playernum=3)
    game["players"] = [
        {"name": "甲"},
        {"name": "乙", "grade": 10, "rate": float("nan")},
        {"name": "丙", "grade": 14, "rate": "1965"},
    ]
    assert format_start_players(game) == "段位未知R未知  甲\n初段R未知  乙\n五段R1965  丙"
