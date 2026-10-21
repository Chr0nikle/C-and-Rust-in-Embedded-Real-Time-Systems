"""Stage 1 operation-inventory orchestration and deterministic evidence writing."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from common.io import sha256_file, write_csv, write_json
from common.config import discover
from common.extractor import build_extractor

from .c_ops import extract_c_scope
from common.layers import LayerClassifier
from .rust_ops import extract_rust_scope


INVENTORY_DIR = Path(__file__).resolve().parent
SAFETY_DIR = INVENTORY_DIR.parent
SPEC_PATH = SAFETY_DIR / "specs" / "counting-spec.json"
LAYERS_PATH = SAFETY_DIR / "specs" / "layers.json"
OPERATION_FIELDS = (
    "occurrence_id", "source_identity_key", "language", "configuration", "scope",
    "scope_label", "layer", "category", "ast_kind", "source_file", "source_line",
    "source_column", "function", "expression", "activity_status",
    "resolution_status", "provenance", "macro_status", "in_unsafe_block",
    "in_unsafe_function", "in_unsafe_context", "unsafe_context_ids",
    "unsafe_context_status", "details", "included_by",
)
FUNCTION_FIELDS = (
    "configuration", "language", "scope", "scope_label", "layer", "name", "file", "line",
    "column", "is_definition", "active", "unsafe", "operation_counts",
    "included_by",
)
KERNEL_FIELDS = (
    "configuration", "language", "source_file", "source_line", "source_column",
    "caller", "callee", "callee_kind", "resolution_status", "activity_status",
    "expression", "macro_status",
)
UNSAFE_FIELDS = (
    "configuration", "language", "context_id", "context_type", "source_file",
    "source_line", "source_column", "start_line", "start_column", "end_line",
    "end_column", "function", "layer", "activity_status",
)

SOURCE_COLUMN_CONVENTIONS = {
    "c": "1-based (libclang Cursor.location.column)",
    "rust": "0-based (proc_macro2 Span::start().column)",
    "note": "Macro spelling locations and macro-definition locations are distinct. For macro-body expansions, macro_definition_* identifies the spelling/definition location. For source-written casts in macro arguments, macro_argument_* identifies the argument and macro_definition_status records that it is not a definition.",
}

MACRO_COUNTING_UNIT = {
    "category": "macro_pointer_conversion",
    "counting_unit": "one reported macro-expansion pointer-cast occurrence per deduplicated invocation/extent key",
    "deduplication_key": "configuration, category, source file, source line, source column, AST kind, expression, function and expansion extent start",
    "nested_casts": "Multiple Clang CSTYLE_CAST_EXPR nodes that share the invocation/extent start collapse into one reported occurrence. The count is not a count of every expanded AST cast node.",
    "explicit_macro_arguments": "A source-written cast in a macro argument is reported as pointer_conversion with provenance explicit_macro_argument. Macro-body pointer casts at the same invocation are retained in that record's collapsed_macro_pointer_conversions detail instead of being added as a second occurrence.",
}


def _portable(value: Any, repo_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _portable(item, repo_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item, repo_root) for item in value]
    if isinstance(value, str):
        return value.replace(str(repo_root), ".")
    return value


def _dedup(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(json.dumps(row.get(field), sort_keys=True) for field in fields)
        if key not in out:
            out[key] = row
        else:
            out[key].setdefault("included_by", [])
            out[key]["included_by"] = sorted(set(out[key]["included_by"]) | set(row.get("included_by", [])))
    return list(out.values())


def _scope_key(configuration: str) -> str:
    if configuration.endswith("c-benchmark"):
        return "c-benchmark"
    if configuration.endswith("c-support-rust"):
        return "c-support"
    return "rust"


def _identity(row: dict[str, Any]) -> str:
    return row.get("source_identity_key") or ""


def _summary(operations: list[dict[str, Any]], unsafe_contexts: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    scopes = ["c-benchmark", "rust", "c-support"]
    result: dict[str, Any] = {
        "schema": "uastw-inventory-summary-v2",
        "counting_spec_version": spec["version"],
        "counting_unit": spec["counting_unit"],
        "scopes": {},
        "counting_units": {},
        "pointer_conversions": {},
        "unsafe_contexts": {},
        "unsafe_context_association": {},
        "kernel_calls": {},
        "notes": [
            "configuration_specific_operation_records counts rows; it is not a count of unique source occurrences.",
            "distinct_source_identities ignores build configuration and is the correct unique-source view.",
            "Category counts may overlap for the same source expression; no category sum is a distinct-operation total.",
            "Active counts include only activity_status=true and resolution_status=resolved.",
            "Inactive and unresolved records remain in operations.csv.",
            "Unsafe-context membership is syntactic placement, not a claim that the operation requires unsafe.",
        ],
    }
    keys_by_scope: dict[str, set[str]] = {}
    for scope in scopes:
        rows = [row for row in operations if row.get("scope") == scope]
        active = [row for row in rows if row.get("activity_status") == "true"]
        resolved = [row for row in active if row.get("resolution_status") == "resolved"]
        unresolved = [row for row in active if row.get("resolution_status") == "unresolved"]
        inactive = [row for row in rows if row.get("activity_status") == "false"]
        by_category = Counter(row["category"] for row in resolved)
        by_layer = Counter(row["layer"] for row in resolved)
        by_function = Counter(row["function"] or "<no-function>" for row in resolved)
        keys_by_scope[scope] = {_identity(row) for row in rows if _identity(row)}
        result["scopes"][scope] = {
            "configuration_specific_operation_records": len(rows),
            "distinct_source_identities": len(keys_by_scope[scope]),
            "active_resolved": len(resolved),
            "active_unresolved": len(unresolved),
            "inactive": len(inactive),
            "active_resolved_by_category": dict(sorted(by_category.items())),
            "active_resolved_by_layer": dict(sorted(by_layer.items())),
            "active_resolved_by_function": dict(sorted(by_function.items())),
            "active_resolved_by_provenance": dict(sorted(Counter(row.get("provenance", "unknown") for row in resolved).items())),
            "unresolved_by_category": dict(sorted(Counter(row["category"] for row in unresolved).items())),
            "unresolved_by_layer": dict(sorted(Counter(row["layer"] for row in unresolved).items())),
            "inactive_by_category": dict(sorted(Counter(row["category"] for row in inactive).items())),
            "inactive_by_layer": dict(sorted(Counter(row["layer"] for row in inactive).items())),
        }
    all_keys = set().union(*keys_by_scope.values()) if keys_by_scope else set()
    c_benchmark_keys = keys_by_scope.get("c-benchmark", set())
    c_support_keys = keys_by_scope.get("c-support", set())
    result["counting_units"] = {
        "configuration_specific_operation_records": len(operations),
        "distinct_source_identities_all_scopes": len(all_keys),
        "distinct_source_identities_by_scope": {scope: len(keys) for scope, keys in keys_by_scope.items()},
        "shared_source_identities_c_benchmark_and_c_support": len(c_benchmark_keys & c_support_keys),
        "c_benchmark_only_source_identities": len(c_benchmark_keys - c_support_keys),
        "c_support_only_source_identities": len(c_support_keys - c_benchmark_keys),
        "note": "Per-scope records are retained separately. The all-scope identity count removes configuration duplicates but does not merge overlapping operation categories.",
    }

    result["source_column_conventions"] = SOURCE_COLUMN_CONVENTIONS
    bench_rust_records = [
        row for row in operations
        if row.get("scope") == "c-benchmark" and (row.get("function") or "").startswith("bench_rust_")
    ]
    result["source_scope_limitations"] = {
        "c_benchmark_bench_rust_shims": {
            "operation_records": len(bench_rust_records),
            "rule": "scope=c-benchmark and function name starts with 'bench_rust_'",
            "note": "These functions are compiled into the RUST=0 build but are not demonstrated to be reachable from the C benchmark call path. Compilation is not runtime reachability; a syntactic call search is not a complete reachability analysis.",
        }
    }

    pointer_categories = (
        "pointer_conversion",
        "macro_pointer_conversion",
        "unresolved_pointer_conversion",
        "compiler_generated_pointer_conversion",
    )
    pointer_rows = [
        row for row in operations
        if row.get("language") == "c"
        and row.get("category") in pointer_categories
        and row.get("activity_status") == "true"
    ]
    explicit = [row for row in pointer_rows if row.get("category") == "pointer_conversion"]
    macro = [row for row in pointer_rows if row.get("category") == "macro_pointer_conversion"]
    unresolved = [row for row in pointer_rows if row.get("category") == "unresolved_pointer_conversion"]
    generated = [row for row in pointer_rows if row.get("category") == "compiler_generated_pointer_conversion"]
    result["pointer_conversions"] = {
        "explicit_source_records": len(explicit),
        "macro_expanded_records": len(macro),
        "unresolved_provenance_records": len(unresolved),
        "compiler_generated_records": len(generated),
        "explicit_source_distinct_source_identities": len({_identity(row) for row in explicit if _identity(row)}),
        "macro_expanded_distinct_source_identities": len({_identity(row) for row in macro if _identity(row)}),
        "unresolved_provenance_distinct_source_identities": len({_identity(row) for row in unresolved if _identity(row)}),
        "explicit_by_direction": dict(sorted(Counter(str((row.get("details") or {}).get("direction", "unknown")) for row in explicit).items())),
        "macro_by_provenance": dict(sorted(Counter(row.get("provenance", "unknown") for row in macro).items())),
        "macro_counting_unit": MACRO_COUNTING_UNIT,
        "by_scope": {
            scope: {
                "explicit_source_records": sum(1 for row in explicit if row.get("scope") == scope),
                "macro_expanded_records": sum(1 for row in macro if row.get("scope") == scope),
                "unresolved_provenance_records": sum(1 for row in unresolved if row.get("scope") == scope),
            }
            for scope in ("c-benchmark", "c-support")
        },
        "note": "pointer_conversion contains explicit project-source casts, including source-written casts in macro arguments. Macro-expanded casts are retained as macro_pointer_conversion. Unresolved provenance is visible as unresolved_pointer_conversion and is never counted as explicit. Pointer-to-integer casts with a non-pointer target are outside these categories.",
    }

    for config_id in sorted({row["configuration"] for row in operations}):
        calls = [row for row in operations if row["configuration"] == config_id and row.get("category") == "kernel_api_call"]
        by_kind = Counter((row.get("details") or {}).get("callee_kind", "unknown") for row in calls)
        active_calls = [row for row in calls if row.get("activity_status") == "true" and row.get("resolution_status") == "resolved"]
        result["kernel_calls"][config_id] = {
            "total": len(calls),
            "active_resolved": len(active_calls),
            "active_unresolved": sum(1 for row in calls if row.get("activity_status") == "true" and row.get("resolution_status") != "resolved"),
            "by_callee_kind": dict(sorted(by_kind.items())),
            "active_resolved_by_callee_kind": dict(sorted(Counter((row.get("details") or {}).get("callee_kind", "unknown") for row in active_calls).items())),
            "note": "Direct RTOS API, project wrapper/FFI and unresolved/indirect call kinds are separate; their sum is not a matched kernel-operation count.",
        }

    rust_active = [row for row in operations if row.get("language") == "rust" and row.get("activity_status") == "true"]
    in_block = [row for row in rust_active if row.get("in_unsafe_block") == "true"]
    in_function = [row for row in rust_active if row.get("in_unsafe_function") == "true"]
    in_both = [row for row in rust_active if row.get("in_unsafe_block") == "true" and row.get("in_unsafe_function") == "true"]
    in_context = [row for row in rust_active if row.get("in_unsafe_context") == "true"]
    outside = [row for row in rust_active if row.get("in_unsafe_context") == "false"]
    unknown = [row for row in rust_active if row.get("in_unsafe_context") not in ("true", "false")]
    result["unsafe_context_association"] = {
        "operations_considered": len(rust_active),
        "in_unsafe_block": len(in_block),
        "in_unsafe_function": len(in_function),
        "in_both_block_and_function": len(in_both),
        "in_unsafe_context_total": len(in_context),
        "outside_explicit_unsafe_context": len(outside),
        "unknown_context_resolution": len(unknown),
        "resolved_operations_in_unsafe_context": sum(1 for row in in_context if row.get("resolution_status") == "resolved"),
        "in_unsafe_context_by_layer": dict(sorted(Counter(row["layer"] for row in in_context).items())),
        "in_unsafe_context_by_category": dict(sorted(Counter(row["category"] for row in in_context).items())),
        "in_unsafe_context_by_layer_and_category": dict(sorted(Counter(f"{row['layer']}|{row['category']}" for row in in_context).items())),
        "note": "Lexical context membership is not equivalent to requiring unsafe, and a safe wrapper call does not imply that its implementation is sound.",
    }

    for context_type in sorted({row["context_type"] for row in unsafe_contexts}):
        selected = [row for row in unsafe_contexts if row["context_type"] == context_type]
        result["unsafe_contexts"][context_type] = {
            "total": len(selected),
            "active": sum(1 for row in selected if row.get("activity_status") == "true"),
            "inactive": sum(1 for row in selected if row.get("activity_status") == "false"),
            "by_layer": dict(sorted(Counter(row["layer"] for row in selected).items())),
            "by_function": dict(sorted(Counter(row["function"] or "<no-function>" for row in selected).items())),
        }
    return result


def _summaries_consistent(summary: dict[str, Any], operations: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    for scope, values in summary["scopes"].items():
        rows = [row for row in operations if row.get("scope") == scope]
        active = [row for row in rows if row.get("activity_status") == "true"]
        resolved = [row for row in active if row.get("resolution_status") == "resolved"]
        if values["configuration_specific_operation_records"] != len(rows):
            errors.append(f"{scope}: operation total mismatch")
        if values["distinct_source_identities"] != len({_identity(row) for row in rows if _identity(row)}):
            errors.append(f"{scope}: source identity total mismatch")
        if values["active_resolved"] != len(resolved):
            errors.append(f"{scope}: active resolved mismatch")
        if values["active_unresolved"] != sum(1 for row in active if row.get("resolution_status") == "unresolved"):
            errors.append(f"{scope}: unresolved mismatch")
        if values["active_resolved_by_category"] != dict(sorted(Counter(row["category"] for row in resolved).items())):
            errors.append(f"{scope}: category totals mismatch")
        if values["active_resolved_by_layer"] != dict(sorted(Counter(row["layer"] for row in resolved).items())):
            errors.append(f"{scope}: layer totals mismatch")
    units = summary["counting_units"]
    if units["configuration_specific_operation_records"] != len(operations):
        errors.append("counting units: configuration-specific total mismatch")
    if units["distinct_source_identities_all_scopes"] != len({_identity(row) for row in operations if _identity(row)}):
        errors.append("counting units: all-scope source identity total mismatch")
    pointer_rows = [
        row for row in operations
        if row.get("language") == "c"
        and row.get("category") in (
            "pointer_conversion", "macro_pointer_conversion",
            "unresolved_pointer_conversion", "compiler_generated_pointer_conversion",
        )
        and row.get("activity_status") == "true"
    ]
    pointer_expected = {
        "explicit_source_records": sum(1 for row in pointer_rows if row["category"] == "pointer_conversion"),
        "macro_expanded_records": sum(1 for row in pointer_rows if row["category"] == "macro_pointer_conversion"),
        "unresolved_provenance_records": sum(1 for row in pointer_rows if row["category"] == "unresolved_pointer_conversion"),
        "compiler_generated_records": sum(1 for row in pointer_rows if row["category"] == "compiler_generated_pointer_conversion"),
    }
    for field, value in pointer_expected.items():
        if summary["pointer_conversions"].get(field) != value:
            errors.append(f"pointer conversion summary mismatch: {field}")
    return errors


def _source_manifest(repo_root: Path, config: dict[str, Any], c_meta: dict[str, dict[str, Any]], rust_meta: dict[str, Any]) -> dict[str, Any]:
    configurations: dict[str, Any] = {}
    unique: dict[str, Any] = {}
    for config_id in ("freertos-stm32f4-c-benchmark", "freertos-stm32f4-c-support-rust"):
        c_config = config["configurations"][config_id]
        meta = c_meta[config_id]
        failed = {item["path"] for item in meta.get("parse_failures", [])}
        files = []
        for unit in c_config["translation_units"]:
            path = repo_root / unit["path"]
            entry = {
                "role": "translation_unit", "path": unit["path"],
                "sha256": sha256_file(path) if path.is_file() else None,
                "bytes": path.stat().st_size if path.is_file() else None,
                "parsed": unit["path"] not in failed,
                "has_bench_language_rust": unit.get("has_bench_language_rust"),
                "preprocessor_definitions": unit.get("preprocessor_definitions", []),
                "clang_args": [arg.replace(str(repo_root), ".") for arg in unit.get("clang_args", [])],
            }
            files.append(entry)
            unique.setdefault(unit["path"], {key: entry[key] for key in ("path", "sha256", "bytes")})
        configurations[config_id] = {
            "language": "c", "configuration": config_id,
            "make_argv": c_config["make_argv"], "make_command": c_config["make_command"],
            "target": c_config["target"], "files": files,
            "parse_failures": meta.get("parse_failures", []),
            "excluded_translation_units": c_config.get("excluded_translation_units", []),
        }
    rust_config = config["configurations"]["freertos-stm32f4-rust"]
    facts = rust_meta["facts"]
    failed = {item.get("file") for item in facts.get("parse_failures", [])}
    files = []
    for path in rust_config["files"]:
        source = repo_root / path
        entry = {"role": "rust_source", "path": path, "sha256": sha256_file(source) if source.is_file() else None,
                 "bytes": source.stat().st_size if source.is_file() else None, "parsed": path not in failed}
        files.append(entry)
        unique.setdefault(path, {key: entry[key] for key in ("path", "sha256", "bytes")})
    configurations["freertos-stm32f4-rust"] = {
        "language": "rust", "configuration": "freertos-stm32f4-rust",
        "target": rust_config["target"], "crate_roots": rust_config["crate_roots"],
        "files": files, "parse_failures": facts.get("parse_failures", []),
    }
    return {
        "schema": "uastw-inventory-sources-v1",
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "worktree_status_count": config["worktree_status_count"],
        "target": {"c": "arm-none-eabi / Cortex-M4F", "rust": rust_config["target"]},
        "configurations": configurations,
        "unique_source_paths": sorted(unique.values(), key=lambda item: item["path"]),
        "excluded": {
            "c": "FreeRTOS-Kernel, HAL, CMSIS, startup and system sources",
            "rust": rust_config.get("excluded_scopes", []),
        },
    }


def run_inventory(repo_root: Path, output: Path) -> dict[str, Any]:
    if output.resolve() in (repo_root.resolve(), INVENTORY_DIR.resolve()):
        raise SystemExit("refusing to delete repository root or inventory tooling directory")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    config = discover(repo_root)
    classifier = LayerClassifier(LAYERS_PATH)
    extractor = build_extractor(repo_root)

    c_bench_ops, c_bench_fns, c_bench_calls, c_bench_meta = extract_c_scope(repo_root, config["configurations"]["freertos-stm32f4-c-benchmark"])
    c_support_ops, c_support_fns, c_support_calls, c_support_meta = extract_c_scope(repo_root, config["configurations"]["freertos-stm32f4-c-support-rust"])
    rust_ops, rust_fns, rust_calls, rust_unsafe, rust_meta = extract_rust_scope(repo_root, config["configurations"]["freertos-stm32f4-rust"], extractor, classifier)

    operations = [*c_bench_ops, *rust_ops, *c_support_ops]
    functions = _dedup([*c_bench_fns, *c_support_fns, *rust_fns], ("configuration", "language", "name", "file", "line", "column", "is_definition"))
    kernel_calls = _dedup([*c_bench_calls, *c_support_calls, *rust_calls], ("configuration", "language", "source_file", "source_line", "source_column", "caller", "callee", "callee_kind", "activity_status"))
    unsafe_contexts = _dedup(rust_unsafe, ("configuration", "language", "context_id", "context_type", "source_file", "source_line", "source_column", "start_line", "start_column", "end_line", "end_column", "function", "activity_status"))

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
    for row in kernel_calls:
        row["layer"] = classifier.classify(_scope_key(row["configuration"]), row.get("source_file"))
        row["macro_status"] = "source_call_or_ast_node"
    for row in unsafe_contexts:
        row["layer"] = classifier.classify("rust", row.get("source_file"))
    for row in kernel_calls:
        if "activity_status" not in row:
            row["activity_status"] = "active"

    operations.sort(key=lambda row: (row["configuration"], row["layer"], row["source_file"] or "", row["source_line"], row["source_column"] or 0, row["category"], row["expression"]))
    functions.sort(key=lambda row: (row["configuration"], row["layer"], row["file"] or "", row["line"], row["name"]))
    kernel_calls.sort(key=lambda row: (row["configuration"], row["layer"], row["source_file"] or "", row["source_line"], row["callee"]))
    unsafe_contexts.sort(key=lambda row: (row["configuration"], row["source_file"] or "", row["source_line"], row["context_type"], row["function"] or ""))

    # Function-level counts are recomputed from operation records.
    op_counts: dict[tuple[str, str, str, str], Counter[str]] = defaultdict(Counter)
    for row in operations:
        if row.get("resolution_status") == "resolved":
            op_counts[(row["configuration"], row["language"], row["function"] or "<no-function>", row["source_file"] or "")][row["category"]] += 1
    for row in functions:
        counts = op_counts.get((row["configuration"], row["language"], row["name"], row["file"] or ""), Counter())
        row["operation_counts"] = dict(sorted(counts.items()))

    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    summary = _summary(operations, unsafe_contexts, spec)
    c_meta = {"freertos-stm32f4-c-benchmark": c_bench_meta, "freertos-stm32f4-c-support-rust": c_support_meta}
    sources = _source_manifest(repo_root, config, c_meta, rust_meta)
    unsupported = [
        {"language": "rust", "category": "union_field_access", "status": "unsupported", "reason": "Rust union field access resolution is not implemented in the syntax-only model", "discovered_unions": len(rust_meta["facts"].get("unions", []))},
        {"language": "c", "category": "unsafe_constructs", "status": "not_applicable", "reason": "unsafe is a Rust language construct; C has no equivalent category"},
        {"language": "c", "category": "implicit_pointer_conversion", "status": "not_counted", "reason": "Implicit compiler conversions have no CSTYLE_CAST_EXPR source node and are outside pointer_conversion"},
        {"language": "c", "category": "pointer_to_integer_conversion", "status": "not_counted", "reason": "pointer_conversion requires a pointer target; explicit pointer-to-integer casts are documented as outside the category"},
        {"language": "c", "category": "macro_pointer_conversion", "status": "descriptive_only", "reason": "Macro-expanded casts are retained separately and are not included in pointer_conversion"},
        {"language": "c", "category": "third_party_macro_internals", "status": "excluded", "reason": "Operations whose spelling location is a third-party macro definition are excluded from project counts; the invocation provenance is retained on descriptive records"},
        {"language": "rust", "category": "macro_not_expanded", "status": "unresolved", "reason": "Unless a narrowly specified addr_of!/addr_of_mut! form is modelled, macro contents remain unresolved rather than being treated as zero operations"},
    ]
    def coverage_scope(config_id: str, files_discovered: int, files_analysed: int, parse_failures: int) -> dict[str, Any]:
        rows = [row for row in operations if row["configuration"] == config_id]
        return {
            "files_discovered": files_discovered,
            "files_analysed": files_analysed,
            "parse_failures": parse_failures,
            "configuration_specific_operation_records": len(rows),
            "distinct_source_identities": len({_identity(row) for row in rows if _identity(row)}),
            "active_operations": sum(1 for row in rows if row["activity_status"] == "true"),
            "inactive_operations": sum(1 for row in rows if row["activity_status"] == "false"),
            "unresolved": sum(1 for row in rows if row["resolution_status"] != "resolved"),
        }

    bench_rust_functions = [
        row for row in functions
        if row["configuration"] == "freertos-stm32f4-c-benchmark"
        and (row.get("name") or "").startswith("bench_rust_")
    ]
    scope_limitations = {
        key: dict(value) for key, value in summary["source_scope_limitations"].items()
    }
    scope_limitations["c_benchmark_bench_rust_shims"]["function_count"] = len(bench_rust_functions)
    coverage = {
        "schema": "uastw-inventory-coverage-v2",
        "counting_spec_version": spec["version"],
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "configurations": {
            "freertos-stm32f4-c-benchmark": coverage_scope(
                "freertos-stm32f4-c-benchmark",
                len(config["configurations"]["freertos-stm32f4-c-benchmark"]["translation_units"]),
                len(c_bench_meta["files"]),
                len(c_bench_meta["parse_failures"]),
            ),
            "freertos-stm32f4-rust": coverage_scope(
                "freertos-stm32f4-rust",
                len(config["configurations"]["freertos-stm32f4-rust"]["files"]),
                len(rust_meta["facts"].get("files", [])),
                len(rust_meta["facts"].get("parse_failures", [])),
            ),
            "freertos-stm32f4-c-support-rust": coverage_scope(
                "freertos-stm32f4-c-support-rust",
                len(config["configurations"]["freertos-stm32f4-c-support-rust"]["translation_units"]),
                len(c_support_meta["files"]),
                len(c_support_meta["parse_failures"]),
            ),
        },
        "skipped_files": [],
        "kernel_call_resolution": { "|".join(key): value for key, value in sorted(Counter((row["configuration"], row["callee_kind"], row["resolution_status"]) for row in kernel_calls).items()) },
        "unsupported_categories": unsupported,
        "out_of_line_modules": rust_meta.get("out_of_line_modules", []),
        "source_column_conventions": SOURCE_COLUMN_CONVENTIONS,
        "macro_counting_unit": MACRO_COUNTING_UNIT,
        "known_source_scope_limitations": scope_limitations,
        "tool_versions": config["tools"],
        "target": {"c": "arm-none-eabi / Cortex-M4F", "rust": config["configurations"]["freertos-stm32f4-rust"]["target"]},
        "compiler_flags": {
            "c_benchmark": [[arg.replace(str(repo_root), ".") for arg in unit["clang_args"]] for unit in config["configurations"]["freertos-stm32f4-c-benchmark"]["translation_units"]],
            "c_support": [[arg.replace(str(repo_root), ".") for arg in unit["clang_args"]] for unit in config["configurations"]["freertos-stm32f4-c-support-rust"]["translation_units"]],
        },
        "limitations": [
            "Rust pointer type resolution is syntax-based and conservative; direct project-function return types are resolved only when unambiguous and active.",
            "Rust macros are not expanded; only direct addr_of!/addr_of_mut! forms for a static-mut identifier are modelled. Other macro expressions, including statement macros, remain unresolved.",
            "Inline Rust modules are traversed with parent and child cfg status. Out-of-line module declarations are reported; they are resolved only when their file is in the configured file set.",
            "C macro-expanded casts are retained as macro_pointer_conversion and are excluded from pointer_conversion. Explicit casts written in macro arguments are pointer_conversion with explicit_macro_argument provenance.",
            "Macro-derived counts are deduplicated macro-expansion cast sites, not every Clang AST cast node; nested casts sharing an extent start collapse.",
            "C postfix pointer increments are included; compound expressions can legitimately yield more than one operation record.",
            "C mutable-global access distinguishes direct object writes, array-element writes, pointer-base reads and address-taking for supported patterns; no alias analysis is performed.",
            "Configuration-specific records and distinct source identities are separate measurements.",
            "Categories may overlap; category counts are not a distinct-operation total.",
            "Rust unsafe-context membership is syntactic placement and is not equivalent to an operation requiring unsafe.",
        ],
    }
    configurations = {
        "schema": "uastw-inventory-configurations-v1",
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "counting_spec": _portable(spec, repo_root),
        "layers": classifier.describe(),
        "configurations": _portable(config["configurations"], repo_root),
    }
    errors = _summaries_consistent(summary, operations)
    occurrence_ids = [row["occurrence_id"] for row in operations]
    if len(occurrence_ids) != len(set(occurrence_ids)):
        errors.append("duplicate occurrence ids")
    for row in operations:
        if not row.get("source_identity_key"):
            errors.append(f"missing source identity: {row.get('occurrence_id')}")
        if not row.get("source_file") or not row.get("source_line"):
            errors.append(f"missing source location: {row.get('occurrence_id')}")
        if row.get("source_file", "").startswith(".local/"):
            errors.append(f"third-party operation retained: {row.get('occurrence_id')}")
        if row.get("language") == "rust" and row.get("in_unsafe_context") not in ("true", "false", "unknown"):
            errors.append(f"invalid unsafe context marker: {row.get('occurrence_id')}")
    for row in functions:
        key = (row["configuration"], row["language"], row["name"], row["file"] or "")
        recomputed = dict(sorted(op_counts.get(key, Counter()).items()))
        if row.get("operation_counts") != recomputed:
            errors.append(f"function counts mismatch: {row['configuration']}/{row['name']}/{row['file']}")
    for scope, values in summary["scopes"].items():
        if values["active_resolved"] + values["active_unresolved"] + values["inactive"] != values["configuration_specific_operation_records"]:
            errors.append(f"{scope}: activity totals do not partition operations")
    if summary["unsafe_context_association"]["in_unsafe_context_total"] > summary["unsafe_context_association"]["operations_considered"]:
        errors.append("unsafe-context association exceeds considered Rust operations")
    if errors:
        raise SystemExit("inventory validation failed:\n" + "\n".join(errors[:20]))
    validation = {
        "schema": "uastw-inventory-validation-v2",
        "status": "passed",
        "configuration_specific_operation_records": len(operations),
        "distinct_source_identities_all_scopes": summary["counting_units"]["distinct_source_identities_all_scopes"],
        "occurrence_ids_unique": True,
        "source_identity_keys_present": True,
        "summary_recomputed_from_operations": True,
        "no_third_party_operations": True,
        "unsafe_context_association_recomputed": True,
        "unsupported_categories": unsupported,
    }

    write_csv(output / "operations.csv", OPERATION_FIELDS, operations)
    write_csv(output / "functions.csv", FUNCTION_FIELDS, functions)
    write_csv(output / "kernel-calls.csv", KERNEL_FIELDS, kernel_calls)
    write_csv(output / "unsafe-contexts.csv", UNSAFE_FIELDS, unsafe_contexts)
    write_json(output / "sources.json", sources)
    write_json(output / "configurations.json", configurations)
    write_json(output / "coverage.json", coverage)
    write_json(output / "summary.json", summary)
    write_json(output / "counting-spec.json", spec)
    write_json(output / "validation.json", validation)
    files = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        files.append({"path": str(path.relative_to(output)), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    write_json(output / "manifest.json", {
        "schema": "uastw-inventory-manifest-v2",
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "counting_spec_version": spec["version"],
        "configurations": list(config["configurations"]),
        "tools": config["tools"],
        "configuration_specific_operation_records": len(operations),
        "distinct_source_identities_all_scopes": summary["counting_units"]["distinct_source_identities_all_scopes"],
        "counting_unit_note": "The first count is the sum of per-configuration records; the second removes configuration duplicates and is not a category-union total.",
        "source_column_conventions": SOURCE_COLUMN_CONVENTIONS,
        "macro_counting_unit": MACRO_COUNTING_UNIT,
        "known_source_scope_limitations": scope_limitations,
        "out_of_line_modules": coverage["out_of_line_modules"],
        "quick_checks": {
            "c_benchmark_files": len(c_bench_meta["files"]),
            "c_support_files": len(c_support_meta["files"]),
            "rust_files": len(rust_meta["facts"].get("files", [])),
            "parse_failures": len(c_bench_meta["parse_failures"]) + len(c_support_meta["parse_failures"]) + len(rust_meta["facts"].get("parse_failures", [])),
            "distinct_source_identities_by_scope": summary["counting_units"]["distinct_source_identities_by_scope"],
        },
        "outputs": files,
        "notes": ["Canonical outputs have sorted keys/rows; timestamps are omitted."],
    })
    return {
        "output": str(output),
        "configuration_specific_operation_records": len(operations),
        "distinct_source_identities_all_scopes": summary["counting_units"]["distinct_source_identities_all_scopes"],
        "functions": len(functions),
        "kernel_calls": len(kernel_calls),
        "unsafe_contexts": len(unsafe_contexts),
        "summary": summary,
        "coverage": coverage,
    }
