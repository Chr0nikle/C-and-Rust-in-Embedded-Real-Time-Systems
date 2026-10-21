#!/usr/bin/env python3
"""Run-level summaries for the replication campaign.

The canonical campaign tables select a single archived run per case. That is a
descriptive development view. This script instead consumes the *replication*
bundle (``results/campaigns/campaign-replication-*/campaign.json``), where each
(RTOS, board, language) combination was measured in several independently
restarted runs:

* it pools the samples of every run for a case and reports the distribution's
  percentiles (min, median, p95, p99, p99.9, max) together with a tail measure,
  p99 minus the median, because latency distributions are skewed and a mean
  would hide the slow tail that matters for a real-time system;
* each run's own median is retained for provenance and traceability.

Pooled sample percentiles describe the observed within-run distribution. They
do not treat the ten restarted runs as exchangeable after pooling for inferential
purposes, and this script does not compute confidence intervals or between-run
standard errors. Use the retained run medians for run-level uncertainty.

Case 11 reports a coalesced (missed) release, and case 27 a zero-copy sample
with no measured benefit, as ``UINT64_MAX``; those samples are excluded from the
statistic and reported as a sentinel fraction, matching
``project/scripts/benchmarks/analyze.py compare``. Case 19 records elapsed ticks
for a fixed 1 MiB transfer, so every sample is converted to bytes per second
before pooling; its zero or wrapped durations are counted as invalid rather
than converted into a rate. The remaining non-latency cases (22/23/24/27) stay
in their raw reported unit (ppm, ticks), and latency cases are microseconds.

Outputs, written next to the other generated tables::

    generated/replication-stats.json   statistics and per-run provenance

Run from this directory::

    python3 replication.py [--bundle results/campaigns/campaign-replication-...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from selection import campaign_bundle

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2] / "project"
REPO_ROOT = PROJECT_ROOT.parent
RESULTS = PROJECT_ROOT.parent / "results" / "campaigns"
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from campaign import (  # noqa: E402
    BOARD_LABEL,
    BOARD_ORDER,
    CASE_NAMES,
    LANG_LABEL,
    LANG_ORDER,
    NON_LATENCY,
    RTOS_LABEL,
    RTOS_ORDER,
    VARIANT_CASES,
)

MISS_SENTINEL = (1 << 64) - 1
TP_CASE = 19
TP_BYTES = 1024 * 1024
TP_MAX_VALID_TICKS = 1 << 63


def ticks_to_throughput(ticks: float, ticks_per_second: int) -> float:
    """Convert an elapsed-tick duration for the fixed 1 MiB transfer to B/s."""
    return TP_BYTES * ticks_per_second / ticks

# Every path is summarised over the same number of independently restarted runs,
# so the run count is a constant across the corpus rather than varying by path.
# The 2026-10-13 campaign reached ten runs for every path, so ten is the common
# budget; runs beyond it are ignored here.
RUNS_PER_COMBO = 10

# The campaign driver names boards by their ``boards/<id>`` directory and the C
# language ``c``; the display tables use the short board tokens and ``bench``.
BOARD_FROM_BUNDLE = {
    "stm32f4discovery": "stm32f4",
    "stm32h723": "stm32h723",
    "esp32c6": "esp32c6",
    "nrf52840dk": "nrf52840dk",
}
LANG_FROM_BUNDLE = {"c": "bench", "bench": "bench", "rust": "rust"}


def normalize_key(rtos: str, board: str, lang: str) -> tuple:
    return (rtos, BOARD_FROM_BUNDLE.get(board, board), LANG_FROM_BUNDLE.get(lang, lang))


def find_bundle(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return campaign_bundle()


def case_tps(data: dict, case: int) -> int:
    by = data.get("ticks_per_second_by_case") or {}
    return int(by.get(str(case), data.get("ticks_per_second", 1)))


def run_values(data: dict, case: int, variant: int = 0) -> tuple[list[int], int]:
    """Return (values, sentinel count).

    ``UINT64_MAX`` marks two anomalies that must stay out of the value
    statistic: a coalesced release (case 11, a deadline miss) and a zero-copy
    sample with no measured benefit (case 27). Both are counted, not treated as
    data.
    """
    ticks = [s["ticks"] for s in data.get("samples", [])
             if s["case"] == case and s.get("variant", 0) == variant]
    if case in (11, 27):
        sentinels = sum(1 for t in ticks if t == MISS_SENTINEL)
        return [t for t in ticks if t != MISS_SENTINEL], sentinels
    return ticks, 0


def headline_variant(case: int, variants: list[int]) -> int:
    """Variant used for the headline cell, matching ``campaign``.

    Case 5 uses the base peer count (variant 0) where present; cases 18 and 23
    use their largest variant (the 256-byte payload / 100 % load).
    """
    if not variants:
        return 0
    if case == 5:
        # Base peer count where present.
        return 0 if 0 in variants else variants[0]
    if case in (11, 18, 23) or case in VARIANT_CASES:
        # Highest load / largest payload (case 11 is a load sweep 0/100/150/200).
        return variants[-1]
    return 0 if 0 in variants else variants[0]


def percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy 'linear' convention)."""
    if not ordered:
        raise ValueError("empty sample")
    k = (len(ordered) - 1) * pct / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return ordered[int(k)]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def compact(summary: dict) -> dict:
    """Scalar-only view of a case summary (drops the per-run value list)."""
    return {k: summary[k] for k in (
        "unit", "n_runs", "sample_count", "miss_count", "miss_fraction",
        "invalid_count", "invalid_fraction", "min", "median", "p95", "p99",
        "p999", "max", "jitter",
    )}


