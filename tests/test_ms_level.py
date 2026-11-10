import hashlib
import io
import json
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from pupulinbot.formatting import format_records
from pupulinbot.models import GameRecord, Player
from pupulinbot.monitor_card import render_monitor_card
from pupulinbot.ms_charts import pt_series, render_history
from pupulinbot.ms_level import (
    delta_text,
    delta_totals,
    format_level,
    point_text,
    rank_identity,
    rank_progress,
    totals_text,
)
from pupulinbot.ms_options import level_label, parse_options
from pupulinbot.ms_profile import profile_content, render_profile
from pupulinbot.ms_sample import sample_summary
from pupulinbot.ms_sample_chart import render_recent, render_sample
from pupulinbot.rank_assets import DIRECTORY, point_run, rank_badge


def record(index=0, *, level=10712, delta=-20):
    start = datetime(2026, 11, 10, tzinfo=UTC) + timedelta(hours=index)
    return GameRecord(
        str(index),
        start,
        start + timedelta(minutes=30),
        "16",
        [
            {
                "account_id": 42,
                "nickname": "魂天示例",
                "level": level,
                "gradingScore": delta,
                "rank": 1,
                "score": 35000,
            }
        ],
        {},
    )


@pytest.mark.parametrize("base", [10000, 20000])
def test_game_reference_lv12_nine_soul_points_in_both_modes(base):
    raw = {"id": base + 712, "score": 900, "delta": 0}
    assert level_label(raw) == "魂天Lv12"
    assert level_label(base + 712) == "魂天Lv12"
    assert format_level(raw) == "魂天Lv12 9.0/20.0 魂珠"
    assert point_text(rank_progress(raw), capacity=False) == "9.0"
    assert delta_text({"level": base + 712, "gradingScore": -20}) == "魂珠 -0.2"
    assert delta_text({"level": base + 502, "gradingScore": 146}) == "PT +146"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"id": 10503, "score": 8990, "delta": 100}, "魂天Lv1 10.0/20.0 魂珠"),
        ({"id": 10712, "score": 1990, "delta": 20}, "魂天Lv13 10.0/20.0 魂珠"),
        ({"id": 10712, "score": 10, "delta": -20}, "魂天Lv11 10.0/20.0 魂珠"),
        ({"id": 10701, "score": 10, "delta": -20}, "圣3 4500/9000"),
        ({"id": 10719, "score": 1990, "delta": 20}, "魂天Lv20 0.0 魂珠"),
        ({"id": 10720, "score": 2600, "delta": 50}, "魂天Lv20 26.5 魂珠"),
        ({"id": 10601, "score": 10000, "delta": 0}, "魂天Lv1 12.0/20.0 魂珠"),
    ],
)
def test_upstream_rank_boundary_vectors_and_legacy_conversion(raw, expected):
    assert format_level(raw) == expected


def test_result_unit_uses_game_rank_even_when_player_has_just_promoted():
    assert delta_text({"level": 10503, "gradingScore": 100}) == "PT +100"
    assert delta_text({"level": 10701, "gradingScore": -20}) == "魂珠 -0.2"
    assert level_label(10712) != "魂天12星"
    for bad in (True, float("nan"), 10700, 10721, 10504, 99999, "bad"):
        assert rank_identity(bad) is None


def test_mixed_rank_history_never_adds_pt_to_soul_points():
    games = [record(0, level=10503, delta=200), record(1, delta=20)]
    totals = delta_totals(games, 42)
    assert totals == {"PT": 200, "魂珠": 0.2}
    assert totals_text(totals) == "PT +200 / 魂珠 +0.2"
    assert sample_summary(games, 42)["point_totals"] == totals
    assert all(math.isnan(v) for v in pt_series(games, 42))
    assert pt_series(games, 42, "PT") == [200, 200]
    assert pt_series(games, 42, "魂珠") == [0, 0.2]
    assert "魂珠 +0.2" in format_records(games, 42)
    assert "PT +20" not in format_records([games[-1]], 42)


