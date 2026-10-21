"""Deterministic, scope-separated structural outputs and coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from common.io import sha256_file, summarize, write_csv, write_json

STRUCTURAL_FIELDS = (
    "configuration", "language", "kind", "name", "file", "line", "column",
    "function", "type", "size", "elements", "call_sites", "active", "cfg",
    "origin", "included_by", "details",
)
FFI_FIELDS = (
    "configuration", "language", "interface", "file", "line", "call_sites",
    "active_call_sites", "inactive_call_sites", "active", "pointer_bearing",
    "c_configuration", "has_c_declaration", "c_file", "c_line",
    "abi_check_status", "check_id", "details",
)
DIAGNOSTIC_FIELDS = (
    "configuration", "tool", "tool_version", "severity", "rule_id", "file",
    "line", "column", "message", "scope", "run_status",
)


def _row(
    *,
    configuration: str,
    language: str,
    kind: str,
    name: str,
    item: dict[str, Any],
    type_text: str | None = None,
    size: int | None = None,
    elements: int | None = None,
    call_sites: int | None = None,
    active: str = "true",
    cfg: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "configuration": configuration,
        "language": language,
        "kind": kind,
        "name": name,
        "file": item.get("file"),
        "line": item.get("line"),
        "column": item.get("column"),
        "function": item.get("function"),
        "type": type_text,
        "size": size,
        "elements": elements,
        "call_sites": call_sites,
        "active": active,
        "cfg": cfg or [],
        "origin": "project",
        "included_by": item.get("included_by", []),
        "details": details or {},
    }


def structural_rows(
    c_benchmark: dict[str, Any],
    c_support: dict[str, Any],
    rust_facts: dict[str, Any],
    *,
    c_benchmark_configuration: str,
    c_support_configuration: str,
    rust_configuration: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    def c_rows(c_facts: dict[str, Any], configuration: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for function in c_facts.get("functions", []):
            rows.append(_row(
                configuration=configuration, language="c",
                kind="function_definition" if function.get("is_definition") else "function_declaration",
                name=function["name"], item={**function, "function": function["name"]},
                type_text=(function.get("type") or {}).get("text"),
                details={"params": function.get("params"), "array_returns": function.get("array_returns", [])},
            ))
        for variable in c_facts.get("variables", []):
            rows.append(_row(
                configuration=configuration, language="c",
                kind="array_declaration" if variable.get("is_array") else "variable_declaration",
                name=variable["name"], item=variable,
                type_text=(variable.get("type") or {}).get("text"),
                size=variable.get("bytes"), elements=variable.get("elements"),
                details={"values": variable.get("values", [])},
            ))
        for pointer in c_facts.get("pointer_decls", []):
            rows.append(_row(
                configuration=configuration, language="c",
                kind=pointer.get("kind", "pointer_declaration"),
                name=pointer["name"], item=pointer,
                type_text=(pointer.get("type") or {}).get("text"),
                size=(pointer.get("type") or {}).get("size"),
            ))
        for item in c_facts.get("dereferences", []):
            rows.append(_row(
                configuration=configuration, language="c", kind="pointer_dereference",
                name=item.get("expression", "*"), item=item,
                details={"expression": item.get("expression")},
            ))
        for item in c_facts.get("subscripts", []):
            rows.append(_row(
                configuration=configuration, language="c", kind="array_subscript",
                name=(item.get("tokens") or [""])[0], item=item,
                details={"tokens": item.get("tokens")},
            ))
        for call in c_facts.get("calls", []):
            kind = {
                "allocation_or_deallocation": "allocation_call",
                "kernel_api": "kernel_api_call",
            }.get(call.get("category"), "call_site")
            rows.append(_row(
                configuration=configuration, language="c", kind=kind,
                name=call["name"], item=call,
                type_text=(call.get("return_type") or {}).get("text"),
                call_sites=1,
                details={"args": [arg.get("tokens") for arg in call.get("args", [])], "category": call.get("category")},
            ))
        for struct in c_facts.get("structs", []):
            rows.append(_row(
                configuration=configuration, language="c", kind="struct_declaration",
                name=struct["name"], item=struct, size=struct.get("size"),
                details={"align": struct.get("align"), "fields": struct.get("fields", [])},
            ))
        return rows

    benchmark_rows = c_rows(c_benchmark, c_benchmark_configuration)
    support_rows = c_rows(c_support, c_support_configuration)
    benchmark_rows.sort(key=lambda row: (row.get("file") or "", row.get("line") or 0, row.get("column") or 0, row["kind"], row["name"]))
    support_rows.sort(key=lambda row: (row.get("file") or "", row.get("line") or 0, row.get("column") or 0, row["kind"], row["name"]))

    rust_rows: list[dict[str, Any]] = []
    for declaration in rust_facts.get("foreign_fns", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="foreign_declaration",
            name=declaration["name"], item=declaration,
            type_text=";".join(param.get("text", "") for param in declaration.get("params", [])) + " -> " + (declaration.get("ret") or {}).get("text", ""),
            active=declaration.get("active", "unknown"), cfg=declaration.get("cfg", []),
            details={"abi": declaration.get("abi_block"), "variadic": declaration.get("variadic")},
        ))
    for function in rust_facts.get("functions", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="function",
            name=function["name"], item=function,
            type_text="params=" + str(len(function.get("params", []))),
            active=function.get("active", "unknown"), cfg=function.get("cfg", []),
            details={
                "unsafe": function.get("unsafe"),
                "unsafe_blocks": function.get("unsafe_blocks"),
                "pointer_derefs": function.get("pointer_derefs"),
                "pointer_arith_ops": function.get("pointer_arith_ops"),
                "impl_type": function.get("impl_type"),
                "assignments": function.get("assignments", []),
            },
        ))
        for call in function.get("calls", []):
            rust_rows.append(_row(
                configuration=rust_configuration, language="rust", kind="call_site",
                name=call.get("callee"), item={**call, "file": function.get("file")},
                active=call.get("active", "unknown"), cfg=[],
                call_sites=1, details={"args": call.get("args", [])},
            ))
        for use in function.get("add_uses", []):
            rust_rows.append(_row(
                configuration=rust_configuration, language="rust", kind="pointer_arithmetic",
                name=use.get("method"), item={**use, "file": function.get("file")},
                active=use.get("active", "unknown"), cfg=[],
                details={"receiver": use.get("receiver"), "offset": use.get("offset")},
            ))
    for struct in rust_facts.get("structs", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="struct_declaration",
            name=struct["name"], item=struct,
            type_text="repr(C)" if struct.get("repr_c") else "repr(default)",
            active=struct.get("active", "unknown"), cfg=struct.get("cfg", []),
            details={"fields": struct.get("fields", []), "repr_c": struct.get("repr_c")},
        ))
    for constant in rust_facts.get("constants", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="constant",
            name=constant["name"], item=constant,
            type_text=(constant.get("type") or {}).get("text"),
            elements=(constant.get("array") or {}).get("len"),
            active=constant.get("active", "unknown"), cfg=constant.get("cfg", []),
            details={"value_expr": constant.get("value_expr"), "array_values": (constant.get("array") or {}).get("elements", [])},
        ))
    for array in rust_facts.get("static_arrays", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="static_array",
            name=array["name"], item=array,
            type_text=(array.get("type") or {}).get("text"),
            elements=(array.get("array") or {}).get("len"),
            active=array.get("active", "unknown"), cfg=array.get("cfg", []),
            details={"mutable": array.get("mutable"), "len_expr": (array.get("array") or {}).get("len_expr")},
        ))
    for item in rust_facts.get("static_muts", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="static_mut",
            name=item["name"], item=item,
            type_text=(item.get("type") or {}).get("text"),
            active=item.get("active", "unknown"), cfg=item.get("cfg", []),
        ))
    for export in rust_facts.get("exports", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="c_callable_export",
            name=export["name"], item=export, type_text="extern C",
            active=export.get("active", "unknown"), cfg=export.get("cfg", []),
            details={"params": export.get("params", [])},
        ))
    for callback in rust_facts.get("callbacks", []):
        rust_rows.append(_row(
            configuration=rust_configuration, language="rust", kind="rust_callback",
            name=callback["name"], item=callback, type_text="extern C",
            active=callback.get("active", "unknown"), cfg=callback.get("cfg", []),
            details={"params": callback.get("params", [])},
        ))
    rust_rows.sort(key=lambda row: (row.get("file") or "", row.get("line") or 0, row.get("column") or 0, row["kind"], row["name"]))
    return benchmark_rows, support_rows, rust_rows


def ffi_interfaces(
    c_facts: dict[str, Any],
    rust_facts: dict[str, Any],
    contracts: list[dict[str, Any]],
    *,
    c_configuration: str,
    rust_configuration: str,
) -> list[dict[str, Any]]:
    status_by_name = {row["interface"]: row["status"] for row in contracts if row["check_id"] == "CHECK-01"}
    call_counts: dict[str, dict[str, int]] = {}
    for function in rust_facts.get("functions", []):
        for call in function.get("calls", []):
            name = call.get("callee", "")
            counts = call_counts.setdefault(name, {"active": 0, "inactive": 0})
            counts["inactive" if call.get("active") == "false" else "active"] += 1
    rows: list[dict[str, Any]] = []
    merged: dict[str, list[dict[str, Any]]] = {}
    for declaration in rust_facts.get("foreign_fns", []):
        merged.setdefault(declaration["name"], []).append(declaration)
    for name in sorted(merged):
        declarations = merged[name]
        declaration = declarations[0]
        c_symbols = c_facts.get("function_symbols", {}).get(name, [])
        c_symbol = next((row for row in c_symbols if row.get("is_definition") and row.get("origin") == "project"), None)
        c_symbol = c_symbol or next((row for row in c_symbols if row.get("origin") == "project"), None) or (c_symbols[0] if c_symbols else None)
        pointer_bearing = any(
            (param.get("type") or {}).get("kind") in ("pointer", "function_pointer", "array", "reference")
            for item in declarations for param in item.get("params", [])
        ) or any((item.get("ret") or {}).get("kind") in ("pointer", "function_pointer", "array", "reference") for item in declarations)
        counts = call_counts.get(name, {"active": 0, "inactive": 0})
        rows.append({
            "configuration": rust_configuration,
            "language": "cross-language",
            "interface": name,
            "file": declaration.get("file"),
            "line": declaration.get("line"),
            "call_sites": counts["active"] + counts["inactive"],
            "active_call_sites": counts["active"],
            "inactive_call_sites": counts["inactive"],
            "active": declaration.get("active"),
            "pointer_bearing": pointer_bearing,
            "c_configuration": c_configuration,
            "has_c_declaration": bool(c_symbol),
            "c_file": (c_symbol or {}).get("file"),
            "c_line": (c_symbol or {}).get("line"),
            "abi_check_status": status_by_name.get(name, "UNKNOWN"),
            "check_id": "CHECK-01",
            "details": {
                "duplicate_declarations": [{"file": item.get("file"), "line": item.get("line"), "cfg": item.get("cfg", [])} for item in declarations],
                "rust_return": (declaration.get("ret") or {}).get("text"),
                "rust_params": [param.get("text") for param in declaration.get("params", [])],
                "c_origin": (c_symbol or {}).get("origin"),
            },
        })
    return rows


def _portable_args(args: list[str] | None, repo_root: Path) -> list[str] | None:
    if args is None:
        return None
    return [item.replace(str(repo_root), ".") for item in args]


def source_manifest(
    repo_root: Path,
    config: dict[str, Any],
    c_benchmark: dict[str, Any],
    c_support: dict[str, Any],
    rust_facts: dict[str, Any],
    *,
    tool_versions: dict[str, Any],
) -> dict[str, Any]:
    configurations: dict[str, Any] = {}
    unique: dict[str, dict[str, Any]] = {}
    for c_facts, c_config in ((c_benchmark, config["configurations"]["freertos-stm32f4-c-benchmark"]), (c_support, config["configurations"]["freertos-stm32f4-c-support-rust"])):
        failed = {str(Path(item.get("file", "")).resolve()) for item in c_facts.get("parse_failures", [])}
        files = []
        for unit in c_config["translation_units"]:
            path = repo_root / unit["path"]
            entry = {
                "role": "translation_unit", "path": unit["path"],
                "sha256": sha256_file(path) if path.is_file() else None,
                "bytes": path.stat().st_size if path.is_file() else None,
                "parsed": str(path.resolve()) not in failed,
                "clang_args": _portable_args(unit.get("clang_args"), repo_root),
                "preprocessor_definitions": unit.get("preprocessor_definitions", []),
                "has_bench_language_rust": unit.get("has_bench_language_rust"),
                "included_by_scope": True,
            }
            files.append(entry)
            unique.setdefault(unit["path"], {"path": unit["path"], "sha256": entry["sha256"], "bytes": entry["bytes"]})
        for path in ("firmware/common/c/include/rtos_bench.h", "firmware/c-kernels/freertos/c/stm32f4/src/bench.h", "firmware/c-kernels/freertos/c/stm32f4/include/FreeRTOSConfig.h", "firmware/c-kernels/freertos/c/stm32f4/Makefile"):
            source = repo_root / path
            files.append({"role": "configuration_or_header", "path": path, "sha256": sha256_file(source) if source.is_file() else None, "bytes": source.stat().st_size if source.is_file() else None, "parsed": None})
        configurations[c_config["configuration"]] = {
            "language": "c", "configuration": c_config["configuration"],
            "make_argv": c_config["make_argv"], "make_command": c_config["make_command"],
            "target": c_config["target"], "files": files,
            "excluded_translation_units": c_config.get("excluded_translation_units", []),
            "parse_failures": c_facts.get("parse_failures", []),
            "source_count": len(c_config["translation_units"]),
        }

    rust_failed = {str(Path(item.get("file", "")).resolve()) for item in rust_facts.get("parse_failures", [])}
    rust_files = []
    for path in config["configurations"]["freertos-stm32f4-rust"]["files"]:
        source = repo_root / path
        entry = {"role": "rust_source", "path": path, "sha256": sha256_file(source) if source.is_file() else None, "bytes": source.stat().st_size if source.is_file() else None, "parsed": str(source.resolve()) not in rust_failed}
        rust_files.append(entry)
        unique.setdefault(path, {"path": path, "sha256": entry["sha256"], "bytes": entry["bytes"]})
    for path in ("firmware/common/rust/Cargo.toml", "firmware/c-kernels/freertos/rust/Cargo.toml"):
        source = repo_root / path
        rust_files.append({"role": "configuration", "path": path, "sha256": sha256_file(source) if source.is_file() else None, "bytes": source.stat().st_size if source.is_file() else None, "parsed": None})
    configurations["freertos-stm32f4-rust"] = {
        "language": "rust", "configuration": "freertos-stm32f4-rust",
        "target": config["configurations"]["freertos-stm32f4-rust"]["target"],
        "crate_roots": config["configurations"]["freertos-stm32f4-rust"]["crate_roots"],
        "files": rust_files, "parse_failures": rust_facts.get("parse_failures", []),
        "source_count": len(config["configurations"]["freertos-stm32f4-rust"]["files"]),
    }
    return {
        "schema": "uastw-safety-sources-v2",
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "worktree_status_count": config["worktree_status_count"],
        "target": {"c": "arm-none-eabi / Cortex-M4F", "rust": config["configurations"]["freertos-stm32f4-rust"]["target"]},
        "tools": tool_versions,
        "configurations": configurations,
        "unique_source_paths": sorted(unique.values(), key=lambda item: item["path"]),
        "excluded": {
            "c_third_party": "FreeRTOS-Kernel, HAL, CMSIS, startup and system sources are excluded from project-written application metrics",
            "rust_scopes": config["configurations"]["freertos-stm32f4-rust"].get("excluded_scopes", []),
        },
    }


def _summary(rows: list[dict[str, Any]], configuration: str, check_id: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("check_id") == check_id and row.get("configuration") == configuration]
    summary = summarize(selected, check_id)
    summary["not_applicable"] = sum(1 for row in selected if row.get("status") == "NOT_APPLICABLE")
    summary["rows"] = len(selected)
    applicable = sum(1 for row in selected if row.get("status") in ("PASS", "FAIL", "UNKNOWN"))
    if check_id in ("CHECK-02", "CHECK-05"):
        applicable = 0
    summary["applicable_discovered"] = applicable
    summary["instances_checked"] = applicable
    return summary


def coverage(
    c_benchmark: dict[str, Any],
    c_support: dict[str, Any],
    rust_facts: dict[str, Any],
    contracts: list[dict[str, Any]],
    analysers: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    c_bench_id = "freertos-stm32f4-c-benchmark"
    c_support_id = "freertos-stm32f4-c-support-rust"
    rust_id = "freertos-stm32f4-rust"
    checks_by_configuration = {
        c_bench_id: {check: _summary(contracts, c_bench_id, check) for check in ("CHECK-04", "CHECK-05")},
        c_support_id: {check: _summary(contracts, c_support_id, check) for check in ("CHECK-01", "CHECK-02", "CHECK-03", "CHECK-05")},
        rust_id: {check: _summary(contracts, rust_id, check) for check in ("CHECK-04", "CHECK-05")},
    }
    rust_active_calls = sum(1 for function in rust_facts.get("functions", []) for call in function.get("calls", []) if call.get("active") != "false")
    rust_inactive_calls = sum(1 for function in rust_facts.get("functions", []) for call in function.get("calls", []) if call.get("active") == "false")
    rust_active_constants = sum(1 for item in rust_facts.get("constants", []) if item.get("active") != "false")
    rust_inactive_constants = sum(1 for item in rust_facts.get("constants", []) if item.get("active") == "false")
    return {
        "schema": "uastw-safety-coverage-v2",
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "checks_by_configuration": checks_by_configuration,
        "project_source_scope": {
            "c_benchmark_translation_units_discovered": len(config["configurations"][c_bench_id]["translation_units"]),
            "c_benchmark_translation_units_parsed": len(c_benchmark.get("files", [])),
            "c_benchmark_parse_failures": len(c_benchmark.get("parse_failures", [])),
            "c_support_translation_units_discovered": len(config["configurations"][c_support_id]["translation_units"]),
            "c_support_translation_units_parsed": len(c_support.get("files", [])),
            "c_support_parse_failures": len(c_support.get("parse_failures", [])),
            "rust_files_discovered": len(config["configurations"][rust_id]["files"]),
            "rust_files_parsed": len(rust_facts.get("files", [])),
            "rust_parse_failures": len(rust_facts.get("parse_failures", [])),
            "skipped_sources": [],
        },
        "extraction_counts": {
            "c_benchmark": {
                "functions": len(c_benchmark.get("functions", [])),
                "variables": len(c_benchmark.get("variables", [])),
                "arrays": len(c_benchmark.get("arrays", [])),
                "calls": len(c_benchmark.get("calls", [])),
                "pointer_declarations": len(c_benchmark.get("pointer_decls", [])),
                "dereferences": len(c_benchmark.get("dereferences", [])),
                "subscripts": len(c_benchmark.get("subscripts", [])),
                "structs": len(c_benchmark.get("structs", [])),
                "allocation_calls": len(c_benchmark.get("allocation_calls", [])),
                "kernel_calls": len(c_benchmark.get("kernel_calls", [])),
            },
            "c_support": {
                "functions": len(c_support.get("functions", [])),
                "variables": len(c_support.get("variables", [])),
                "arrays": len(c_support.get("arrays", [])),
                "calls": len(c_support.get("calls", [])),
                "pointer_declarations": len(c_support.get("pointer_decls", [])),
                "dereferences": len(c_support.get("dereferences", [])),
                "subscripts": len(c_support.get("subscripts", [])),
                "structs": len(c_support.get("structs", [])),
                "allocation_calls": len(c_support.get("allocation_calls", [])),
                "kernel_calls": len(c_support.get("kernel_calls", [])),
            },
            "rust": {
                "functions": len(rust_facts.get("functions", [])),
                "foreign_declarations": len(rust_facts.get("foreign_fns", [])),
                "foreign_declarations_active": sum(1 for item in rust_facts.get("foreign_fns", []) if item.get("active") == "true"),
                "foreign_declarations_inactive": sum(1 for item in rust_facts.get("foreign_fns", []) if item.get("active") == "false"),
                "active_call_sites": rust_active_calls,
                "inactive_call_sites": rust_inactive_calls,
                "active_constants": rust_active_constants,
                "inactive_constants": rust_inactive_constants,
                "static_mut": len(rust_facts.get("static_muts", [])),
                "unsafe_blocks": sum(function.get("unsafe_blocks", 0) for function in rust_facts.get("functions", [])),
                "pointer_dereferences": sum(function.get("pointer_derefs", 0) for function in rust_facts.get("functions", [])),
                "pointer_arithmetic": sum(function.get("pointer_arith_ops", 0) for function in rust_facts.get("functions", [])),
            },
        },
        "analyser_runs": analysers,
        "diagnostic_counts_by_tool": {
            "clang-ast": {
                "c_benchmark": len(c_benchmark.get("diagnostics", [])),
                "c_support": len(c_support.get("diagnostics", [])),
            },
            "clang-static-analyzer": {
                "c_benchmark": len(analysers.get("clang_c_benchmark", {}).get("diagnostics", [])),
                "c_support": len(analysers.get("clang_c_support", {}).get("diagnostics", [])),
            },
            "clippy": {"rust": len(analysers.get("clippy", {}).get("diagnostics", []))},
        },
        "unknown_or_not_checked": [
            {"configuration": row.get("configuration"), "check_id": row["check_id"], "interface": row["interface"], "status": row["status"], "reason": row["reason"]}
            for row in contracts if row["status"] in ("UNKNOWN", "NOT_CHECKED", "NOT_APPLICABLE")
        ],
        "limitations": [
            "CHECK-01 compares source scalar types and ABI representation; value-domain compatibility is not checked.",
            "CHECK-03 follows direct accessor roots and simple local aliases; unresolved aliases force UNKNOWN.",
            "CHECK-04 excludes zero-item semaphores/mutexes from the applicable message-buffer denominator and follows common helper item sizes.",
            "Rust cfg evaluation is limited to simple target predicates; unsupported predicates remain unknown.",
            "CHECK-05 remains NOT_CHECKED; analyser diagnostics are not contract findings.",
            "C benchmark and C support scopes are separate and must not be combined into a single metric.",
        ],
    }


def write_standard_outputs(
    output: Path,
    *,
    structural_c_benchmark: list[dict[str, Any]],
    structural_c_support: list[dict[str, Any]],
    structural_rust: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
    sources: dict[str, Any],
    coverage_document: dict[str, Any],
    config: dict[str, Any],
    tool_versions: dict[str, Any],
) -> None:
    write_csv(output / "structural-c-benchmark.csv", STRUCTURAL_FIELDS, structural_c_benchmark)
    write_csv(output / "structural-c-support.csv", STRUCTURAL_FIELDS, structural_c_support)
    write_csv(output / "structural-rust.csv", STRUCTURAL_FIELDS, structural_rust)
    write_csv(output / "structural-rust-active.csv", STRUCTURAL_FIELDS, [row for row in structural_rust if row.get("active") == "true"])
    write_csv(output / "ffi-interfaces.csv", FFI_FIELDS, interfaces)
    write_csv(output / "contracts.csv", (
        "check_id", "configuration", "language", "interface", "property", "status",
        "source_file", "source_line", "evidence", "assumptions", "tool",
        "tool_version", "reason", "applicability", "source_type_status",
        "abi_representation_status", "value_domain_status", "calling_convention_status",
    ), contracts)
    write_csv(output / "diagnostics.csv", DIAGNOSTIC_FIELDS, diagnostics)
    write_json(output / "sources.json", sources)
    write_json(output / "coverage.json", coverage_document)
    write_json(output / "configuration.json", config)


def write_manifest(output: Path, *, config: dict[str, Any], tool_versions: dict[str, Any], contracts: list[dict[str, Any]]) -> None:
    files = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        files.append({"path": str(path.relative_to(output)), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    write_json(output / "manifest.json", {
        "schema": "uastw-safety-manifest-v2",
        "source_revision": config["source_revision"],
        "worktree_dirty": config["worktree_dirty"],
        "configurations": list(config["configurations"]),
        "target": {"c": "arm-none-eabi / Cortex-M4F", "rust": config["configurations"]["freertos-stm32f4-rust"]["target"]},
        "generation": {"canonical": True, "timestamps_omitted": True, "sorting": "JSON keys sorted; CSV rows sorted by scope, source and identity"},
        "tools": tool_versions,
        "contract_rows": len(contracts),
        "outputs": files,
        "notes": [
            "C benchmark and C support scopes are separate evidence scopes.",
            "Static-analysis diagnostics are not contract findings and are never combined into a safety score.",
            "PASS is limited to the property and assumptions recorded on each row.",
        ],
    })
