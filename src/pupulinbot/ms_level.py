"""One Mahjong Soul rank/unit model for text, cards, history and standings.

Upstream: SAPikachu/amae-koromo src/data/types/level.ts. Celestial uses
100 internal score units per soul point; its minor rank is a level, never stars.
"""

import math
from decimal import ROUND_HALF_UP, Decimal

CAPS = (20, 80, 200, 600, 800, 1000, 1200, 1400, 2000, 2800, 3200, 3600, 4000, 6000, 9000)
NAMES = {1: "初", 2: "士", 3: "杰", 4: "豪", 5: "圣", 7: "魂天"}
FULL_NAMES = {1: "初心", 2: "雀士", 3: "雀杰", 4: "雀豪", 5: "雀圣", 7: "魂天"}
SOUL_COLOR = "#55c7dc"


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def rank_identity(value):
    """Read a rank ID without inventing missing points or applying a result delta."""
    code = _number(value.get("id") if isinstance(value, dict) else value)
    if code is None or not code.is_integer() or int(code) // 10000 not in {1, 2}:
        return None
    major, star = divmod(int(code) % 10000, 100)
    if (major, star) == (6, 1):
        major = 7
    if major not in NAMES or not 1 <= star <= (20 if major == 7 else 3):
        return None
    return {"major": major, "star": star, "points": None, "cap": None}


def rank_progress(value):
    rank = rank_identity(value)
    if rank is None or not isinstance(value, dict):
        return None
    code, score, delta = (_number(value.get(k)) for k in ("id", "score", "delta"))
    if score is None or delta is None:
        return None
    major, star = rank["major"], rank["star"]
    points = score + delta
    if not math.isfinite(points):
        return None
    if int(code) % 10000 // 100 == 6:
        points = math.ceil(points / 100) * 10 + 200

    def cap():
        return (None if star == 20 else 2000) if major == 7 else CAPS[(major - 1) * 3 + star - 1]

    maximum = cap()
    if maximum is not None and points >= maximum:
        star += 1
        if major != 7 and star == 4:
            major, star = major + 1, 1
            if major == 6:
                major = 7
        maximum = cap()
        points = (0 if maximum is None else maximum / 2) if major != 1 else 0
    elif points < 0:
        if major == 1 or (major, star) == (2, 1) or maximum is None:
            points = 0
        else:
            star -= 1
            if star == 0:
                major, star = (5 if major == 7 else major - 1), 3
            maximum = cap()
            points = maximum / 2
    return {"major": major, "star": star, "points": points, "cap": maximum}


def rank_label(rank, *, full=False):
    if rank is None:
        return "未知段位"
    major, star = rank["major"], rank["star"]
    if major == 7:
        return f"魂天Lv{star}"
    return f"{FULL_NAMES[major]}{star}星" if full else f"{NAMES[major]}{star}"


def point_unit(rank):
    return "魂珠" if rank and rank["major"] == 7 else "PT"


def point_value(value, rank):
    value = _number(value)
    return None if value is None else value / (100 if point_unit(rank) == "魂珠" else 1)


def point_number(value, unit="PT", *, signed=False):
    value = _number(value)
    if value is None:
        return "—"
    if unit == "魂珠":
        # Display exactly one decimal, matching the in-game soul-point counter.
        rounded = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if not rounded:
            rounded = abs(rounded)
        return format(rounded, "+.1f" if signed else ".1f")
    return format(value, "+g" if signed else "g")


def point_text(rank, *, capacity=True, unit=False, spaced=False):
    name = point_unit(rank)
    text = point_number(point_value(rank["points"], rank), name) if rank else "—"
    if capacity and rank and rank["cap"]:
        text += (" / " if spaced else "/") + point_number(point_value(rank["cap"], rank), name)
    return text + (" " + name if unit else "")


def format_level(value):
    rank = rank_progress(value)
    if rank is None:
        return "暂无完整段位数据"
    return rank_label(rank) + " " + point_text(rank, unit=rank["major"] == 7)


def seat_delta(seat):
    """Return a result in its original rank's unit, even across promotion/demotion."""
    raw = _number(seat.get("gradingScore", seat.get("grading_score")))
    if raw is None:
        return None, None
    level = seat.get("level")
    rank = rank_identity(level)
    # Unknown historical rank: retain its raw value with a neutral unit.
    if rank is None:
        return raw, "积分"
    code = level.get("id") if isinstance(level, dict) else level
    if int(_number(code)) % 10000 // 100 == 6:
        before = _number(level.get("score")) if isinstance(level, dict) else None
        if before is not None:
            return (math.ceil((before + raw) / 100) - math.ceil(before / 100)) / 10, "魂珠"
        # The legacy score conversion rounds to 100 old points. Without a
        # starting score, only a whole 100-point step has an exact difference.
        return (raw / 1000 if raw % 100 == 0 else None), "魂珠"
    return point_value(raw, rank), point_unit(rank)


def delta_text(seat):
    value, unit = seat_delta(seat)
    return "—" if value is None else f"{unit} {point_number(value, unit, signed=True)}"


def delta_totals(games, account):
    totals = {}
    for game in games:
        value, unit = seat_delta(game.seat(account) or {})
        if value is not None:
            totals[unit] = totals.get(unit, 0) + value
    return totals


def totals_text(totals):
    return (
        " / ".join(
            f"{unit} {point_number(totals[unit], unit, signed=True)}"
            for unit in ("PT", "魂珠", "积分")
            if unit in totals
        )
        or "—"
    )
