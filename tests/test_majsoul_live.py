import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.db import Database
from pupulinbot.live import LiveGame, LiveServiceError
from pupulinbot.majsoul_live import MajsoulLiveWatch
from pupulinbot.majsoul_observe import (
    HEADER,
    Observation,
    ObserverSocket,
    observer_frame,
    watch_uuid,
)
from pupulinbot.majsoul_rounds import MajsoulRoundResults

UUID = "261113-510ble96-8620-41e0-8a5d-68a63d8bf487"


def varint(value):
    value &= 0xFFFFFFFFFFFFFFFF
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def integer(key, value):
    return varint(key * 8) + varint(value)


def blob(key, data):
    return varint(key * 8 + 2) + varint(len(data)) + data


def packed(key, *values):
    return blob(key, b"".join(varint(value) for value in values))


def wrapped(name, payload):
    return blob(1, (".lq." + name).encode()) + blob(2, payload)


def test_observer_decodes_wire_and_round_results_with_multiple_winners():
    game = SimpleNamespace(
        players=[{"nickname": name} for name in ("东", "南", "西", "北")], mode="12"
    )
    result = MajsoulRoundResults(game)
    initial = packed(5, 25000, 25000, 25000, 25000)
    discard = integer(1, 1)
    fan = blob(12, integer(2, 1) + integer(3, 2))
    hule = (
        blob(1, blob(3, b"0p") + integer(11, 1) + integer(13, 30) + integer(15, 1500) + fan)
        + blob(1, integer(4, 2) + integer(11, 2) + integer(13, 30) + integer(15, 2000))
        + packed(2, 25000, 25000, 25000, 25000)
        + packed(3, 1500, -3500, 2000, 0)
    )
    for ordinal, (name, payload) in enumerate(
        [("RecordNewRound", initial), ("RecordDiscardTile", discard), ("RecordHule", hule)]
    ):
        wire = wrapped(name, payload)
        frame = HEADER.pack(ordinal, ordinal * 10, 0, 1, len(wire)) + wire
        assert observer_frame(frame) == (ordinal, ordinal * 10, name, payload)
        notice = result.feed(name, payload)
    assert notice.key == "0:0:0"
    assert "东 荣和 南（赤5筒）" in notice.text and "西 荣和 南" in notice.text
    assert "役种　立直" in notice.text.splitlines()
    assert "打点　1番30符 / 1,500点" in notice.text
    assert "21,500　-3,500　南" in notice.text and "27,000　+2,000　西" in notice.text
    assert result.feed("RecordHule", hule) is None


def test_sanma_tsumo_displays_base_payments_and_preserves_net_scores():
    game = SimpleNamespace(
        players=[{"nickname": n} for n in ("冰璃、", "芒果1212", "ちくわ界の王")], mode="24"
    )
    result = MajsoulRoundResults(game)
    initial = integer(2, 1) + integer(3, 1) + packed(5, 60100, 34800, 10100)
    result.feed("RecordNewRound", initial)
    win = (
        blob(3, b"6s")
        + integer(4, 2)
        + integer(5, 1)
        + integer(11, 4)
        + integer(13, 30)
        + integer(15, 7700)
        + integer(16, 3900)
        + integer(17, 2000)
        + integer(19, 11800)
        + integer(20, 5900)
    )
    for fan_id, count in ((1, 1), (9, 1), (2, 1), (32, 1), (33, 0)):
        win += blob(12, integer(3, fan_id) + (integer(2, count) if count else b""))
    payload = (
        blob(1, win)
        + packed(2, 60100, 34800, 9100)
        + packed(3, -2100, -4000, 7100)
        + packed(5, 58000, 30800, 16200)
    )
    notice = result.feed("RecordHule", payload)
    assert notice.key == "0:1:1"
    assert "ちくわ界の王 自摸（6索）" in notice.text
    assert "打点　4番30符 / 5,900点" in notice.text
    assert "役种　门前清自摸和、役牌 中、立直、红宝牌" in notice.text.splitlines()
    assert "里宝牌" not in notice.text and "11,800" not in notice.text
    assert notice.text.splitlines()[-4:] == [
        "结算点数 / 本局增减",
        "58,000　-2,100　冰璃、",
        "30,800　-4,000　芒果1212",
        "16,200　+6,100　ちくわ界の王",
    ]


def test_sanma_draw_with_riichi_and_negative_score():
    game = SimpleNamespace(players=[{"nickname": str(n)} for n in range(3)], mode="24")
    result = MajsoulRoundResults(game)
    result.feed("RecordNewRound", integer(1, 1) + packed(5, 400, 45000, 55000))
    result.feed("RecordDealTile", blob(5, integer(2, -600)))
    notice = result.feed("RecordNoTile", blob(3, packed(3, 0, -1000, 1000)))
    assert "三麻玉南　南1局" in notice.text and "荒牌流局" in notice.text
    assert "\u2007\u2007-600　-1,000　0" in notice.text
    assert "56,000　+1,000　2" in notice.text


