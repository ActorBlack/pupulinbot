"""Unmodified pure parsers extracted from browser.py; see LICENSE and ORIGIN.json."""
BAD_MOVE_STRICT_LIMIT = 5
BAD_MOVE_LOOSE_LIMIT = 10

def parse_killerducky_metadata(data):
    """Convert KillerDucky's report JSON into the metadata shape used by results.py."""
    data = data if isinstance(data, dict) else {}
    review = data.get("review")
    review = review if isinstance(review, dict) else {}

    total_matches = review.get("total_matches")
    total_reviewed = review.get("total_reviewed")
    matches_total = ""
    if (
        isinstance(total_matches, (int, float))
        and isinstance(total_reviewed, (int, float))
        and total_reviewed > 0
    ):
        rate = 100 * total_matches / total_reviewed
        matches_total = f"{int(total_matches)}/{int(total_reviewed)} = {rate:.3f}%"

    rating = review.get("rating")
    formatted_rating = ""
    if isinstance(rating, (int, float)):
        formatted_rating = f"{rating * 100:.3f}"

    def text(value):
        return "" if value is None else str(value)

    return {
        "engine": text(data.get("engine")),
        "model tag": text(review.get("model_tag")),
        "rating": formatted_rating,
        "matches/total": matches_total,
        "temperature": text(review.get("temperature")),
        "game length": text(data.get("game_length")),
        "player id": text(data.get("player_id")),
        "review duration": text(data.get("review_time")),
    }

def parse_killerducky_bad_move_stats(
    data,
    strict_limit=BAD_MOVE_STRICT_LIMIT,
    loose_limit=BAD_MOVE_LOOSE_LIMIT,
):
    """Calculate bad-move rates from KillerDucky's structured Mortal decisions."""
    data = data if isinstance(data, dict) else {}
    review = data.get("review")
    review = review if isinstance(review, dict) else {}
    total_reviewed = review.get("total_reviewed")
    denominator = total_reviewed if isinstance(total_reviewed, int) and total_reviewed >= 0 else None

    strict_count = 0
    loose_count = 0
    mismatch_count = 0
    unparsed_count = 0

    kyokus = review.get("kyokus")
    for kyoku in kyokus if isinstance(kyokus, list) else []:
        if not isinstance(kyoku, dict):
            continue
        entries = kyoku.get("entries")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or entry.get("is_equal") is not False:
                continue

            mismatch_count += 1
            actual_index = entry.get("actual_index")
            details = entry.get("details")
            if (
                not isinstance(actual_index, int)
                or not isinstance(details, list)
                or actual_index < 0
                or actual_index >= len(details)
                or not isinstance(details[actual_index], dict)
            ):
                unparsed_count += 1
                continue

            probability = details[actual_index].get("prob")
            if not isinstance(probability, (int, float)):
                unparsed_count += 1
                continue

            probability_percent = probability * 100
            if probability_percent <= strict_limit:
                strict_count += 1
            if probability_percent <= loose_limit:
                loose_count += 1

    def format_rate(count):
        if denominator is None or denominator <= 0:
            return ""
        return f"{100 * count / denominator:.3f}%"

    return {
        "badMoveRate5": format_rate(strict_count),
        "badMoveCount5": str(strict_count),
        "badMoveRate10": format_rate(loose_count),
        "badMoveCount10": str(loose_count),
        "badMoveDenominator": "" if denominator is None else str(denominator),
        "badMoveOrderLossCount": str(mismatch_count),
        "badMoveUnparsedCount": str(unparsed_count),
    }
