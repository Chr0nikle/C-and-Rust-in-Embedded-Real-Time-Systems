#!/usr/bin/env python3
"""Summarise the hardware-verified benchmark campaign into Typst tables.

Reads the project's ``results/`` records and, for every (RTOS, board, language)
path and every benchmark case, selects the archived run with the most samples for
that case. Per-case selections are recorded in
``generated/campaign-provenance.json`` so every number is traceable to a
preserved record. Emits per-case timing matrices (rows = case, columns = board),
a coverage matrix, per-variant sweep tables, and a memory-collector table.

Run from this directory::

    python3 campaign.py
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2] / "project"
REPO_ROOT = PROJECT_ROOT.parent  # <root>/project
RESULTS = PROJECT_ROOT.parent / "results" / "campaigns"
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"

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
    10: "trigger_to_isr",
    11: "periodic_jitter_loaded",
    12: "semaphore_latency",
    13: "mutex_uncontended",
    14: "alloc_latency",
    15: "sleep_accuracy",
    16: "scheduler_overhead",
    17: "notification_latency",
    18: "ipc_payload_sweep",
    19: "ipc_throughput",
    20: "nested_irq_overhead",
    21: "tick_overhead",
    22: "idle_cpu_overhead",
    23: "cpu_overhead_under_load",
    24: "heap_fragmentation",
    25: "max_interrupt_disabled",
    26: "priority_scheduling_correctness",
    27: "zero_copy_ipc",
    28: "common_arena_fragmentation",
}
NON_LATENCY = {19, 22, 23, 24, 27, 28}
VARIANT_CASES = {5, 18, 23}
# Thesis presentation numbering: a contiguous 1..26 in the order the cases are
# presented in the methodology and the results table. Protocol IDs are the keys;
# the retired cases (a kernel-specific callback context, a binary conformance
# flag and a portability control) are absent.
THESIS_ORDER = [0, 1, 12, 13, 14, 2, 5, 6, 8, 16, 17, 7, 18, 19, 27,
                4, 10, 20, 21, 25, 3, 11, 15, 22, 23, 24]
PROTO2THESIS = {proto: i + 1 for i, proto in enumerate(THESIS_ORDER)}

RTOS_ORDER = [
    "freertos", "nuttx", "zephyr",
    "freertos-in-rust", "rivet",
]
BOARD_ORDER = {
    "freertos": ["stm32f4", "stm32h723", "esp32c6", "nrf52840dk"],
    "nuttx": ["stm32f4", "stm32h723", "esp32c6", "nrf52840dk"],
    "zephyr": ["stm32f4", "stm32h723", "esp32c6", "nrf52840dk"],
    # Multi-OS expansion paths (2026-10-05); Rust-only frameworks use the
    # CLI's default `bench` token but are single-language.
    # Pure-Rust kernels (2026-10-08), single board/language.
    "freertos-in-rust": ["stm32f4", "stm32h723"],
    "rivet": ["stm32f4"],
}
BOARD_TOKENS = {
    "freertos": {"stm32f4": "stm32f4", "h723": "stm32h723",
                 "c6": "esp32c6", "nrf52840": "nrf52840dk"},
    "nuttx": {"stm32f4": "stm32f4", "h723": "stm32h723",
              "c6": "esp32c6", "nrf52840dk": "nrf52840dk"},
    "zephyr": {
        "stm32f4": "stm32f4",
        "h723": "stm32h723",
        "c6": "esp32c6",
        "nrf52840dk": "nrf52840dk",
    },
    "freertos-in-rust": {"stm32f4": "stm32f4", "stm32h723": "stm32h723"},
    "rivet": {"stm32f4": "stm32f4"},
}
BOARD_LABEL = {
    "stm32f4": "STM32F4",
    "stm32h723": "STM32H723",
    "esp32c6": "ESP32-C6",
    "nrf52840dk": "nRF52840",
}
LANG_ORDER = ["bench", "rust"]
LANG_LABEL = {"bench": "C", "rust": "Rust"}
RTOS_LABEL = {
    "freertos": "FreeRTOS",
    "nuttx": "NuttX",
    "zephyr": "Zephyr",
    "freertos-in-rust": "FreeRTOS-in-Rust",
    "rivet": "Rivet RTOS",
}
# Single-language Rust paths whose result directories carry the CLI's default
# `bench` token rather than `rust`.
RUST_ONLY = {"freertos-in-rust", "rivet"}


def parse_dir(name: str):
    # RTOS names may contain hyphens (e.g. `freertos-in-rust`), so match the
    # longest known name that the directory starts with rather than assuming
    # the first hyphen-delimited token is the RTOS.
    rtos = None
    for candidate in RTOS_ORDER:
        if name == candidate or name.startswith(candidate + "-"):
            if rtos is None or len(candidate) > len(rtos):
                rtos = candidate
    if rtos is None or name == rtos:
        return None
    rest = name[len(rtos) + 1:].split("-")
    if len(rest) < 2:
        return None
    if rtos in RUST_ONLY:
        lang = "rust"
    else:
        lang = rest[-2]
        if lang not in LANG_ORDER:
            return None
    token = "-".join(rest[:-2])
    board = BOARD_TOKENS[rtos].get(token)
    if board is None:
        return None
    return rtos, board, lang


def corpus_paths() -> set:
    """Result files that belong to the compared campaign corpus.

    The coverage, timing, variant and memory tables must describe the same
    runs as the replication statistics, otherwise a superseded or
    out-of-scope run (for example a Rust path later removed from the corpus)
    reappears in the coverage table. ``replication-stats.json`` lists exactly
    the runs that were compared, so restrict to those.
    """
    stats = DATA / "replication-stats.json"
    if not stats.exists():
        return set()
    doc = json.loads(stats.read_text())
    paths = set()
    for entry in doc.get("paths", {}).values():
        for run in entry.get("runs", []):
            rel = run.get("result")
            if rel:
                paths.add((REPO_ROOT / rel).resolve())
    return paths


def load_runs() -> dict:
    runs: dict[tuple, list] = defaultdict(list)
    allow = corpus_paths()
    candidates = list(RESULTS.glob("*-*-*-*/benchmark-run-*.json"))
    candidates += list(RESULTS.glob("campaign-replication-*/benchmarks/*/benchmark-run-*.json"))
    for path in sorted(candidates):
        if allow and path.resolve() not in allow:
            continue
        parsed = parse_dir(path.parent.name)
        if parsed is None:
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not data.get("samples"):
            continue
        runs[parsed].append((path, data))
    return runs


def case_tps(data: dict, case: int) -> int:
    by = data.get("ticks_per_second_by_case") or {}
    return int(by.get(str(case), data.get("ticks_per_second", 1)))


def ticks_to_us(ticks: int, tps: int) -> float:
    return 1e6 * ticks / tps


def run_ts(path: Path) -> int:
    """Extract the numeric run timestamp from a benchmark file name.

    File names are ``benchmark-run-<epoch-seconds>[-<hex>].json``. Higher is
    newer; this is the tie-break so that, when two archived runs carry the same
    sample count for a case, the most recent measurement wins instead of the
    lexicographically largest build id.
    """
    stem = path.name
    prefix = "benchmark-run-"
    if not stem.startswith(prefix):
        return 0
    digits = ""
    for ch in stem[len(prefix):]:
        if ch.isdigit():
            digits += ch
        else:
            break
    return int(digits) if digits else 0


def select_series(runs_for_path: list) -> tuple[dict, dict, dict]:
    """Best run per case and per (case, variant), preferring sample count and
    then the most recent run.

    Returns ``(case_series, variant_series, coverage)``. Each series value is a
    tuple ``(median_ticks, tps, run_path)``; coverage is the set of cases seen.
    """
    best_case: dict[int, tuple] = {}
    best_var: dict[tuple[int, int], tuple] = {}
    coverage: set[int] = set()
    for path, data in runs_for_path:
        by_case: dict[int, list] = defaultdict(list)
        by_var: dict[tuple[int, int], list] = defaultdict(list)
        for s in data.get("samples", []):
            by_case[s["case"]].append(s["ticks"])
            by_var[(s["case"], s.get("variant", 0))].append(s["ticks"])
        coverage.update(by_case)
        for case, xs in by_case.items():
            score = (len(xs), run_ts(path))
            if case not in best_case or score > best_case[case][0]:
                best_case[case] = (score, int(statistics.median(xs)), case_tps(data, case), str(path))
        for key, xs in by_var.items():
            score = (len(xs), run_ts(path))
            if key not in best_var or score > best_var[key][0]:
                best_var[key] = (score, int(statistics.median(xs)), case_tps(data, key[0]), str(path))
    return best_case, best_var, coverage


def case_ranges(cases) -> str:
    """Compact a set of integers to '0–20, 24–28'."""
    cases = sorted(set(cases))
    if not cases:
        return "none"
    spans = []
    start = prev = cases[0]
    for c in cases[1:]:
        if c == prev + 1:
            prev = c
            continue
        spans.append((start, prev))
        start = prev = c
    spans.append((start, prev))
    parts = [str(a) if a == b else f"{a}–{b}" for a, b in spans]
    return ", ".join(parts)


def emit_coverage(series: dict) -> str:
    out = ["// Generated by analysis/benchmark/campaign.py. Do not edit by hand."]
    out.append("")
    out.append("#figure(")
    out.append("  text(size: 7.5pt, table(")
    out.append("    columns: (1.6fr, 1fr),")
    out.append("    align: (left, left),")
    out.append("    stroke: (x: none, y: 0.2pt + luma(210)),")
    out.append("    inset: (x: 3pt, y: 1.4pt),")
    out.append("    column-gutter: 2pt,")
    out.append("    table.header(")
    out.append("      table.cell(fill: luma(235))[*Path*],")
    out.append("      table.cell(fill: luma(235))[*Advertised cases (at least one measured run)*],")
    out.append("    ),")
    out.append("    table.hline(stroke: 0.6pt + luma(150)),")
    for rtos in RTOS_ORDER:
        for board in BOARD_ORDER[rtos]:
            for lang in LANG_ORDER:
                key = (rtos, board, lang)
                if key not in series:
                    continue
                cov = sorted(PROTO2THESIS[c] for c in series[key][2]
                             if c in PROTO2THESIS)
                out.append(
                    f"    [{RTOS_LABEL[rtos]} {BOARD_LABEL[board]} {LANG_LABEL[lang]}], "
                    f"[{case_ranges(cov)}],"
                )
    out.append("  )),")
    out.append(
        "  caption: [Case coverage by board, RTOS and language],"
    )
    out.append(") <campaign-coverage>")
    return "\n".join(out) + "\n"


def preserved_native_memory_rows(existing: str) -> list[str]:
    """Keep published Rust-native memory rows when their raw bundle is absent.

    These rows are historical summaries, not re-derived values. The publication
    caption identifies them and the evidence lock continues to mark the raw
    bundle as missing.
    """
    prefixes = ("    [FreeRTOS-in-Rust ", "    [Rivet RTOS ")
    return [
        line.replace("[Rivet RTOS ", "[Rivet ")
        for line in existing.splitlines()
        if line.lstrip().startswith(tuple(p.strip() for p in prefixes))
    ]


def emit_memory(runs: dict, native_rows: list[str] | None = None) -> str:
    native_rows = native_rows or []
    out = ["// Generated by analysis/benchmark/campaign.py. Do not edit by hand."]
    out.append("")
    out.append("#align(center)[#text(size: 11pt, weight: \"bold\")"
               "[Runtime memory measurements]]")
    out.append("#align(center)[#text(size: 8pt, fill: luma(100))"
               "[bytes unless noted; · or - = not reported]]")
    out.append("#v(0.3em)")
    out.append("#figure(")
    out.append("  text(size: 7.5pt, table(")
    out.append("    columns: (auto, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr),")
    out.append("    stroke: (x: none, y: 0.25pt + luma(210)),")
    out.append("    inset: (x: 3pt, y: 1.5pt),")
    out.append("    column-gutter: 2pt,")
    out.append("    align: (left, right, right, right, right, right, right, right, right),")
    out.append("    table.header(")
    out.append("      table.cell(fill: luma(235))[*Path*],")
    for header in ("Heap over", "Task RAM", "Tasks", "Worst", "Best",
                   "Bench stack free", "Heap min", "Heap free"):
        out.append(f"      table.cell(fill: luma(235))[*{header}*],")
    out.append("    ),")
    out.append("    table.hline(stroke: 0.6pt + luma(150)),")

    def cell(name: str) -> str:
        value = mem.get(name)
        return "·" if value is None else str(value)

    for rtos in ("freertos", "nuttx", "zephyr"):
        out.append(f"    table.cell(colspan: 9, align: left, fill: luma(248))"
                   f"[#v(0.2em) *{RTOS_LABEL[rtos]}* #v(0.2em)],")
        for board in BOARD_ORDER[rtos]:
            for lang in LANG_ORDER:
                key = (rtos, board, lang)
                if key not in runs:
                    continue
                def mem_rank(entry):
                    path, data = entry
                    candidate = data.get("memory_high_water") or {}
                    has_mem = any(v is not None for v in candidate.values())
                    return (
                        1 if has_mem else 0,
                        run_ts(path),
                        candidate.get("per_task_ram_overhead_bytes") is not None,
                        sum(1 for v in candidate.values() if v is not None),
                    )
                _, data = max(runs[key], key=mem_rank)
                mem = data.get("memory_high_water") or {}
                label = f"{BOARD_LABEL[board]} {LANG_LABEL[lang]}"
                out.append(
                    f"    [{label}], "
                    f"[{cell('heap_overhead_bytes')}], "
                    f"[{cell('per_task_ram_overhead_bytes')}], "
                    f"[{cell('task_count')}], "
                    f"[{cell('worst_stack_headroom_bytes')}], "
                    f"[{cell('best_stack_headroom_bytes')}], "
                    f"[{cell('stack_high_water_bytes')}], "
                    f"[{cell('heap_min_free_bytes')}], "
                    f"[{cell('heap_free_bytes')}],"
                )

    if native_rows:
        out.append("    table.cell(colspan: 9, align: left, fill: luma(248))"
                   "[#v(0.2em) *Rust-native (historical summaries)* #v(0.2em)],")
        out.extend(native_rows)
    elif any(rtos in ("freertos-in-rust", "rivet") for rtos, _, _ in runs):
        out.append("    table.cell(colspan: 9, align: left, fill: luma(248))"
                   "[#v(0.2em) *Rust-native* #v(0.2em)],")
        for rtos in ("freertos-in-rust", "rivet"):
            for board in BOARD_ORDER[rtos]:
                for lang in LANG_ORDER:
                    key = (rtos, board, lang)
                    if key not in runs:
                        continue
                    _, data = max(runs[key], key=mem_rank)
                    mem = data.get("memory_high_water") or {}
                    label = f"{RTOS_LABEL[rtos]} {BOARD_LABEL[board]}"
                    out.append(
                        f"    [{label}], "
                        f"[{cell('heap_overhead_bytes')}], "
                        f"[{cell('per_task_ram_overhead_bytes')}], "
                        f"[{cell('task_count')}], "
                        f"[{cell('worst_stack_headroom_bytes')}], "
                        f"[{cell('best_stack_headroom_bytes')}], "
                        f"[{cell('stack_high_water_bytes')}], "
                        f"[{cell('heap_min_free_bytes')}], "
                        f"[{cell('heap_free_bytes')}],"
                    )
    out.append("  )),")
    caption = ("Runtime memory fields by configuration; matched rows derive from the "
               "campaign selected by the thesis evidence lock. Bytes except task count. "
               "`Bench stack free` is the benchmark task's minimum unused stack. "
               "`·` or `-` means not reported.")
    if native_rows:
        caption += (" Rust-native rows are preserved historical summaries because "
                    "their raw campaign bundle is unavailable.")
    out.append(f"  caption: [{caption}],")
    out.append(") <campaign-memory>")
    return "\n".join(out) + "\n"


def memory_json_images(path: Path):
    """Footprint from a ``memory.json`` written by ``rtos memory`` beside a run.

    ``rtos memory`` re-derives the section sizes from a freshly linked image and
    stores them next to the build's result directory. When present it is newer
    and more authoritative than the footprint embedded in the benchmark record,
    so it is preferred (used to record the size-tuned ThreadX and ChibiOS Rust
    builds).
    """
    mj = path.parent / "memory.json"
    if not mj.exists():
        return None
    try:
        doc = json.loads(mj.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if "flash_bytes" not in doc or "ram_bytes" not in doc:
        return None
    return [{
        "image": Path(doc.get("elf", "image")).name,
        "flash": doc["flash_bytes"],
        "ram": doc["ram_bytes"],
    }]


def footprint_images(runs_for_path: list):
    """Newest static footprint for a path, or ``None``.

    Prefers a sibling ``memory.json`` from ``rtos memory``; otherwise the build
    workflow's ``static_footprint`` field in the benchmark result JSON
    (``core/src/workflow.rs``). Older runs wrote only a sibling
    ``static-footprint.json``, kept as a last fallback.
    """
    best = None
    for path, data in runs_for_path:
        images = memory_json_images(path)
        source = path
        if not images:
            images = (data.get("static_footprint") or {}).get("images") or []
        if not images:
            sibling = path.parent / "static-footprint.json"
            if sibling.exists():
                try:
                    images = json.loads(sibling.read_text())["footprint"]["images"]
                    source = sibling
                except (json.JSONDecodeError, KeyError, OSError):
                    images = []
        if not images:
            continue
        ts = run_ts(path)
        if best is None or ts > best[0]:
            best = (ts, images, source)
    return best



def emit_static(runs: dict) -> str:
    out = ["// Generated by analysis/benchmark/campaign.py. Do not edit by hand."]
    out.append("")
    out.append("#align(center)[#text(size: 11pt, weight: \"bold\")"
               "[Static memory measurements]]")
    out.append("#align(center)[#text(size: 8pt, fill: luma(100))"
               "[KiB; · = not reported]]")
    out.append("#v(0.3em)")
    out.append("#figure(")
    out.append("  text(size: 7.5pt, align(center, block(width: 68%, table(")
    out.append("    columns: (2fr, 1fr, 1fr),")
    out.append("    stroke: (x: none, y: 0.25pt + luma(210)),")
    out.append("    inset: (x: 3pt, y: 1.5pt),")
    out.append("    column-gutter: 3pt,")
    out.append("    align: (left, right, right),")
    out.append("    table.header(")
    out.append("      table.cell(fill: luma(235))[*Path*],")
    out.append("      table.cell(fill: luma(235))[*Flash (KiB)*],")
    out.append("      table.cell(fill: luma(235))[*RAM (KiB)*],")
    out.append("    ),")
    out.append("    table.hline(stroke: 0.6pt + luma(150)),")

    for rtos in ("freertos", "nuttx", "zephyr"):
        out.append("    table.cell(colspan: 3, align: left, fill: luma(248))"
                   f"[#v(0.2em) *{RTOS_LABEL[rtos]}* #v(0.2em)],")
        for board in BOARD_ORDER[rtos]:
            for lang in LANG_ORDER:
                key = (rtos, board, lang)
                if key not in runs:
                    continue
                label = f"{BOARD_LABEL[board]} {LANG_LABEL[lang]}"
                found = footprint_images(runs[key])
                if found is None:
                    out.append(f"    [{label}], [·], [·],")
                    continue
                _, images, _ = found
                flash = sum(i.get("flash", 0) for i in images)
                ram = sum(i.get("ram", 0) for i in images)
                out.append(
                    f"    [{label}], [{flash / 1024:.1f}], [{ram / 1024:.1f}],"
                )

    out.append("    table.cell(colspan: 3, align: left, fill: luma(248))"
               "[#v(0.2em) *Rust-native* #v(0.2em)],")
    for rtos in ("freertos-in-rust", "rivet"):
        for board in BOARD_ORDER[rtos]:
            for lang in LANG_ORDER:
                key = (rtos, board, lang)
                if key not in runs:
                    continue
                label = f"{RTOS_LABEL[rtos]} {BOARD_LABEL[board]}"
                found = footprint_images(runs[key])
                if found is None:
                    out.append(f"    [{label}], [·], [·],")
                    continue
                _, images, _ = found
                flash = sum(i.get("flash", 0) for i in images)
                ram = sum(i.get("ram", 0) for i in images)
                out.append(
                    f"    [{label}], [{flash / 1024:.1f}], [{ram / 1024:.1f}],"
                )

    out.append("  )))),")
    out.append(
        "  caption: [Static footprint by configuration; excludes bootloaders and partition tables],"
    )
    out.append(") <campaign-static>")
    return "\n".join(out) + "\n"


def main() -> None:
    TYP.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    runs = load_runs()
    series = {key: select_series(entries) for key, entries in runs.items()}
    native_keys = sorted(f"{rtos}/{board}/{lang}" for rtos, board, lang in runs
                         if rtos in {"freertos-in-rust", "rivet"})
    memory_target = TYP / "campaign-memory.typ"
    existing_memory = memory_target.read_text() if memory_target.is_file() else ""
    native_memory_rows = (
        [] if native_keys else preserved_native_memory_rows(existing_memory)
    )
    preserved_files = []
    for name, content, labels in (
        ("campaign-coverage.typ", emit_coverage(series), ("FreeRTOS-in-Rust", "Rivet")),
        ("campaign-memory.typ", emit_memory(runs, native_memory_rows),
         ("FreeRTOS-in-Rust", "Rivet")),
        ("campaign-static.typ", emit_static(runs), ("FreeRTOS-in-Rust", "Rivet")),
    ):
        target = TYP / name
        existing = target.read_text() if target.is_file() else ""
        preserve = not native_keys and any(label in existing for label in labels)
        if name == "campaign-memory.typ":
            target.write_text(content)
            if native_memory_rows:
                preserved_files.append(name)
        elif preserve:
            target.touch()
            preserved_files.append(name)
        else:
            target.write_text(content)
    if preserved_files:
        print("warning: selected campaign has no Rust-native rows; preserving historical "
              "Rust-native rows in " + ", ".join(preserved_files))
    stats_path = DATA / "replication-stats.json"
    stats = json.loads(stats_path.read_text()) if stats_path.is_file() else {}
    supplementary_keys = sorted({
        path for entry in stats.get("supplementary_bundles", [])
        for path in entry.get("paths", [])
    })
    provenance = {}
    for (rtos, board, lang), (case_series, variant_series, coverage) in sorted(series.items()):
        provenance[f"{rtos}/{board}/{lang}"] = {
            "coverage": sorted(coverage),
            "cases": {
                str(c): {
                    "run_path": case_series[c][3],
                    "median_ticks": case_series[c][1],
                    "ticks_per_second": case_series[c][2],
                }
                for c in sorted(case_series)
            },
        }
    provenance_path = DATA / "campaign-provenance.json"
    existing_provenance = provenance_path.read_text() if provenance_path.is_file() else ""
    native_history = {}
    if not native_keys and existing_provenance:
        try:
            existing_payload = json.loads(existing_provenance)
        except json.JSONDecodeError:
            existing_payload = {}
        native_history = {
            key: value for key, value in existing_payload.items()
            if key.split("/", 1)[0] in {"freertos-in-rust", "rivet"}
        }
    preserve_provenance = bool(native_history)
    if native_history:
        provenance.update(native_history)
        print("warning: retained historical Rust-native entries in campaign-provenance.json")
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(series)} path summaries to {TYP}")

    (DATA / "static-evidence.json").write_text(json.dumps({
        "schema_version": 1,
        "status": "mixed-historical" if preserved_files else "complete",
        "selected_campaign": Path(stats.get("bundle", "")).name or None,
        "preserved_historical_keys": supplementary_keys if preserved_files else [],
        "preserved_historical_rows_from_previous_table": bool(preserved_files or preserve_provenance),
        "preserved_files": [f"thesis/generated/typ/{name}" for name in preserved_files],
        "preserved_campaign_provenance": preserve_provenance,
        "preserved_campaign_provenance_sha256": (
            hashlib.sha256(provenance_path.read_bytes()).hexdigest() if preserve_provenance else None
        ),
        "preserved_sha256": {
            f"thesis/generated/typ/{name}": hashlib.sha256((TYP / name).read_bytes()).hexdigest()
            for name in preserved_files
        },
        "preserved_native_memory_rows": bool(native_memory_rows),
        "reason": (
            "the selected campaign has no Rust-native measurements; matched rows are "
            "regenerated and previously published Rust-native rows in coverage, memory "
            "and static tables are preserved rather than silently replaced"
        ) if preserved_files else "all campaign tables were regenerated from the selected campaign",
    }, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
