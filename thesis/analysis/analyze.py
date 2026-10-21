#!/usr/bin/env python3
"""Thesis analysis entry point.

Stages:
    benchmark   campaign tables, replication statistics and result figures
    integration complexity, duplication and integration-surface tables/figure
    all         run both stages
    test        run the analysis test-suite

Run with the analysis environment that provides matplotlib:

    ../.venv/bin/python analyze.py all
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unittest
from pathlib import Path

ANALYSIS = Path(__file__).resolve().parent
GENERATED = ANALYSIS.parent / "generated"
BENCHMARK = ANALYSIS / "benchmark"
INTEGRATION = ANALYSIS / "integration"
SAFETY = ANALYSIS / "safety"

sys.path.insert(0, str(BENCHMARK))


def run(script: Path, *args: str) -> None:
    subprocess.run([sys.executable, str(script), *args], cwd=ANALYSIS, check=True)


def _has_native_static() -> bool:
    evidence = GENERATED / "data" / "static-evidence.json"
    if evidence.is_file():
        try:
            return json.loads(evidence.read_text()).get("status") == "complete"
        except (OSError, json.JSONDecodeError):
            return False
    table = GENERATED / "typ" / "campaign-static.typ"
    text = table.read_text() if table.is_file() else ""
    return "FreeRTOS-in-Rust" in text or "Rivet" in text


def _has_native_stats() -> bool:
    import json

    stats = GENERATED / "data" / "replication-stats.json"
    if not stats.is_file():
        return False
    paths = json.loads(stats.read_text()).get("paths", {})
    return "freertos-in-rust/stm32f4/rust" in paths and "rivet/stm32f4/rust" in paths


def run_benchmark() -> None:
    # The performance figure owns the resumable statistics/table refresh and
    # preserves Rust-native paths that the newest matched campaign omits.
    run(BENCHMARK / "performance.py")
    if _has_native_static():
        run(BENCHMARK / "memory.py")
    else:
        print("warning: newest campaign has no Rust-native static rows; keeping memory figure")
    if _has_native_stats():
        run(BENCHMARK / "rust_kernel.py")
    else:
        print("warning: newest campaign has no Rust-native timing rows; keeping kernel figure")


def run_integration(skip_complexity: bool = False) -> None:
    if not skip_complexity:
        run(INTEGRATION / "complexity.py")
    run(INTEGRATION / "duplication.py")
    run(INTEGRATION / "surface.py")
    run(SAFETY / "figures.py")


def run_tests() -> int:
    suite = unittest.defaultTestLoader.discover(str(ANALYSIS / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("benchmark", help="regenerate benchmark tables and figures")
    integration = sub.add_parser("integration", help="regenerate integration tables and figure")
    integration.add_argument("--skip-complexity", action="store_true",
                             help="reuse the existing complexity.json")
    sub.add_parser("all", help="run benchmark and integration analysis")
    sub.add_parser("test", help="run the analysis test-suite")

    args = parser.parse_args(argv)
    if args.command == "benchmark":
        from selection import campaign_bundle
        campaign_bundle()  # fail clearly before importing plotting dependencies
        run_benchmark()
        from analysis_provenance import record_analysis_provenance
        record_analysis_provenance(" ".join([sys.executable, *sys.argv]))
    elif args.command == "integration":
        run_integration(args.skip_complexity)
    elif args.command == "all":
        from selection import campaign_bundle
        campaign_bundle()
        run_benchmark()
        run_integration()
        from analysis_provenance import record_analysis_provenance
        record_analysis_provenance(" ".join([sys.executable, *sys.argv]))
    elif args.command == "test":
        return run_tests()
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
