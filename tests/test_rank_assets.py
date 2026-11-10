import hashlib
import json

import pytest
from PIL import Image

from pupulinbot import rank_assets
from pupulinbot.monitor_card import rank_snapshot


@pytest.mark.parametrize("mode", [3, 4])
@pytest.mark.parametrize("major", [1, 2, 3, 4, 5, 7])
def test_all_official_rank_images_are_local_transparent_and_integrity_checked(mode, major):
    icon = rank_assets.rank_icon(major, mode)
    assert icon is not None and icon.mode == "RGBA" and icon.size == (64, 64)
    assert icon.getchannel("A").getextrema() == (0, 255)
    manifest = json.loads((rank_assets.DIRECTORY / "ORIGIN.json").read_text())
    prefix = "sima_" if mode == 4 else "sanma_"
    filename = prefix + rank_assets.NAMES[major] + ".png"
    entry = manifest["files"][filename]
    assert (
        hashlib.sha256((rank_assets.DIRECTORY / filename).read_bytes()).hexdigest()
        == entry["sha256"]
    )
    assert "/extendRes/level/" + filename in entry["url"]
    with Image.open(rank_assets.DIRECTORY / filename) as image:
        assert image.size == (256, 256)


def test_sanma_and_four_player_badges_use_distinct_original_artwork():
    assert rank_assets.rank_icon(4, 3).tobytes() != rank_assets.rank_icon(4, 4).tobytes()
    assert rank_assets.rank_icon(5, 3).tobytes() != rank_assets.rank_icon(5, 4).tobytes()
    assert rank_assets.rank_icon(99) is None
    assert rank_assets.rank_icon(4, 2) is None


def test_unknown_or_missing_artwork_degrades_without_inventing_an_icon(tmp_path, monkeypatch):
    rank_assets.rank_icon.cache_clear()
    with monkeypatch.context() as m:
        m.setattr(rank_assets, "DIRECTORY", tmp_path)
        assert rank_assets.rank_icon(4) is None
    rank_assets.rank_icon.cache_clear()
    assert rank_assets.rank_icon(4) is not None


def test_legacy_celestial_maps_to_current_celestial_badge_without_fake_pt():
    rank = rank_snapshot({"level": 10601}, None)
    assert rank["major"] == 7 and rank["points"] is None