@pytest.mark.parametrize(
    ("seats", "dealer", "dealer_payment", "other_payment", "points"),
    [(3, True, 0, 8000, 16000), (4, True, 0, 3900, 11700), (4, False, 3900, 2000, 7900)],
)
def test_tsumo_uses_the_number_of_actual_payers(
    seats, dealer, dealer_payment, other_payment, points
):
    game = SimpleNamespace(players=[{"nickname": str(i)} for i in range(seats)], mode="12")
    result = MajsoulRoundResults(game)
    result.feed("ActionNewRound", packed(6, *([25000] * seats)))
    win = (
        integer(5, 1)
        + integer(6, dealer)
        + integer(11, 4)
        + integer(13, 30)
        + integer(16, dealer_payment)
        + integer(17, other_payment)
        + integer(19, 51800)
    )
    notice = result.feed("ActionHule", blob(1, win) + packed(5, *([25000] * seats)))
    assert f" / {points:,}点" in notice.text
    assert "51,800" not in notice.text


def test_ron_on_north_reveal_uses_the_revealing_seat_and_game_end_scores():
    game = SimpleNamespace(players=[{"nickname": n} for n in ("东", "南", "西")], mode="24")
    result = MajsoulRoundResults(game)
    result.feed("RecordNewRound", packed(5, 35000, 35000, 35000))
    result.feed("RecordDiscardTile", integer(1, 0))
    result.feed("RecordBaBei", integer(1, 1))
    win = blob(3, b"4z") + integer(4, 2) + integer(15, 5800) + integer(19, 35800)
    end = blob(6, packed(1, 35000, 29200, 40800))
    notice = result.feed("RecordHule", blob(1, win) + end)
    assert "西 荣和 南（北）" in notice.text
    assert "打点　5,800点" in notice.text and "0番" not in notice.text
    assert "29,200　-5,800　南" in notice.text
    assert result.finished


def test_four_riichi_abortive_draw_collects_the_last_stick():
    game = SimpleNamespace(players=[{"nickname": n} for n in ("东", "南", "西", "北")], mode="12")
    result = MajsoulRoundResults(game)
    result.feed("RecordNewRound", packed(5, 25000, 25000, 25000, 1000))
    for seat in range(3):
        result.feed("RecordDiscardTile", integer(1, seat) + integer(3, 1))
        result.feed("RecordDealTile", blob(5, integer(1, seat) + integer(2, 24000)))
    result.feed("RecordDiscardTile", integer(1, 3) + integer(3, 1))
    notice = result.feed("RecordLiuJu", integer(1, 4))
    assert "四家立直" in notice.text
    assert "0　-1,000　北" in notice.text
    assert notice.text.count("　-1,000　") == 4


def test_multiple_nagashi_payments_use_each_delta_once():
    game = SimpleNamespace(players=[{"nickname": n} for n in ("东", "南", "西", "北")], mode="12")
    result = MajsoulRoundResults(game)
    result.feed("RecordNewRound", packed(5, 25000, 25000, 25000, 25000))
    start = packed(2, 25000, 25000, 25000, 25000)
    first_payment = blob(3, start + packed(3, 12000, -4000, -4000, -4000))
    second_payment = blob(
        3, packed(2, 37000, 21000, 21000, 21000) + packed(3, -4000, 8000, -2000, -2000)
    )
    notice = result.feed("RecordNoTile", integer(1, 1) + first_payment + second_payment)
    assert "流局满贯" in notice.text
    assert "33,000　+8,000　东" in notice.text and "29,000　+4,000　南" in notice.text


def test_supported_watch_urls():
    assert watch_uuid(f"https://game.maj-soul.com/1/?paipu={UUID}") == UUID
    assert watch_uuid(f"https://game.maj-soul.com/1/?paipu={UUID}_a123_2") == UUID
    assert watch_uuid("paipu=" + UUID) == UUID
    assert watch_uuid("https://example.org/1/?paipu=" + UUID) is None


async def test_observer_recovers_missing_segment_and_skips_duplicates():
    stream = ObserverSocket(None)
    stream.last_sequence = 7
    data = wrapped("RecordDiscardTile", integer(1, 2))
    missing = HEADER.pack(8, 20, 0, 1, len(data)) + data
    stream.request = AsyncMock(return_value={"segments": base64.b64encode(missing)})
    frame = (9, 40, "RecordHule", b"")
    assert [record async for record in stream._ordered(frame)] == [
        (8, 20, "RecordDiscardTile", integer(1, 2)),
        frame,
    ]
    stream.request.assert_awaited_once_with("FetchSegment", {"seq": 8})
    assert [record async for record in stream._ordered(frame)] == []


