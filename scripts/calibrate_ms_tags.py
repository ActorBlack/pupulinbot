"""Build a frozen jade-room reference from authenticated public aggregate exports.

Usage: python scripts/calibrate_ms_tags.py INPUT_DIRECTORY OUTPUT_JSON
Input: global_statistics_2_11.json, global_statistics_2_12.json, global_histogram.json.
No network access. Never substitutes the all-rank histogram for this five-grade cohort.
"""

import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

LEVELS = ("10402", "10403", "10501", "10502", "10503")
RATE_KEYS = ("和牌率", "放铳率", "副露率", "立直率")
WIN_KEYS = ("平均打点", "和了巡数", "默听率")


def ceil_rate(value):
    return math.ceil(value * 1000 - 1e-10) / 1000


def floor_rate(value):
    return math.floor(value * 1000 + 1e-10) / 1000


def build_reference(directory):
    histogram = json.loads((directory / "global_histogram.json").read_text())
    result = {
        "version": 2,
        "source_base": "https://5-data.amae-koromo.com/api/v2/pl4",
        "reference_levels": list(LEVELS),
        "reference_names": ["豪2", "豪3", "圣1", "圣2", "圣3"],
        "source_files": {},
        "modes": {},
    }
    for name in ("global_statistics_2_11", "global_statistics_2_12", "global_histogram"):
        path = directory / (name + ".json")
        result["source_files"][path.name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "retrieved_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
        }
    for mode in ("11", "12"):
        data = json.loads((directory / f"global_statistics_2_{mode}.json").read_text())[mode]
        rows = [data[level] for level in LEVELS]
        hands = sum(row["extended"]["count"] for row in rows)
        games = sum(row["basic"]["count"] for row in rows)
        wins = sum(row["extended"]["count"] * row["extended"]["和牌率"] for row in rows)
        means = {
            key: sum(row["extended"][key] * row["extended"]["count"] for row in rows) / hands
            for key in RATE_KEYS
        }
        means.update(
            {
                key: sum(
                    row["extended"][key] * row["extended"]["count"] * row["extended"]["和牌率"]
                    for row in rows
                )
                / wins
                for key in WIN_KEYS
            }
        )
        means["rank_rates"] = [
            sum(row["basic"]["rank_rates"][i] * row["basic"]["count"] for row in rows) / games
            for i in range(4)
        ]
        means["avg_rank"] = sum((i + 1) * rate for i, rate in enumerate(means["rank_rates"]))
        income_by_grade = {level: histogram[mode][level]["局收支"]["mean"] for level in LEVELS}
        income_mean = sum(income_by_grade.values()) / len(LEVELS)
        thresholds = {
            "call_high": ceil_rate(means["副露率"] + 0.05),
            "call_low": floor_rate(means["副露率"] - 0.05),
            "riichi_high": ceil_rate(means["立直率"] + 0.03),
            "deal_high": ceil_rate(means["放铳率"] + 0.02),
            "deal_low": floor_rate(means["放铳率"] - 0.02),
            "win_floor": ceil_rate(means["和牌率"] - 0.01),
            "win_attack": ceil_rate(means["和牌率"]),
            "win_high": ceil_rate(means["和牌率"] + 0.015),
            "dama_high": ceil_rate(means["默听率"] + 0.05),
            "points_high": math.ceil((means["平均打点"] + 800) / 100) * 100,
            "points_low": math.floor((means["平均打点"] - 500) / 100) * 100,
            "turn_low": math.floor((means["和了巡数"] - 0.4) * 10) / 10,
            "first_high": ceil_rate(means["rank_rates"][0] + 0.05),
            "fourth_low": floor_rate(means["rank_rates"][3] - 0.05),
            "income_high": math.ceil((income_mean + 100) / 10) * 10,
        }
        result["modes"][mode] = {
            "name": "玉东" if mode == "11" else "玉南",
            "player_game_appearances": games,
            "player_hand_appearances": hands,
            "means": means,
            "thresholds": thresholds,
            "levels": {level: data[level] for level in LEVELS},
            "income_reference": {
                "method": "equal-grade average of per-player means; not hand-weighted",
                "mean": income_mean,
                "by_grade": income_by_grade,
            },
            "cohort_percentiles_available": False,
        }
    return result


if __name__ == "__main__":
    destination = Path(sys.argv[2])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(build_reference(Path(sys.argv[1])), ensure_ascii=False, indent=2) + "\n"
    )
