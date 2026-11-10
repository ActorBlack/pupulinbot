import io
import json
import math
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image, ImageDraw

from pupulinbot.tenhou import TenhouClient, TenhouError
from pupulinbot.tenhou_options import parse_tenhou_options
from pupulinbot.tenhou_profile import phoenix_percentiles
from pupulinbot.tenhou_queries import TenhouQueries, make_view
from pupulinbot.tenhou_query_card import SIZE, percentile_label, render_profile

FIXTURES = Path(__file__).parent / "fixtures/tenhou"


@pytest.mark.parametrize("games", [1, 312, 499, 500, 1000])
def test_percentile_matches_public_site_without_applying_cohort_minimum_to_player(games):
    population = {
        "houjuuC": {"min": "500", "data": [".1", ".2", ".2", ".4"]},
        "agariC": {"min": "1000", "data": [".1", ".2", ".3", ".4"]},
    }
    stats = {"totalrecord": games, "houjuuC": 0.2, "agariC": 0.3}
    assert phoenix_percentiles(stats, population) == {"houjuuC": 50, "agariC": 50}
    stats["houjuuC"] = 0.05
    assert phoenix_percentiles(stats, population)["houjuuC"] == 0


@pytest.mark.parametrize("games", [0, -1, None, math.nan])
def test_no_player_games_does_not_create_comparison(games):
    assert (
        phoenix_percentiles(
            {"totalrecord": games, "agariC": 0.2},
            {"agariC": {"min": "500", "data": [0.1, 0.2, 0.3]}},
        )
        == {}
    )


@pytest.mark.parametrize(
    "distribution",
    [
        None,
        {},
        {"data": [1, 1]},
        {"data": [3, 2, 1]},
        {"data": ["bad", "data"]},
        {"data": [1, 2], "min": "bad"},
        {"data": [1, 2], "min": math.inf},
    ],
)
def test_invalid_or_degenerate_reference_has_no_fabricated_comparison(distribution):
    assert phoenix_percentiles({"totalrecord": 851, "rate": 1}, {"rate": distribution}) == {}


@pytest.mark.parametrize(
    "pct,expected",
    [
        (0, "低位 1%"),
        (8, "低位 8%"),
        (49, "低位 49%"),
        (50, "高位 50%"),
        (72, "高位 28%"),
        (99, "高位 1%"),
        (100, "高位 1%"),
        (None, "—"),
        (101, "—"),
    ],
)
def test_tail_labels_match_website_minimum_one_percent(pct, expected):
    assert percentile_label(pct) == expected


@pytest.mark.asyncio
async def test_client_comparisons_strictly_match_mode_and_length_no_auth_and_one_cached_fetch():
    seen = []
    scopes = {
        "s4": {"agariC": {"min": "500", "data": [0.1, 0.2, 0.3, 0.4]}},
        "s4_2": {"agariC": {"min": "500", "data": [0.2, 0.3, 0.4, 0.5]}},
        "s3": {"agariC": {"min": "500", "data": [0.3, 0.4, 0.5, 0.6]}},
    }

    def respond(request):
        seen.append(request)
        assert request.url.path == "/s/phoenix_scale_all.js"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=scopes)

    source = TenhouClient(httpx.MockTransport(respond))
    stats = {"totalrecord": 851, "agariC": 0.3}
    try:
        assert await source.phoenix_percentiles(stats, 4) == {"agariC": 50}
        assert await source.phoenix_percentiles(stats, 4, 2) == {"agariC": 25}
        assert await source.phoenix_percentiles(stats, 3) == {"agariC": 0}
        assert await source.phoenix_percentiles(stats, 3, 2) == {}
        assert len(seen) == 1
    finally:
        await source.close()


def snapshot():
    row = json.loads((FIXTURES / "result.json").read_text())
    data = {"name": "applePie", "list": [row]}
    stats = json.loads((FIXTURES / "profile.json").read_text())["s4"]
    return data, stats


@pytest.mark.asyncio
async def test_image_uses_matching_scope_and_text_does_not_fetch_reference():
    data, stats = snapshot()
    source = AsyncMock()
    source.user_history.return_value = data
    source.phoenix_stats.return_value = stats
    source.phoenix_percentiles.return_value = {"agariC": 20}
    service = TenhouQueries(None, source)
    await service.query("th4", "applePie", "user", "group")
    source.phoenix_percentiles.assert_not_awaited()
    png = await service.query("th4", "applePie -i -r 凤", "user", "group")
    source.phoenix_percentiles.assert_awaited_once_with(stats, 4, 2)
    assert Image.open(io.BytesIO(png)).size == SIZE
    source.phoenix_percentiles.side_effect = TenhouError("reference timeout")
    assert (await service.query("th4", "applePie -i", "user", "group")).startswith(b"\x89PNG")


def test_all_rows_draw_comparison_without_confusing_raw_values_and_positions(monkeypatch):
    data, stats = snapshot()
    view = make_view(
        data,
        "applePie",
        parse_tenhou_options(""),
        recent=False,
        phoenix=stats,
        percentiles={"agariC": 8, "houjuuC": 90},
    )
    drawn = []
    original = ImageDraw.ImageDraw.text

    def capture(self, xy, text, *args, **kwargs):
        box = self.textbbox(xy, text, font=kwargs["font"], anchor=kwargs.get("anchor"))
        assert 0 <= box[0] <= box[2] <= SIZE[0]
        assert 0 <= box[1] <= box[3] <= SIZE[1]
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    assert render_profile(view).startswith(b"\x89PNG")
    assert "20.7%" in drawn and "11.5%" in drawn
    assert "低位 8%" in drawn and "高位 10%" in drawn
    assert any("中位数" in text for text in drawn)
    assert drawn.index("防守与流局") < drawn.index("立直与副露")
