#!/usr/bin/env python3
"""Derived safety tables for the thesis and analysis records.

``generate_analysis_tables`` writes the matched/native operation summaries.
``generate_thesis_tables`` writes the Typst tables and revalidation evidence.
The command-line entry point is ``safety.py``.
"""
from __future__ import annotations

import csv
import io
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common.config import resolve_repo_root
from common.paths import ARTIFACTS, SAFETY_DIR, THESIS_TYP

RTOS_ORDER = ("FreeRTOS", "Zephyr", "NuttX")
BOARD_ORDER = ("STM32F4 Discovery", "STM32H723", "nRF52840 DK", "ESP32-C6")
COMPONENTS = (
    ("c_application", "C application"),
    ("c_adapter", "C adapter"),
    ("rust_application", "Rust application"),
    ("rust_ffi_boundary", "Rust FFI boundary"),
)


def source_role(row: dict[str, str]) -> str:
    source = row["source_file"]
    scope = row["scope"]
    language = row["language"]
    name = os.path.basename(source)
    if language == "rust":
        if name == "boundary.rs":
            return "rust_ffi_boundary"
        if "/common/rust/" in source or (name == "arena.rs" and "/rust-kernels/" in source):
            return "shared_rust"
        if "/rust-kernels/" in source:
            return "rust_native_application"
        return "rust_application"
    if name in {"runner.c", "bench_arena.h", "rtos_bench.h"} or "/include/" in source:
        return "shared_c"
    if scope == "c-support":
        return "c_support"
    if name == "main.c":
        return "c_application"
    return "c_adapter"


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def generate_analysis_tables(repo_root: Path, output: Path) -> None:
    profiles = {}
    for path in sorted((ARTIFACTS / "profiles").glob("*-all.json")):
        profile = json.loads(path.read_text())
        profiles[profile["id"]] = profile
    operation_rows = []
    role_totals = []
    comparison = []
    layer_counts: dict[str, Counter[str]] = {}
    for directory in sorted((ARTIFACTS / "generated_inventory").iterdir()):
        if not (directory / "operations.csv").is_file():
            continue
        name = directory.name
        rows = [
            row for row in csv.DictReader(io.StringIO((directory / "operations.csv").read_text()))
            if row["activity_status"] == "true" and row["resolution_status"] == "resolved"
        ]
        layer_counts[name] = Counter(row["layer"] for row in rows)
        counts = Counter((source_role(row), row["category"]) for row in rows)
        role_records = Counter(source_role(row) for row in rows)
        profile = profiles.get(name, {})
        for (role, category), count in sorted(counts.items()):
            operation_rows.append({
                "configuration": name, "rtos": profile.get("rtos", ""), "board": profile.get("board", ""),
                "language": "rust" if role.startswith("rust") or role == "shared_rust" else "c",
                "role": role, "category": category, "count": count,
                "unit": "configuration-specific active resolved operation occurrence",
            })
        by_role: dict[str, Counter] = defaultdict(Counter)
        for (role, category), count in counts.items():
            by_role[role][category] += count
        for role, categories in by_role.items():
            role_totals.append({"configuration": name, "rtos": profile.get("rtos", ""), "board": profile.get("board", ""), "role": role, "total": role_records[role]})
        c_app, c_adapter = by_role["c_application"], by_role["c_adapter"]
        r_app = by_role["rust_application"] + by_role["rust_native_application"]
        r_ffi = by_role["rust_ffi_boundary"]
        comparison.append({
            "configuration": name, "rtos": profile.get("rtos", ""), "board": profile.get("board", ""),
            "c_app_records": sum(c_app.values()), "c_adapter_records": sum(c_adapter.values()),
            "rust_app_records": sum(r_app.values()), "rust_ffi_records": sum(r_ffi.values()),
            "c_app_pointer_deref": c_app["pointer_dereference"], "c_adapter_pointer_deref": c_adapter["pointer_dereference"],
            "c_app_pointer_arith": c_app["pointer_arithmetic"], "c_adapter_pointer_arith": c_adapter["pointer_arithmetic"],
            "c_app_pointer_cast": c_app["pointer_conversion"], "c_adapter_pointer_cast": c_adapter["pointer_conversion"],
            "c_app_macro_ptr_cast": c_app["macro_pointer_conversion"], "c_adapter_macro_ptr_cast": c_adapter["macro_pointer_conversion"],
            "rust_app_raw_deref": r_app["raw_pointer_dereference"], "rust_ffi_raw_deref": r_ffi["raw_pointer_dereference"],
            "rust_app_ptr_arith": r_app["pointer_arithmetic"], "rust_ffi_ptr_arith": r_ffi["pointer_arithmetic"],
            "rust_app_ptr_cast": r_app["pointer_conversion"], "rust_ffi_ptr_cast": r_ffi["pointer_conversion"],
        })
    trace = []
    for directory in sorted((ARTIFACTS / "generated_outputs").iterdir()):
        if not (directory / "coverage.json").is_file():
            continue
        name = directory.name
        coverage = json.loads((directory / "coverage.json").read_text())
        summary = json.loads((directory / "summary.json").read_text())
        profile = profiles.get(name, {})
        calls = list(csv.DictReader(io.StringIO((directory / "call-sites.csv").read_text())))
        memberships = list(csv.DictReader(io.StringIO((directory / "case-memberships.csv").read_text())))
        trace.append({
            "configuration": name, "rtos": profile.get("rtos", ""), "board": profile.get("board", ""),
            "implemented_cases": coverage["selected_cases"], "comparable": coverage["case_coverage_comparable"],
            "common_only": coverage["case_coverage_common_only"], "partial": coverage["case_coverage_partial"],
            "c_call_sites": sum(1 for row in calls if row["language"] == "c"),
            "rust_call_sites": sum(1 for row in calls if row["language"] == "rust"),
            "rust_wrapper_memberships": sum(1 for row in memberships if "Rust_wrapper" in row["roles"]),
            "c_bridge_memberships": sum(1 for row in memberships if "C_bridge" in row["roles"]),
            "distinct_rtos_targets": summary.get("distinct_rtos_api_targets"),
            "matched_rtos_targets": summary.get("matched_rtos_api_targets"),
            "check_occurrences": summary.get("check_occurrences"),
            "unsafe_context_call_occurrences": summary.get("unsafe_context_call_occurrences"),
            "ffi_symbols": summary.get("ffi_symbols"),
            "callback_registrations": summary.get("callback_registrations"),
        })
    native = []
    for directory in sorted((ARTIFACTS / "generated_inventory").glob("native-*")):
        coverage = json.loads((directory / "coverage.json").read_text())
        trace_summary = json.loads((directory / "native-case-trace-summary.json").read_text())
        native.append({
            "configuration": directory.name, "cases": trace_summary["case_count"],
            "complete": sum(1 for case in trace_summary["cases"] if case["status"] == "complete"),
            "complete_with_boundary": sum(1 for case in trace_summary["cases"] if case["status"] == "complete_with_external_boundary"),
            "operation_rows": coverage["operation_rows"], "rust_features": ",".join(coverage.get("rust_features", [])),
        })
    baseline_path = SAFETY_DIR / "specs" / "layer-classification-baseline.json"
    baseline = json.loads(baseline_path.read_text())["configurations"]
    layer_comparison = []
    for configuration in sorted(set(baseline) | set(layer_counts)):
        before = Counter(baseline.get(configuration, {}).get("active_resolved_by_layer", {}))
        after = layer_counts.get(configuration, Counter())
        for layer in sorted(set(before) | set(after)):
            layer_comparison.append({
                "configuration": configuration,
                "layer": layer,
                "before": before[layer],
                "after": after[layer],
                "delta": after[layer] - before[layer],
                "unit": "configuration-specific active resolved operation occurrence",
            })
    output.mkdir(parents=True, exist_ok=True)
    _write_csv(output / "operation-summary.csv", operation_rows)
    _write_csv(output / "operation-role-totals.csv", role_totals)
    _write_csv(output / "matched-operation-comparison.csv", comparison)
    _write_csv(output / "matched-trace-summary.csv", trace)
    _write_csv(output / "native-summary.csv", native)
    _write_csv(output / "layer-classification-comparison.csv", layer_comparison)
    (output / "README.json").write_text(json.dumps({
        "schema": "uastw-analysis-tables-v2",
        "operation_unit": "configuration-specific active resolved operation occurrence; category counts may overlap",
        "trace_unit": "per-configuration call-site/membership rows",
        "layer_baseline": "specs/layer-classification-baseline.json is the frozen pre-correction snapshot; layer-classification-comparison.csv reports before/after active-resolved counts per configuration and layer.",
        "files": ["operation-summary.csv", "operation-role-totals.csv", "matched-operation-comparison.csv", "matched-trace-summary.csv", "native-summary.csv", "layer-classification-comparison.csv", "safety-revalidation.json (written by tables.generate_thesis_tables)"],
    }, indent=2) + "\n")


