"""Local original Mahjong Soul rank artwork; no network calls during rendering."""

import json
from functools import lru_cache
from pathlib import Path

from PIL import Image

DIRECTORY = Path(__file__).parent / "assets" / "ranks"
NAMES = {1: "fish", 2: "queshi", 3: "quejie", 4: "quehao", 5: "quesheng", 7: "huntian"}


@lru_cache(maxsize=32)
def rank_icon(major, mode=4, size=64):
    if major not in NAMES or mode not in {3, 4}:
        return None

    filename = ("sima_" if mode == 4 else "sanma_") + NAMES[major] + ".png"
    try:
        with Image.open(DIRECTORY / filename) as source:
            image = source.convert("RGBA")
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        return image
    except (OSError, ValueError):
        # A damaged/missing local asset must not stop a settlement notification.
        return None


@lru_cache(maxsize=128)
def ui_sprite(name):
    try:
        with Image.open(DIRECTORY / (name + ".png")) as source:
            return source.convert("RGBA")
    except (OSError, ValueError):
        return None


@lru_cache(maxsize=128)
def rank_badge(major, minor, mode=4, size=90):
    """Original client ring, emblem and three flower slots, or blue Lv digits."""
    if not isinstance(minor, int) or not 1 <= minor <= (20 if major == 7 else 3):
        return None
    icon = rank_icon(major, mode, 224)
    if icon is None:
        return None
    badge = Image.new("RGBA", (256, 256))
    ring = ui_sprite("ui_rank_bg")
    if ring is not None:
        badge.alpha_composite(ring, (7, 20))
    badge.alpha_composite(icon, (20, 0))
    if major == 7:
        names = ["ui_Lv"] + ["ui_hl_" + digit for digit in str(minor)]
        cursor = 37
        for name in names:
            sprite = ui_sprite(name)
            if sprite is not None:
                badge.alpha_composite(sprite, (cursor, 199))
                cursor += sprite.width - 3
    else:
        for index, xy in enumerate(((4, 141), (45, 184), (103, 201))):
            sprite = ui_sprite("ui_star_shine" if index >= 3 - minor else "ui_star_dark")
            if sprite is not None:
                badge.alpha_composite(sprite.resize((57, 55), Image.Resampling.LANCZOS), xy)
    return badge.resize((size, size), Image.Resampling.LANCZOS)


@lru_cache(maxsize=4)
def _font_meta(name):
    try:
        return json.loads((DIRECTORY / ("font_" + name + ".json")).read_text())
    except (OSError, ValueError):
        return None


def game_number(value, height=36, color="#4aaeb3", name="youyuan"):
    """Render the original game's numeric bitmap font, using original advances."""
    meta = _font_meta(name)
    if not meta or any(c not in meta["glyphs"] for c in str(value)):
        return None
    glyphs = [meta["glyphs"][c] for c in str(value)]
    width = sum(g["xadvance"] for g in glyphs) + 8
    layer = Image.new("RGBA", (max(1, width), meta["line_height"] + 14))
    cursor = 0
    for g in glyphs:
        sprite = ui_sprite(Path(g["file"]).stem)
        if sprite is None:
            return None
        tinted = Image.new("RGBA", sprite.size, color)
        tinted.putalpha(sprite.getchannel("A"))
        layer.alpha_composite(tinted, (cursor + g["xoffset"], g["yoffset"]))
        cursor += g["xadvance"]
    bounds = layer.getbbox()
    if bounds is None:
        return None
    layer = layer.crop(bounds)
    return layer.resize(
        (max(1, round(layer.width * height / layer.height)), height), Image.Resampling.LANCZOS
    )


def point_run(rank, height=32, *, capacity=False):
    from .ms_level import point_text, point_unit

    unit = point_unit(rank)
    value = game_number(point_text(rank, capacity=capacity), height)
    if value is None:
        return None
    if unit == "魂珠":
        orb = ui_sprite("ui_hpoint_l")
        if orb is None:
            return value
        orb = orb.resize((round(height * 0.70), round(height * 0.84)), Image.Resampling.LANCZOS)
        run = Image.new("RGBA", (orb.width + 12 + value.width, height + 4))
        run.alpha_composite(orb, (0, 2))
        run.alpha_composite(value, (orb.width + 12, 0))
    else:
        pt = game_number("pt", max(12, round(height * 0.52)), color="#75afd6", name="hanyi")
        if pt is None:
            return value
        run = Image.new("RGBA", (value.width + 6 + pt.width, height + 4))
        run.alpha_composite(value, (0, 0))
        run.alpha_composite(pt, (value.width + 6, height - pt.height))
    return run