async def test_find_public_game_by_numeric_nickname_account_or_own_binding():
    players = [
        {"account_id": 12345, "nickname": "8444"},
        {"account_id": 7, "nickname": "西"},
        {"account_id": 9, "nickname": "北"},
    ]
    game = LiveGame(UUID, "24", datetime(2026, 11, 13, 10, 30, tzinfo=UTC), players)
    browser = SimpleNamespace(
        public_games=AsyncMock(return_value=dict.fromkeys((12345, 7, 9), game)),
        recently_listed_games=dict,
    )
    db = SimpleNamespace(
        binding=AsyncMock(return_value={"account_id": 12345, "nickname": "旧名"}),
    )
    send = AsyncMock()
    service = MajsoulLiveWatch(db, browser, send)
    expected = f"8444 正在对局\n三麻玉南　11-13 18:30 开始\nmslive {UUID}"
    for value in ("8444", "12345", ""):
        assert await service.find("a", value) == expected
    assert not service.tables
    send.assert_not_awaited()
    assert "未找到对局" in await service.find("a", "无此人")
    players[1]["nickname"] = "8444"
    assert "同名玩家" in await service.find("a", "8444")
    browser.public_games.side_effect = LiveServiceError("观战连接超时")
    with pytest.raises(LiveServiceError, match="观战连接超时"):
        await service.find("a", "8444")


async def test_find_rolled_out_table_verifies_official_head(monkeypatch):
    start = int(time()) - 500
    players = [
        {"account_id": account, "nickname": name}
        for account, name in ((12345, "芒果1212"), (7, "西"), (9, "北"))
    ]
    game = LiveGame(UUID, "24", datetime.fromtimestamp(start, UTC), players)
    auth = Observation("wss://route-2.maj-soul.com/ob", "placeholder", 300, start)
    browser = SimpleNamespace(
        public_games=AsyncMock(return_value={}),
        recently_listed_games=lambda: dict.fromkeys((12345, 7, 9), game),
        observation=AsyncMock(return_value=auth),
    )
    head = {
        "uuid": UUID,
        "start_time": start,
        "game_config": {"meta": {"mode_id": 24}},
        "seat_list": [12345, 7, 9],
        "players": players,
    }

    @asynccontextmanager
    async def observe(_):
        yield head, None

    monkeypatch.setattr("pupulinbot.majsoul_live.observe", observe)
    service = MajsoulLiveWatch(None, browser, None)
    assert (await service.find("a", "芒果1212")).endswith(f"mslive {UUID}")
    browser.observation.assert_awaited_once_with(UUID)
    browser.observation.side_effect = LiveServiceError("对局已结束")
    with pytest.raises(LiveServiceError, match="对局已结束"):
        await service.find("a", "12345")


@pytest.mark.asyncio
async def test_groups_without_watchlists_share_stream_and_deduplicate(tmp_path, monkeypatch):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    start = int(time()) - 300
    auth = Observation("wss://route-2.maj-soul.com/ob", "placeholder", 300, start)
    browser = SimpleNamespace(observation=AsyncMock(return_value=auth))
    names = ["东", "南", "西", "北"]
    head = {
        "uuid": UUID,
        "start_time": start,
        "game_config": {"meta": {"mode_id": 12}},
        "seat_list": [1, 2, 3, 4],
        "players": [{"nickname": name, "account_id": i} for i, name in enumerate(names, 1)],
    }
    send = AsyncMock()
    release = asyncio.Event()

    async def events():
        await release.wait()
        yield (1, 0, "RecordNewRound", packed(5, 25000, 25000, 25000, 25000))
        yield (2, 3000, "RecordNoTile", b"")
        yield (3, 3000, "NotifyGameEndResult", b"")

    @asynccontextmanager
    async def observe(_):
        yield head, SimpleNamespace(events=events, start=AsyncMock())

    monkeypatch.setattr("pupulinbot.majsoul_live.observe", observe)
    service = MajsoulLiveWatch(db, browser, send)
    assert "已开启" in await service.follow("1", "a", UUID)
    assert "已开启" in await service.follow("2", "b", UUID)
    task = service.current("1").task
    assert service.current("2").task is task
    release.set()
    await task
    assert {call.args[0] for call in send.await_args_list} == {"1", "2"}
    assert all("荒牌流局" in call.args[1] for call in send.await_args_list)
    assert "未开启" in service.status("1")
    browser.observation.assert_awaited_once_with(UUID)