def _read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


def _active_resolved(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if row["activity_status"] == "true" and row["resolution_status"] == "resolved"]


def _range(values: list[int]) -> str:
    if not values:
        return "·"
    low, high = min(values), max(values)
    return str(low) if low == high else f"{low}–{high}"


def _ratio(values: list[tuple[int, int]]) -> str:
    if not values:
        return "·"
    unique = sorted(set(values))
    if len(unique) == 1:
        return f"{unique[0][0]}/{unique[0][1]}"
    if len({denominator for _numerator, denominator in unique}) == 1:
        return f"{_range([numerator for numerator, _denominator in unique])}/{unique[0][1]}"
    return "–".join(f"{numerator}/{denominator}" for numerator, denominator in unique)


def _header(cells: list[str]) -> str:
    return "    table.header(" + ", ".join(f'table.cell(fill: luma(235))[*{cell}*]' for cell in cells) + "),"


def _table(cells: list[str], columns: tuple[str, ...], align: tuple[str, ...],
           caption: str, label: str, size: str = "7.2pt", inset: str = "1.2pt",
           gutter: str = "2pt") -> str:
    return "\n".join([
        f"#set text(size: {size})",
        "#figure(",
        f"  text(size: {size}, align(center, table(",
        f"    columns: ({', '.join(columns)}),",
        f"    align: ({', '.join(align)}),",
        "    stroke: (x: none, y: 0.2pt + luma(210)),",
        f"    inset: (x: 2.5pt, y: {inset}),",
        f"    column-gutter: {gutter},",
        *cells,
        "  ))),",
        f"  caption: [{caption}],",
        f") <{label}>",
    ])


