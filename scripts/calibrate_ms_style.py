"""Build radar scales from saved Paipuya cohorts, histograms and player profiles."""

import argparse
import hashlib
import json
from pathlib import Path
from statistics import NormalDist, quantiles

from pupulinbot.ms_options import ROOMS
from pupulinbot.ms_style_radar import AXES, composite_metrics, metric_value

NORMAL_IQR = 2 * NormalDist().inv_cdf(0.75)
PUBLISHED_COMPOSITES = {"铳点损失", "净打点效率"}
MIN_JOINT_PROFILES = 24


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


def build_reference(data, source_digest, profiles, profile_digest):
    rooms = {}
    for modes in ROOMS[4].values():
        for mode_id in modes:
            mode = str(mode_id)
            histograms = data["global_histogram"][mode]["0"]
            counts = histograms["count"]["histogramFull"]
            raw_profiles = [row for row in profiles["rooms"][mode] if row["count"] >= counts["min"]]
            if len(raw_profiles) < MIN_JOINT_PROFILES:
                raise ValueError(f"Insufficient joint profiles for {mode}")
            derived_profiles = [composite_metrics(row) for row in raw_profiles]
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
            for level, row in data["global_statistics_2"][mode].items():
                basic, extended = row["basic"], row["extended"]
                rank_rates = basic["rank_rates"]
                if len(rank_rates) != 4 or abs(sum(rank_rates) - 1) > 1e-6:
                    raise ValueError(f"Invalid placements for {mode}/{level}")
                derived = composite_metrics(extended)
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
                "quartiles": quartiles,
                "scales": scales,
                "observations": observations,
                "levels": levels,
            }
    return {
        "retrieved_at": data["retrieved_at"],
        "source": ["global_statistics_2", "global_histogram"],
        "source_sha256": source_digest,
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
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = args.input.read_bytes()
    profiles = args.profiles.read_bytes()
    data = build_reference(
        json.loads(payload),
        hashlib.sha256(payload).hexdigest(),
        json.loads(profiles),
        hashlib.sha256(profiles).hexdigest(),
    )
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
