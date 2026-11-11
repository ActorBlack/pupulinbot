import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from test_tenhou import fixture

from pupulinbot.db import Database
from pupulinbot.tenhou import TenhouError
from pupulinbot.tenhou_live import RoundResults, TenhouLiveWatch, spectator_frames, watch_number
from pupulinbot.tenhou_monitor import TenhouMonitor, TenhouStore


def player_names(game):
    return {"tag": "UN", **{f"n{i}": quote(p["name"]) for i, p in enumerate(game["players"])}}


def initial(index=0, honba=0, scores="250,250,250,250"):
    return {"tag": "INIT", "seed": f"{index},{honba},0,0,0,0", "ten": scores}


def test_double_ron_includes_both_winners_and_round_net_after_riichi():
    game = fixture("live")[0]
    names = [p["name"] for p in game["players"]]
    results = RoundResults(game)
    results.feed(player_names(game))
    results.feed(initial(1, 2))
    first = {
        "tag": "AGARI",
        "who": "0",
        "fromWho": "1",
        "machi": "52",
        "ten": "30,3900,0",
        "yaku": "8,1,52,1,54,1",
        "sc": "240,49,250,-39,250,0,250,0",
    }
    assert results.feed(first) is None
    assert results.feed(first) is None
    assert (
        results.feed(
            {
                **first,
                "who": "2",
                "ten": "30,8000,1",
                "yaku": "1,1,7,1,53,2,54,1",
                "sc": "289,0,211,-80,250,80,250,0",
            }
        )
        is None
    )
    hand, text = results.feed(initial(2, 0, "289,131,330,250"))
    assert hand == "1:2"
    assert "东2局 2本场" in text
    assert (
        f"{names[0]} 荣和 {names[1]}（赤5筒）\n"
        "役种 断幺九1番、宝牌1番、赤宝牌1番\n合计 3番30符，3,900点"
    ) in text
    assert (
        f"{names[2]} 荣和 {names[1]}（赤5筒）\n"
        "役种 立直1番、平和1番、里宝牌2番、赤宝牌1番\n合计 5番30符（满贯），8,000点"
    ) in text
    assert "·" not in text
    assert f"{names[0]} 28,900（+3,900）" in text
    assert f"{names[1]} 13,100（-11,900）" in text
    assert results.feed({"tag": "OWARI"}) is None
    assert results.finished


def test_sanma_final_draw_uses_three_scores_and_checks_public_table():
    game = fixture("live")[0]
    game["info"]["playernum"] = 3
    game["players"].pop()
    results = RoundResults(game)
    names = player_names(game)
    with pytest.raises(TenhouError, match="对局不一致"):
        results.feed({**names, "n1": "other"})
    results.feed(names)
    results.feed(initial(6, 0, "350,350,350,0"))
    hand, text = results.feed(
        {"tag": "RYUUKYOKU", "sc": "350,30,350,-15,350,-15,0,0", "owari": "final"}
    )
    assert hand == "6:0" and results.finished
    assert text.splitlines()[1] == "流局"
    assert len(text.splitlines()) == 6
    assert "38,000（+3,000）" in text and "33,500（-1,500）" in text


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        (
            {"yaku": "28,2,18,1,31,2,52,8", "ten": "50,32000,5", "m": "48649,29954,31520,27722"},
            "役种 对对和2番、役牌白1番、混老头2番、宝牌7番、拔北1番\n"
            "合计 13番50符（累计役满），32,000点",
        ),
        (
            {"yakuman": "50,42", "ten": "50,64000,5"},
            "役种 小四喜、字一色\n合计 2倍役满，64,000点",
        ),
        (
            {"yaku": "1,1,53,0", "ten": "40,1300,0"},
            "役种 立直1番\n合计 1番40符，1,300点",
        ),
        (
            {"yaku": "1,1,53", "ten": "40,1300,0"},
            "\n1,300点\n",
        ),
    ],
)
def test_sanma_hand_value_from_tenhou_scoring_fields(details, expected):
    game = fixture("live")[0]
    game["info"]["playernum"] = 3
    game["players"].pop()
    results = RoundResults(game)
    results.feed(player_names(game))
    results.feed(initial(scores="350,350,350,0"))
    _, text = results.feed(
        {
            "tag": "AGARI",
            "who": "0",
            "fromWho": "1",
            "machi": "124",
            "sc": "350,13,350,-13,350,0,0,0",
            "owari": "final",
            **details,
        }
    )
    assert expected in text


