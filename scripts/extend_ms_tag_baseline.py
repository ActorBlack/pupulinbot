"""Extend frozen tag references from saved, authenticated global_statistics_2 responses.

Run with PYTHONPATH=src python scripts/extend_ms_tag_baseline.py --input <directory>.
The directory contains global_statistics_2_{8,9,16}.json and its manifest.json.
"""

import argparse
import hashlib
import json
from pathlib import Path

from pupulinbot.ms_tag_reference import build_reference
from pupulinbot.ms_trait_tags import build_trait_reference


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("src/pupulinbot/assets/ms_tag_baseline.json")
    )
    args = parser.parse_args()
    document = json.loads(args.output.read_text())
    manifest = json.loads((args.input / "manifest.json").read_text())
    for mode, name in (("8", "金东"), ("9", "金南"), ("16", "王座南")):
        filename = f"global_statistics_2_{mode}.json"
        path = args.input / filename
        if not manifest.get(filename, {}).get("ok") or not path.is_file():
            if mode == "16":
                document["modes"].pop(mode, None)
                continue
            raise ValueError(f"{name}未取得有效统计")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != manifest[filename]["sha256"]:
            raise ValueError(f"{name}统计校验失败")
        data = json.loads(path.read_text()).get(mode, {})
        # Gold must include its lower ranks; Throne must include Konten.
        # Reusing Jade's historical high-rank cohort would omit most of a room.
        levels = data
        try:
            reference = build_reference(levels, name, source_mode=mode)
        except ValueError:
            if mode == "16":
                document["modes"].pop(mode, None)
                continue
            raise
        document["modes"][mode] = reference
        document["source_files"][filename] = {
            "sha256": digest,
            "retrieved_at": manifest[filename]["retrieved_at"],
        }
        print(
            json.dumps(
                {
                    "mode": mode,
                    "name": name,
                    "levels": list(levels),
                    "thresholds": reference["thresholds"],
                },
                ensure_ascii=False,
            )
        )
    document["version"] = 7
    document.pop("reference_levels", None)
    document.pop("reference_names", None)
    for mode in ("11", "12"):
        reference = document["modes"][mode]
        reference.update(
            source_mode=mode,
            reference_kind="jade_high_rank_cohort",
            reference_levels=list(reference["levels"]),
            traits=build_trait_reference(reference["levels"]),
            thresholds=build_reference(reference["levels"], reference["name"], source_mode=mode)[
                "thresholds"
            ],
        )
    document["fallback_policy"] = {"16": {"source_mode": "12", "level": "10503"}}
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
