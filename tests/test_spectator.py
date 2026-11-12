import asyncio
import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.live import LiveServiceError
from pupulinbot.majsoul_replay import (
    LOBBY_URL,
    RECORD_RPC,
    MajsoulConnectionLost,
    ReplayBrowserUnavailable,
    click_lobby,
    official_tab,
    replay_response,
)
from pupulinbot.models import Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.spectator import BrowserSpectatorClient, parse_head

NOW = datetime(2026, 11, 8, 4, tzinfo=UTC)
UUID = "261108-510ble96-8620-41e0-8a5d-68a63d8bf487"


def head(mode=12):
    count = 3 if mode >= 21 else 4
    return {
        "uuid": UUID,
        "start_time": int(NOW.timestamp()) - 60,
        "game_config": {"meta": {"mode_id": mode}},
        "players": [
            {
                "account_id": n,
                "nickname": f"player{n}",
                "level": {"id": 10502},
                "level3": {"id": 20501},
            }
            for n in range(1, count + 1)
        ],
        "seat_list": list(range(count, 0, -1)),
    }


@pytest.mark.parametrize("mode", [12, 16, 24, 26])
def test_official_heads_keep_real_time_seats_and_mode_specific_rank(mode):
    game = parse_head(head(mode), now=NOW)
    assert game.uuid == UUID
    assert game.mode == str(mode)
    assert game.started_at == NOW - timedelta(seconds=60)
    assert [p["account_id"] for p in game.players] == head(mode)["seat_list"]
    assert game.players[0]["level"]["id"] == (20501 if mode >= 21 else 10502)


@pytest.mark.parametrize(
    "field,value",
    [
        ("uuid", "bad?uuid"),
        ("start_time", None),
        ("players", []),
        ("seat_list", [1, 1, 2, 3]),
    ],
)
def test_malformed_head_never_becomes_start_notification(field, value):
    data = head()
    data[field] = value
    with pytest.raises(LiveServiceError):
        parse_head(data, now=NOW)


def test_old_future_and_unsupported_tables_are_not_active():
    for started in (NOW.timestamp() - 21601, NOW.timestamp() + 60):
        data = head()
        data["start_time"] = started
        assert parse_head(data, now=NOW) is None
    assert parse_head(head(99), now=NOW) is None


@pytest.mark.asyncio
async def test_all_subscribers_share_one_scan_and_failures_are_not_idle():
    client = BrowserSpectatorClient("unused", enabled=True)
    game = parse_head(head(), now=NOW)
    client._load = AsyncMock(return_value={n: game for n in range(1, 5)})
    results = await asyncio.gather(*(client.current_game(n) for n in (1, 2, 3, 4)))
    assert all(g.uuid == UUID for g in results)
    client._load.assert_awaited_once()
    assert await client.current_game(99) is None
    await client.cache.close()
    client._load = AsyncMock(side_effect=LiveServiceError("请登录"))
    with pytest.raises(LiveServiceError, match="请登录"):
        await client.current_game(1)
    await client.close()


@pytest.mark.asyncio
async def test_login_error_is_visible_and_throttled_not_cached_as_empty():
    client = BrowserSpectatorClient("unused", enabled=True)
    client.opened = True
    client.process = SimpleNamespace(
        returncode=None, stdin=SimpleNamespace(write=lambda _: None, drain=AsyncMock())
    )
    client._read = AsyncMock(return_value={"error": "rpc_rejected", "code": 151})
    for _ in range(2):
        with pytest.raises(LiveServiceError, match="完成登录.*151"):
            await client.current_game(1)
    client._read.assert_awaited_once()
    assert "完成登录" in client.status() and client.last_success is None
    client.process = None
    await client.close()