@pytest.mark.asyncio
async def test_transport_uses_only_spectator_handshake_and_unpacks_timed_events(monkeypatch):
    game = fixture("live")[0]
    names, hand = player_names(game), initial()
    agari = {"tag": "AGARI"}

    class Socket:
        send = AsyncMock()
        recv = AsyncMock(
            side_effect=[json.dumps({"tag": "HELO"}), json.dumps({"tag": "GO", "kansen": "1"})]
        )

        async def __aiter__(self):
            for event in (
                names,
                {"tag": "INITBYLOG", "childNodes": [hand, {"tag": "D12"}]},
                {"tag": "WGC", "childNodes": [0, {"tag": "T23"}, 0, agari, 0, initial(1)]},
            ):
                yield json.dumps(event)

    socket = Socket()

    @asynccontextmanager
    async def open_socket(uri, *, origin, **kwargs):
        assert uri == "wss://b-wk.mjv.jp" and origin == "https://tenhou.net"
        yield socket

    monkeypatch.setattr("pupulinbot.tenhou_live.connect", open_socket)
    assert [event async for event in spectator_frames("F2821E7D")] == [
        names,
        hand,
        agari,
        initial(1),
    ]
    assert [json.loads(call.args[0]) for call in socket.send.await_args_list] == [
        {"tag": "HELO", "name": "NoName", "sx": "M"},
        {"tag": "WG", "id": "F2821E7D", "tw": 0},
        {"tag": "GOK"},
    ]


@pytest.mark.asyncio
async def test_two_groups_share_stream_and_stopping_is_group_scoped(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    await db.initialize()
    store = TenhouStore(db)
    await store.initialize()
    game = fixture("live")[0]
    for group in ("1", "2"):
        await store.add(group, "manager", game["players"][0]["name"])
    client = SimpleNamespace(live=AsyncMock(return_value=[game]))
    send = AsyncMock()
    service = TenhouLiveWatch(store, client, TenhouMonitor(store, client), send)
    go = asyncio.Event()
    connections = []

    async def stream(watch_id):
        connections.append(watch_id)
        await go.wait()
        yield player_names(game)
        yield initial()
        yield {
            "tag": "AGARI",
            "who": "0",
            "fromWho": "0",
            "machi": "0",
            "ten": "30,1500,0",
            "sc": "250,15,250,-5,250,-5,250,-5",
            "owari": "final",
        }

    monkeypatch.setattr("pupulinbot.tenhou_live.spectator_frames", stream)
    with pytest.raises(TenhouError, match="尚未监控"):
        await service.follow("3", "viewer", "F2821E7D")
    assert "已开启" in await service.follow("1", "viewer", "f2821e7d")
    assert "已开启" in await service.follow("2", "other", "https://tenhou.net/4/?wg=F2821E7D")
    table = service.current("1")
    assert table is service.current("2")
    with pytest.raises(TenhouError, match="开启者或管理员"):
        await service.stop("1", "other")
    assert "已停止" in await service.stop("1", "viewer")
    go.set()
    await asyncio.wait_for(table.task, timeout=2)
    assert connections == ["F2821E7D"]
    send.assert_awaited_once()
    assert send.await_args.args[0] == "2"
    assert "自摸（1万）\n1,500点" in send.await_args.args[1]
    assert "26,500（+1,500）" in send.await_args.args[1]
    assert service.current("2") is None
    await service.close()


def test_watch_input_rejects_replays_and_untrusted_hosts():
    assert watch_number("https://tenhou.net/0/?wg=f2821e7d&tw=3") == "F2821E7D"
    for value in (
        "https://tenhou.net/4/?log=2026110806gm-00a9-0000-f946a576",
        "https://tenhou.net.example/4/?wg=F2821E7D",
        "https://tenhou.net/4/?wg=F2821E7D&wg=deadbeef",
    ):
        with pytest.raises(TenhouError):
            watch_number(value)
