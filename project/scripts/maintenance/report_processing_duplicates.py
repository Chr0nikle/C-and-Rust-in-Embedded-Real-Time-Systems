#!/usr/bin/env python3
"""Read-only report of equivalent processing-provenance records."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PROCESSING = ROOT / "results" / "processing"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args()
    groups = defaultdict(list)
    for path in sorted(PROCESSING.glob("*/*/provenance.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            groups[("invalid", path.as_posix())].append(path)
            continue
        key = (
            payload.get("analysis_version", "unknown"),
            payload.get("input_manifest_sha256", ""),
            payload.get("outputs_identity_sha256") or payload.get("outputs_manifest_sha256", ""),
            payload.get("campaign_lock_sha256", ""),
            payload.get("implementation_sha256", ""),
        )
        groups[key].append(path)
    duplicates = {key: paths for key, paths in groups.items() if len(paths) > 1}
    if args.json:
        print(json.dumps({
            "records": sum(len(paths) for paths in groups.values()),
            "groups": len(groups),
            "duplicate_groups": {
                "|".join(key): [path.relative_to(ROOT).as_posix() for path in paths]
                for key, paths in duplicates.items()
            },
        }, indent=2, sort_keys=True))
    else:
        print(f"processing records: {sum(len(paths) for paths in groups.values())}")
        print(f"unique identities: {len(groups)}")
        print(f"duplicate groups: {len(duplicates)}")
        for key, paths in duplicates.items():
            print("\n" + " | ".join(key[:4]))
            for path in paths:
                print(f"  {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
