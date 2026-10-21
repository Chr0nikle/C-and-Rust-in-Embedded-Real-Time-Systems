"""Configuration-specific Stage 1 operation inventory for matched and native profiles."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from inventory.c_ops import extract_c_scope
from common.layers import LayerClassifier
from inventory.pipeline import (
    FUNCTION_FIELDS, KERNEL_FIELDS, OPERATION_FIELDS, SPEC_PATH, UNSAFE_FIELDS,
    _dedup, _scope_key, _summary,
)
from inventory.rust_ops import extract_rust_scope
from common.io import sha256_file, write_csv, write_json
from common.config import resolve_repo_root
from common.paths import ARTIFACTS, SAFETY_DIR
from profiles.definitions import discover_profile_configs
from matched.r_extract import build_rust_index, reachable_rust_functions
from profiles.generator import BOARD_INFO, layout_for

NATIVE = {
    "freertos-in-rust-stm32f4": {
        "crate": "firmware/rust-kernels/freertos-in-rust/rust",
        "target": "thumbv7em-none-eabihf",
        "features": ["f4"],
        "description": "FreeRTOS-in-Rust, STM32F4",
    },
    "freertos-in-rust-stm32h723": {
        "crate": "firmware/rust-kernels/freertos-in-rust/rust",
        "target": "thumbv7em-none-eabihf",
        "features": ["stm32h723"],
        "description": "FreeRTOS-in-Rust, STM32H723",
    },
    "rivet-stm32f4": {
        "crate": "firmware/rust-kernels/rivet/rust/stm32f4",
        "target": "thumbv7em-none-eabi",
        "description": "Rivet, STM32F4",
    },
}


def _normalise(operations: list[dict[str, Any]], functions: list[dict[str, Any]],
               kernel_calls: list[dict[str, Any]], unsafe_contexts: list[dict[str, Any]],
               classifier: LayerClassifier) -> None:
    for row in operations:
        if row.get("language") == "c":
            row.setdefault("provenance", "explicit_source")
            row.setdefault("in_unsafe_block", "not_applicable")
            row.setdefault("in_unsafe_function", "not_applicable")
            row.setdefault("in_unsafe_context", "not_applicable")
            row.setdefault("unsafe_context_ids", [])
            row.setdefault("unsafe_context_status", "not_applicable")
        row.setdefault("provenance", "unknown")
        row.setdefault("macro_status", row.get("provenance", "unknown"))
        row.setdefault("in_unsafe_block", "unknown")
        row.setdefault("in_unsafe_function", "unknown")
        row.setdefault("in_unsafe_context", "unknown")
        row.setdefault("unsafe_context_ids", [])
        row.setdefault("unsafe_context_status", "unknown")
        row["layer"] = classifier.classify(_scope_key(row["configuration"]), row.get("source_file"))
        row["scope"] = _scope_key(row["configuration"])
        row["scope_label"] = {"c-benchmark": "C benchmark", "rust": "Rust benchmark", "c-support": "C support linked with Rust"}[row["scope"]]
    for row in functions:
        row["layer"] = classifier.classify(_scope_key(row["configuration"]), row.get("file"))
        row["scope"] = _scope_key(row["configuration"])
        row["scope_label"] = {"c-benchmark": "C benchmark", "rust": "Rust benchmark", "c-support": "C support linked with Rust"}[row["scope"]]
    counts: dict[tuple[str, str, str, str], Counter[str]] = defaultdict(Counter)
    for row in operations:
        if row.get("resolution_status") == "resolved":
            counts[(row["configuration"], row["language"], row["function"] or "<no-function>", row["source_file"] or "")][row["category"]] += 1
    for row in functions:
        row["operation_counts"] = dict(sorted(counts.get((row["configuration"], row["language"], row["name"], row["file"] or ""), Counter()).items()))
    for row in kernel_calls:
        row["layer"] = classifier.classify(_scope_key(row["configuration"]), row.get("source_file"))
        row["macro_status"] = "source_call_or_ast_node"
        row.setdefault("activity_status", "active")
    for row in unsafe_contexts:
        row["layer"] = classifier.classify("rust", row.get("source_file"))


def _run(repo: Path, output: Path, id: str, description: str,
         c_configs: list[dict[str, Any]], rust_config: dict[str, Any]) -> dict[str, Any]:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    classifier = LayerClassifier(SAFETY_DIR / "specs/layers.json")
    extractor = Path(__file__).resolve().parent.parent / "rust_extractor/target/release/uastw-rust-extract"
    operations: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    kernel_calls: list[dict[str, Any]] = []
    unsafe_contexts: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    for config in c_configs:
        ops, fns, calls, meta = extract_c_scope(repo, config)
        operations.extend(ops); functions.extend(fns); kernel_calls.extend(calls)
        metadata[config["configuration"]] = meta
    if rust_config:
        ops, fns, calls, unsafe, meta = extract_rust_scope(repo, rust_config, extractor, classifier)
        operations.extend(ops); functions.extend(fns); kernel_calls.extend(calls); unsafe_contexts.extend(unsafe)
        metadata[rust_config["configuration"]] = meta
    _normalise(operations, functions, kernel_calls, unsafe_contexts, classifier)
    operations.sort(key=lambda row: (row["configuration"], row["layer"], row["source_file"] or "", row["source_line"], row["source_column"] or 0, row["category"], row["expression"]))
    functions = _dedup(functions, ("configuration", "language", "name", "file", "line", "column", "is_definition"))
    functions.sort(key=lambda row: (row["configuration"], row["layer"], row["file"] or "", row["line"], row["name"]))
    kernel_calls = _dedup(kernel_calls, ("configuration", "language", "source_file", "source_line", "source_column", "caller", "callee", "callee_kind", "activity_status"))
    kernel_calls.sort(key=lambda row: (row["configuration"], row["layer"], row["source_file"] or "", row["source_line"], row["callee"]))
    unsafe_contexts = _dedup(unsafe_contexts, ("configuration", "language", "context_id", "context_type", "source_file", "source_line", "source_column", "start_line", "start_column", "end_line", "end_column", "function", "activity_status"))
    spec = json.loads(SPEC_PATH.read_text())
    summary = _summary(operations, unsafe_contexts, spec)
    write_csv(output / "operations.csv", OPERATION_FIELDS, operations)
    write_csv(output / "functions.csv", FUNCTION_FIELDS, functions)
    write_csv(output / "kernel-calls.csv", KERNEL_FIELDS, kernel_calls)
    write_csv(output / "unsafe-contexts.csv", UNSAFE_FIELDS, unsafe_contexts)
    write_json(output / "summary.json", summary)
    configurations = sorted({row["configuration"] for row in operations})
    coverage = {
        "schema": "uastw-profile-inventory-coverage-v1",
        "profile": id,
        "description": description,
        "configuration_count": len(configurations),
        "configurations": configurations,
        "operation_rows": len(operations),
        "function_rows": len(functions),
        "kernel_call_rows": len(kernel_calls),
        "unsafe_context_rows": len(unsafe_contexts),
        "parse_failures": {key: len(meta.get("parse_failures", [])) for key, meta in metadata.items()},
        "rust_features": (rust_config or {}).get("features", []),
    }
    write_json(output / "coverage.json", coverage)
    files = sorted({row["source_file"] for row in operations if row.get("source_file")})
    sources = {
        "schema": "uastw-profile-inventory-sources-v1",
        "profile": id,
        "description": description,
        "source_revision": __import__("subprocess").run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip(),
        "configurations": configurations,
        "rust_features": (rust_config or {}).get("features", []),
        "files": [{"path": path, "sha256": sha256_file(repo / path)} for path in files if (repo / path).is_file()],
    }
    write_json(output / "sources.json", sources)
    return coverage


def run_matched(repo: Path, profile_path: Path, output: Path) -> dict[str, Any]:
    profile = json.loads(profile_path.read_text())
    configurations = discover_profile_configs(repo, profile)["configurations"]
    c_configs = [configurations[profile["source_configs"][key]["configuration"]] for key in ("c_benchmark", "c_support")]
    rust_config = configurations[profile["source_configs"]["rust"]["configuration"]]
    return _run(repo, output, profile["id"], profile.get("description", ""), c_configs, rust_config)


def run_native(repo: Path, native: str, output: Path) -> dict[str, Any]:
    spec = NATIVE[native]
    crate = repo / spec["crate"]
    files = [str(path.relative_to(repo)) for path in sorted((crate / "src").glob("*.rs"))]
    rust_config = {
        "configuration": native,
        "files": files,
        "target": {"target": spec["target"]},
        "features": spec.get("features", []),
        "crate_roots": [str((crate / "Cargo.toml").relative_to(repo))],
    }
    result = _run(repo, output, f"native-{native}", spec["description"], [], rust_config)
    _native_trace(repo, native, rust_config, output)
    return result


def _native_match_arms(text: str, function_name: str) -> dict[int, tuple[int, int, list[str]]]:
    match = re.search(rf"\bfn\s+{re.escape(function_name)}\s*\([^)]*\)[^{{]*\{{", text)
    if not match:
        return {}
    start = match.end() - 1
    depth = 0
    end = start
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    body = text[start:end + 1]
    arms: dict[int, tuple[int, int, list[str]]] = {}
    matches = list(re.finditer(r"(?m)^\s*(\d+)\s*=>\s*", body))
    for position, arm in enumerate(matches):
        number = int(arm.group(1))
        arm_start = arm.end()
        arm_end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        expression = body[arm_start:arm_end]
        callees = re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", expression)
        callees = [name for name in dict.fromkeys(callees) if name not in {"if", "match", "while", "for", "return", "unwrap", "expect"}]
        arms[number] = (body[:arm.start()].count("\n") + 1, body[:arm_end].count("\n") + 1, callees)
    return arms


def _native_trace(repo: Path, native: str, rust_config: dict[str, Any], output: Path) -> None:
    classifier = LayerClassifier(SAFETY_DIR / "specs/layers.json")
    extractor = Path(__file__).resolve().parent.parent / "rust_extractor/target/release/uastw-rust-extract"
    config = dict(rust_config)
    config["configuration"] = native
    index = build_rust_index(repo, config, extractor, classifier)
    # Protocol IDs -> thesis case numbers from the shared case registry.
    cases = json.loads((SAFETY_DIR / "specs/cases.json").read_text())["cases"]
    wire_to_case = {int(case["c_wire_id"]): int(case["thesis_case"]) for case in cases}
    measure_names = [name for name in ("measure_u32", "measure", "measure_case", "rust_bench_run_case") if name in index["by_simple"]]
    measure_name = None
    source_text = ""
    source_file = None
    arms: dict[int, tuple[int, int, list[str]]] = {}
    for candidate_name in measure_names:
        for function in index["by_simple"][candidate_name]:
            candidate_file = function.get("file")
            if not candidate_file or not (repo / candidate_file).is_file():
                continue
            candidate_text = (repo / candidate_file).read_text()
            candidate_arms = _native_match_arms(candidate_text, candidate_name)
            if candidate_arms:
                measure_name, source_file, source_text, arms = candidate_name, candidate_file, candidate_text, candidate_arms
                break
        if arms:
            break
    method_map: dict[str, tuple[str, dict[str, Any]]] = {}
    for (impl_type, simple), functions in index.get("by_impl", {}).items():
        if len(functions) == 1:
            method_map.setdefault(simple, (impl_type, functions[0]))
    free_map = {name: functions[0] for name, functions in index.get("by_simple", {}).items() if len(functions) == 1}
    for source in rust_config.get("files", []):
        source_path = repo / source
        if not source_path.is_file():
            continue
        source_content = source_path.read_text()
        for match in re.finditer(r"\bfn\s+(measure_case(\d+))\s*\(", source_content):
            wire = int(match.group(2))
            if wire in arms or wire_to_case.get(wire) is None:
                continue
            arms[wire] = (0, 0, [match.group(1)])
    case_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for wire, (start_line, end_line, callees) in sorted(arms.items()):
        thesis_case = wire_to_case.get(wire)
        if thesis_case is None:
            continue
        direct = []
        if measure_name and source_file:
            for function in index["by_simple"].get(measure_name, []):
                if function.get("file") != source_file:
                    continue
                for call in function.get("calls", []):
                    if start_line <= int(call.get("line") or 0) <= end_line:
                        direct.append(call)
        roots = [name for name in callees if name in free_map and name != measure_name]
        edges: list[dict[str, Any]] = []
        visited_names: set[str] = set()
        queue = list(roots)
        while queue:
            name = queue.pop(0)
            if name in visited_names:
                continue
            visited_names.add(name)
            if name in free_map:
                new_edges, _ = reachable_rust_functions(index, [name])
                edges.extend(new_edges)
            elif name in method_map:
                impl_type, _function = method_map[name]
                new_edges, _ = reachable_rust_edges(index, impl_type, [name])
                edges.extend(new_edges)
            for edge in edges:
                target = edge.get("target_name")
                if (target in free_map or target in method_map) and target not in visited_names:
                    queue.append(target)
        rows = []
        for call in direct:
            rows.append({
                "configuration": native, "thesis_case": thesis_case, "wire_case": wire,
                "root": measure_name, "caller": measure_name, "source_file": source_file,
                "source_line": call.get("line"), "source_column": call.get("column"),
                "source_name": call.get("callee"), "target_kind": call.get("target_kind", "unresolved"),
                "target_name": call.get("callee"), "resolution_status": call.get("resolution_status", "resolved"),
            })
        for edge in edges:
            rows.append({
                "configuration": native, "thesis_case": thesis_case, "wire_case": wire,
                "root": measure_name, "caller": edge.get("caller"), "source_file": edge.get("source_file"),
                "source_line": edge.get("source_line"), "source_column": edge.get("source_column"),
                "source_name": edge.get("source_name"), "target_kind": edge.get("target_kind"),
                "target_name": edge.get("target_name"), "resolution_status": edge.get("resolution_status", "resolved"),
            })
        seen = set()
        unique = []
        for row in rows:
            key = (row["source_file"], row["source_line"], row["source_column"], row["target_name"], row["target_kind"])
            if key not in seen:
                seen.add(key); unique.append(row)
        case_rows.extend(unique)
        boundary_targets = sorted({
            row["target_name"] for row in unique
            if str(row["resolution_status"]).startswith("unresolved")
            and row["target_name"] not in free_map and row["target_name"] not in method_map
        })
        summaries.append({
            "thesis_case": thesis_case, "wire_case": wire, "roots": roots,
            "edge_rows": len(unique), "boundary_edges": len(boundary_targets),
            "boundary_targets": boundary_targets,
            "status": "complete_with_external_boundary" if boundary_targets else "complete",
        })
    fields = ("configuration", "thesis_case", "wire_case", "root", "caller", "source_file",
              "source_line", "source_column", "source_name", "target_kind", "target_name", "resolution_status")
    case_rows.sort(key=lambda row: (row["thesis_case"], row["source_file"] or "", row["source_line"] or 0, row["source_column"] or 0))
    write_csv(output / "native-case-trace.csv", fields, case_rows)
    write_json(output / "native-case-trace-summary.json", {
        "schema": "uastw-native-case-trace-v1",
        "configuration": native,
        "analysis_scope": "rust_only_unmatched",
        "measure_function": measure_name,
        "case_count": len(summaries),
        "cases": summaries,
        "note": "Per-case source trace only. No C counterpart or matched A/B/C classification is claimed.",
    })
    # Keep the generic main trace as a separate provenance view.
    roots = [name for name in ("main", "rust_bench_run_case") if name in index["by_simple"]]
    edges, unresolved = reachable_rust_functions(index, roots)
    rows = []
    for edge in edges:
        rows.append({
            "configuration": native, "language": "rust", "analysis_scope": "rust_only_unmatched",
            "caller": edge.get("caller"), "source_file": edge.get("source_file"),
            "source_line": edge.get("source_line"), "source_column": edge.get("source_column"),
            "source_name": edge.get("source_name"), "target_kind": edge.get("target_kind"),
            "target_name": edge.get("target_name"), "resolution_status": edge.get("resolution_status", "resolved"),
            "activity_status": edge.get("activity_status", "active"),
        })
    rows.sort(key=lambda row: (row["source_file"] or "", row["source_line"] or 0, row["source_column"] or 0, row["target_name"] or ""))
    write_csv(output / "rust-trace.csv", tuple(rows[0].keys()) if rows else ("configuration", "language", "analysis_scope", "caller", "source_file", "source_line", "source_column", "source_name", "target_kind", "target_name", "resolution_status", "activity_status"), rows)
    write_json(output / "rust-trace-summary.json", {
        "schema": "uastw-native-rust-trace-v1",
        "configuration": native,
        "analysis_scope": "rust_only_unmatched",
        "roots": roots,
        "edge_rows": len(rows),
        "unresolved_roots": unresolved,
        "note": "No matched C/Rust Level A/B/C comparison is claimed for Rust-native kernels.",
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root")
    parser.add_argument("--profile", action="append", help="matched profile path or name (repeatable)")
    parser.add_argument("--native", action="append", choices=sorted(NATIVE), help="native configuration (repeatable)")
    parser.add_argument("--output", default=str(ARTIFACTS / "generated_inventory"))
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve() if args.repo_root else resolve_repo_root(Path(__file__).resolve().parent)
    out = Path(args.output)
    if args.profile:
        targets = args.profile
    elif args.native:
        targets = []
    else:
        targets = [str(ARTIFACTS / "profiles" / f"{rtos}-{info['short']}-all.json") for rtos in ("freertos", "nuttx", "zephyr") for info in BOARD_INFO.values()]
    for value in targets:
        path = Path(value)
        if not path.is_absolute():
            candidate = ARTIFACTS / "profiles" / f"{value}-all.json" if not value.endswith(".json") else Path(__file__).resolve().parent / value
            path = candidate if candidate.is_file() else path
        profile = json.loads(path.read_text())
        result = run_matched(repo, path, out / profile["id"])
        print(f"{profile['id']}: {result['operation_rows']} operations")
    for native in args.native or []:
        result = run_native(repo, native, out / f"native-{native}")
        print(f"native-{native}: {result['operation_rows']} operations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
