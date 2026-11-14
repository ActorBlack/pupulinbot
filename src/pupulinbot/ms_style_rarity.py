"""Rarity colors from observed appearances in the four-player style card."""

import json
from functools import lru_cache
from pathlib import Path

TARGET_SAMPLES = 5000
# Lower bounds in basis points, checked from most to least common.
TIERS = (
    (900, "gray"),
    (450, "green"),
    (300, "blue"),
    (200, "purple"),
    (50, "orange"),
    (0, "gold"),
)

# Plate, edge, socket, accent, text.
PALETTES = {
    "gray": ("#f3f6fa", "#d0d9e5", "#e7edf5", "#eaf3ff", "#32445a"),
    "green": ("#eef7f0", "#c1dbc9", "#ddede1", "#c8f5d7", "#275542"),
    "blue": ("#edf5fd", "#c4d8ed", "#dceaf9", "#c9e7ff", "#264f79"),
    "purple": ("#f5effb", "#dacbeb", "#eadff5", "#ebd5ff", "#593974"),
    "orange": ("#fff3ea", "#eed3be", "#f8e5d5", "#ffdab7", "#7c451f"),
    "gold": ("#fff5d8", "#e3c98d", "#faecbe", "#fff0ca", "#634822"),
    "combination": ("#f1ebe0", "#d2c9b9", "#526e89", "#fff7e8", "#253a50"),
    "easter_egg": ("#f8f2fa", "#e2d3e9", "#eee2f3", "#f8edff", "#624773"),
}


@lru_cache(maxsize=1)
def rarity_counts():
    path = Path(__file__).with_name("assets") / "ms_style_rarity.json"
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("complete") is not True
        or data.get("samples") != TARGET_SAMPLES
        or not isinstance(data.get("visible_hits"), dict)
    ):
        return None
    counts = data["visible_hits"]
    if any(type(count) is not int or not 0 <= count <= TARGET_SAMPLES for count in counts.values()):
        return None
    return counts


def tier_for_count(hits):
    return next(tier for minimum, tier in TIERS if hits * 10000 >= TARGET_SAMPLES * minimum)


def rarity_tier(tag):
    if tag == "普普林":
        return "easter_egg"
    counts = rarity_counts()
    if counts is None:
        return "gray"
    return tier_for_count(counts[tag]) if tag in counts else "gray"