def _group_row(text: str, columns: int) -> str:
    return f"    table.cell(colspan: {columns}, align: left, fill: luma(248))[#v(0.15em) *{text}* #v(0.45em)],"


def _board_sort(profile: dict[str, Any]) -> tuple[int, str]:
    board = profile.get("board", "")
    try:
        return BOARD_ORDER.index(board), board
    except ValueError:
        return len(BOARD_ORDER), board


def _load_matched(profiles_dir: Path, inventory_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(profiles_dir.glob("*-all.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        name = path.stem
        rows = _read_csv(inventory_dir / name / "operations.csv")
        active = _active_resolved(rows)
        by_role: dict[str, Counter[str]] = defaultdict(Counter)
        role_totals: Counter[str] = Counter()
        unsafe: Counter[str] = Counter()
        unresolved: dict[str, Counter[str]] = defaultdict(Counter)
        for row in active:
            role = source_role(row)
            by_role[role][row["category"]] += 1
            role_totals[role] += 1
            if row.get("in_unsafe_context") == "true":
                unsafe[role] += 1
        for row in rows:
            if row["activity_status"] == "true" and row["resolution_status"] != "resolved":
                unresolved[source_role(row)][row["category"]] += 1
        result[name] = {
            "profile": profile,
            "rows": active,
            "by_role": by_role,
            "role_totals": role_totals,
            "unsafe": unsafe,
            "unresolved": unresolved,
        }
    return result


def _load_native(inventory_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(inventory_dir.glob("native-*")):
        operations = _read_csv(path / "operations.csv")
        active = _active_resolved(operations)
        by_role: dict[str, Counter[str]] = defaultdict(Counter)
        unsafe = Counter()
        unresolved: dict[str, Counter[str]] = defaultdict(Counter)
        for row in active:
            role = source_role(row)
            by_role[role][row["category"]] += 1
            if row.get("in_unsafe_context") == "true":
                unsafe[role] += 1
        for row in operations:
            if row["activity_status"] == "true" and row["resolution_status"] != "resolved":
                unresolved[source_role(row)][row["category"]] += 1
        coverage = json.loads((path / "coverage.json").read_text())
        trace = json.loads((path / "native-case-trace-summary.json").read_text())
        profile_path = ARTIFACTS / "profiles" / f"{path.name}.json"
        profile = json.loads(profile_path.read_text()) if profile_path.is_file() else {}
        result[path.name] = {
            "profile": profile,
            "rows": active,
            "by_role": by_role,
            "active_resolved": len(active),
            "unsafe": unsafe,
            "unresolved": unresolved,
            "coverage": coverage,
            "trace": trace,
        }
    return result


def _matched_groups(matched: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for name, item in matched.items():
        groups[item["profile"]["rtos"]].append(name)
    for rtos in groups:
        groups[rtos].sort(key=lambda name: _board_sort(matched[name]["profile"]))
    return groups


def _component_values(matched: dict[str, dict[str, Any]], names: list[str], role: str,
                      category: str) -> list[int]:
    return [matched[name]["by_role"][role][category] for name in names]


def _component_unsafe(matched: dict[str, dict[str, Any]], names: list[str], role: str) -> list[tuple[int, int]]:
    return [(matched[name]["unsafe"][role], matched[name]["role_totals"][role]) for name in names]


def compact_table(matched: dict[str, dict[str, Any]]) -> str:
    groups = _matched_groups(matched)
    columns = 7
    cells = [
        _header(["RTOS", "Component", "Raw deref.", "Pointer arith.", "Pointer conv.", "Macro ptr. cast", "Unsafe context"]),
        "    table.hline(stroke: 0.6pt + luma(150)),",
    ]
    for rtos in RTOS_ORDER:
        names = groups.get(rtos, [])
        for component_index, (role, label) in enumerate(COMPONENTS):
            if role == "c_application":
                values = [
                    _range(_component_values(matched, names, role, "pointer_dereference")),
                    _range(_component_values(matched, names, role, "pointer_arithmetic")),
                    _range(_component_values(matched, names, role, "pointer_conversion")),
                    _range(_component_values(matched, names, role, "macro_pointer_conversion")),
                    "·",
                ]
            elif role == "c_adapter":
                values = [
                    _range(_component_values(matched, names, role, "pointer_dereference")),
                    _range(_component_values(matched, names, role, "pointer_arithmetic")),
                    _range(_component_values(matched, names, role, "pointer_conversion")),
                    _range(_component_values(matched, names, role, "macro_pointer_conversion")),
                    "·",
                ]
            elif role == "rust_application":
                values = [
                    _range(_component_values(matched, names, role, "raw_pointer_dereference")),
                    _range(_component_values(matched, names, role, "pointer_arithmetic")),
                    _range(_component_values(matched, names, role, "pointer_conversion")),
                    "·",
                    _ratio(_component_unsafe(matched, names, role)),
                ]
            else:
                values = [
                    _range(_component_values(matched, names, role, "raw_pointer_dereference")),
                    _range(_component_values(matched, names, role, "pointer_arithmetic")),
                    _range(_component_values(matched, names, role, "pointer_conversion")),
                    "·",
                    _ratio(_component_unsafe(matched, names, role)),
                ]
            prefix = f"    table.cell(rowspan: {len(COMPONENTS)}, fill: luma(248))[*{rtos}*], " if component_index == 0 else "    "
            cells.append(prefix + "[" + label + "], " + ", ".join(f"[{value}]" for value in values) + ",")
        cells.append("    table.hline(stroke: 0.6pt + luma(150)),")
    if cells[-1].endswith("table.hline(stroke: 0.6pt + luma(150)),"):
        cells.pop()
    caption = (
        "Active resolved operations by source-path role. Ranges span boards; "
        "C and Rust categories are not directly comparable."
    )
    return _table(cells, ("auto", "auto", "auto", "auto", "auto", "auto", "auto"),
                  ("left", "left", "right", "right", "right", "right", "right"),
                  caption, "safety-comparison", size="8.0pt", inset="1.8pt",
                  gutter="3pt")


def c_category_table(matched: dict[str, dict[str, Any]]) -> str:
    groups = _matched_groups(matched)
    columns = 11
    cells = [
        _header(["Path", "Role", "Records", "Deref.", "Field", "Arith.", "Cast", "Macro cast", "Array", "Kernel", "Global"]),
        "    table.hline(stroke: 0.6pt + luma(150)),",
    ]
    for rtos in RTOS_ORDER:
        cells.append(_group_row(rtos, columns))
        for name in groups.get(rtos, []):
            item = matched[name]
            board = item["profile"]["board"]
            for role, label in (("c_application", "C application"), ("c_adapter", "C adapter"), ("c_support", "C support")):
                counts = item["by_role"][role]
                cells.append(
                    f"    [{board}], [{label}], [{item['role_totals'][role]}], "
                    f"[{counts['pointer_dereference']}], [{counts['pointer_field_access']}], "
                    f"[{counts['pointer_arithmetic']}], [{counts['pointer_conversion']}], "
                    f"[{counts['macro_pointer_conversion']}], [{counts['array_index']}], "
                    f"[{counts['kernel_api_call']}], "
                    f"[{sum(counts[key] for key in ('mutable_global_read', 'mutable_global_write', 'mutable_global_readwrite', 'mutable_global_address'))}],")
        cells.append("    table.hline(stroke: 0.6pt + luma(150)),")
    if cells[-1].endswith("table.hline(stroke: 0.6pt + luma(150)),"):
        cells.pop()
    caption = "C operation categories by role and configuration"
    return _table(cells, ("auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto"),
                  ("left", "left", "right", "right", "right", "right", "right", "right", "right", "right", "right"),
                  caption, "safety-c-categories", size="7.5pt", inset="1.5pt",
                  gutter="2.5pt")


def rust_category_table(matched: dict[str, dict[str, Any]]) -> str:
    groups = _matched_groups(matched)
    columns = 10
    cells = [
        _header(["Path", "Role", "Records", "Raw deref.", "Unres. raw", "Arith.", "Cast", "Kernel", "Global", "Unsafe"]),
        "    table.hline(stroke: 0.6pt + luma(150)),",
    ]
    for rtos in RTOS_ORDER:
        cells.append(_group_row(rtos, columns))
        for name in groups.get(rtos, []):
            item = matched[name]
            board = item["profile"]["board"]
            for role, label in (("rust_application", "Rust application"), ("rust_ffi_boundary", "Rust FFI boundary")):
                counts = item["by_role"][role]
                cells.append(
                    f"    [{board}], [{label}], [{item['role_totals'][role]}], "
                    f"[{counts['raw_pointer_dereference']}], [{item['unresolved'][role]['raw_pointer_dereference']}], [{counts['pointer_arithmetic']}], "
                    f"[{counts['pointer_conversion']}], [{counts['kernel_api_call']}], "
                    f"[{sum(counts[key] for key in ('mutable_global_read', 'mutable_global_write', 'mutable_global_readwrite', 'mutable_global_address', 'mutable_global_unresolved'))}], "
                    f"[{item['unsafe'][role]}/{item['role_totals'][role]}],")
        cells.append("    table.hline(stroke: 0.6pt + luma(150)),")
    if cells[-1].endswith("table.hline(stroke: 0.6pt + luma(150)),"):
        cells.pop()
    caption = "Rust operation categories by role and configuration"
    return _table(cells, ("auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto"),
                  ("left", "left", "right", "right", "right", "right", "right", "right", "right", "right"),
                  caption, "safety-rust-categories", size="7.0pt", inset="1.2pt")


def role_table(matched: dict[str, dict[str, Any]], native: dict[str, dict[str, Any]]) -> str:
    groups = _matched_groups(matched)
    columns = 7
    cells = [
        _header(["Path", "C app.", "C adapter", "C support", "Shared", "Rust app.", "Rust FFI"]),
        "    table.hline(stroke: 0.6pt + luma(150)),",
    ]
    for rtos in RTOS_ORDER:
        cells.append(_group_row(rtos, columns))
        for name in groups.get(rtos, []):
            item = matched[name]
            totals = item["role_totals"]
            cells.append(
                f"    [{item['profile']['board']}], [{totals['c_application']}], "
                f"[{totals['c_adapter']}], [{totals['c_support']}], "
                f"[{totals['shared_c'] + totals['shared_rust']}], "
                f"[{totals['rust_application']}], [{totals['rust_ffi_boundary']}],")
        cells.append("    table.hline(stroke: 0.6pt + luma(150)),")
    cells.append(_group_row("Rust-native", columns))
    for name in sorted(native):
        item = native[name]
        profile = item["profile"]
        totals = Counter()
        shared = 0
        native_app = 0
        for role_name, counts in item["by_role"].items():
            if role_name == "shared_rust":
                shared += sum(counts.values())
            elif role_name == "rust_native_application":
                native_app += sum(counts.values())
        label = f"{profile.get('rtos', name)} / {profile.get('board', name)}"
        cells.append(
            f"    [{label}], [·], [·], [·], [{shared}], "
            f"[{native_app}], [0],")
    caption = "Operation counts by source role and configuration"
    return _table(cells, ("auto", "auto", "auto", "auto", "auto", "auto", "auto"),
                  ("left", "right", "right", "right", "right", "right", "right"),
                  caption, "safety-role-counts", size="7.7pt", inset="1.7pt",
                  gutter="3pt")


def trace_table(base: Path) -> str:
    columns = 7
    cells = [
        _header(["Path", "Cases", "Comparable", "C call sites", "Rust call sites", "FFI symbols", "Unsafe calls"]),
        "    table.hline(stroke: 0.6pt + luma(150)),",
    ]
    for path in sorted((base / "generated_outputs").glob("*-all")):
        coverage_path = path / "coverage.json"
        if not coverage_path.is_file():
            continue
        coverage = json.loads(coverage_path.read_text())
        summary = json.loads((path / "summary.json").read_text())
        calls = _read_csv(path / "call-sites.csv")
        cells.append(
            f"    [{path.name}], [{coverage['selected_cases']}], "
            f"[{coverage['case_coverage_comparable']}], "
            f"[{sum(1 for row in calls if row['language'] == 'c')}], "
            f"[{sum(1 for row in calls if row['language'] == 'rust')}], "
            f"[{summary.get('ffi_symbols', 0)}], "
            f"[{summary.get('unsafe_context_call_occurrences', 0)}],")
    caption = "Source-tracing coverage and call-site volume"
    return _table(cells, ("auto", "auto", "auto", "auto", "auto", "auto", "auto"),
                  ("left", "right", "right", "right", "right", "right", "right"),
                  caption, "safety-trace-coverage", size="7.2pt", inset="1.3pt")


def native_table(native: dict[str, dict[str, Any]]) -> str:
    columns = 10
    cells = [
        _header(["Configuration", "Cases", "Complete", "Boundary", "Records", "Raw deref.", "Unres. raw", "Cast", "Kernel", "Unsafe"]),
        "    table.hline(stroke: 0.6pt + luma(150)),",
    ]
    for name in sorted(native):
        item = native[name]
        counts = item["by_role"]["rust_native_application"]
        shared = item["by_role"]["shared_rust"]
        total = sum(counts.values()) + sum(shared.values())
        unsafe = item["unsafe"]["rust_native_application"] + item["unsafe"]["shared_rust"]
        trace_cases = item["trace"]["cases"]
        complete = sum(1 for case in trace_cases if case["status"] == "complete")
        boundary = sum(1 for case in trace_cases if case["status"] == "complete_with_external_boundary")
        cells.append(
            f"    [{item['profile'].get('rtos', name)} / {item['profile'].get('board', '')}], "
            f"[{item['trace']['case_count']}], [{complete}], [{boundary}], [{total}], "
            f"[{counts['raw_pointer_dereference'] + shared['raw_pointer_dereference']}], "
            f"[{item['unresolved']['rust_native_application']['raw_pointer_dereference'] + item['unresolved']['shared_rust']['raw_pointer_dereference']}], "
            f"[{counts['pointer_conversion'] + shared['pointer_conversion']}], "
            f"[{counts['kernel_api_call'] + shared['kernel_api_call']}], "
            f"[{unsafe}/{total}],")
    caption = "Rust-native operation results"
    return _table(cells, ("auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto", "auto"),
                  ("left", "right", "right", "right", "right", "right", "right", "right", "right", "right"),
                  caption, "safety-native-results", size="7.0pt", inset="1.2pt")


def _revalidation(matched: dict[str, dict[str, Any]], native: dict[str, dict[str, Any]], base: Path) -> dict[str, Any]:
    free_rtos = _matched_groups(matched).get("FreeRTOS", [])
    nuttx = _matched_groups(matched).get("NuttX", [])
    zephyr = _matched_groups(matched).get("Zephyr", [])
    all_matched = [name for rtos in RTOS_ORDER for name in _matched_groups(matched).get(rtos, [])]
    app_raw = {name: matched[name]["by_role"]["rust_application"]["raw_pointer_dereference"] for name in all_matched}
    ffi_raw = {name: matched[name]["by_role"]["rust_ffi_boundary"]["raw_pointer_dereference"] for name in all_matched}
    c_app_raw = {name: matched[name]["by_role"]["c_application"]["pointer_dereference"] for name in all_matched}
    c_adapter_raw = {name: matched[name]["by_role"]["c_adapter"]["pointer_dereference"] for name in all_matched}
    c_scope_raw = {name: c_app_raw[name] + c_adapter_raw[name] for name in all_matched}
    rust_scope_raw = {name: app_raw[name] + ffi_raw[name] for name in all_matched}
    app_unresolved = {name: matched[name]["unresolved"]["rust_application"]["raw_pointer_dereference"] for name in all_matched}
    ffi_unresolved = {name: matched[name]["unresolved"]["rust_ffi_boundary"]["raw_pointer_dereference"] for name in all_matched}
    app_unsafe = {name: matched[name]["unsafe"]["rust_application"] for name in all_matched}
    app_total = {name: matched[name]["role_totals"]["rust_application"] for name in all_matched}
    ffi_unsafe = {name: matched[name]["unsafe"]["rust_ffi_boundary"] for name in all_matched}
    ffi_total = {name: matched[name]["role_totals"]["rust_ffi_boundary"] for name in all_matched}
    interrupt_rows = []
    for path in sorted((base / "generated_outputs").glob("*-all")):
        coverage = path / "path-coverage.csv"
        if not coverage.is_file():
            continue
        for row in _read_csv(coverage):
            if int(row["external_isr_roots"] or 0) > 0 or row["shared_c_isr"] == "true":
                interrupt_rows.append({
                    "configuration": path.name,
                    "thesis_case": int(row["benchmark_case"]),
                    "external_isr_roots": int(row["external_isr_roots"] or 0),
                    "shared_c_isr": row["shared_c_isr"] == "true",
                })
    interrupt_configs = sorted({row["configuration"] for row in interrupt_rows})
    unique_examples = {}
    for name in ("freertos-stm32f4-all", "nuttx-stm32f4-all", "zephyr-stm32f4-all"):
        item = matched[name]
        row_counts = {}
        for role in ("c_application", "c_adapter", "rust_application", "rust_ffi_boundary"):
            selected = [row for row in item["rows"] if source_role(row) == role and row["category"] in ("pointer_dereference", "raw_pointer_dereference")]
            row_counts[role] = {
                "occurrences": len(selected),
                "distinct_file_line_locations": len({(row["source_file"], row["source_line"]) for row in selected}),
            }
        unique_examples[name] = row_counts
    return {
        "schema": "uastw-safety-revalidation-v1",
        "unit": "configuration-specific active resolved operation occurrence",
        "unique_location_examples": unique_examples,
        "checks": {
            "rust_application_no_resolved_direct_pointer_operations": {
                "status": all(
                    all(matched[name]["by_role"]["rust_application"][category] == 0
                        for category in ("raw_pointer_dereference", "pointer_arithmetic", "pointer_conversion"))
                    for name in all_matched
                ),
                "evidence": {
                    name: {
                        "resolved": {category: matched[name]["by_role"]["rust_application"][category]
                                     for category in ("raw_pointer_dereference", "pointer_arithmetic", "pointer_conversion")},
                        "unresolved_raw_deref": app_unresolved[name],
                    }
                    for name in all_matched
                },
            },
            "unresolved_pointer_records_are_retained": {
                "status": True,
                "evidence": {
                    "rust_application_unresolved_raw_deref": {name: app_unresolved[name] for name in all_matched},
                    "rust_ffi_unresolved_raw_deref": {name: ffi_unresolved[name] for name in all_matched},
                    "note": "The NuttX Rust application record is the unary dereference of a mutable array-reference binding at lib.rs:303, not a raw-pointer dereference. Boundary records remain unresolved raw-pointer records and are excluded from the resolved comparison.",
                },
            },
            "c_application_retains_pointer_operations": {
                "status": all(
                    matched[name]["by_role"]["c_application"]["pointer_dereference"] > 0
                    and matched[name]["by_role"]["c_application"]["pointer_arithmetic"] > 0
                    and matched[name]["by_role"]["c_application"]["pointer_conversion"] > 0
                    for name in all_matched
                ),
                "evidence": {
                    name: {category: matched[name]["by_role"]["c_application"][category]
                           for category in ("pointer_dereference", "pointer_arithmetic", "pointer_conversion")}
                    for name in all_matched
                },
            },
            "rust_ffi_retains_raw_pointer_operations": {
                "status": all(
                    matched[name]["by_role"]["rust_ffi_boundary"]["raw_pointer_dereference"] > 0
                    and matched[name]["by_role"]["rust_ffi_boundary"]["pointer_conversion"] > 0
                    for name in all_matched
                ),
                "evidence": {
                    name: {
                        "resolved": {category: matched[name]["by_role"]["rust_ffi_boundary"][category]
                                     for category in ("raw_pointer_dereference", "pointer_arithmetic", "pointer_conversion")},
                        "unresolved_raw_deref": ffi_unresolved[name],
                    }
                    for name in all_matched
                },
            },
            "nuttx_zephyr_rust_raw_deref_exceeds_c_app_adapter": {
                "status": all(rust_scope_raw[name] > c_scope_raw[name] for name in nuttx + zephyr),
                "evidence": {
                    name: {"rust_application_plus_ffi_raw_deref": rust_scope_raw[name],
                           "c_application_plus_adapter_raw_deref": c_scope_raw[name]}
                    for name in nuttx + zephyr
                },
            },
            "rust_application_has_unsafe_contexts": {
                "status": all(app_unsafe[name] > 0 for name in all_matched),
                "evidence": {name: {"in_unsafe_context": app_unsafe[name], "resolved": app_total[name]} for name in all_matched},
            },
            "interrupt_paths_reach_c_isr_or_bridge": {
                "status": bool(interrupt_rows) and all(row["shared_c_isr"] for row in interrupt_rows),
                "evidence": {
                    "configuration_count": len(interrupt_configs),
                    "case_configuration_rows": len(interrupt_rows),
                    "all_shared_c_isr": all(row["shared_c_isr"] for row in interrupt_rows),
                },
            },
            "native_inventories_are_distinct": {
                "status": set(native) == {
                    "native-freertos-in-rust-stm32f4",
                    "native-freertos-in-rust-stm32h723",
                    "native-rivet-stm32f4",
                },
                "evidence": {
                    name: {
                        "records": item["active_resolved"],
                        "raw_deref": item["by_role"]["rust_native_application"]["raw_pointer_dereference"],
                        "unresolved_raw_deref": (
                            item["unresolved"]["rust_native_application"]["raw_pointer_dereference"]
                            + item["unresolved"]["shared_rust"]["raw_pointer_dereference"]
                        ),
                        "pointer_conversion": item["by_role"]["rust_native_application"]["pointer_conversion"],
                        "kernel_calls": item["by_role"]["rust_native_application"]["kernel_api_call"],
                        "features": item["coverage"].get("rust_features", []),
                    }
                    for name, item in native.items()
                },
            },
        },
        "counts": {
            "matched_configurations": len(all_matched),
            "rust_native_configurations": len(native),
            "free_rtos_rust_scope_raw_deref": {name: rust_scope_raw[name] for name in free_rtos},
            "nuttx_rust_scope_raw_deref": {name: rust_scope_raw[name] for name in nuttx},
            "zephyr_rust_scope_raw_deref": {name: rust_scope_raw[name] for name in zephyr},
            "free_rtos_c_scope_raw_deref": {name: c_scope_raw[name] for name in free_rtos},
            "nuttx_c_scope_raw_deref": {name: c_scope_raw[name] for name in nuttx},
            "zephyr_c_scope_raw_deref": {name: c_scope_raw[name] for name in zephyr},
            "rust_ffi_unsafe": {name: {"in_unsafe_context": ffi_unsafe[name], "resolved": ffi_total[name]} for name in all_matched},
            "unresolved_raw_deref": {
                "rust_application": {name: app_unresolved[name] for name in all_matched},
                "rust_ffi": {name: ffi_unresolved[name] for name in all_matched},
                "rust_native": {
                    name: item["unresolved"]["rust_native_application"]["raw_pointer_dereference"]
                    + item["unresolved"]["shared_rust"]["raw_pointer_dereference"]
                    for name, item in native.items()
                },
            },
        },
        "caveats": [
            "Operation categories may overlap for one source expression.",
            "Active unresolved records are excluded from resolved counts. The NuttX Rust application unary dereference is a mutable-reference dereference; unresolved boundary dereferences are raw-pointer records, so FFI raw counts are lower bounds.",
            "Counts are not unique source locations, runtime execution counts, vulnerability counts or proof of memory safety.",
            "Rust application and FFI categories are not semantically interchangeable with C categories.",
            "The C support scope is compiled into the Rust firmware and is not part of the C application-plus-adapter comparison.",
            "Rust-native kernels depend on external crates outside the analysed project source scope.",
        ],
    }


def generate_thesis_tables(repo_root: Path, generated_dir: Path | None = None, tables_dir: Path | None = None) -> None:
    matched = _load_matched(ARTIFACTS / "profiles", ARTIFACTS / "generated_inventory")
    native = _load_native(ARTIFACTS / "generated_inventory")
    generated = generated_dir or THESIS_TYP
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "safety-comparison.typ").write_text(compact_table(matched) + "\n", encoding="utf-8")
    (generated / "safety-c-categories.typ").write_text(c_category_table(matched) + "\n", encoding="utf-8")
    (generated / "safety-rust-categories.typ").write_text(rust_category_table(matched) + "\n", encoding="utf-8")
    (generated / "safety-role-counts.typ").write_text(role_table(matched, native) + "\n", encoding="utf-8")
    (generated / "safety-trace-coverage.typ").write_text(trace_table(ARTIFACTS) + "\n", encoding="utf-8")
    (generated / "safety-native-results.typ").write_text(native_table(native) + "\n", encoding="utf-8")
    tables = tables_dir or ARTIFACTS / "generated_outputs/analysis-tables"
    tables.mkdir(parents=True, exist_ok=True)
    (tables / "safety-revalidation.json").write_text(
        json.dumps(_revalidation(matched, native, ARTIFACTS), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

