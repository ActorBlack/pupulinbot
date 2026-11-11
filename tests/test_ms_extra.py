import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_extra import matchup, render_comparison, split_comparison
from pupulinbot.ms_options import parse_options
from pupulinbot.paipuya import PaipuyaClient


def game(i, own_rank=1, other_rank=2, *, ended=True, pt=10):
    now = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(hours=i)
    return GameRecord(
        str(i),
        now,
        now + timedelta(minutes=30) if ended else None,
        "12",
        [
            {
                "account_id": 42,
                "nickname": "本人",
                "rank": own_rank,
                "score": 25000,
                "gradingScore": pt,
            },
            {
                "account_id": 99,
                "nickname": "对手",
                "rank": other_rank,
                "score": 25000,
                "gradingScore": None,
            },
        ],
        {},
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("room=玉南", [12]),
        ("room=王座南 mode=3", [26]),
        ("room=玉之间 game=半庄", [12]),
        ("game=东 mode=3", [21, 23, 25]),
        ("room=金东,玉南", [8, 12]),
        ("room=金,玉 game=南", [9, 12]),
        ("room=王座 game=东风", [15]),
    ],
)
def test_exact_room_length_modes(text, expected):
    assert parse_options(text).modes == expected


@pytest.mark.parametrize(
    "text", ["room=玉东 game=南", "room=王南 game=东", "game=速战", "room=银南"]
)
def test_conflicting_room_and_length_fail_before_network(text):
    with pytest.raises(ValueError):
        parse_options(text)


def test_matchup_uses_rank_for_tied_points_and_excludes_duplicates_unfinished():
    rows = [game(0), game(1, 3, 1, pt=None), game(2, None, 2), game(3, ended=False)]
    text = matchup(
        Player(42, "本人"), Player(99, "99"), rows + [rows[0]], parse_options("last=5"), True
    )
    assert "同桌 3 场" in text and "领先 1 / 落后 1 / 同顺位 0 / 顺位缺失 1" in text
    assert "有效 3/3" in text and "暂无积分" in text and "已知 2/3" in text
    assert "本人 vs 对手" in text
    assert "完整历史" not in text and "已达查询上限" not in text
    assert "未找到" in matchup(Player(42, "本人"), Player(88, "无"), rows, parse_options(""), False)


def test_comparison_splits_chronologically_and_preserves_unknown_values():
    rows = [game(i, own_rank=i % 4 + 1, pt=None if i >= 2 else 10) for i in range(5)]
    halves, summary = split_comparison(list(reversed(rows)) + [rows[0], game(9, ended=False)], 42)
    assert [len(h) for h in halves] == [2, 3]
    assert halves[0][0].uuid == "0"
    assert summary[0]["pt_delta"] == 20 and summary[1]["pt_delta"] is None
    with pytest.raises(ValueError, match="至少需要4场"):
        split_comparison(rows[:3], 42)
    png = render_comparison(Player(42, "比较测试"), rows, True, parse_options("last=5"))
    assert Image.open(io.BytesIO(png)).size == (1560, 910)


@pytest.mark.asyncio
async def test_matchup_routes_own_binding_and_sanma_without_opponent_lookup():
    db = SimpleNamespace(binding=AsyncMock(return_value={"account_id": 42, "nickname": "本人"}))
    four = SimpleNamespace(history=AsyncMock())
    three = SimpleNamespace(history=AsyncMock(return_value=([game(0)], False)), resolve=AsyncMock())
    query = MahjongQueries(db, four, three)
    text = await query.query("msvs", "99 mode=3 room=玉南", 1, 2)
    assert "三麻" in text and "同桌 1 场" in text
    assert three.history.call_args.args[0] == 42
    assert three.history.call_args.kwargs["modes"] == [24]
    three.resolve.assert_not_called()
    four.history.assert_not_called()
    three.history.reset_mock()
    with pytest.raises(ValueError, match="不能与本人"):
        await query.query("msvs", "42", 1, 2)
    with pytest.raises(ValueError, match="用法"):
        await query.query("msvs", "", 1, 2)
    three.history.assert_not_called()


@pytest.mark.asyncio
async def test_matchup_default_room_is_based_on_the_callers_latest_game():
    db = SimpleNamespace(binding=AsyncMock(return_value={"account_id": 42, "nickname": "本人"}))
    latest = game(1)
    latest.mode = "16"
    client = SimpleNamespace(
        modes=[8, 9, 11, 12, 15, 16],
        history=AsyncMock(side_effect=[([latest], False), ([latest], False)]),
    )
    query = MahjongQueries(db, client, None)
    assert "同桌 1 场" in await query.query("msvs", "99", 1, 2)
    latest_call, sample_call = client.history.await_args_list
    assert latest_call.args == (42, 1) and latest_call.kwargs["modes"] is None
    assert sample_call.args == (42, 200) and sample_call.kwargs["modes"] == [16]


@pytest.mark.asyncio
async def test_compare_defaults_to_100_and_exact_mode_filter():
    db = SimpleNamespace(binding=AsyncMock(return_value={"account_id": 42, "nickname": "本人"}))
    client = SimpleNamespace(history=AsyncMock(return_value=([game(i) for i in range(4)], False)))
    query = MahjongQueries(db, client, client)
    with patch("pupulinbot.ms_commands.render_comparison", return_value=b"comparison"):
        assert await query.query("mscompare", "room=玉南", 1, 2) == b"comparison"
    assert client.history.call_args.args == (42, 100)
    assert client.history.call_args.kwargs["modes"] == [12]


@pytest.mark.asyncio
async def test_http_pool_reuse_close_and_reopen_without_caching_raw_reads():
    real = httpx.AsyncClient
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=[])

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    ) as factory:
        client = PaipuyaClient("https://example.invalid")
        await client.records(42)
        await client.records(42)
        assert factory.call_count == 1 and count == 2
        pool = client._http
        await client.close()
        assert pool.is_closed
        await client.records(42)
        assert factory.call_count == 2 and count == 3
        await client.close()


@pytest.mark.asyncio
async def test_stale_pooled_connection_retries_one_idempotent_read():
    real = httpx.AsyncClient
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.RemoteProtocolError("server disconnected")
        return httpx.Response(200, json=[])

    with patch(
        "httpx.AsyncClient",
        side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    ):
        client = PaipuyaClient("https://example.invalid")
        assert await client.records(42) == [] and calls == 2
        await client.close()