@pytest.mark.asyncio
async def test_observe_query_uses_live_source_not_history_and_never_claims_offline():
    client = SimpleNamespace(resolve=AsyncMock(return_value=Player(1, "普普林")))
    live = SimpleNamespace(
        enabled=True, current_game=AsyncMock(return_value=parse_head(head(), now=NOW))
    )
    service = MahjongQueries(None, client, client, live=live)
    text = await service.query("ms4", "普普林 -o", 1, 2)
    assert "正在对局" in text and UUID in text and "player4" in text
    live.current_game.return_value = None
    assert "未在公开观战列表" in await service.query("ms4", "普普林 -o", 1, 2)
    live.current_game.side_effect = LiveServiceError("请登录")
    assert await service.query("ms4", "普普林 -o", 1, 2) == "请登录"
    with pytest.raises(ValueError, match="观战不支持"):
        await service.query("ms4", "普普林 -o last=100", 1, 2)


def test_wire_decoder_reads_official_fields_and_rejects_corrupt_packets():
    from pupulinbot.spectator_wire import decode_list

    def varint(value):
        result = bytearray()
        while value > 127:
            result.append((value & 127) | 128)
            value >>= 7
        result.append(value)
        return bytes(result)

    def blob(key, value):
        return varint(key * 8 + 2) + varint(len(value)) + value

    def integer(key, value):
        return varint(key * 8) + varint(value)

    players = b"".join(
        blob(4, integer(1, n) + blob(4, f"player{n}".encode()) + blob(5, integer(1, 10502)))
        for n in range(1, 5)
    )
    header = (
        blob(1, UUID.encode())
        + integer(2, int(NOW.timestamp()) - 60)
        + blob(3, blob(3, integer(2, 12)))
        + players
        + blob(5, bytes([4, 3, 2, 1]))
    )
    response = decode_list(blob(2, blob(2, header)))
    game = parse_head(response["data"]["live_list"][0], now=NOW)
    assert game.uuid == UUID and [p["account_id"] for p in game.players] == [4, 3, 2, 1]
    assert decode_list(blob(2, blob(1, integer(1, 151)))) == {"error": "rpc_rejected", "code": 151}
    for data in [b"\xff", b"\x12\x80", b"\x12\x05x"]:
        with pytest.raises(LiveServiceError):
            decode_list(data)


