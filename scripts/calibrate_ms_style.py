"""Build radar scales from saved Paipuya cohorts, histograms and player profiles."""

import argparse
import hashlib
import json
from math import sqrt
from pathlib import Path
from statistics import NormalDist, quantiles

from pupulinbot.ms_options import ROOMS
from pupulinbot.ms_style_radar import AXES, composite_metrics, metric_value, speed_turns

NORMAL_IQR = 2 * NormalDist().inv_cdf(0.75)
PUBLISHED_COMPOSITES = {"铳点损失", "净打点效率"}
MIN_JOINT_PROFILES = 24
SPEED_METRIC = "和立牌平均巡目"
DEFAULT_SPEED_ROOM = 12
GOLD_SOUTH = 9


def histogram_quantile(histogram, quantile):
    """Invert the piecewise linear CDF of the equal-width full histogram."""
    bins = histogram["bins"]
    low, high = histogram["min"], histogram["max"]
    if not bins or not low < high or any(count < 0 for count in bins) or not sum(bins):
        raise ValueError("Invalid or empty histogram")
    width = (high - low) / len(bins)
    target = quantile * sum(bins)
    cumulative = 0
    for i, count in enumerate(bins):
        if count and cumulative + count >= target:
            return low + width * (i + (target - cumulative) / count)
        cumulative += count
    raise ValueError("Quantile outside histogram")


def estimate_speed_sampling(payloads, win_weight, source_room):
    windows = {}
    digests = []
    for payload in payloads:
        contributes = False
        for line in payload.splitlines():
            row = json.loads(line)
            if "player_key" in row and row["room"] == source_room:
                windows.setdefault(row["player_key"], {})[row.get("games", 100)] = row
                contributes = True
        if contributes:
            digests.append(hashlib.sha256(payload).hexdigest())

    residuals = []
    for pair in windows.values():
        if not {12, 100} <= pair.keys():
            continue
        short, long = pair[12], pair[100]
        short_speed, long_speed = (speed_turns(row["raw"], win_weight) for row in (short, long))
        if short_speed is None or long_speed is None or not 0 < short["hands"] < long["hands"]:
            continue
        residuals.append((short_speed - long_speed) / sqrt(1 / short["hands"] - 1 / long["hands"]))
    if len(residuals) < MIN_JOINT_PROFILES:
        raise ValueError(f"Insufficient matched 12/100-game windows for room {source_room}")
    q1, _, q3 = quantiles(residuals, method="inclusive")
    if q3 <= q1:
        raise ValueError(f"Zero speed sampling IQR for room {source_room}")
    return {
        "source_room": source_room,
        "matched_players": len(residuals),
        "variance": ((q3 - q1) / NORMAL_IQR) ** 2,
        "source_sha256": digests,
    }


def build_reference(data, source_digest, profiles, profile_digest, speed_windows):
    rooms = {}
    for modes in ROOMS[4].values():
        for mode_id in modes:
            mode = str(mode_id)
            histograms = data["global_histogram"][mode]["0"]
            counts = histograms["count"]["histogramFull"]
            raw_profiles = [row for row in profiles["rooms"][mode] if row["count"] >= counts["min"]]
            if len(raw_profiles) < MIN_JOINT_PROFILES:
                raise ValueError(f"Insufficient joint profiles for {mode}")
            cohorts = data["global_statistics_2"][mode]
            wins = sum(
                row["extended"]["和牌率"] * row["extended"]["count"] for row in cohorts.values()
            )
            riichis = sum(
                row["extended"]["立直率"] * row["extended"]["count"] for row in cohorts.values()
            )
            win_weight = wins / (wins + riichis)
            derived_profiles = [
                {**composite_metrics(row), SPEED_METRIC: speed_turns(row, win_weight)}
                for row in raw_profiles
            ]
            scales, quartiles, observations = {}, {}, {}
            for axis in AXES:
                metric = axis.metric
                if metric.key in PUBLISHED_COMPOSITES:
                    histogram = histograms[metric.key]["histogramFull"]
                    q1, q3 = (histogram_quantile(histogram, q) for q in (0.25, 0.75))
                    observations[metric.key] = sum(histogram["bins"])
                else:
                    values = [
                        value
                        for row in derived_profiles
                        if (value := metric_value(row, metric)) is not None
                    ]
                    if len(values) < MIN_JOINT_PROFILES:
                        raise ValueError(f"Insufficient values for {mode}/{metric.key}")
                    q1, _, q3 = quantiles(values, method="inclusive")
                    observations[metric.key] = len(values)
                if q3 <= q1:
                    raise ValueError(f"Zero IQR for {mode}/{metric.key}")
                quartiles[metric.key] = [q1, q3]
                scales[metric.key] = (q3 - q1) / NORMAL_IQR

            levels = {}
            for level, row in cohorts.items():
                basic, extended = row["basic"], row["extended"]
                rank_rates = basic["rank_rates"]
                if len(rank_rates) != 4 or abs(sum(rank_rates) - 1) > 1e-6:
                    raise ValueError(f"Invalid placements for {mode}/{level}")
                derived = composite_metrics(extended)
                derived[SPEED_METRIC] = speed_turns(extended, win_weight)
                metrics = {key: value for key, value in derived.items() if value is not None}
                for axis in AXES:
                    for metric in axis.inputs:
                        if metric.key in derived:
                            continue
                        value = metric_value(extended, metric)
                        if value is not None:
                            metrics[metric.key] = value
                levels[level] = {
                    "games": basic["count"],
                    "hands": extended["count"],
                    "rank_rates": rank_rates,
                    "metrics": metrics,
                }
            rooms[mode] = {
                "histogram_players": sum(counts["bins"]),
                "min_hands": counts["min"],
                "speed_win_weight": win_weight,
                "quartiles": quartiles,
                "scales": scales,
                "observations": observations,
                "levels": levels,
            }
    gold_south = rooms[str(GOLD_SOUTH)]
    gold_south["speed_sampling"] = estimate_speed_sampling(
        speed_windows, gold_south["speed_win_weight"], GOLD_SOUTH
    )
    return {
        "retrieved_at": data["retrieved_at"],
        "source": ["global_statistics_2", "global_histogram"],
        "source_sha256": source_digest,
        "speed_sampling": estimate_speed_sampling(
            speed_windows, rooms[str(DEFAULT_SPEED_ROOM)]["speed_win_weight"], DEFAULT_SPEED_ROOM
        ),
        "profile_sample": {
            "anchor": profiles["anchor"],
            "lookbacks_days": profiles["lookbacks_days"],
            "room_overrides": profiles.get("room_overrides", {}),
            "limit_per_room": profiles["limit_per_room"],
            "source": "games → player_extended_stats",
            "source_sha256": profile_digest,
        },
        "rooms": rooms,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--speed-windows", type=Path, nargs="+", required=True)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = args.input.read_bytes()
    profiles = args.profiles.read_bytes()
    data = build_reference(
        json.loads(payload),
        hashlib.sha256(payload).hexdigest(),
        json.loads(profiles),
        hashlib.sha256(profiles).hexdigest(),
        [path.read_bytes() for path in args.speed_windows],
    )
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