def test_unknown_rank_is_not_silently_counted_as_pt_or_soul():
    game = record(level=None, delta=20)
    assert delta_text(game.players[0]) == "积分 +20"
    assert delta_totals([game], 42) == {"积分": 20}


def test_missing_other_unit_does_not_break_soul_series_but_missing_soul_does():
    games = [record(0, level=10503, delta=None), record(1, delta=20)]
    assert pt_series(games, 42, "魂珠") == [0, 0.2]
    assert all(math.isnan(v) for v in pt_series(games, 42, "PT"))
    games.append(record(2, delta=None))
    assert math.isnan(pt_series(games, 42, "魂珠")[-1])


def test_legacy_delta_rounding_is_not_invented_without_a_starting_score():
    assert delta_text({"level": 10601, "gradingScore": 100}) == "魂珠 +0.1"
    assert delta_text({"level": 10601, "gradingScore": -20}) == "—"
    assert delta_text({"level": {"id": 10601, "score": 10050}, "gradingScore": -20}) == "魂珠 +0.0"


def test_original_ui_art_and_numeric_glyphs_are_complete_and_verified():
    manifest = json.loads((DIRECTORY / "ORIGIN.json").read_text())
    for name, entry in manifest["files"].items():
        assert hashlib.sha256((DIRECTORY / name).read_bytes()).hexdigest() == entry["sha256"]
    for mode in (3, 4):
        for major in (1, 2, 3, 4, 5, 7):
            for minor in (1, 12, 20) if major == 7 else (1, 2, 3):
                badge = rank_badge(major, minor, mode)
                assert badge is not None and badge.mode == "RGBA" and badge.size == (90, 90)
    assert rank_badge(7, 12).tobytes() != rank_badge(7, 2).tobytes()
    for code in (10502, 10712, 10720):
        image = point_run(rank_progress({"id": code, "score": 900, "delta": 0}), capacity=True)
        assert image is not None and image.getbbox()


def test_celestial_card_labels_and_result_are_not_pt_or_stars(monkeypatch):
    texts = []
    original = ImageDraw.ImageDraw.text

    def capture(draw, xy, value, *args, **kwargs):
        texts.append(str(value))
        return original(draw, xy, value, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    game = record()
    stats = SimpleNamespace(raw={"level": {"id": 10712, "score": 900, "delta": 0}})
    image = render_monitor_card(game, profiles={42: {"stats": stats}})
    assert Image.open(io.BytesIO(image)).size == (1080, 706)
    assert "魂珠 -0.2" in texts
    assert "9.0 / 20.0 魂珠" in texts
    assert not any("PT" in t or "★" in t or "星" in t for t in texts)


def test_profile_current_and_highest_use_independent_correct_units():
    from test_ms_profile import stats

    value = stats()
    value.raw.update(
        level={"id": 10712, "score": 900, "delta": 0},
        max_level={"id": 10713, "score": 1300, "delta": 0},
    )
    player = Player(42, "魂天示例")
    content = profile_content(player, value)
    assert content["rank"] == "魂天Lv12"
    assert content["point_text"] == "9.0 / 20.0 魂珠"
    assert content["highest_rank"] == "魂天Lv13"
    assert content["highest_points"] == "13.0 / 20.0"
    assert Image.open(io.BytesIO(render_profile(player, value))).size == (1600, 1148)


def test_recent_sample_and_history_display_soul_units_without_missing_font_glyphs(monkeypatch):
    from pupulinbot.ms_sample_chart import Figure

    texts = []
    original = Figure.text

    def capture(fig, x, y, value, *args, **kwargs):
        texts.append(str(value))
        return original(fig, x, y, value, *args, **kwargs)

    monkeypatch.setattr(Figure, "text", capture)
    player, games, options = Player(42, "魂天示例"), [record()], parse_options("-l 1")
    assert render_recent(player, games).startswith(b"\x89PNG")
    assert render_sample(player, games, False, options).startswith(b"\x89PNG")
    assert "魂珠 -0.2" in texts
    assert render_history("mslife", player, games, False, options).startswith(b"\x89PNG")
