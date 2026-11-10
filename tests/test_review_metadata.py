from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_batch_review import native_report, report_metrics

from pupulinbot.batch_review import BatchReviews
from pupulinbot.native_report import compact_report
from pupulinbot.review import ReviewError
from pupulinbot.review_metadata import apply_report_result, report_game_result


def reviewed_game():
    data = native_report()
    data["mjai_log"] = [
        {"type": "start_game", "names": ["Aさん", "Bさん", "Cさん", "Dさん"]},
        {"type": "start_kyoku", "scores": [25000] * 4},
        {"type": "ryukyoku", "deltas": [1500, -1500, 1500, -1500]},
        {"type": "end_kyoku"},
        {"type": "start_kyoku", "scores": [23700, 19600, 34800, 20900]},
        {"type": "reach_accepted", "actor": 1},
        {"type": "hora", "deltas": [6500, -4500, 0, 0]},
        {"type": "end_kyoku"},
        {"type": "end_game"},
    ]
    return data


def test_final_rank_includes_accepted_riichi_and_only_last_round():
    result = report_game_result(reviewed_game())
    assert result == {"scores": [30200, 14100, 34800, 20900], "ranks": [2, 4, 1, 3]}


def test_compaction_preserves_final_result_and_analysis_metrics_without_anonymous_names():
    data = reviewed_game()
    compact = compact_report(data)
    assert report_metrics(compact) == report_metrics(data)
    assert report_game_result(compact) == report_game_result(data)
    assert "mjai_log" not in compact
    assert "Aさん" not in str(compact)


@pytest.mark.parametrize("invalid", ["unfinished", "missing_delta", "bad_actor", "bad_score"])
def test_incomplete_or_invalid_logs_do_not_produce_ranks(invalid):
    data = reviewed_game()
    if invalid == "unfinished":
        data["mjai_log"].pop()
    elif invalid == "missing_delta":
        data["mjai_log"][-3].pop("deltas")
    elif invalid == "bad_actor":
        data["mjai_log"][-4]["actor"] = True
    else:
        data["mjai_log"][-5]["scores"][0] = float("nan")
    assert report_game_result(data) is None


def test_report_cannot_override_original_rank_or_another_seat():
    row = {"seat": 0, "rank": 1, "rank_source": "original"}
    before = deepcopy(row)
    apply_report_result(row, reviewed_game())
    assert row == before
    row = {"seat": 1}
    apply_report_result(row, reviewed_game())
    assert row == {"seat": 1}


@pytest.mark.asyncio
async def test_analysis_keeps_rank_even_when_nickname_lookup_fails(tmp_path):
    data = compact_report(reviewed_game())
    online = SimpleNamespace(
        submit=AsyncMock(return_value=(data, "https://mjai.ekyu.moe/report/abcdef0123456789.json"))
    )
    batches = BatchReviews(
        tmp_path, None, SimpleNamespace(client=SimpleNamespace(url=None)), online
    )
    job = await batches.create_links(["https://game.maj-soul.com/1/?paipu=abcdefgh"], "g", "u")
    batches.name_resolver = AsyncMock(side_effect=ReviewError("牌谱屋未收录银之间"))
    await batches._analyze_row(job["id"], "g", "u", 0, job["rows"][0])
    row = batches.load(job["id"], "g", "u")["rows"][0]
    assert row["status"] == "done" and row["rank"] == 2
    assert row["rank_source"] == "mortal_mjai"
    assert row.get("player_name") is None
    assert row["metrics"] == report_metrics(data)
    online.submit.assert_awaited_once()
