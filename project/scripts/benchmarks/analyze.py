#!/usr/bin/env python3
"""Benchmark evidence analysis: comparison tables and vector plots.

Subcommands:
    compare   Consolidate the latest benchmark record per path into a
              ``comparison.json`` plus a Markdown report.
    report    Render a ``comparison.json`` as vector diagrams.

Usage:
    python3 scripts/benchmarks/analyze.py compare [--results results] [--out DIR]
    python3 scripts/benchmarks/analyze.py report [COMPARISON] [--out DIR]

The ``report`` subcommand requires matplotlib; install it in a dedicated
environment (``python3 -m venv .local/venv/analysis`` then pip install).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
RESULTS = PROJECT.parent / "results" / "campaigns"

plt = None


def _load_matplotlib() -> None:
    global plt
    if plt is not None:
        return
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as pyplot
    except ImportError:  # pragma: no cover - operator-facing message
        print(
            "matplotlib is required for plotting; install it in a dedicated "
            "environment (python3 -m venv .local/venv/analysis && "
            ".local/venv/analysis/bin/pip install matplotlib)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    plt = pyplot

def _board_from_build(build_id: str) -> str:
    if "nrf52840" in build_id:
        return "nrf52840dk"
    if "stm32f4" in build_id:
        return "stm32f4discovery"
    if "-c6-" in build_id or build_id.startswith("c6"):
        return "esp32c6"
    return "unknown"


def parse_path(build_id: str, data: dict | None = None) -> tuple[str, str, str]:
    """Derive (rtos, board, language) for a run.

    Newer benchmark records carry explicit `rtos`/`board`/`language` fields;
    those are authoritative. The build_id-token heuristic is only a fallback for
    evidence written before those fields existed.
    """
    d = data or {}
    rtos = str(d.get("rtos") or (build_id.split("-", 1)[0] if build_id else "unknown"))
    board = str(d.get("board") or _board_from_build(build_id))
    if d.get("language") is not None:
        language = str(d["language"])
    else:
        # Rust-native kernels/frameworks do not carry a `-rust-` token in their
        # build id (only C routes with a Rust variant do).
        rust_only = {
            "freertos-in-rust",
            "rivet",
        }
        language = "rust" if "-rust-" in build_id or rtos in rust_only else "c"
    return rtos, board, language


def percentile(sorted_ticks: list[int], pct: float) -> int:
    """Linear-interpolated percentile (numpy 'linear' convention).

    Rounded to the nearest integer tick. For a 1000-sample run p99.9 still
    resolves to the top sample because 0.1% of 1000 is a single sample, so
    p99.9 is only meaningful when n is well above 1000 (see the markdown note).
    """
    n = len(sorted_ticks)
    if n == 0:
        return 0
    if n == 1:
        return sorted_ticks[0]
    rank = pct / 100.0 * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_ticks[lo]
    frac = rank - lo
    return round(sorted_ticks[lo] + (sorted_ticks[hi] - sorted_ticks[lo]) * frac)


def median_abs_deviation(ticks: list[int]) -> float:
    """Unscaled median absolute deviation: robust spread that ignores tails."""
    med = statistics.median(ticks)
    return statistics.median([abs(t - med) for t in ticks])


TP_CASE = 19
TP_BYTES = 1024 * 1024  # case 19 transfers a fixed 1 MiB in 64-byte messages
TP_MSG_BYTES = 64

# Cases whose sample is not a duration and must not be averaged into latency
# comparisons: 19 = elapsed time for a fixed transfer; 22/23/24/28 = parts per
# million (kernel CPU share / fragmentation). The raw sample stays in
# `median_ticks`; the *_us latency fields are cleared. Case 27 (copied minus
# zero-copy) IS a latency difference, so it keeps its *_us fields and is only
# tagged as a delta (never ranked against absolute latencies).
NON_LATENCY_CASES = {19, 22, 23, 24, 28}
DELTA_CASE = 27

# Case 11 reports a coalesced release (missed deadline) and case 27 a zero-copy
# sample with no measured benefit, both as UINT64_MAX, so a statistic must
# exclude those samples and report the sentinel fraction instead of letting the
# sentinel dominate the mean/max.
MISS_SENTINEL = (1 << 64) - 1


def case_stats(ticks: list[int], hz: int, case: int | None = None) -> dict:
    """Latency distribution, robust spread, tail and drift for one case.

    Means hide the worst case a real-time system is judged on, so the tail
    percentiles, the median absolute deviation (tail-insensitive), the count
    above p99 and the first-half/second-half drift are recorded alongside.
    Case 19 (`ipc_throughput`) is non-latency: its ticks are the elapsed time
    for a fixed TP_BYTES transfer, so the latency fields are cleared and
    bytes/s and messages/s are reported instead.
    """
    miss_count = 0
    if case in (11, DELTA_CASE):
        miss_count = sum(1 for t in ticks if t == MISS_SENTINEL)
        ticks = [t for t in ticks if t != MISS_SENTINEL]
    total = len(ticks) + miss_count
    ordered = sorted(ticks)
    n = len(ordered)
    scale = 1e6 / hz if hz else None
    if n == 0:
        return {
            "n": 0,
            "miss_count": miss_count,
            "miss_fraction": 1.0 if total else None,
            "below_min_reps": True,
            "median_ticks": None, "median_us": None, "mean_ticks": None,
            "mean_us": None, "stddev_us": None, "min_us": None, "max_us": None,
            "mad_us": None, "p90_us": None, "p95_us": None, "p99_us": None,
            "p999_us": None, "jitter_us": None, "iqr_us": None, "drift_us": None,
            "outliers_above_p99": 0,
        }

    def us(value: float) -> float | None:
        return value * scale if scale is not None else None

    mean = statistics.fmean(ordered)
    stddev = math.sqrt(statistics.pvariance(ordered)) if n > 1 else 0.0
    mad = median_abs_deviation(ordered)
    p99 = percentile(ordered, 99)
    p999 = percentile(ordered, 99.9)
    # Temporal drift: mean of the second half of the run minus the first half,
    # in arrival order (the sample list is persisted in measurement order). This
    # must NOT be computed on the sorted array, which would only measure skew.
    half = len(ticks) // 2
    drift = (
        statistics.fmean(ticks[half:]) - statistics.fmean(ticks[:half])
        if half
        else 0.0
    )
    median = statistics.median(ordered)
    stats = {
        "n": n,
        "below_min_reps": n < 1000,
        "median_ticks": median,
        "median_us": us(median),
        "mean_ticks": mean,
        "mean_us": us(mean),
        "variance_ticks2": statistics.pvariance(ordered) if n > 1 else 0.0,
        "stddev_ticks": stddev,
        "stddev_us": us(stddev),
        "min_ticks": ordered[0],
        "min_us": us(ordered[0]),
        "max_ticks": ordered[-1],
        "max_us": us(ordered[-1]),
        "mad_ticks": mad,
        "mad_us": us(mad),
        "cv": (stddev / mean) if mean else None,
        "p90_us": us(percentile(ordered, 90)),
        "p95_us": us(percentile(ordered, 95)),
        "p99_us": us(p99),
        "p999_us": us(p999),
        "jitter_us": us(p99 - median),
        "iqr_us": us(percentile(ordered, 75) - percentile(ordered, 25)),
        "outliers_above_p99": sum(1 for t in ordered if t > p99),
        "drift_us": us(drift),
        "drift_ratio": (drift / mean) if mean else None,
    }
    if case in NON_LATENCY_CASES:
        stats["non_latency"] = True
        for key in (
            "median_us", "mean_us", "stddev_us", "min_us", "max_us", "mad_us",
            "p90_us", "p95_us", "p99_us", "p999_us", "jitter_us", "iqr_us",
            "drift_us",
        ):
            stats[key] = None
        if case == TP_CASE:
            # Elapsed seconds for the fixed volume, from the median sample time.
            seconds = (median / hz) if hz else None
            stats.update(
                {
                    "median_s": seconds,
                    "bytes_per_s": (TP_BYTES / seconds) if seconds else None,
                    "mib_per_s": (TP_BYTES / seconds / (1024 * 1024)) if seconds else None,
                    "msgs_per_s": (TP_BYTES / TP_MSG_BYTES / seconds) if seconds else None,
                }
            )
    if case == 11:
        stats["miss_count"] = miss_count
        stats["miss_fraction"] = (miss_count / total) if total else None
    if case == DELTA_CASE:
        stats["is_delta"] = True
    return stats


def collect_runs(results: Path) -> dict[tuple[str, str, str], list[dict]]:
    """All benchmark runs per board x RTOS x language path, oldest run_id first.

    Build ids include the git sha, so several build ids can describe the same
    path; keeping every run lets repeated full runs be compared with each other.
    """
    runs: dict[tuple[str, str, str], list[dict]] = {}
    paths = list(results.glob("*/benchmark-*.json"))
    paths += list(results.glob("campaign-replication-*/benchmarks/*/benchmark-*.json"))
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("kind") != "benchmark":
            continue
        build_id = str(data.get("build_id", ""))
        if not build_id:
            continue
        data = dict(data, evidence=str(path))
        runs.setdefault(parse_path(build_id, data), []).append(data)
    for entries in runs.values():
        entries.sort(key=lambda d: str(d.get("run_id", "")))
    return runs


def case_hz(data: dict, case: int) -> int:
    """Timer rate for one case: prefer the per-case map, fall back to the global."""
    by_case = data.get("ticks_per_second_by_case") or {}
    if str(case) in by_case:
        return int(by_case[str(case)])
    return int(data.get("ticks_per_second", 0))


def run_variation(
    runs: dict[tuple[str, str, str], list[dict]],
    reports: list[dict],
    required: list[int] | None = None,
) -> dict:
    """Between-run spread of each case mean, over repeats of the same build.

    Only runs of the identical firmware build (and repetition count) are
    combined, so the spread reflects host/board luck rather than a code change.
    When `required` is set, only runs that contain every required case are
    counted, so a partial run cannot pollute a case's spread. Paths with fewer
    than two such runs are omitted rather than reported as a misleading zero.
    """
    required_set = set(required or [])
    out = {}
    for r in reports:
        key = (r["rtos"], r["board"], r["language"])
        reps = r.get("repetitions")
        entries = [
            d
            for d in runs.get(key, [])
            if d.get("repetitions") == reps
            and str(d.get("build_id", "")) == str(r["build_id"])
            and (
                not required_set
                or required_set
                <= {int(s["case"]) for s in d.get("samples", [])}
            )
        ]
        if len(entries) < 2:
            continue
        cases = {}
        for case in sorted({int(s["case"]) for d in entries for s in d.get("samples", [])}):
            means = [
                statistics.fmean(
                    [int(s["ticks"]) for s in d.get("samples", []) if int(s["case"]) == case]
                )
                for d in entries
                if any(int(s["case"]) == case for s in d.get("samples", []))
            ]
            if len(means) < 2:
                continue
            hz = case_hz(entries[0], case)
            scale = 1e6 / hz if hz else None
            spread = math.sqrt(statistics.pvariance(means))
            if case in NON_LATENCY_CASES:
                # Raw sample values (ppm / transfer elapsed / delta), not µs.
                cases[str(case)] = {
                    "runs": len(means),
                    "non_latency": True,
                    "value_mean": statistics.fmean(means),
                    "value_stddev": spread,
                    "value_min": min(means),
                    "value_max": max(means),
                }
            else:
                cases[str(case)] = {
                    "runs": len(means),
                    "mean_us": (statistics.fmean(means) * scale) if scale else None,
                    "stddev_us": (spread * scale) if scale else None,
                    "min_us": (min(means) * scale) if scale else None,
                    "max_us": (max(means) * scale) if scale else None,
                }
        if cases:
            out[f"{key[0]}/{key[1]}/{key[2]}"] = {
                "build_id": r["build_id"],
                "repetitions": reps,
                "cases": cases,
            }
    return out


def load_latest(
    runs: dict, required: list[int] | None = None
) -> tuple[list[dict], list[tuple[str, str, str, list[int]]]]:
    """Newest benchmark run per path, plus paths that lack the required cases.

    With `required`, a path is included only via its newest run that contains
    every required case ID; a path with no such run is returned as excluded
    together with the case IDs its newest run does provide. This keeps a partial
    collection from mixing a full run with a single-case verification run.
    """
    required_set = set(required or [])
    reports: list[dict] = []
    excluded: list[tuple[str, str, str, list[int]]] = []
    for key, entries in sorted(runs.items()):
        data = None
        if required_set:
            for candidate in reversed(entries):
                have = {int(s["case"]) for s in candidate.get("samples", [])}
                if required_set <= have:
                    data = candidate
                    break
            if data is None:
                have = (
                    sorted({int(s["case"]) for s in entries[-1].get("samples", [])})
                    if entries
                    else []
                )
                excluded.append((*key, have))
                continue
        else:
            # Without a required set, prefer the newest run with the most
            # distinct cases, so a single-case verification run (e.g. a quick
            # `--cases 10` smoke) cannot shadow a full collection of the path.
            data = max(
                reversed(entries),
                key=lambda d: len({int(s["case"]) for s in d.get("samples", [])}),
            )
        cases: dict[int, list[int]] = {}
        for sample in data.get("samples", []):
            cases.setdefault(int(sample["case"]), []).append(int(sample["ticks"]))
        reports.append(
            {
                "build_id": str(data.get("build_id", "")),
                "rtos": key[0],
                "board": key[1],
                "language": key[2],
                "ticks_per_second": int(data.get("ticks_per_second", 0)),
                "ticks_per_second_by_case": {
                    str(c): case_hz(data, c) for c in cases
                },
                "repetitions": data.get("repetitions"),
                "warmup": data.get("warmup"),
                "memory_high_water": data.get("memory_high_water"),
                "static_footprint": data.get("static_footprint"),
                "evidence": data.get("evidence"),
                "cases": {
                    str(c): case_stats(t, case_hz(data, c), c) for c, t in sorted(cases.items())
                },
            }
        )
    return reports, excluded


def fmt(value, digits: int = 3) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "-"


def render_markdown(
    reports: list[dict],
    variation: dict | None = None,
    excluded: list[tuple[str, str, str, list[int]]] | None = None,
) -> str:
    case_ids = sorted({int(c) for r in reports for c in r["cases"]})
    lines = [
        "# Benchmark comparison",
        "",
        "Per-case mean latency in microseconds, with population standard deviation",
        "and worst case in parentheses. Ticks are normalized by each device's",
        "reported ticks_per_second; n is the persisted sample count per case.",
        "",
    ]
    if excluded:
        lines.append(
            "Coverage: these paths were excluded because no single run provides the"
        )
        lines.append("required case set (their newest run's cases are shown):")
        for rtos, board, lang, have in excluded:
            lines.append(f"- {rtos}/{board}/{lang}: cases {have}")
        lines.append("")
    header = "| RTOS | Board | Lang | n | " + " | ".join(f"case {c}" for c in case_ids) + " |"
    lines.append(header)
    lines.append("| --- | --- | --- | --- | " + " | ".join("---" for _ in case_ids) + " |")
    for r in reports:
        cells = []
        for c in case_ids:
            entry = r["cases"].get(str(c))
            if entry and entry["mean_us"] is not None:
                cells.append(
                    f"{entry['mean_us']:.3f} ± {entry['stddev_us']:.3f} "
                    f"(max {entry['max_us']:.3f})"
                )
            else:
                cells.append("-")
        lines.append(
            f"| {r['rtos']} | {r['board']} | {r['language']} | {r['repetitions']} | "
            + " | ".join(cells)
            + " |"
        )
    lines.append("")
    lines.append("Case IDs: 0 timer_overhead, 1 queue_roundtrip, 2 task_handoff,")
    lines.append("3 periodic_jitter (period/deadline error against the nominal 1 ms period,")
    lines.append("not a latency; 0 = cycle-deterministic release on a quiet path),")
    lines.append("4 isr_to_task, 5 yield_roundtrip, 6 preempt_latency,")
    lines.append("7 ipc_pingpong (two-task request/response round trip),")
    lines.append("8 mutex_priority_inheritance (blocking lock latency with a spinner),")
    lines.append("9 timer_callback_latency (OS timer callback deviation from nominal),")
    lines.append("10 trigger_to_isr (task-asserted interrupt through ISR entry),")
    lines.append("11 periodic_jitter_loaded (hardware release grid under injected ISR load;")
    lines.append("UINT64_MAX = missed release),")
    lines.append("12 semaphore_latency, 13 mutex_uncontended, 14 alloc_latency (uncontended")
    lines.append("API round trips), 15 sleep_accuracy (|actual - nominal| for a 1 ms delay).")
    lines.append("")
    throughput_rows = [
        (r, r["cases"][str(TP_CASE)])
        for r in reports
        if str(TP_CASE) in r["cases"]
        and r["cases"][str(TP_CASE)].get("mib_per_s") is not None
    ]
    if throughput_rows:
        lines.append(
            f"Case {TP_CASE} ipc_throughput (non-latency; {TP_BYTES // (1024 * 1024)} MiB "
            f"in {TP_MSG_BYTES}-byte messages, median of the run):"
        )
        lines.append("")
        lines.append("| RTOS | Board | Lang | MiB/s | msgs/s | elapsed ms |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r, e in throughput_rows:
            lines.append(
                f"| {r['rtos']} | {r['board']} | {r['language']} | "
                f"{fmt(e['mib_per_s'], 3)} | {fmt(e['msgs_per_s'], 1)} | "
                f"{fmt((e['median_s'] or 0.0) * 1000, 3)} |"
            )
        lines.append("")
    lines.append("Runtime memory high-water (bytes, when reported):")
    lines.append("")
    lines.append(
        "| RTOS | Board | Lang | stack_high_water | worst_task_headroom | "
        "best_task_headroom | tasks | per_task_ram | heap_min_free | heap_free | heap_overhead |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in reports:
        memory = r.get("memory_high_water") or {}
        stack = memory.get("stack_high_water_bytes")
        worst_stack = memory.get("worst_stack_headroom_bytes")
        best_stack = memory.get("best_stack_headroom_bytes")
        tasks = memory.get("task_count")
        per_task = memory.get("per_task_ram_overhead_bytes")
        heap_min = memory.get("heap_min_free_bytes")
        heap_free = memory.get("heap_free_bytes")
        heap_over = memory.get("heap_overhead_bytes")
        lines.append(
            f"| {r['rtos']} | {r['board']} | {r['language']} | "
            f"{stack if stack is not None else '-'} | "
            f"{worst_stack if worst_stack is not None else '-'} | "
            f"{best_stack if best_stack is not None else '-'} | "
            f"{tasks if tasks is not None else '-'} | "
            f"{per_task if per_task is not None else '-'} | "
            f"{heap_min if heap_min is not None else '-'} | "
            f"{heap_free if heap_free is not None else '-'} | "
            f"{heap_over if heap_over is not None else '-'} |"
        )
    lines.append("")
    lines.append("Static image footprint (bytes; allocatable ELF sections only, so")
    lines.append("`flash` = text + rodata + data and `ram` = data + bss):")
    lines.append("")
    lines.append("| RTOS | Board | Lang | image | text | rodata | data | bss | flash | ram |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in reports:
        footprint = r.get("static_footprint") or {}
        images = footprint.get("images") or []
        if not images:
            lines.append(
                f"| {r['rtos']} | {r['board']} | {r['language']} | "
                + " | ".join(["-"] * 7)
                + " |"
            )
            continue
        for image in images:
            lines.append(
                f"| {r['rtos']} | {r['board']} | {r['language']} | "
                f"{image.get('image', '-')} | {image.get('text', '-')} | "
                f"{image.get('rodata', '-')} | {image.get('data', '-')} | "
                f"{image.get('bss', '-')} | {image.get('flash', '-')} | "
                f"{image.get('ram', '-')} |"
            )
    lines.append("")
    lines.append("Tail latency and spread (µs; MAD is the unscaled median absolute")
    lines.append("deviation, CV = stddev/mean, `jitter` = p99 - p50 tail excess, max is the")
    lines.append("worst observed sample, `>p99` counts samples above p99, and drift is the")
    lines.append("second-half minus first-half mean of the run in arrival order). A trailing")
    lines.append("`*` on `n` marks a run below the 1000-sample proposal minimum (development,")
    lines.append("not final). `p99.9` equals the maximum for n <= 1000 and is only meaningful")
    lines.append("well above that. Case 27 is a latency *difference* (copied - zero-copy),")
    lines.append("reported in µs but never ranked against absolute latencies.")
    lines.append("")
    lines.append(
        "| RTOS | Board | Lang | case | n | p50 | p95 | p99 | p99.9 | max | jitter | MAD | CV | >p99 | drift |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for r in reports:
        for c in sorted(int(k) for k in r["cases"]):
            e = r["cases"][str(c)]
            n_str = f"{e['n']}" + ("*" if e.get("below_min_reps") else "")
            lines.append(
                f"| {r['rtos']} | {r['board']} | {r['language']} | {c} | {n_str} | "
                f"{fmt(e['median_us'])} | {fmt(e['p95_us'])} | {fmt(e['p99_us'])} | "
                f"{fmt(e['p999_us'])} | {fmt(e['max_us'])} | {fmt(e['jitter_us'])} | "
                f"{fmt(e['mad_us'])} | {fmt(e['cv'], 3)} | "
                f"{e['outliers_above_p99']} | {fmt(e['drift_us'])} |"
            )
    lines.append("")
    lines.append("Worst observed latency per path (determinism headline: the case holding")
    lines.append("the single largest sample, with its p99.9 and the max/p50 tail ratio):")
    lines.append("")
    lines.append("| RTOS | Board | Lang | worst case | worst (µs) | p99.9 (µs) | max/p50 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for r in reports:
        best_case = None
        best = None
        for c in sorted(int(k) for k in r["cases"]):
            e = r["cases"][str(c)]
            if e.get("max_us") is None:
                continue
            if best is None or e["max_us"] > best["max_us"]:
                best_case, best = c, e
        if best is None:
            continue
        ratio = (best["max_us"] / best["median_us"]) if best.get("median_us") else None
        lines.append(
            f"| {r['rtos']} | {r['board']} | {r['language']} | {best_case} | "
            f"{fmt(best['max_us'])} | {fmt(best['p999_us'])} | {fmt(ratio, 2)} |"
        )
    lines.append("")
    if variation:
        lines.append("Run-to-run variation of each case mean (same build and repetition count;")
        lines.append("build ids embed the git HEAD revision, so a rebuild from a modified")
        lines.append("tree shares an id -- a large spread there is a provenance signal).")
        lines.append("Rows for non-latency cases (19, 22, 23, 24, 27, 28) are raw sample")
        lines.append("values (ppm / elapsed time / delta), not microseconds:")
        lines.append("")
        lines.append("| Path | case | runs | mean | stddev | min | max |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for path in sorted(variation):
            for c in sorted(int(k) for k in variation[path]["cases"]):
                e = variation[path]["cases"][str(c)]
                if e.get("non_latency"):
                    mean, stddev = e["value_mean"], e["value_stddev"]
                    lo, hi = e["value_min"], e["value_max"]
                else:
                    mean, stddev = e["mean_us"], e["stddev_us"]
                    lo, hi = e["min_us"], e["max_us"]
                lines.append(
                    f"| {path} | {c} | {e['runs']} | {fmt(mean)} | "
                    f"{fmt(stddev)} | {fmt(lo)} | {fmt(hi)} |"
                )
        lines.append("")
    return "\n".join(lines)


def compare_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default=str(RESULTS), type=Path)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--cases",
        default=None,
        help="comma-separated required case IDs (e.g. 0,1,2); a path is included "
        "only when one run provides all of them. Omit to use each path's newest run.",
    )
    args = parser.parse_args(argv)

    required = None
    if args.cases is not None:
        required = sorted({int(x) for x in args.cases.split(",") if x.strip() != ""})

    runs = collect_runs(args.results)
    reports, excluded = load_latest(runs, required)
    if not reports:
        print(f"no benchmark evidence under {args.results}", file=sys.stderr)
        return 1
    for rtos, board, lang, have in excluded:
        print(
            f"excluded {rtos}/{board}/{lang}: newest run has cases {have}",
            file=sys.stderr,
        )

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = args.results / f"comparison-{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    variation = run_variation(runs, reports, required)
    (out / "comparison.json").write_text(
        json.dumps(
            {"required_cases": required, "paths": reports, "run_variation": variation},
            indent=2,
        )
        + "\n"
    )
    markdown = render_markdown(reports, variation, excluded)
    (out / "comparison.md").write_text(markdown)
    print(markdown)
    print(f"saved {out / 'comparison.json'} and {out / 'comparison.md'}")
    return 0


CASE_NAMES = {
    0: "timer_overhead",
    1: "queue_roundtrip",
    2: "task_handoff",
    3: "periodic_jitter",
    4: "isr_to_task",
    5: "yield_roundtrip",
    6: "preempt_latency",
    7: "ipc_pingpong",
    8: "mutex_priority_inheritance",
    9: "timer_callback_latency",
}
# Stable display order for the distilled scope.
BOARDS = [("stm32f4discovery", "STM32F4"), ("esp32c6", "ESP32-C6")]
RTOSES = [("freertos", "FreeRTOS"), ("zephyr", "Zephyr"), ("nuttx", "NuttX")]
LANGUAGES = [("c", "C", "#1f77b4"), ("rust", "Rust", "#ff7f0e")]
# Cases drawn on a log axis; periodic_jitter (3) contains exact zeros.
LOG_CASES = {0, 1, 2, 4, 5, 6}
# ECDF line colour per RTOS; the language selects solid/dashed.
RTOS_COLORS = {
    "freertos": "#1f77b4",
    "zephyr": "#2f9e44",
    "nuttx": "#d9480f",
}


def newest_comparison(results: Path) -> Path:
    dirs = sorted(
        (p for p in results.glob("comparison-*") if (p / "comparison.json").is_file()),
        key=lambda p: p.name,
    )
    if not dirs:
        raise SystemExit(f"no comparison-*/comparison.json under {results}")
    return dirs[-1] / "comparison.json"


def resolve_comparison(arg: str | None) -> Path:
    if arg is None:
        return newest_comparison(RESULTS)
    path = Path(arg)
    if path.is_dir():
        path = path / "comparison.json"
    if not path.is_file():
        raise SystemExit(f"comparison.json not found: {path}")
    return path


def index_paths(reports: list[dict]) -> dict[tuple[str, str, str], dict]:
    return {(r["rtos"], r["board"], r["language"]): r for r in reports}


def case_entry(report: dict, case: int) -> dict | None:
    return (report.get("cases") or {}).get(str(case))


def plot_case(
    ax,
    index: dict,
    case: int,
    *,
    compact: bool,
    metric: str = "mean_us",
    whisker: str | None = "stddev_us",
    whisker_hi: str | None = None,
) -> None:
    """Grouped C/Rust bars for one case across the board x RTOS paths.

    `metric` selects the bar height, `whisker` a symmetric error bar and
    `whisker_hi` a one-sided upper whisker (e.g. max above a p99 bar).
    """
    keys: list[tuple[str, str] | None] = []
    labels: list[str] = []
    for rtos, rtos_label in RTOSES:
        for board, board_label in BOARDS:
            if not any((rtos, board, lang) in index for lang, _, _ in LANGUAGES):
                continue
            keys.append((rtos, board))
            labels.append(f"{rtos_label}\n{board_label}")

    def whiskers(entry: dict, value: float) -> tuple[float, float]:
        if whisker and entry.get(whisker) is not None:
            return entry[whisker], entry[whisker]
        if whisker_hi and entry.get(whisker_hi) is not None:
            return 0.0, max(entry[whisker_hi] - value, 0.0)
        return 0.0, 0.0

    width = 0.38
    for offset, (lang, lang_label, color) in enumerate(LANGUAGES):
        xs: list[float] = []
        values: list[float] = []
        lo: list[float] = []
        hi: list[float] = []
        for i, key in enumerate(keys):
            if key is None:
                continue  # Tock group, drawn separately below
            entry = case_entry(index.get((key[0], key[1], lang), {}), case)
            if entry is None or entry.get(metric) is None:
                continue
            value = entry[metric]
            lower, upper = whiskers(entry, value)
            xs.append(i + (offset - 0.5) * width)
            values.append(value)
            lo.append(lower)
            hi.append(upper)
        if xs:
            ax.bar(
                xs,
                values,
                width=width,
                yerr=[lo, hi] if any(hi) or any(lo) else None,
                capsize=2,
                label=lang_label,
                color=color,
                error_kw={"elinewidth": 0.8, "ecolor": "#444444"},
            )

    if keys and keys[-1] is None:
        entry = case_entry(tock, case)
        if entry and entry.get(metric) is not None:
            value = entry[metric]
            lower, upper = whiskers(entry, value)
            ax.bar(
                [len(keys) - 1 + 0.5 * width],
                [value],
                width=width,
                yerr=[[lower], [upper]] if upper or lower else None,
                capsize=2,
                label="Rust (Tock)",
                color="#2ca02c",
                error_kw={"elinewidth": 0.8, "ecolor": "#444444"},
            )

    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, fontsize=7 if not compact else 5)
    if case in LOG_CASES:
        ax.set_yscale("log")
    if not compact:
        ax.set_ylabel(f"{metric} (µs)")
        ax.set_title(f"case {case} · {CASE_NAMES[case]}")
    ax.grid(axis="y", which="both", alpha=0.25, linewidth=0.5)
    ax.margins(x=0.02)


def plot_overview(index: dict, cases: list[int], out: Path) -> None:
    fig, axes = plt.subplots(1, len(cases), figsize=(3.0 * len(cases), 3.4))
    if len(cases) == 1:
        axes = [axes]
    for ax, case in zip(axes, cases):
        plot_case(ax, index, case, compact=True)
        ax.set_title(f"case {case}\n{CASE_NAMES[case]}", fontsize=8)
        if case not in LOG_CASES:
            ax.set_ylabel("µs (deviation)", fontsize=8)
    axes[0].legend(fontsize=7, loc="upper left")
    fig.suptitle(
        "Per-case mean latency by path (1000 samples/case); note log axes",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out.with_suffix(".svg"))
    fig.savefig(out.with_suffix(".png"), dpi=200)
    plt.close(fig)


def load_samples(report: dict) -> dict[int, list[float]]:
    """Per-sample latencies in microseconds, read from the report's raw evidence.

    comparison.json keeps only aggregates, so distribution plots re-read the
    referenced benchmark run; a missing evidence file simply drops that path.
    """
    path = Path(str(report.get("evidence") or ""))
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    by_case = data.get("ticks_per_second_by_case") or {}
    global_hz = int(data.get("ticks_per_second", 0))
    series: dict[int, list[float]] = {}
    for sample in data.get("samples", []):
        case = int(sample["case"])
        hz = int(by_case.get(str(case), global_hz)) or 1
        series.setdefault(case, []).append(int(sample["ticks"]) * 1e6 / hz)
    return series


def plot_ecdf(reports: list[dict], cases: list[int], out: Path) -> bool:
    """Per-case latency ECDF (linear µs); solid = C, dashed = Rust, colour = RTOS."""
    if not cases:
        return False
    fig, axes = plt.subplots(1, len(cases), figsize=(3.0 * len(cases), 3.4))
    if len(cases) == 1:
        axes = [axes]
    seen: set[tuple[str, str]] = set()
    drew = False
    for ax, case in zip(axes, cases):
        for report in reports:
            values = sorted(load_samples(report).get(case) or [])
            if not values:
                continue
            drew = True
            n = len(values)
            key = (report["rtos"], report["language"])
            label = f"{report['rtos']} {report['language']}" if key not in seen else None
            seen.add(key)
            ax.plot(
                values,
                [(i + 1) / n for i in range(n)],
                linewidth=1.0,
                color=RTOS_COLORS.get(report["rtos"], "#333333"),
                linestyle="-" if report["language"] == "c" else "--",
                label=label,
                alpha=0.85,
            )
        ax.set_title(f"case {case}\n{CASE_NAMES[case]}", fontsize=8)
        ax.set_xlabel("latency (µs)", fontsize=7)
        ax.grid(alpha=0.25, linewidth=0.5)
    axes[0].set_ylabel("ECDF", fontsize=7)
    handles: list = []
    labels: list[str] = []
    for ax in axes:
        for handle, label in zip(*ax.get_legend_handles_labels()):
            if label and label not in labels:
                handles.append(handle)
                labels.append(label)
    if handles:
        axes[-1].legend(handles, labels, fontsize=5, loc="lower right")
    fig.suptitle("Per-sample latency ECDF; solid=C, dashed=Rust", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out.with_suffix(".svg"))
    fig.savefig(out.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return drew


def plot_tail(
    index: dict,
    cases: list[int],
    out: Path,
    metric: str = "p99_us",
    label: str = "p99",
) -> None:
    """Per-case tail bars with a one-sided whisker to the observed maximum."""
    if not cases:
        return
    fig, axes = plt.subplots(1, len(cases), figsize=(3.0 * len(cases), 3.4))
    if len(cases) == 1:
        axes = [axes]
    for ax, case in zip(axes, cases):
        plot_case(
            ax,
            index,
            case,
            compact=True,
            metric=metric,
            whisker=None,
            whisker_hi="max_us",
        )
        ax.set_title(f"case {case}\n{CASE_NAMES[case]} ({label})", fontsize=8)
    axes[0].legend(fontsize=7, loc="upper left")
    fig.suptitle(
        f"Per-case {label} latency by path (whisker to max); note log axes", fontsize=9
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(out.with_suffix(".svg"))
    fig.savefig(out.with_suffix(".png"), dpi=200)
    plt.close(fig)


def plot_memory(reports: list[dict], out: Path) -> bool:
    rows = []
    for r in reports:
        memory = r.get("memory_high_water") or {}
        stack = memory.get("stack_high_water_bytes")
        heap_min = memory.get("heap_min_free_bytes")
        heap_free = memory.get("heap_free_bytes")
        if stack is None and heap_min is None and heap_free is None:
            continue
        rows.append((r, stack, heap_min, heap_free))
    if not rows:
        return False

    labels = [f"{r['rtos']}\n{short(r['board'])}\n{r['language']}" for r, _, _, _ in rows]
    # NaN omits a metric on the log axis instead of plotting an invalid log(0).
    stack = [s if s else float("nan") for _, s, _, _ in rows]
    heap_min = [h if h else float("nan") for _, _, h, _ in rows]
    heap_free = [h if h else float("nan") for _, _, _, h in rows]
    xs = range(len(rows))
    fig, ax = plt.subplots(figsize=(1.5 * len(rows) + 3, 3.8))
    ax.bar([x - 0.27 for x in xs], stack, width=0.27, label="stack high-water", color="#1f77b4")
    ax.bar(xs, heap_min, width=0.27, label="heap min-free", color="#7f7f7f")
    ax.bar([x + 0.27 for x in xs], heap_free, width=0.27, label="heap free (run end)", color="#ff7f0e")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, fontsize=6)
    ax.set_yscale("log")
    ax.set_ylabel("bytes (log)")
    ax.set_title("Runtime memory high-water (where the port reports it)")
    ax.grid(axis="y", alpha=0.25, linewidth=0.5)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out.with_suffix(".svg"))
    fig.savefig(out.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return True


def plot_footprint(reports: list[dict], out: Path) -> bool:
    """Static flash and RAM footprint per path, from the built ELF images."""
    rows = []
    for report in reports:
        images = (report.get("static_footprint") or {}).get("images") or []
        if images:
            rows.append((report, images[0]))
    if not rows:
        return False

    labels = [f"{r['rtos']}\n{short(r['board'])}\n{r['language']}" for r, _ in rows]
    flash = [image.get("flash") or float("nan") for _, image in rows]
    ram = [image.get("ram") or float("nan") for _, image in rows]
    xs = range(len(rows))
    fig, ax = plt.subplots(figsize=(1.4 * len(rows) + 3, 3.8))
    ax.bar([x - 0.2 for x in xs], flash, width=0.4, label="flash (text+rodata+data)", color="#1f77b4")
    ax.bar([x + 0.2 for x in xs], ram, width=0.4, label="ram (data+bss)", color="#ff7f0e")
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, fontsize=6)
    ax.set_yscale("log")
    ax.set_ylabel("bytes (log)")
    ax.set_title("Static image footprint by path (allocatable ELF sections)")
    ax.grid(axis="y", alpha=0.25, linewidth=0.5)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out.with_suffix(".svg"))
    fig.savefig(out.with_suffix(".png"), dpi=200)
    plt.close(fig)
    return True


def short(board: str) -> str:
    return "STM32F4" if board == "stm32f4discovery" else "ESP32-C6"


def write_csv(reports: list[dict], out: Path) -> None:
    with out.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "rtos",
                "board",
                "language",
                "case",
                "n",
                "mean_us",
                "stddev_us",
                "min_us",
                "max_us",
                "median_us",
                "p95_us",
                "p99_us",
                "p999_us",
                "jitter_us",
            ]
        )
        for r in reports:
            for case, entry in sorted(r.get("cases", {}).items(), key=lambda kv: int(kv[0])):
                writer.writerow(
                    [
                        r["rtos"],
                        r["board"],
                        r["language"],
                        case,
                        entry.get("n"),
                        entry.get("mean_us"),
                        entry.get("stddev_us"),
                        entry.get("min_us"),
                        entry.get("max_us"),
                        entry.get("median_us"),
                        entry.get("p95_us"),
                        entry.get("p99_us"),
                        entry.get("p999_us"),
                        entry.get("jitter_us"),
                    ]
                )




def plot_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison", nargs="?", default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    comparison = resolve_comparison(args.comparison)
    reports = json.loads(comparison.read_text()).get("paths", [])
    if not reports:
        raise SystemExit(f"no paths in {comparison}")

    out = args.out or (comparison.parent / "plots")
    out.mkdir(parents=True, exist_ok=True)
    index = index_paths(reports)

    written: list[Path] = []
    for case in sorted(CASE_NAMES):
        if not any(case_entry(r, case) for r in reports):
            continue
        fig, ax = plt.subplots(figsize=(8.5, 4.0))
        plot_case(ax, index, case, compact=False)
        ax.legend(fontsize=8)
        fig.tight_layout()
        stem = out / f"case_{case}_{CASE_NAMES[case]}"
        fig.savefig(stem.with_suffix(".svg"))
        fig.savefig(stem.with_suffix(".png"), dpi=200)
        plt.close(fig)
        written.append(stem.with_suffix(".svg"))

    cases = [c for c in sorted(CASE_NAMES) if any(case_entry(r, c) for r in reports)]
    plot_tail(index, cases, out / "tail_p99")
    written.append(out / "tail_p99.svg")
    if any(
        (case_entry(r, c) or {}).get("p999_us") is not None
        for r in reports
        for c in cases
    ):
        plot_tail(index, cases, out / "tail_p999", metric="p999_us", label="p99.9")
        written.append(out / "tail_p999.svg")
    plot_overview(index, cases, out / "overview")
    written.append((out / "overview.svg"))
    if plot_ecdf(reports, cases, out / "ecdf"):
        written.append(out / "ecdf.svg")
    if plot_memory(reports, out / "memory_high_water"):
        written.append(out / "memory_high_water.svg")
    if plot_footprint(reports, out / "static_footprint"):
        written.append(out / "static_footprint.svg")
    write_csv(reports, out / "data.csv")

    for path in written:
        print(f"wrote {path}")
    print(f"saved diagrams under {out}")
    return 0



def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        print(__doc__)
        return 0
    command, rest = argv[0], argv[1:]
    if command == "compare":
        return compare_main(rest)
    if command == "report":
        _load_matplotlib()
        return plot_main(rest)
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