def test_real_javascript_bridge_keeps_official_and_spectator_responses_separate():
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser bridge protocol test")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).parent / "fixtures/spectator-bridge-check.cjs"),
            str(Path(__file__).parent.parent / "src/pupulinbot/spectator_bridge.js"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_record_browser_is_opt_in_and_reuses_the_running_chrome(monkeypatch):
    url = f"https://game.maj-soul.com/1/?paipu={UUID}_a123"
    fetch = AsyncMock(return_value={"uuid": UUID})
    monkeypatch.setattr("pupulinbot.spectator.fetch_replay_head", fetch)
    disabled = BrowserSpectatorClient("unused")
    disabled.start = AsyncMock()
    assert await disabled.record_head(url, UUID) is None
    disabled.start.assert_not_awaited()
    fetch.assert_not_awaited()

    client = BrowserSpectatorClient("unused", records_enabled=True)
    client.start = AsyncMock()
    assert await client.current_game(1) is None
    assert await client.record_head(url, UUID) == {"uuid": UUID}
    client.start.assert_not_awaited()

    fetch.side_effect = [ReplayBrowserUnavailable(), {"uuid": UUID}]
    assert await client.record_head(url, UUID) == {"uuid": UUID}
    client.start.assert_awaited_once()


async def test_record_read_reconnects_once_on_disconnect_but_not_on_a_missing_head(monkeypatch):
    fetch = AsyncMock(side_effect=[MajsoulConnectionLost(), {"uuid": UUID}, None])
    monkeypatch.setattr("pupulinbot.spectator.fetch_replay_head", fetch)
    client = BrowserSpectatorClient("unused", records_enabled=True)
    client.record_lobby.close = AsyncMock()
    client.start = AsyncMock()
    url = f"https://game.maj-soul.com/1/?paipu={UUID}"
    assert await client.record_head(url, UUID) == {"uuid": UUID}
    assert fetch.await_count == 2
    assert await client.record_head(url, UUID) is None
    assert fetch.await_count == 3
    client.start.assert_not_awaited()


async def test_lobby_click_sends_browser_input_only_for_a_valid_point(monkeypatch):
    send = AsyncMock(
        side_effect=[{"result": {"value": None}}, {"result": {"value": {"x": 8, "y": 4}}}, {}, {}]
    )
    monkeypatch.setattr("pupulinbot.majsoul_replay.command", send)
    page = object()
    assert not await click_lobby(page)
    assert send.await_count == 1
    assert await click_lobby(page)
    clicks = send.await_args_list[2:]
    assert [call.args[1] for call in clicks] == ["Input.dispatchMouseEvent"] * 2
    assert [call.kwargs["type"] for call in clicks] == ["mousePressed", "mouseReleased"]
    assert [(call.kwargs["x"], call.kwargs["y"]) for call in clicks] == [(8, 4)] * 2


async def test_official_tab_reuses_game_page_and_closes_duplicate_pages(monkeypatch):
    targets = [
        {"type": "page", "targetId": "A", "url": "about:blank"},
        {"type": "page", "targetId": "B", "url": LOBBY_URL + "?paipu=game"},
        {"type": "page", "targetId": "C", "url": LOBBY_URL},
        {"type": "page", "targetId": "D", "url": "https://example.com/"},
    ]
    send = AsyncMock(
        side_effect=[
            {"targetInfos": targets},
            {"success": True},
            {"success": True},
            {"targetInfos": [targets[1], targets[3]]},
        ]
    )
    monkeypatch.setattr("pupulinbot.majsoul_replay.command", send)
    browser = object()
    assert await official_tab(browser) == "B"
    assert [call.args[1] for call in send.await_args_list] == [
        "Target.getTargets",
        "Target.closeTarget",
        "Target.closeTarget",
        "Target.getTargets",
    ]
    assert [call.kwargs["targetId"] for call in send.await_args_list[1:3]] == ["C", "A"]


async def test_official_tab_waits_for_old_tab_to_close_before_returning_new_tab(monkeypatch):
    old = {"type": "page", "targetId": "A", "url": LOBBY_URL}
    new = {"type": "page", "targetId": "B", "url": "about:blank"}
    send = AsyncMock(
        side_effect=[
            {"targetInfos": [old]},
            {"targetId": "B"},
            {"success": True},
            {"targetInfos": [new]},
        ]
    )
    monkeypatch.setattr("pupulinbot.majsoul_replay.command", send)
    assert await official_tab(object(), renew=True) == "B"
    assert send.await_args_list[1].kwargs == {"url": "about:blank"}
    assert send.await_args_list[2].kwargs == {"targetId": "A"}


async def test_replay_response_correlates_uuid_and_socket_before_reading_head():
    def blob(key, data):
        assert len(data) < 128
        return bytes([key * 8 + 2, len(data)]) + data

    def event(direction, socket, packet):
        return json.dumps(
            {
                "method": f"Network.webSocketFrame{direction}",
                "params": {
                    "requestId": socket,
                    "response": {"opcode": 2, "payloadData": base64.b64encode(packet).decode()},
                },
            }
        )

    request = blob(1, RECORD_RPC) + blob(2, blob(1, UUID.encode()))
    seat = bytes([8, 42]) + blob(3, "东".encode())
    head = blob(1, UUID.encode()) + bytes([24, 100]) + blob(11, seat) + blob(12, blob(1, b""))
    reply = blob(2, blob(3, head) + blob(4, b"unparsed replay body"))
    sent = b"\x02\x05\x00" + request
    received = b"\x03\x05\x00" + reply

    class Page:
        send = AsyncMock()
        recv = AsyncMock(return_value='{"id":1,"result":{}}')

        async def __aiter__(self):
            yield event("Received", "a", received)
            yield event("Sent", "a", sent)
            yield event("Received", "b", received)
            yield event("Received", "a", b"\x03\x06\x00")
            yield event("Received", "a", received)

    page = Page()
    url = f"https://game.maj-soul.com/1/?paipu={UUID}_a123"
    assert await replay_response(page, url, UUID) == {
        "uuid": UUID,
        "end_time": 100,
        "accounts": [{"account_id": 42, "seat": 0, "nickname": "东"}],
        "result_seats": [0],
    }
    assert [json.loads(call.args[0])["method"] for call in page.send.await_args_list] == [
        "Network.enable",
        "Page.navigate",
    ]
