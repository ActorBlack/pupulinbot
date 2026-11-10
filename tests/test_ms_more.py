import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.models import GameRecord, Player
from pupulinbot.ms_commands import MahjongQueries
from pupulinbot.ms_ids import account_to_friend, friend_to_account
from pupulinbot.ms_options import parse_options
from pupulinbot.ms_sample import sample_summary
from pupulinbot.ms_sample_chart import render_recent, render_sample
from pupulinbot.paipuya import PaipuyaError


def game(i, rank=1, score=40000, pt=100, ended=True):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=i)
    return GameRecord(
        str(i),
        start,
        start + timedelta(minutes=30) if ended else None,
        "12",
        [{"account_id": 42, "rank": rank, "score": score, "gradingScore": pt}],
        {},
    )


def official_encode_reference(account):
    # Independent literal arithmetic from public client 0.11.252.w Tools.
    value = account ^ 6139246
    upper, low = value & ~67108863, value & 67108863
    for _ in range(5):
        low = ((low & 511) << 17) | (low >> 9)
    return upper + low + 10000000


def test_friend_known_vector_and_current_client_reference_high_bits():
    assert account_to_friend(15628582) == 42121878
    assert friend_to_account(42121878) == 15628582
    for account in [1, 42, 9934016, 2**26 - 1, 2**26, 2**27 + 12345, 2**30 + 15, 2**31 - 1]:
        code = account_to_friend(account)
        assert code == official_encode_reference(account)
        assert friend_to_account(code) == account


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 2**31])
def test_friend_rejects_invalid_account(value):
    with pytest.raises(ValueError):
        account_to_friend(value)


@pytest.mark.parametrize("value", [9999999, True, -1, 1.5, 2**31 + 10000000])
def test_friend_rejects_invalid_code(value):
    with pytest.raises(ValueError):
        friend_to_account(value)


def test_sample_excludes_unfinished_and_unknown_from_denominators():
    rows = [
        game(0),
        game(1, 1, -500, -100),
        game(2, None, None, None),
        game(3, 2, 20000, 25),
        game(4, ended=False),
    ]
    result = sample_summary(rows, 42)
    assert result["games"] == 4 and result["read_games"] == 5
    assert result["known_ranks"] == 3 and result["average_rank"] == pytest.approx(4 / 3)
    assert result["rank_counts"] == [2, 1, 0, 0]
    assert result["negative_rate"] == pytest.approx(1 / 3)
    assert result["pt_delta"] == 25 and result["known_deltas"] == 3
    assert result["best_first_streak"] == 2 and result["current_first_streak"] == 0
    assert sample_summary([game(0, rank=None, score=math.nan, pt=math.inf)], 42)["pt_delta"] is None
    assert sample_summary([game(0)], 999)["average_rank"] is None


def test_sanma_has_three_ranks_and_missing_breaks_streak():
    result = sample_summary([game(0), game(1, 4), game(2)], 42, 3)
    assert result["rank_counts"] == [2, 0, 0]
    assert result["known_ranks"] == 2 and result["best_first_streak"] == 1
    assert result["current_first_streak"] == 1


def test_recent_and_sample_images_and_empty_handling():
    player = Player(42, "测试")
    games = [game(0), game(1, rank=None, score=None, pt=None)]
    assert render_recent(player, games).startswith(b"\x89PNG")
    assert render_sample(player, games, True, parse_options("last=2")).startswith(b"\x89PNG")
    with pytest.raises(ValueError):
        render_recent(player, [])
    with pytest.raises(ValueError):
        render_recent(player, [game(0)] * 21)
    with pytest.raises(ValueError):
        render_sample(player, [game(0, ended=False)], False, parse_options(""))


@pytest.mark.asyncio
async def test_numeric_friend_conversion_works_without_network():
    client = SimpleNamespace(resolve=AsyncMock(), stats=AsyncMock(), records=AsyncMock())
    service = MahjongQueries(None, client, client)
    result = await service.query("mssfri", "15628582", 1, 2)
    assert "42121878" in result
    client.resolve.assert_not_called()
    client.stats.assert_not_called()
    client.stats.side_effect = PaipuyaError("offline")
    result = await service.query("msfri", "42121878", 1, 2)
    assert "15628582" in result and "暂时无法查询" in result


@pytest.mark.asyncio
async def test_image_recent_bounds_and_sample_filters():
    client = SimpleNamespace(
        resolve=AsyncMock(return_value=Player(42, "测试")),
        history=AsyncMock(return_value=([game(0)], True)),
    )
    service = MahjongQueries(None, client, client)
    with pytest.raises(ValueError, match="最多 20"):
        await service.query("mslast", "测试 -i last=21", 1, 2)
    client.history.assert_not_called()
    assert (await service.query("msdetail", "测试 mode=3 room=玉", 1, 2)).startswith(b"\x89PNG")
    assert client.history.call_args.args[1] == 100
    assert client.history.call_args.kwargs["modes"] == [24]
