import copy
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pupulinbot.tenhou import CN, TenhouError
from pupulinbot.tenhou_options import parse_tenhou_options
from pupulinbot.tenhou_profile import rank_history
from pupulinbot.tenhou_queries import TenhouQueries, make_view, summary_text

FIXTURE = Path(__file__).parent / "fixtures/tenhou/result.json"
NOW = datetime(2026, 11, 10, 23, tzinfo=CN).timestamp()


def reused_history():
    base = json.loads(FIXTURE.read_text())
    rows = []
    for start, count, old in (
        (datetime(2016, 1, 1, tzinfo=CN).timestamp(), 15, True),
        (datetime(2026, 1, 7, tzinfo=CN).timestamp(), 970, False),
    ):
        for index in range(count):
            row = {
                **base,
                "starttime": start + index * 3600,
                "rate": 9999 if old else 1900 + index % 100,
            }
            if old:
                row["player4"] = "lingran"
            else:
                row["player1"] = "lingran"
            rows.append(row)
    for _ in range(7):
        rows.append(
            {
                **base,
                "starttime": rows[-1]["starttime"] + 3600,
                "playernum": 3,
                "player1": "lingran",
                "rate": 1700,
            }
        )
    return {"name": "lingran", "list": rows, "rate": {"4": 1999, "3": 1700}}


def test_985_historical_four_player_games_are_970_in_current_lifetime_not_sanma():
    data = reused_history()
    original = copy.deepcopy(data)
    assert sum(r["playernum"] == 4 for r in data["list"]) == 985
    view = make_view(data, "lingran", parse_tenhou_options(""), recent=False, now=NOW)
    assert view["count"] == view["total_count"] == view["progress"]["ranked_count"] == 970
    assert view["counts"] == [970, 0, 0, 0] and view["average"] == 1
    assert view["highest_rate"] == 1999 and view["rate"] == 1999
    assert "对战数: 970;" in summary_text(view)
    sample = make_view(data, "lingran", parse_tenhou_options("-l 0"), recent=True, now=NOW)
    assert sample["count"] == 500 and sample["total_count"] == 970
    sanma = make_view(data, "lingran", parse_tenhou_options("mode=3"), recent=False, now=NOW)
    assert sanma["count"] == sanma["total_count"] == 7
    assert data == original  # Keep archived source records intact.


def test_old_dates_do_not_pull_records_from_previous_account_life():
    with pytest.raises(TenhouError, match="没有已收录"):
        make_view(
            reused_history(),
            "lingran",
            parse_tenhou_options("start=2016-01-01 end=2016-02-01"),
            recent=True,
            now=NOW,
        )


def test_explicit_reinitialization_applies_across_both_modes():
    data = reused_history()
    data["list"] = data["list"][-10:]
    reset = data["list"][-2]["starttime"] - 1
    data["ptevents"] = [{"starttime": reset, "continuous": False}]
    view = make_view(data, "lingran", parse_tenhou_options("mode=3"), recent=False, now=NOW)
    assert view["count"] == 2 and view["total_count"] == 2
    with pytest.raises(TenhouError, match="没有已收录"):
        make_view(data, "lingran", parse_tenhou_options(""), recent=False, now=NOW)
    result = rank_history(data, "lingran", 3, NOW, include_records=True)
    assert len(result["current_records"]) == 2 and result["ranked_count"] == 2


@pytest.mark.asyncio
async def test_ranking_does_not_list_players_only_active_in_previous_life():
    data = reused_history()
    reset = data["list"][-2]["starttime"] - 1
    data["ptevents"] = [{"starttime": reset, "continuous": False}]
    store = SimpleNamespace(member_names=AsyncMock(return_value=["lingran"]))
    source = SimpleNamespace(user_history=AsyncMock(return_value=data))
    service = TenhouQueries(store, source)
    assert await service.ranking("", {"member"}) == "水表网暂时没有本群玩家的段位数据。"
    (page,) = await service.ranking("sanma", {"member"})
    assert page.startswith(b"\x89PNG")
