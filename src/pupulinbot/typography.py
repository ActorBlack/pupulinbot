"""Shared chart typography with local CJK fonts and consistent numeric figures."""

import hashlib
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

os.environ.setdefault("MPLCONFIGDIR", str(Path("data/chart-cache").resolve()))


@lru_cache(maxsize=8)
def font_source(style="regular"):
    bold = style in {"title", "bold"}
    if style == "number":
        candidates = [
            ("/System/Library/Fonts/Avenir Next.ttc", 2),
            ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
        ]
    else:
        candidates = [
            ("/System/Library/Fonts/Hiragino Sans GB.ttc", 2 if bold else 0),
            (
                "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
                if bold
                else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                0,
            ),
            (
                "/System/Library/Fonts/STHeiti Medium.ttc"
                if bold
                else "/System/Library/Fonts/STHeiti Light.ttc",
                0,
            ),
        ]
    for filename, index in candidates:
        if Path(filename).is_file():
            return filename, index
    from matplotlib import font_manager

    return font_manager.findfont(
        font_manager.FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal")
    ), 0


@lru_cache(maxsize=64)
def face(size, style="regular"):
    filename, index = font_source(style)
    return ImageFont.truetype(filename, size, index=index)


@lru_cache(maxsize=8)
def profile_font_source(style="regular"):
    """Use lighter UI faces for profile cards without changing other charts."""
    if style == "number":
        candidate = Path("/System/Library/Fonts/Avenir Next.ttc")
        if candidate.is_file():
            return str(candidate), 5  # Medium, rather than Demi Bold.
    else:
        paths = [Path("/System/Library/Fonts/PingFang.ttc")]
        root = Path("/System/Library/AssetsV2/com_apple_MobileAsset_Font7")
        if root.is_dir():
            paths += sorted(root.glob("*/AssetData/PingFang.ttc"))
        for path in paths:
            if path.is_file():
                return str(path), 11 if style == "title" else 7 if style == "bold" else 3
    return font_source(style)


@lru_cache(maxsize=64)
def profile_face(size, style="regular"):
    filename, index = profile_font_source(style)
    return ImageFont.truetype(filename, size, index=index)


@lru_cache(maxsize=8)
def mpl_font(style="regular"):
    from matplotlib import font_manager

    filename, index = font_source(style)
    # Matplotlib only reads the first TTC face. Export the selected local face
    # into a private render cache; never modify or bundle the installed font.
    if index:
        try:
            from fontTools.ttLib import TTFont

            source = Path(filename)
            digest = hashlib.sha256(
                f"{filename}:{source.stat().st_mtime_ns}:{index}".encode()
            ).hexdigest()[:16]
            target = Path("data/font-cache") / (digest + ".ttf")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".ttf")
                os.close(fd)
                try:
                    with TTFont(filename, fontNumber=index) as font:
                        font.save(temporary)
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            filename = str(target.resolve())
        except (ImportError, OSError):
            pass
    return font_manager.FontProperties(fname=filename)