def case_summary(entries: list, case: int, variant: int = 0) -> dict:
    """Pooled percentile summary for one (case, variant) across runs.

    The samples of every run are pooled in the case's reported unit before the
    percentiles are taken, so a tail percentile is estimated from the full
    sample budget (1,000 samples for ten runs of 100) rather than from the run
    medians. Cases split across variants divide that budget per variant.
    """
    pooled: list[float] = []
    run_medians: list[float] = []
    miss_count = 0
    invalid_count = 0
    sample_count = 0
    for entry in entries:
        ticks, misses = run_values(entry["data"], case, variant)
        miss_count += misses
        sample_count += len(ticks) + misses
        if not ticks:
            continue
        tps = case_tps(entry["data"], case)
        if case == TP_CASE:
            values = []
            for tick in ticks:
                # A 32-bit DWT wrap appears as a uint64 underflow near 2**64 in
                # the archived case-19 samples. Neither a wrapped duration nor a
                # zero duration can describe a 1 MiB transfer.
                if tick <= 0 or tick >= TP_MAX_VALID_TICKS:
                    invalid_count += 1
                    continue
                values.append(ticks_to_throughput(tick, tps))
        elif case in NON_LATENCY:
            values = [float(t) for t in ticks]
        else:
            values = [1e6 * t / tps for t in ticks]
        if not values:
            continue
        pooled.extend(values)
        run_medians.append(statistics.median(values))

    if case == TP_CASE:
        unit = "B/s"
    elif case in (22, 23, 24):
        unit = "ppm"
    elif case == 27:
        unit = "ticks"
    elif case in NON_LATENCY:
        unit = "count"
    else:
        unit = "us"
    summary = {
        "unit": unit,
        "n_runs": len(run_medians),
        "miss_count": miss_count,
        "sample_count": sample_count,
        "miss_fraction": (miss_count / sample_count) if sample_count else None,
        "invalid_count": invalid_count,
        "invalid_fraction": (invalid_count / sample_count) if sample_count else None,
        # Per-run medians in run order, retained for provenance only.
        "values": run_medians,
    }
    if not pooled:
        summary.update(min=None, median=None, p95=None, p99=None, p999=None,
                       max=None, jitter=None)
        return summary
    ordered = sorted(pooled)
    p50 = percentile(ordered, 50.0)
    p99 = percentile(ordered, 99.0)
    summary.update(
        min=ordered[0],
        median=p50,
        p95=percentile(ordered, 95.0),
        p99=p99,
        p999=percentile(ordered, 99.9),
        max=ordered[-1],
        jitter=p99 - p50,
    )
    return summary


def load_paths(bundle: dict) -> dict:
    entries = defaultdict(list)
    for unit in bundle.get("units", []):
        # The Pico 2 (RP2350) path is not part of the thesis corpus; its
        # archived records are excluded. The STM32H723 board is included.
        if unit.get("board") in ("pico2",):
            continue
        if unit.get("run", 0) >= RUNS_PER_COMBO:
            continue
        rel = unit.get("result")
        if not rel:
            continue
        path = REPO_ROOT / rel
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        key = normalize_key(unit["rtos"], unit["board"], unit["language"])
        entries[key].append({
            "run": unit["run"],
            "path": rel,
            "build_id": data.get("build_id"),
            "data": data,
        })

    paths: dict = {}
    for key, units in entries.items():
        variants_by_case: dict = defaultdict(set)
        for u in units:
            for s in u["data"].get("samples", []):
                variants_by_case[s["case"]].add(s.get("variant", 0))
        cases = sorted(variants_by_case)
        case_block = {}
        for c in cases:
            variants = sorted(variants_by_case[c])
            headline = headline_variant(c, variants)
            summary = case_summary(units, c, headline)
            summary["variant"] = headline
            summary["variants"] = {
                str(v): compact(case_summary(units, c, v)) for v in variants
            }
            case_block[c] = summary
        paths[key] = {
            "runs": [
                {"run": u["run"], "result": u["path"], "build_id": u["build_id"]}
                for u in sorted(units, key=lambda u: u["run"])
            ],
            "coverage": cases,
            "cases": case_block,
        }
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default=None,
                        help="replication bundle directory (default: newest)")
    args = parser.parse_args()

    bundle_path = find_bundle(args.bundle)
    bundle = json.loads((bundle_path / "campaign.json").read_text())
    if not bundle.get("complete"):
        print(f"warning: {bundle_path} is not marked complete")

    paths = load_paths(bundle)
    DATA.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256((bundle_path / "campaign.json").read_bytes()).hexdigest()
    payload = {
        "generated_by": "analysis/benchmark/replication.py",
        "bundle": str(bundle_path.relative_to(REPO_ROOT)),
        "bundle_sha256": digest,
        "runs_per_combo": bundle.get("runs_per_combo", RUNS_PER_COMBO),
        "complete": bundle.get("complete"),
        "statistic": "pooled percentiles (min, median, p95, p99, p99.9, max; "
                     "tail = p99 - median) over every valid sample of the measured runs; "
                     "case-19 zero and wrapped durations are counted and excluded",
        "miss_sentinel": MISS_SENTINEL,
        "paths": {
            f"{rtos}/{board}/{lang}": data
            for (rtos, board, lang), data in sorted(paths.items())
        },
    }
    (DATA / "replication-stats.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    n_cases = sum(len(d["cases"]) for d in paths.values())
    print(f"wrote run-level stats for {len(paths)} paths / {n_cases} cases "
          f"from {bundle_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
