"""Stage 2 matched C/Rust RTOS API and FFI pilot pipeline.

The output separates underlying RTOS API relationships from project-written C
bridge definitions, keeps mechanism-specific and common harness calls apart,
and represents callback registration and callback-body coverage explicitly.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from common.layers import LayerClassifier
from common.config import discover, tool_versions
from common.paths import ARTIFACTS, SAFETY_DIR
from matched.c_extract import application_calls, extract_c_index, reachable_edges
from matched.classify import classify_check, map_arguments, return_handling
from matched.r_extract import (
    application_method_calls,
    build_rust_index,
    ffi_rows,
    reachable_rust_edges,
    reachable_rust_functions,
)
from profiles.definitions import discover_profile_configs, load_profile
from matched.registry import load_cases, verify_profile_registry, verify_registry

HERE = Path(__file__).resolve().parent
SAFETY = HERE.parent
SPEC = SAFETY / "specs" / "measurement-spec.json"
CASES = SAFETY / "specs" / "cases.json"

APP_ROLES = {"C_application", "Rust_application"}
C_IMPLEMENTATION_ROLES = {"C_adapter", "C_bridge", "C_isr", "C_callback"}
RUST_IMPLEMENTATION_ROLES = {"Rust_wrapper", "Rust_callback"}
BRIDGE_SUFFIX = "c_bridge"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            encoded = {}
            for key in fields:
                value = row.get(key)
                if isinstance(value, (list, dict, set)):
                    value = json.dumps(sorted(value) if isinstance(value, set) else value, sort_keys=True)
                elif isinstance(value, bool):
                    value = "true" if value else "false"
                elif value is None:
                    value = ""
                encoded[key] = value
            writer.writerow(encoded)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _function_id(configuration: str, language: str, name: str | None, file: str | None, line: int | None) -> str:
    material = "|".join((configuration, language, name or "", file or "", str(line or "")))
    return f"{language}-fn-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _check_id(check: dict[str, Any]) -> str:
    material = "|".join((check.get("configuration", ""), check.get("language", ""), check.get("function", ""), check.get("source_file", ""), str(check.get("source_line", "")), str(check.get("source_column", "")), check.get("condition", "")))
    return "check-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _source_check_id(check: dict[str, Any]) -> str:
    material = "|".join((check.get("language", ""), check.get("function", ""), check.get("source_file", ""), str(check.get("source_line", "")), str(check.get("source_column", "")), check.get("condition", "")))
    return "check-source-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _source_occurrence_id(edge: dict[str, Any]) -> str:
    material = "|".join(str(part or "") for part in (edge.get("language", ""), edge.get("caller", ""), edge.get("source_file", ""), str(edge.get("source_line", "")), str(edge.get("source_column", "")), edge.get("source_name", ""), edge.get("raw_target_name") or edge.get("target_name", "")))
    return "source-" + hashlib.sha256(material.encode()).hexdigest()[:20]


def rtos_match_status(c_ids: list[str] | set[str], rust_language_ids: list[str] | set[str],
                      rust_cross_language_ids: list[str] | set[str]) -> str:
    """Return the validated match state for one RTOS target relationship."""
    c_present = bool(c_ids)
    rust_present = bool(rust_language_ids) or bool(rust_cross_language_ids)
    if c_present and rust_present:
        return "matched_rtos_api"
    if c_present:
        return "c_only_rtos_api"
    if rust_present:
        return "rust_only_rtos_api"
    return "unresolved_rtos_api"


def _target_category(edge: dict[str, Any], ffi_by_name: dict[str, dict[str, Any]]) -> str:
    kind = edge.get("target_kind")
    if kind == "rtos_api":
        return "rtos_api"
    if kind in ("project_function", "project_method", "port_function_pointer"):
        return "project_wrapper"
    if kind == "rust_ffi":
        row = ffi_by_name.get(edge.get("raw_target_name") or edge.get("target_name"), {})
        target_kind = str(row.get("c_target_kind", ""))
        if target_kind.startswith("c_bridge"):
            return "c_bridge"
        if target_kind == "rtos_api":
            return "rtos_api"
        return "external_function"
    if kind == "external_function":
        return "external_function"
    return "unresolved"


def _level_membership(role: str, target_category: str) -> list[str]:
    levels: set[str] = set()
    if role in APP_ROLES:
        levels.add("A")
    elif role in C_IMPLEMENTATION_ROLES | RUST_IMPLEMENTATION_ROLES:
        levels.add("B")
    if target_category == "rtos_api":
        levels.add("C")
    return sorted(levels)


def _merge_edge(store: dict[str, dict[str, Any]], edge: dict[str, Any], case: int, role: str,
                scope: str, ffi_by_name: dict[str, dict[str, Any]]) -> None:
    key = edge["call_site_id"]
    record = store.get(key)
    if record is None:
        record = dict(edge)
        record["cases"] = []
        record["roles"] = []
        record["levels"] = []
        record["scopes"] = []
        record["root_kinds"] = []
        record["source_occurrence_id"] = _source_occurrence_id(edge)
        record["target_category"] = _target_category(record, ffi_by_name)
        store[key] = record
        edge["source_occurrence_id"] = record["source_occurrence_id"]
    record["cases"] = sorted(set(record["cases"]) | {case})
    record["roles"] = sorted(set(record["roles"]) | {role})
    record["levels"] = sorted(set(record["levels"]) | set(_level_membership(role, record["target_category"])))
    record["scopes"] = sorted(set(record["scopes"]) | {scope})
    record["root_kinds"] = sorted(set(record.get("root_kinds", [])) | {edge.get("root_kind", "benchmark_path")})
    record["common_harness"] = "common" in record["scopes"]
    record["mechanism_specific"] = "mechanism" in record["scopes"]
    record["level"] = record["roles"][0]
    record["level_a"] = "A" in record["levels"]
    record["level_b"] = "B" in record["levels"]
    record["level_c"] = "C" in record["levels"]
    if edge.get("target_params") and not record.get("target_params"):
        record["target_params"] = edge["target_params"]


def _merge_argument(store: dict[tuple[str, int], dict[str, Any]], edge: dict[str, Any], argument: dict[str, Any],
                    case: int, role: str, scope: str) -> None:
    key = (edge["call_site_id"], int(argument["position"]))
    record = store.get(key)
    if record is None:
        record = dict(argument)
        record["call_site_id"] = edge["call_site_id"]
        record["source_occurrence_id"] = edge.get("source_occurrence_id")
        record["cases"] = []
        record["roles"] = []
        record["levels"] = []
        record["scopes"] = []
        store[key] = record
    record["cases"] = sorted(set(record["cases"]) | {case})
    record["roles"] = sorted(set(record["roles"]) | {role})
    record["levels"] = sorted(set(record["levels"]) | set(edge.get("levels", edge.get("level_membership", []))))
    record["scopes"] = sorted(set(record["scopes"]) | {scope})


def _normalize_check(check: dict[str, Any], language: str, configuration: str, file: str | None) -> dict[str, Any]:
    result = dict(check)
    result["language"] = language
    result["configuration"] = configuration
    result["source_file"] = file
    result["source_line"] = check.get("source_line", check.get("line"))
    result["source_column"] = check.get("source_column", check.get("column"))
    result["end_line"] = check.get("end_line", result["source_line"])
    result["end_column"] = check.get("end_column", result["source_column"])
    result["source_check_id"] = _source_check_id(result)
    return result


def _merge_check(store: dict[str, dict[str, Any]], check: dict[str, Any], case: int, role: str, scope: str) -> None:
    key = _check_id(check)
    record = store.get(key)
    if record is None:
        record = classify_check(check)
        record["check_id"] = key
        record["source_check_id"] = check["source_check_id"]
        record["cases"] = []
        record["roles"] = []
        record["scopes"] = []
        store[key] = record
    record["cases"] = sorted(set(record["cases"]) | {case})
    record["roles"] = sorted(set(record["roles"]) | {role})
    record["scopes"] = sorted(set(record["scopes"]) | {scope})
    record["common_harness"] = "common" in record["scopes"]
    record["mechanism_specific"] = "mechanism" in record["scopes"]
    levels = set(record.get("level_membership", []))
    if "common" in record["scopes"] or role in APP_ROLES:
        levels.add("A")
    else:
        levels.add("B")
    if record.get("function_has_rtos") and record.get("check_kind") in ("status_or_error_check", "size_or_bound_check", "call_result_check", "flag_check", "ordering_check"):
        levels.add("C")
    record["level_membership"] = sorted(levels)


def _verify_stage1(repo_root: Path) -> dict[str, Any]:
    output = ARTIFACTS / "inventory"
    manifest = _read_json(output / "manifest.json")
    verified = True
    for item in manifest.get("outputs", []):
        path = output / item["path"]
        if not path.is_file() or _sha256(path) != item["sha256"] or path.stat().st_size != item["bytes"]:
            verified = False
    return {
        "manifest_verified": verified,
        "operations_sha256": _sha256(output / "operations.csv") if (output / "operations.csv").is_file() else None,
        "manifest_sha256": _sha256(output / "manifest.json") if (output / "manifest.json").is_file() else None,
        "source_revision": manifest.get("source_revision"),
    }


def _metric_row(case: dict[str, Any], view: str, level: str, language: str, selected: list[dict[str, Any]],
                args: list[dict[str, Any]], checks: list[dict[str, Any]], callback_count: int) -> dict[str, Any]:
    return {
        "benchmark_case": int(case["thesis_case"]),
        "thesis_name": case["thesis_name"],
        "view": view,
        "level": level,
        "language": language,
        "call_sites": len(selected),
        "distinct_source_call_sites": len({edge["source_occurrence_id"] for edge in selected}),
        "raw_pointer_arguments": sum(1 for row in args if row["raw_pointer"]),
        "raw_data_pointer_arguments": sum(1 for row in args if row["raw_data_pointer"]),
        "dispatch_context_arguments": sum(1 for row in args if row["dispatch_context"]),
        "output_pointer_arguments": sum(1 for row in args if row["output_pointer"]),
        "buffer_pointer_arguments": sum(1 for row in args if row["buffer_pointer"]),
        "handle_arguments": sum(1 for row in args if row["handle"]),
        "function_pointer_callback_arguments": sum(1 for row in args if row["callback"]),
        "explicit_size_memberships": sum(1 for row in args if row["explicit_size"]),
        "derived_size_memberships": sum(1 for row in args if row["derived_size"]),
        "callback_registrations": callback_count,
        "project_wrapper_operations": sum(1 for edge in selected if edge.get("level_b")),
        "underlying_rtos_calls": sum(1 for edge in selected if edge.get("target_category") == "rtos_api"),
        "unsafe_context_memberships": sum(1 for edge in selected if edge.get("in_unsafe_context") == "true"),
        "check_memberships": len(checks),
        "unresolved_call_sites": sum(1 for edge in selected if edge.get("resolution_status") == "unresolved"),
        "partial_call_sites": sum(1 for edge in selected if edge.get("coverage_status") == "partial_external_callback"),
    }


def _size_occurrences(args: list[dict[str, Any]], language: str, key: str, level: str | None = None) -> int:
    rows = [row for row in args if row.get("language") == language and row.get(key)]
    if level is not None:
        rows = [row for row in rows if level in row.get("levels", [])]
    return len(rows)


def _size_memberships(args: list[dict[str, Any]], language: str, key: str, level: str | None = None,
                      case: int | None = None) -> int:
    rows = [row for row in args if row.get("language") == language and row.get(key)]
    if level is not None:
        rows = [row for row in rows if level in row.get("levels", [])]
    if case is not None:
        return sum(1 for row in rows if case in row.get("cases", []))
    return sum(len(row.get("cases", [])) for row in rows)


def run_matched_api(repo_root: Path, output: Path, case_numbers: list[int] | None = None,
                    profile_id: str | None = None) -> dict[str, Any]:
    safety = SAFETY_DIR
    spec = _read_json(SPEC)
    profile: dict[str, Any] | None = None
    if profile_id:
        profile = load_profile(repo_root, profile_id)
        if not profile.get("cases"):
            raise SystemExit("profile must contain at least one thesis case")
        discovery = discover_profile_configs(repo_root, profile)
        registry = verify_profile_registry(repo_root, safety, profile)
        cases_doc = {
            "common_c_application_methods": profile.get("common_c_application_methods", []),
            "common_rust_application_methods": profile.get("common_rust_application_methods", []),
        }
        configs = profile["source_configs"]
        c_bench_config = discovery["configurations"][configs["c_benchmark"]["configuration"]]
        c_support_config = discovery["configurations"][configs["c_support"]["configuration"]]
        rust_config = discovery["configurations"][configs["rust"]["configuration"]]
        rtos_impl_type = profile["rust_impl_type"]
        profile_cases = {int(case["thesis_case"]): case for case in profile.get("cases", [])}
        alias_by_raw = {
            alias["rust_symbol"]: alias for alias in profile.get("api", {}).get("aliases", [])
        }
        alias_by_raw.update({
            alias["c_symbol"]: alias for alias in profile.get("api", {}).get("aliases", [])
        })
        rtos_symbols = set(profile.get("api", {}).get("rtos_symbols", []))
        allocation_symbols = set(profile.get("api", {}).get("allocation_symbols", []))
        allocator_size_arguments = profile.get("api", {}).get("allocator_size_arguments", {})
    else:
        cases_doc = load_cases(repo_root, safety)
        discovery = discover(repo_root)
        registry = verify_registry(repo_root, safety, cases_doc)
        c_bench_config = discovery["configurations"]["freertos-stm32f4-c-benchmark"]
        c_support_config = discovery["configurations"]["freertos-stm32f4-c-support-rust"]
        rust_config = discovery["configurations"]["freertos-stm32f4-rust"]
        rtos_impl_type = "FreertosPort"
        profile_cases = {}
        alias_by_raw = {}
        rtos_symbols = set()
        allocation_symbols = set()
        allocator_size_arguments = {}
    expected_cases = len(registry["rows"]) if case_numbers is None else len(case_numbers)
    if case_numbers is not None:
        selected = set(int(number) for number in case_numbers)
        available = {int(row["thesis_case"]) for row in registry["rows"]}
        missing = sorted(selected - available)
        if missing:
            raise SystemExit(f"requested cases are not in the registry: {missing}")
        registry["rows"] = [row for row in registry["rows"] if int(row["thesis_case"]) in selected]
    classifier = LayerClassifier(safety / "specs/layers.json")
    extractor = safety / "rust_extractor/target/release/uastw-rust-extract"
    c_bench = extract_c_index(repo_root, c_bench_config)
    c_support = extract_c_index(repo_root, c_support_config)
    rust_index = build_rust_index(repo_root, rust_config, extractor, classifier)
    rust_index["profile_mode"] = bool(profile)
    ffi = ffi_rows(repo_root, rust_index, c_support, rtos_symbols=rtos_symbols, allocation_symbols=allocation_symbols)
    ffi_by_name = {row["rust_name"]: row for row in ffi}

    def apply_alias(edge: dict[str, Any]) -> None:
        alias = alias_by_raw.get(edge.get("target_name") or "")
        if alias and edge.get("target_name") != alias.get("canonical"):
            edge["raw_target_name"] = edge.get("target_name")
            edge["target_name"] = alias["canonical"]
            edge["target_alias"] = alias["canonical"]
            edge["target_alias_justification"] = alias.get("justification")

    def apply_aliases(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for edge in edges:
            apply_alias(edge)
        return edges

    common_c_methods = cases_doc.get("common_c_application_methods", [])
    common_rust_methods = cases_doc.get("common_rust_application_methods", [])
    common_c_map = {method: registry["c_port_methods"].get(method) for method in common_c_methods}
    known_c_enums = {row["c_enum"] for row in registry["rows"]}
    known_rust_variants = {row["rust_variant"] for row in registry["rows"]}

    all_edges: dict[str, dict[str, Any]] = {}
    all_args: dict[tuple[str, int], dict[str, Any]] = {}
    all_checks: dict[str, dict[str, Any]] = {}
    root_rows: list[dict[str, Any]] = []
    rtos_rows: list[dict[str, Any]] = []
    bridge_rows: list[dict[str, Any]] = []
    callback_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    macro_rows_by_id: dict[str, dict[str, Any]] = {}
    case_meta: dict[int, dict[str, Any]] = {}
    case_function_roles: dict[int, dict[tuple[str, str, str], set[str]]] = defaultdict(lambda: defaultdict(set))
    case_check_lists: dict[int, list[dict[str, Any]]] = defaultdict(list)

    def remember_function(case_number: int, configuration: str, language: str, function: str, role: str) -> None:
        case_function_roles[case_number][(configuration, language, function)].add(role)

    def record_irq_connect_registration(edges: list[dict[str, Any]], case_number: int) -> list[dict[str, Any]]:
        """Record the static IRQ_CONNECT vector declaration while keeping the
        runtime arm_irq_priority_set call in ordinary call evidence.

        IRQ_CONNECT expands to Z_ISR_DECLARE(...) (build-time vector data) and
        z_arm_irq_priority_set(...) (runtime priority configuration).  The
        latter remains an external runtime arch/interrupt-control call; it is
        not promoted to an RTOS target and is not dropped as compile-time-only.
        """
        for edge in edges:
            if not (str(edge.get("source_expression") or "").lstrip().startswith("IRQ_CONNECT(")
                    and str(edge.get("target_name") or "") == "arm_irq_priority_set"):
                continue
            material = "|".join((
                edge["configuration"], "c", edge.get("caller") or "", edge.get("source_file") or "",
                str(edge.get("source_line") or ""), str(edge.get("source_column") or ""),
                "IRQ_CONNECT", edge.get("source_file") or "",
            ))
            macro_id = "macro-" + hashlib.sha256(material.encode()).hexdigest()[:20]
            record = macro_rows_by_id.get(macro_id)
            if record is None:
                record = {
                    "macro_site_id": macro_id,
                    "case_attribution": "selected_case_path",
                    "configuration": edge["configuration"],
                    "language": "c",
                    "function": edge.get("caller"),
                    "source_file": edge.get("source_file"),
                    "source_line": edge.get("source_line"),
                    "source_column": edge.get("source_column"),
                    "macro": "IRQ_CONNECT",
                    "macro_effect": "static_vector_registration(runtime_arm_irq_priority_set_retained)",
                    "common_harness": False,
                    "resolution_status": "static_registration",
                    "reason": "IRQ_CONNECT records a static vector declaration and a runtime arm_irq_priority_set call; both remain visible and are not collapsed",
                    "cases": [],
                }
                macro_rows_by_id[macro_id] = record
            record["cases"] = sorted(set(record["cases"]) | {case_number})
        return edges

    for case in registry["rows"]:
        number = int(case["thesis_case"])
        c_edges, c_roots = reachable_edges(c_bench, case["c_root_functions"])
        c_edges = record_irq_connect_registration(c_edges, number)
        apply_aliases(c_edges)
        for root in c_roots:
            root_rows.append({"benchmark_case": number, "language": "c", "root": root["root"], "status": root["status"], "definition_file": root.get("definition_file"), "definition_line": root.get("definition_line"), "root_kind": "c_adapter"})
        c_app_case = application_calls(c_bench, case["c_port_function_map"], number)
        c_app_common = application_calls(c_bench, common_c_map, number)
        for row in c_app_common:
            row["common_harness"] = True
        for row in c_app_case + c_app_common:
            definitions = c_bench["definitions_by_name"].get(row["target_name"], [])
            if len(definitions) == 1:
                row["target_params"] = definitions[0]["params"]
                row["target_file"] = definitions[0]["file"]
                row["target_line"] = definitions[0]["line"]
                row["target_definition"] = True
            row["target_kind"] = "port_function_pointer"
        c_app = c_app_case + c_app_common

        c_interrupt: list[dict[str, Any]] = []
        for interrupt_root in case.get("interrupt_roots", []):
            edges, roots = reachable_edges(c_bench, [interrupt_root])
            for edge in edges:
                edge["root_kind"] = "interrupt_handler"
            apply_aliases(edges)
            c_interrupt.extend(edges)
            for root in roots:
                root_rows.append({"benchmark_case": number, "language": "c", "root": root["root"], "status": root["status"], "definition_file": root.get("definition_file"), "definition_line": root.get("definition_line"), "root_kind": "interrupt_handler"})

        c_support_interrupt: list[dict[str, Any]] = []
        if profile:
            for interrupt_root in case.get("interrupt_roots", []):
                edges, roots = reachable_edges(c_support, [interrupt_root])
                edges = record_irq_connect_registration(edges, number)
                apply_aliases(edges)
                for edge in edges:
                    edge["root_kind"] = "c_support_interrupt_handler"
                    edge["rust_cross_language"] = True
                c_support_interrupt.extend(edges)
                for root in roots:
                    root_rows.append({"benchmark_case": number, "language": "c", "root": root["root"], "status": root["status"], "definition_file": root.get("definition_file"), "definition_line": root.get("definition_line"), "root_kind": "c_support_interrupt_handler"})

        r_edges, r_roots = reachable_rust_edges(rust_index, rtos_impl_type, case["rust_application_methods"])
        r_edges = [edge for edge in r_edges if edge.get("target_kind") != "language_builtin"]
        apply_aliases(r_edges)
        for root in r_roots:
            root_rows.append({"benchmark_case": number, "language": "rust", "root": root["root"], "status": root["status"], "definition_file": root.get("file"), "definition_line": root.get("line"), "root_kind": "rust_adapter"})
        r_app_case = application_method_calls(rust_index, rtos_impl_type, case["rust_application_methods"], number)
        r_app_common = application_method_calls(rust_index, rtos_impl_type, common_rust_methods, number)
        for row in r_app_common:
            row["common_harness"] = True
        r_app = r_app_case + r_app_common

        # Callback registration and callback-body paths.  Only registrations on
        # the selected direct path are attributed to this case.
        c_direct_ids = {edge["call_site_id"] for edge in c_edges + c_interrupt}
        c_callback_edges: list[dict[str, Any]] = []
        c_callbacks: list[dict[str, Any]] = []
        for registration in c_bench.get("callback_registrations", []):
            if registration["registration_call_site_id"] not in c_direct_ids:
                continue
            item = dict(registration)
            item["benchmark_case"] = number
            item["language"] = "c"
            item["call_site_ids"] = []
            if item["resolution_status"] == "resolved_callback_definition" and item.get("callback_symbol"):
                body, body_roots = reachable_edges(c_bench, [item["callback_symbol"]])
                body = record_irq_connect_registration(body, number)
                for edge in body:
                    edge["root_kind"] = "callback_body"
                    edge["callback_registration_id"] = item["registration_call_site_id"]
                item["call_site_ids"] = sorted(edge["call_site_id"] for edge in body)
                item["body_root_status"] = body_roots[0]["status"] if body_roots else "unresolved_root"
                apply_aliases(body)
                c_callback_edges.extend(body)
            c_callbacks.append(item)
            callback_rows.append(item)

        r_direct_ids = {edge["call_site_id"] for edge in r_edges}
        r_callback_edges: list[dict[str, Any]] = []
        r_callbacks: list[dict[str, Any]] = []
        for registration in rust_index.get("callback_registrations", []):
            selected_registration = registration.get("application_registration_call_site_id") in r_direct_ids
            if not selected_registration:
                continue
            item = dict(registration)
            item["benchmark_case"] = number
            item["language"] = "rust"
            item["call_site_ids"] = []
            if item["resolution_status"] == "resolved_callback_definition" and item.get("callback_symbol"):
                body, body_roots = reachable_rust_functions(rust_index, [item["callback_symbol"]])
                for edge in body:
                    edge["root_kind"] = "callback_body"
                    edge["callback_registration_id"] = item["registration_call_site_id"]
                item["call_site_ids"] = sorted(edge["call_site_id"] for edge in body)
                item["body_root_status"] = body_roots[0]["status"] if body_roots else "unresolved_root"
                body = [edge for edge in apply_aliases(body) if edge.get("target_kind") != "language_builtin"]
                r_callback_edges.extend(body)
            r_callbacks.append(item)
            callback_rows.append(item)

        # C bridges are traversed only when Rust FFI or a resolved C call reaches
        # them.  Configured but unreachable bridges remain in the inventory.
        bridge_ffi_symbols = {
            edge["target_name"] for edge in r_edges + r_callback_edges
            if edge.get("target_kind") == "rust_ffi"
            and str((ffi_by_name.get(edge["target_name"]) or {}).get("c_target_kind", "")).startswith("c_bridge")
        }
        c_project_bridge_calls = {
            edge["target_name"] for edge in c_edges + c_interrupt + c_callback_edges
            if edge.get("target_kind") == "project_function"
        }
        configured_bridges = set(case.get("c_bridge_roots", [])) | bridge_ffi_symbols
        reachable_bridges = set(bridge_ffi_symbols) | (configured_bridges & c_project_bridge_calls)
        c_bridge_edges: list[dict[str, Any]] = []
        for symbol in sorted(reachable_bridges):
            body, roots = reachable_edges(c_support, [symbol])
            body = record_irq_connect_registration(body, number)
            apply_aliases(body)
            for edge in body:
                edge["root_kind"] = "c_bridge"
                edge["bridge_root"] = symbol
                edge["bridge_reachability"] = "rust_ffi" if symbol in bridge_ffi_symbols else "c_call"
            c_bridge_edges.extend(body)
            for root in roots:
                root_rows.append({"benchmark_case": number, "language": "c", "root": root["root"], "status": root["status"], "definition_file": root.get("definition_file"), "definition_line": root.get("definition_line"), "root_kind": "c_bridge"})
        c_bridge_edges = list({edge["call_site_id"]: edge for edge in c_bridge_edges}.values())

        # C-support bridges may create RTOS tasks whose task-entry callbacks are
        # also part of the selected Rust-through-C path.  Traverse only callbacks
        # registered by a call already reached in the selected bridge body.
        bridge_edges_by_id = {edge["call_site_id"]: edge for edge in c_bridge_edges}
        for registration in c_support.get("callback_registrations", []):
            registration_id = registration.get("registration_call_site_id")
            parent = bridge_edges_by_id.get(registration_id)
            if parent is None:
                continue
            item = dict(registration)
            item["benchmark_case"] = number
            item["language"] = "c"
            item["source_scope"] = "c_support_bridge"
            item["bridge_root"] = parent.get("bridge_root")
            item["call_site_ids"] = []
            if item["resolution_status"] == "resolved_callback_definition" and item.get("callback_symbol"):
                body, body_roots = reachable_edges(c_support, [item["callback_symbol"]])
                body = record_irq_connect_registration(body, number)
                for edge in body:
                    edge["root_kind"] = "bridge_callback_body"
                    edge["bridge_root"] = parent.get("bridge_root")
                    edge["callback_registration_id"] = registration_id
                    edge["bridge_reachability"] = "c_support_callback"
                item["call_site_ids"] = sorted(edge["call_site_id"] for edge in body)
                item["body_root_status"] = body_roots[0]["status"] if body_roots else "unresolved_root"
                apply_aliases(body)
                c_bridge_edges.extend(body)
            c_callbacks.append(item)
            callback_rows.append(item)
        c_bridge_edges = list({edge["call_site_id"]: edge for edge in c_bridge_edges}.values())

        # Merge call edges into the occurrence store.
        edge_sets = [
            (c_edges, "C_adapter", "mechanism"),
            (c_app_case, "C_application", "mechanism"),
            (c_app_common, "C_application", "common"),
            (c_interrupt, "C_isr", "mechanism"),
            (c_support_interrupt, "C_isr", "mechanism"),
            (c_bridge_edges, "C_bridge", "mechanism"),
            (c_callback_edges, "C_callback", "mechanism"),
            (r_edges, "Rust_wrapper", "mechanism"),
            (r_app_case, "Rust_application", "mechanism"),
            (r_app_common, "Rust_application", "common"),
            (r_callback_edges, "Rust_callback", "mechanism"),
        ]
        for edges, role, scope in edge_sets:
            for edge in edges:
                edge["scope"] = scope
                edge["common_harness"] = scope == "common"
                _merge_edge(all_edges, edge, number, role, scope, ffi_by_name)
                remember_function(number, edge["configuration"], edge["language"], edge["caller"], role)
                args, mapping, unmapped = map_arguments(
                    edge["language"], edge.get("source_args", []), edge.get("target_params", []),
                    edge.get("target_kind", ""), edge.get("target_name"),
                    allocator_size_arguments if profile else None,
                )
                edge["argument_mapping_status"] = mapping
                edge["unmapped_target_parameters"] = unmapped
                edge["target_category"] = _target_category(edge, ffi_by_name)
                edge["level_membership"] = _level_membership(role, edge["target_category"])
                edge["level_a"] = "A" in edge["level_membership"]
                edge["level_b"] = "B" in edge["level_membership"]
                edge["level_c"] = "C" in edge["level_membership"]
                edge["levels"] = edge["level_membership"]
                edge["roles"] = [role]
                for argument in args:
                    _merge_argument(all_args, edge, argument, number, role, scope)

        # Checks are attached by configuration and actual path role, not by a
        # single global function-level priority.
        case_rtos_functions = {
            (edge["configuration"], edge["language"], edge["caller"])
            for edges in (c_edges, c_interrupt, c_support_interrupt, c_bridge_edges, c_callback_edges, r_edges, r_callback_edges)
            for edge in edges
            if edge.get("target_category") == "rtos_api"
        }
        c_check_roles = case_function_roles[number]
        c_checks: list[dict[str, Any]] = []
        for check in c_bench["checks"]:
            function = check.get("function")
            if function in ("sample", "bench_run_case"):
                condition = check.get("condition") or ""
                case_enums = set(re.findall(r"\bBENCH_[A-Z0-9_]+\b", condition)) & known_c_enums
                if function == "bench_run_case" and not case_enums:
                    continue
                if case_enums and case["c_enum"] not in case_enums:
                    continue
                item = _normalize_check(check, "c", c_bench_config["configuration"], check.get("source_file"))
                if case_enums:
                    item["common_harness"] = False
                    item["mechanism_specific"] = True
                    item["scope"] = "mechanism"
                else:
                    item["common_harness"] = True
                    item["mechanism_specific"] = False
                    item["scope"] = "common"
                item["roles"] = ["C_application"]
                item["function_has_rtos"] = False
                c_checks.append(item)
                continue
            roles = c_check_roles.get((c_bench_config["configuration"], "c", function), set())
            if not roles:
                continue
            item = _normalize_check(check, "c", c_bench_config["configuration"], check.get("source_file"))
            item["common_harness"] = False
            item["mechanism_specific"] = True
            item["roles"] = sorted(roles)
            item["scope"] = "mechanism"
            item["function_has_rtos"] = (c_bench_config["configuration"], "c", function) in case_rtos_functions
            c_checks.append(item)
        for check in c_support["checks"]:
            roles = c_check_roles.get((c_support_config["configuration"], "c", check.get("function")), set())
            if not roles:
                continue
            item = _normalize_check(check, "c", c_support_config["configuration"], check.get("source_file"))
            item["common_harness"] = False
            item["mechanism_specific"] = True
            item["roles"] = sorted(roles)
            item["scope"] = "mechanism"
            item["function_has_rtos"] = (c_support_config["configuration"], "c", check.get("function")) in case_rtos_functions
            c_checks.append(item)
        r_checks: list[dict[str, Any]] = []
        for function in rust_index["facts"].get("functions", []):
            name = function.get("name")
            if name in ("sample", "run_case"):
                for check in function.get("checks", []):
                    condition = check.get("condition") or ""
                    case_variants = set(re.findall(r"Case::([A-Za-z0-9_]+)", condition)) & known_rust_variants
                    if name == "run_case" and not case_variants:
                        continue
                    if case_variants and case["rust_variant"] not in case_variants:
                        continue
                    item = _normalize_check(check, "rust", rust_config["configuration"], function.get("file"))
                    if case_variants:
                        item["common_harness"] = False
                        item["mechanism_specific"] = True
                        item["scope"] = "mechanism"
                    else:
                        item["common_harness"] = True
                        item["mechanism_specific"] = False
                        item["scope"] = "common"
                    item["roles"] = ["Rust_application"]
                    item["function_has_rtos"] = False
                    r_checks.append(item)
                continue
            roles = c_check_roles.get((rust_config["configuration"], "rust", name), set())
            if not roles:
                continue
            for check in function.get("checks", []):
                item = _normalize_check(check, "rust", rust_config["configuration"], function.get("file"))
                item["common_harness"] = False
                item["mechanism_specific"] = True
                item["roles"] = sorted(roles)
                item["scope"] = "mechanism"
                item["function_has_rtos"] = (rust_config["configuration"], "rust", name) in case_rtos_functions
                r_checks.append(item)
        for check in c_checks + r_checks:
            for role in check["roles"]:
                _merge_check(all_checks, check, number, role, check["scope"])
        case_check_lists[number] = c_checks + r_checks

        # Macro sites remain visible but are described accurately.
        for function in rust_index["facts"].get("functions", []):
            name = function.get("name")
            if (rust_config["configuration"], "rust", name) not in c_check_roles:
                continue
            for macro in function.get("macro_uses", []):
                material = "|".join((rust_config["configuration"], "rust", name or "", function.get("file") or "", str(macro.get("line") or ""), str(macro.get("column") or ""), macro.get("macro") or ""))
                macro_id = "macro-" + hashlib.sha256(material.encode()).hexdigest()[:20]
                record = macro_rows_by_id.get(macro_id)
                if record is None:
                    record = {
                        "macro_site_id": macro_id,
                        "case_attribution": "shared_unreachable_arm" if name in ("sample", "run_case") else "selected_case_path",
                        "configuration": rust_config["configuration"],
                        "language": "rust",
                        "function": name,
                        "source_file": function.get("file"),
                        "source_line": macro.get("line"),
                        "source_column": macro.get("column"),
                        "macro": macro.get("macro"),
                        "macro_effect": "unreachable_match_arm" if macro.get("macro") == "unreachable" else "unexpanded_macro",
                        "common_harness": name in ("sample", "run_case"),
                        "resolution_status": "unresolved",
                        "reason": "syn has no macro expansion; not treated as zero operations",
                        "cases": [],
                    }
                    macro_rows_by_id[macro_id] = record
                if name not in ("sample", "run_case"):
                    record["cases"] = sorted(set(record["cases"]) | {number})

        # RTOS target relationships.
        case_rtos: dict[str, dict[str, Any]] = {}

        def add_rtos(target: str, c_ids: list[str] | None = None, rust_ids: list[str] | None = None,
                     cross_ids: list[str] | None = None, origin: str | None = None) -> None:
            row = case_rtos.setdefault(target, {"c_ids": set(), "rust_language_ids": set(), "rust_cross_language_ids": set(), "c_origins": set(), "rust_origins": set(), "raw_c_targets": set(), "raw_rust_targets": set()})
            row["c_ids"].update(c_ids or [])
            row["rust_language_ids"].update(rust_ids or [])
            row["rust_cross_language_ids"].update(cross_ids or [])
            if origin:
                if c_ids:
                    row["c_origins"].add(origin)
                if rust_ids or cross_ids:
                    row["rust_origins"].add(origin)

        for edge in c_edges:
            if edge["target_kind"] == "rtos_api":
                add_rtos(edge["target_name"], c_ids=[edge["call_site_id"]], origin="c_direct")
                case_rtos[edge["target_name"]]["raw_c_targets"].add(edge.get("raw_target_name") or edge["target_name"])
        for edge in c_interrupt:
            if edge["target_kind"] == "rtos_api":
                add_rtos(edge["target_name"], c_ids=[edge["call_site_id"]], origin="c_isr")
                case_rtos[edge["target_name"]]["raw_c_targets"].add(edge.get("raw_target_name") or edge["target_name"])
        for edge in c_support_interrupt:
            if edge["target_kind"] == "rtos_api":
                add_rtos(edge["target_name"], cross_ids=[edge["call_site_id"]], origin="c_support_isr")
                case_rtos[edge["target_name"]]["raw_c_targets"].add(edge.get("raw_target_name") or edge["target_name"])
        for edge in c_bridge_edges:
            if edge["target_kind"] == "rtos_api":
                add_rtos(edge["target_name"], c_ids=[edge["call_site_id"]], origin=f"c_bridge:{edge.get('bridge_root')}")
                case_rtos[edge["target_name"]]["raw_c_targets"].add(edge.get("raw_target_name") or edge["target_name"])
        for edge in c_callback_edges:
            if edge["target_kind"] == "rtos_api":
                add_rtos(edge["target_name"], c_ids=[edge["call_site_id"]], origin="c_callback")
                case_rtos[edge["target_name"]]["raw_c_targets"].add(edge.get("raw_target_name") or edge["target_name"])
        for edge in r_edges + r_callback_edges:
            if edge["target_kind"] == "rust_ffi":
                row = ffi_by_name.get(edge.get("raw_target_name") or edge["target_name"], {})
                if row.get("c_target_kind") == "rtos_api":
                    add_rtos(edge["target_name"], rust_ids=[edge["call_site_id"]], origin="rust_ffi")
                    case_rtos[edge["target_name"]]["raw_rust_targets"].add(edge.get("raw_target_name") or edge["target_name"])
        for edge in c_bridge_edges:
            if edge.get("bridge_root") in bridge_ffi_symbols and edge["target_kind"] == "rtos_api":
                add_rtos(edge["target_name"], cross_ids=[edge["call_site_id"]], origin=f"c_bridge:{edge.get('bridge_root')}")
        if not profile and case.get("interrupt_roots") and case.get("c_bridge_roots"):
            for edge in c_interrupt:
                if edge["target_kind"] == "rtos_api":
                    add_rtos(edge["target_name"], cross_ids=[edge["call_site_id"]], origin="shared_c_isr")

        for target in sorted(case_rtos):
            row = case_rtos[target]
            c_ids = sorted(row["c_ids"])
            rust_ids = sorted(row["rust_language_ids"])
            cross_ids = sorted(row["rust_cross_language_ids"])
            status = rtos_match_status(c_ids, rust_ids, cross_ids)
            rtos_rows.append({
                "benchmark_case": number,
                "thesis_name": case["thesis_name"],
                "target_category": "rtos_api",
                "target_name": target,
                "c_target_api": target if (c_ids or cross_ids) else None,
                "rust_target_api": target if (rust_ids or cross_ids) else None,
                "raw_c_target_names": sorted(row["raw_c_targets"]),
                "raw_rust_target_names": sorted(row["raw_rust_targets"]),
                "match_status": status,
                "c_call_path_ids": c_ids,
                "rust_language_path_ids": rust_ids,
                "rust_cross_language_path_ids": cross_ids,
                "c_path_origins": sorted(row["c_origins"]),
                "rust_path_origins": sorted(row["rust_origins"]),
                "unresolved_edges": sorted({edge["call_site_id"] for edge in c_edges + c_interrupt + c_bridge_edges + c_callback_edges + r_edges + r_callback_edges if edge["resolution_status"] == "unresolved"}),
            })

        # C bridge definitions: eight selected project bridges plus the unused
        # bench_frag_measure definition.  Bridge bodies never become RTOS targets.
        for symbol in sorted(configured_bridges):
            definitions = c_support["definitions_by_name"].get(symbol, [])
            definition = definitions[0] if len(definitions) == 1 else None
            rust_calls = sorted({edge["call_site_id"] for edge in r_edges + r_callback_edges if edge.get("target_kind") == "rust_ffi" and edge["target_name"] == symbol})
            c_calls = sorted({edge["call_site_id"] for edge in c_edges + c_interrupt + c_callback_edges if edge["target_name"] == symbol})
            body_targets = sorted({edge["target_name"] for edge in c_bridge_edges if edge.get("bridge_root") == symbol and edge["target_kind"] == "rtos_api"})
            if not definition:
                status = "unresolved_c_bridge_definition"
            elif rust_calls:
                status = "matched_c_bridge_definition"
            elif c_calls:
                status = "c_only_c_bridge_definition"
            else:
                status = "unused_c_bridge_definition"
            bridge_rows.append({
                "benchmark_case": number,
                "thesis_name": case["thesis_name"],
                "target_category": "c_bridge_definition",
                "target_name": symbol,
                "match_status": status,
                "c_definition_file": definition.get("file") if definition else None,
                "c_definition_line": definition.get("line") if definition else None,
                "c_call_site_ids": c_calls,
                "rust_language_path_ids": rust_calls,
                "rust_cross_language_path_ids": [],
                "body_rtos_targets": body_targets,
                "body_traversed": symbol in reachable_bridges,
                "reachable_by": "rust_ffi" if symbol in bridge_ffi_symbols else ("c_call" if symbol in c_project_bridge_calls else "unused"),
                "inventory_scope": "selected_configured_reached",
                "c_path_origins": ["c_call"] if c_calls else [],
                "rust_path_origins": ["rust_ffi"] if rust_calls else [],
                "unresolved_edges": [],
            })

        coverage_status = "complete_direct_path"
        if any(item["resolution_status"] != "resolved_callback_definition" for item in c_callbacks + r_callbacks):
            coverage_status = "partial_external_callback"
        elif c_callbacks or r_callbacks:
            coverage_status = "complete_with_resolved_external_callback"
        coverage_rows.append({
            "benchmark_case": number,
            "thesis_name": case["thesis_name"],
            "coverage_status": coverage_status,
            "callback_registrations": len(c_callbacks) + len(r_callbacks),
            "resolved_callbacks": sum(1 for item in c_callbacks + r_callbacks if item["resolution_status"] == "resolved_callback_definition"),
            "partial_callbacks": sum(1 for item in c_callbacks + r_callbacks if item["resolution_status"] != "resolved_callback_definition"),
            "external_isr_roots": len(case.get("interrupt_roots", [])),
            "shared_c_isr": bool(case.get("interrupt_roots")),
            "unresolved_call_sites": 0,
            "common_harness_calls": len(c_app_common) + len(r_app_common),
        })
        case_meta[number] = {
            "c_edges": c_edges,
            "c_app_case": c_app_case,
            "c_app_common": c_app_common,
            "c_interrupt": c_interrupt,
            "c_support_interrupt": c_support_interrupt,
            "c_bridge_edges": c_bridge_edges,
            "c_callback_edges": c_callback_edges,
            "r_edges": r_edges,
            "r_app_case": r_app_case,
            "r_app_common": r_app_common,
            "r_callback_edges": r_callback_edges,
            "callbacks": c_callbacks + r_callbacks,
            "coverage_status": coverage_status,
            "configured_bridges": configured_bridges,
            "reachable_bridges": reachable_bridges,
        }

    # Return handling after all checks are known.
    for edge in all_edges.values():
        relevant_checks = []
        for case_number in edge["cases"]:
            relevant_checks.extend(case_check_lists.get(int(case_number), []))
        edge["return_handling"] = return_handling(edge, relevant_checks)

    # Function signatures.
    function_rows: list[dict[str, Any]] = []
    for index, language, configuration in ((c_bench, "c", c_bench_config["configuration"]), (c_support, "c", c_support_config["configuration"])):
        for function in index["functions"]:
            if not function["is_project"]:
                continue
            function_rows.append({
                "function_id": _function_id(configuration, "c", function["name"], function["file"], function["line"]),
                "configuration": configuration, "language": "c", "name": function["name"],
                "file": function["file"], "line": function["line"],
                "kind": "definition" if function["is_definition"] else "declaration",
                "is_project": True, "unsafe": "false", "active": "true",
                "signature": function["signature"], "params": function["params"],
                "return_type": function["return_type"], "evidence_method": "libclang_function_decl",
            })
    for function in rust_index["facts"].get("functions", []):
        function_rows.append({
            "function_id": _function_id(rust_config["configuration"], "rust", function.get("name"), function.get("file"), function.get("line")),
            "configuration": rust_config["configuration"], "language": "rust", "name": function.get("name"),
            "file": function.get("file"), "line": function.get("line"), "kind": "definition",
            "is_project": True, "unsafe": function.get("unsafe", False), "active": function.get("active"),
            "signature": "", "params": function.get("params", []), "return_type": function.get("ret"),
            "evidence_method": "syn_function_signature",
        })
    for foreign in rust_index["facts"].get("foreign_fns", []):
        row = ffi_by_name.get(foreign.get("name"), {})
        function_rows.append({
            "function_id": _function_id(rust_config["configuration"], "rust_ffi", foreign.get("name"), foreign.get("file"), foreign.get("line")),
            "configuration": rust_config["configuration"], "language": "rust_ffi", "name": foreign.get("name"),
            "file": foreign.get("file"), "line": foreign.get("line"), "kind": "foreign_declaration",
            "is_project": False, "unsafe": "foreign", "active": foreign.get("active"), "signature": "",
            "params": foreign.get("params", []), "return_type": foreign.get("ret"),
            "c_target_file": row.get("c_target_file"), "c_target_line": row.get("c_target_line"),
            "resolution_status": row.get("resolution_status"), "evidence_method": "syn_extern_block+exact_c_symbol_match",
        })
    function_rows.sort(key=lambda row: (row["configuration"], row["language"], row["file"] or "", row["line"] or 0, row["name"] or ""))
    caller_id_by_key = {(row["configuration"], row["language"], row["name"], row["file"]): row["function_id"] for row in function_rows}

    # Edge export rows.
    edge_rows = []
    for edge in all_edges.values():
        caller_id = caller_id_by_key.get((edge["configuration"], edge["language"], edge["caller"], edge["caller_file"]), _function_id(edge["configuration"], edge["language"], edge["caller"], edge["caller_file"], edge.get("source_line")))
        callee_language = "rust_ffi" if edge.get("target_kind") == "rust_ffi" else ("rust" if edge.get("target_kind") in ("project_function", "project_method", "ambiguous_project_function") else "c")
        callee_id = _function_id(edge["configuration"], callee_language, edge.get("target_name"), edge.get("target_file"), edge.get("target_line"))
        row = dict(edge)
        row["caller_id"] = caller_id
        row["callee_id"] = callee_id
        row["callee_kind"] = edge.get("target_kind")
        row["evidence_method"] = edge.get("evidence_method")
        row["coverage_status"] = next((meta["coverage_status"] for case_number, meta in case_meta.items() if case_number in edge["cases"]), "unresolved")
        edge_rows.append(row)
    edge_rows.sort(key=lambda row: (row["configuration"], row["language"], row["source_file"] or "", row["source_line"] or 0, row["source_column"] or 0, row["source_name"] or ""))

    check_rows = sorted(all_checks.values(), key=lambda row: (row["language"], row["source_file"] or "", row["source_line"] or 0, row["function"] or "", row["check_id"]))
    argument_rows = sorted(all_args.values(), key=lambda row: (row["call_site_id"], row["position"]))

    # Role/scope exposure table.
    exposure_rows = []
    all_roles = ("C_application", "C_adapter", "C_bridge", "C_isr", "C_callback", "Rust_application", "Rust_wrapper", "Rust_callback")
    for case in registry["rows"]:
        number = int(case["thesis_case"])
        for role in all_roles:
            scopes = ["combined"]
            if role in APP_ROLES:
                scopes = ["mechanism", "common", "combined"]
            for scope in scopes:
                selected = [edge for edge in all_edges.values() if number in edge["cases"] and role in edge.get("roles", []) and (scope == "combined" or scope in edge.get("scopes", []))]
                if not selected and scope != "combined":
                    continue
                args = [row for row in argument_rows if number in row["cases"] and role in row.get("roles", []) and (scope == "combined" or scope in row.get("scopes", []))]
                checks = [row for row in check_rows if number in row["cases"] and role in row.get("roles", []) and (scope == "combined" or scope in row.get("scopes", []))]
                exposure_rows.append({
                    "benchmark_case": number, "thesis_name": case["thesis_name"], "role": role, "scope": scope,
                    "case_call_sites": len(selected),
                    "distinct_source_call_sites": len({edge["source_occurrence_id"] for edge in selected}),
                    "raw_pointer_arguments": sum(1 for row in args if row["raw_pointer"]),
                    "raw_data_pointer_arguments": sum(1 for row in args if row["raw_data_pointer"]),
                    "dispatch_context_arguments": sum(1 for row in args if row["dispatch_context"]),
                    "output_pointer_arguments": sum(1 for row in args if row["output_pointer"]),
                    "buffer_pointer_arguments": sum(1 for row in args if row["buffer_pointer"]),
                    "handle_arguments": sum(1 for row in args if row["handle"]),
                    "function_pointer_callback_arguments": sum(1 for row in args if row["callback"]),
                    "explicit_size_memberships": sum(1 for row in args if row["explicit_size"]),
                    "derived_size_memberships": sum(1 for row in args if row["derived_size"]),
                    "unsafe_context_memberships": sum(1 for edge in selected if edge.get("in_unsafe_context") == "true"),
                    "check_memberships": len(checks),
                    "unresolved_call_sites": sum(1 for edge in selected if edge["resolution_status"] == "unresolved"),
                })

    # Comparable Level A/B/C view.  An edge can appear at more than one level:
    # a C/Rust wrapper call may be Level B and also target a Level C RTOS API.
    comparison_rows: list[dict[str, Any]] = []
    for case in registry["rows"]:
        number = int(case["thesis_case"])
        for view in ("mechanism_specific", "common_harness", "combined"):
            for level in ("A", "B", "C"):
                for language in ("C", "Rust"):
                    selected = [
                        edge for edge in all_edges.values()
                        if number in edge["cases"] and (("C" if edge.get("language") == "c" else "Rust") == language)
                        and level in edge.get("levels", [])
                        and (view == "combined" or (view == "mechanism_specific" and "mechanism" in edge.get("scopes", [])) or (view == "common_harness" and "common" in edge.get("scopes", [])))
                    ]
                    ids = {edge["call_site_id"] for edge in selected}
                    args = [row for row in argument_rows if row["call_site_id"] in ids and level in row.get("levels", [])]
                    checks = [row for row in check_rows if number in row["cases"] and (("C" if row.get("language") == "c" else "Rust") == language) and level in row.get("level_membership", []) and (view == "combined" or (view == "mechanism_specific" and "mechanism" in row.get("scopes", [])) or (view == "common_harness" and "common" in row.get("scopes", [])))]
                    callback_count = 0 if view == "common_harness" else sum(1 for row in callback_rows if row.get("benchmark_case") == number and row.get("language", "").lower() == language.lower() and level == "B")
                    comparison_rows.append(_metric_row(case, view, level, language, selected, args, checks, callback_count))

    # Per-case coverage and compact comparison.  These rows use occurrence or
    # per-case-membership units as named in the column prefixes.
    case_coverage_rows: list[dict[str, Any]] = []
    case_comparison_rows: list[dict[str, Any]] = []
    for case in registry["rows"]:
        number = int(case["thesis_case"])
        source_case = profile_cases.get(number, case)
        meta = case_meta[number]
        case_roots = [row for row in root_rows if row["benchmark_case"] == number]
        c_roots = [row for row in case_roots if row["language"] == "c" and row["root_kind"] in ("c_adapter", "interrupt_handler", "c_bridge")]
        r_roots = [row for row in case_roots if row["language"] == "rust" and row["root_kind"] == "rust_adapter"]
        c_root_status = "common_harness_only" if not c_roots and not case["c_root_functions"] else ("unresolved" if any(row["status"] != "resolved_root" for row in c_roots) else "resolved")
        rust_root_status = "common_harness_only" if not r_roots and not case["rust_application_methods"] else ("unresolved" if any(row["status"] != "resolved_root" for row in r_roots) else "resolved")
        callbacks = [row for row in callback_rows if row.get("benchmark_case") == number]
        if not callbacks:
            callback_status = "not_applicable"
        elif any(row.get("resolution_status") != "resolved_callback_definition" for row in callbacks):
            callback_status = "partial"
        else:
            callback_status = "resolved"
        case_ffi = [edge for edge in all_edges.values() if number in edge["cases"] and edge.get("target_kind") == "rust_ffi"]
        if not case_ffi:
            ffi_status = "not_applicable"
        elif any(not edge["resolution_status"].startswith("resolved") for edge in case_ffi):
            ffi_status = "partial"
        else:
            ffi_status = "resolved"
        case_rtos = [row for row in rtos_rows if row["benchmark_case"] == number]
        if not case_rtos:
            rtos_status = "not_applicable"
        elif all(row["match_status"] == "matched_rtos_api" for row in case_rtos):
            rtos_status = "matched"
        elif any(row["match_status"] == "matched_rtos_api" for row in case_rtos):
            rtos_status = "partial"
        else:
            rtos_status = "unmatched"
        limitations = list(source_case.get("external_entry_limitations", []))
        callback_partial = callback_status == "partial"
        unresolved_edges = sum(1 for edge in all_edges.values() if number in edge["cases"] and edge["resolution_status"] == "unresolved")
        unresolved_c = sum(1 for edge in all_edges.values() if number in edge["cases"] and edge["language"] == "c" and edge["resolution_status"] == "unresolved")
        unresolved_rust = sum(1 for edge in all_edges.values() if number in edge["cases"] and edge["language"] == "rust" and edge["resolution_status"] == "unresolved")
        external_evidence = source_case.get("external_entry_evidence")
        if limitations or callback_partial or c_root_status == "unresolved" or rust_root_status == "unresolved":
            external_status = "partial"
        elif external_evidence:
            external_status = "resolved_external_boundary"
        elif case.get("interrupt_roots") or callback_status == "resolved":
            external_status = "resolved"
        else:
            external_status = "absent"
        if c_root_status == "common_harness_only":
            c_path_status = "common_harness_only"
        elif limitations or callback_partial or c_root_status == "unresolved" or unresolved_c:
            c_path_status = "partial"
        else:
            c_path_status = "resolved_within_model"
        if rust_root_status == "common_harness_only":
            rust_path_status = "common_harness_only"
        elif limitations or callback_partial or rust_root_status == "unresolved" or unresolved_rust:
            rust_path_status = "partial"
        else:
            rust_path_status = "resolved_within_model"
        if c_path_status == "common_harness_only" and rust_path_status == "common_harness_only":
            exposure_status = "common_only"
        elif limitations or callback_partial or c_root_status == "unresolved" or rust_root_status == "unresolved" or unresolved_edges:
            exposure_status = "partial"
        else:
            exposure_status = "comparable"
        unresolved_evidence = list(limitations)
        if callback_partial:
            unresolved_evidence.append("unresolved callback registration")
        if c_root_status == "unresolved" or rust_root_status == "unresolved":
            unresolved_evidence.append("unresolved root")
        if unresolved_edges:
            unresolved_evidence.append(f"{unresolved_edges} unresolved call edge(s)")
        case_coverage_rows.append({
            "thesis_case": number,
            "mechanism": case["thesis_name"],
            "c_root_status": c_root_status,
            "rust_root_status": rust_root_status,
            "c_call_path_status": c_path_status,
            "rust_call_path_status": rust_path_status,
            "callback_status": callback_status,
            "ffi_status": ffi_status,
            "rtos_target_status": rtos_status,
            "exposure_status": exposure_status,
            "external_entry_status": external_status,
            "unresolved_evidence": unresolved_evidence,
            "analysis_boundary": source_case.get("analysis_boundary", ""),
            "external_entry_evidence": external_evidence or {},
            "semantic_limitations": case.get("semantic_limitations", []),
        })

        combined_a_c = next((row for row in comparison_rows if row["benchmark_case"] == number and row["view"] == "combined" and row["level"] == "A" and row["language"] == "C"), {})
        combined_a_r = next((row for row in comparison_rows if row["benchmark_case"] == number and row["view"] == "combined" and row["level"] == "A" and row["language"] == "Rust"), {})
        combined_b_c = next((row for row in comparison_rows if row["benchmark_case"] == number and row["view"] == "combined" and row["level"] == "B" and row["language"] == "C"), {})
        combined_b_r = next((row for row in comparison_rows if row["benchmark_case"] == number and row["view"] == "combined" and row["level"] == "B" and row["language"] == "Rust"), {})
        combined_c = next((row for row in comparison_rows if row["benchmark_case"] == number and row["view"] == "combined" and row["level"] == "C" and row["language"] == "C"), {})
        mechanism_checks = sum(1 for row in check_rows if number in row["cases"] and row.get("mechanism_specific"))
        case_comparison_rows.append({
            "thesis_case": number,
            "mechanism": case["thesis_name"],
            "c_application_raw_pointer_arguments": combined_a_c.get("raw_pointer_arguments", 0),
            "c_dispatch_context_arguments": combined_a_c.get("dispatch_context_arguments", 0),
            "c_output_pointer_arguments": combined_a_c.get("output_pointer_arguments", 0),
            "c_buffer_pointer_arguments": combined_a_c.get("buffer_pointer_arguments", 0),
            "rust_application_raw_pointer_arguments": combined_a_r.get("raw_pointer_arguments", 0),
            "rust_buffer_pointer_arguments": combined_a_r.get("buffer_pointer_arguments", 0),
            "c_application_explicit_size_memberships": _size_memberships(argument_rows, "c", "explicit_size", "A", number),
            "rust_application_explicit_size_memberships": _size_memberships(argument_rows, "rust", "explicit_size", "A", number),
            "c_application_derived_size_memberships": _size_memberships(argument_rows, "c", "derived_size", "A", number),
            "rust_application_derived_size_memberships": _size_memberships(argument_rows, "rust", "derived_size", "A", number),
            "c_all_level_explicit_size_memberships": _size_memberships(argument_rows, "c", "explicit_size", None, number),
            "rust_all_level_explicit_size_memberships": _size_memberships(argument_rows, "rust", "explicit_size", None, number),
            "c_all_level_derived_size_memberships": _size_memberships(argument_rows, "c", "derived_size", None, number),
            "rust_all_level_derived_size_memberships": _size_memberships(argument_rows, "rust", "derived_size", None, number),
            "callback_registrations": len(callbacks),
            "rtos_target_relationships": len(case_rtos),
            "selected_bridge_relationships": len([row for row in bridge_rows if row["benchmark_case"] == number]),
            "c_wrapper_operations": combined_b_c.get("call_sites", 0),
            "rust_wrapper_operations": combined_b_r.get("call_sites", 0),
            "unsafe_context_memberships": combined_c.get("unsafe_context_memberships", 0) + combined_b_r.get("unsafe_context_memberships", 0),
            "common_harness_check_memberships": sum(1 for row in check_rows if number in row["cases"] and row.get("common_harness")),
            "mechanism_specific_check_memberships": mechanism_checks,
            "coverage_status": "partial" if exposure_status == "partial" else ("common_harness_only" if exposure_status == "common_only" else "resolved_within_model"),
            "exposure_status": exposure_status,
            "units": "per-case memberships; size fields are language- and level-specific; unsafe_context_memberships are lexical Rust call memberships; check memberships are per-case; target/bridge relationships are counts",
            "semantic_limitations": case.get("semantic_limitations", []),
        })

    coverage_by_case = {int(row["thesis_case"]): row for row in case_coverage_rows}
    for row in coverage_rows:
        cov = coverage_by_case.get(int(row["benchmark_case"]), {})
        row["external_entry_status"] = cov.get("external_entry_status")
        row["exposure_status"] = cov.get("exposure_status")
        row["callback_status"] = cov.get("callback_status")
        if cov.get("exposure_status") == "partial":
            row["coverage_status"] = "partial"
        elif cov.get("exposure_status") == "common_only":
            row["coverage_status"] = "common_harness_only"
        elif cov.get("exposure_status") == "comparable" and row.get("coverage_status") == "complete_direct_path":
            row["coverage_status"] = "resolved_within_model"

    # Profile-only evidence: aliases, semantic limitations and the 26-case
    # availability regression.  Case 19 is explicitly absent, not zero.
    alias_rows: list[dict[str, Any]] = []
    if profile:
        for alias in profile.get("api", {}).get("aliases", []):
            source_locations = alias.get("source_locations", [])
            source_texts = []
            for location in source_locations:
                path = repo_root / location.split(":", 1)[0]
                if path.is_file():
                    source_texts.append(path.read_text())
            verified = bool(source_texts) and any(alias["c_symbol"] in text or alias["rust_symbol"] in text for text in source_texts)
            alias_rows.append({
                "profile": profile["id"],
                "rtos": profile["rtos"],
                "c_symbol": alias["c_symbol"],
                "rust_symbol": alias["rust_symbol"],
                "canonical_target": alias["canonical"],
                "justification": alias.get("justification"),
                "source_locations": source_locations,
                "verification_status": "source_backed" if verified else "unresolved",
            })
    semantic_rows: list[dict[str, Any]] = []
    for case in registry["rows"]:
        for limitation in case.get("semantic_limitations", []):
            semantic_rows.append({
                "thesis_case": case["thesis_case"],
                "thesis_name": case["thesis_name"],
                "limitation": limitation,
                "source_evidence": "firmware/c-kernels/nuttx/c/port.c" if profile and profile["rtos"] == "NuttX" and case["thesis_case"] == 9 else None,
                "effect": "semantic qualification only; source-level call/handle/pointer evidence remains valid",
            })
    full_cases = load_cases(repo_root, safety)["cases"]
    selected_numbers = {int(row["thesis_case"]) for row in registry["rows"]}
    absent_numbers = {int(item["thesis_case"]): item for item in (profile or {}).get("absent_cases", [])}
    availability_rows = []
    for full_case in full_cases:
        number = int(full_case["thesis_case"])
        if number in selected_numbers:
            source_status = "available"
            pilot_status = "selected"
            evidence = "verified in the selected source registry"
        elif number in absent_numbers:
            source_status = "absent"
            pilot_status = "absent_not_quantified"
            evidence = absent_numbers[number].get("reason")
        else:
            source_status = "not_assessed"
            pilot_status = "outside_pilot_scope"
            evidence = "not selected for the five-case pilot"
        availability_rows.append({
            "thesis_case": number,
            "mechanism": full_case["thesis_name"],
            "c_enum": full_case["c_enum"],
            "c_wire_id": full_case["c_wire_id"],
            "source_status": source_status,
            "pilot_status": pilot_status,
            "evidence": evidence,
        })

    # Coverage and summaries.
    rtos_relationships = len(rtos_rows)
    matched_rtos = sum(1 for row in rtos_rows if row["match_status"] == "matched_rtos_api")
    distinct_rtos = len({row["target_name"] for row in rtos_rows})
    bridge_definitions = len(bridge_rows)
    matched_bridges = sum(1 for row in bridge_rows if row["match_status"] == "matched_c_bridge_definition")
    unused_bridges = sum(1 for row in bridge_rows if row["match_status"] == "unused_c_bridge_definition")
    common_edges = sum(1 for edge in all_edges.values() if edge.get("common_harness"))
    mechanism_edges = sum(1 for edge in all_edges.values() if edge.get("mechanism_specific"))
    distinct_source_sites = len({edge["source_occurrence_id"] for edge in all_edges.values()})
    distinct_source_checks = len({row["source_check_id"] for row in check_rows})
    unresolved_edges = [edge["call_site_id"] for edge in all_edges.values() if edge["resolution_status"] == "unresolved"]
    coverage = {
        "schema": "uastw-matched-api-coverage-v3" if profile else "uastw-matched-api-coverage-v2",
        "version": spec["version"],
        "selected_cases": len(registry["rows"]),
        "expected_cases": expected_cases,
        "verified_roots": sum(1 for row in registry["rows"] if row["match_status"] == "verified_from_registry"),
        "resolved_c_roots": sum(1 for row in root_rows if row["language"] == "c" and row["status"] == "resolved_root"),
        "resolved_rust_roots": sum(1 for row in root_rows if row["language"] == "rust" and row["status"] == "resolved_root"),
        "unresolved_root_rows": [row for row in root_rows if row["status"] != "resolved_root"],
        "call_site_occurrences": len(all_edges),
        "configuration_specific_call_sites": len(all_edges),
        "per_case_call_memberships": sum(len(edge["cases"]) for edge in all_edges.values()),
        "distinct_source_call_sites": distinct_source_sites,
        "common_harness_call_sites": common_edges,
        "mechanism_specific_call_sites": mechanism_edges,
        "resolved_call_sites": sum(1 for edge in all_edges.values() if edge["resolution_status"].startswith("resolved")),
        "unresolved_call_sites": len(unresolved_edges),
        "unsafe_context_call_occurrences": sum(1 for edge in all_edges.values() if edge.get("in_unsafe_context") == "true"),
        "unsafe_context_per_case_memberships": sum(len(edge["cases"]) for edge in all_edges.values() if edge.get("in_unsafe_context") == "true"),
        "unsafe_context_distinct_source_identities": len({edge["source_occurrence_id"] for edge in all_edges.values() if edge.get("in_unsafe_context") == "true"}),
        "unsafe_context_scope": "lexical Rust call membership; not intrinsic unsafety",
        "ffi_symbols": len(ffi),
        "resolved_ffi_symbols": sum(1 for row in ffi if row["resolution_status"].startswith("resolved")),
        "unresolved_ffi_symbols": [row for row in ffi if row["resolution_status"] == "unresolved"],
        "ambiguous_ffi_symbols": [row for row in ffi if row["resolution_status"].startswith("ambiguous")],
        "ffi_symbols_in_selected_path": len({(edge.get("raw_target_name") or edge["target_name"]) for edge in all_edges.values() if edge.get("language") == "rust" and edge.get("target_kind") == "rust_ffi"}),
        "arguments_rows": len(argument_rows),
        "unknown_argument_roles": sum(1 for row in argument_rows if row["unknown_role"]),
        "c_all_level_explicit_size_occurrences": _size_occurrences(argument_rows, "c", "explicit_size"),
        "rust_all_level_explicit_size_occurrences": _size_occurrences(argument_rows, "rust", "explicit_size"),
        "c_all_level_explicit_size_memberships": _size_memberships(argument_rows, "c", "explicit_size"),
        "rust_all_level_explicit_size_memberships": _size_memberships(argument_rows, "rust", "explicit_size"),
        "c_all_level_derived_size_occurrences": _size_occurrences(argument_rows, "c", "derived_size"),
        "rust_all_level_derived_size_occurrences": _size_occurrences(argument_rows, "rust", "derived_size"),
        "c_all_level_derived_size_memberships": _size_memberships(argument_rows, "c", "derived_size"),
        "rust_all_level_derived_size_memberships": _size_memberships(argument_rows, "rust", "derived_size"),
        "c_application_explicit_size_occurrences": _size_occurrences(argument_rows, "c", "explicit_size", "A"),
        "rust_application_explicit_size_occurrences": _size_occurrences(argument_rows, "rust", "explicit_size", "A"),
        "c_application_explicit_size_memberships": _size_memberships(argument_rows, "c", "explicit_size", "A"),
        "rust_application_explicit_size_memberships": _size_memberships(argument_rows, "rust", "explicit_size", "A"),
        "c_application_derived_size_occurrences": _size_occurrences(argument_rows, "c", "derived_size", "A"),
        "rust_application_derived_size_occurrences": _size_occurrences(argument_rows, "rust", "derived_size", "A"),
        "c_application_derived_size_memberships": _size_memberships(argument_rows, "c", "derived_size", "A"),
        "rust_application_derived_size_memberships": _size_memberships(argument_rows, "rust", "derived_size", "A"),
        "check_occurrences": sum(1 for row in check_rows if row["check_status"] == "observed"),
        "distinct_source_checks": distinct_source_checks,
        "common_harness_check_occurrences": sum(1 for row in check_rows if row.get("common_harness")),
        "mechanism_specific_check_occurrences": sum(1 for row in check_rows if row.get("mechanism_specific")),
        "common_harness_check_memberships": sum(len(row["cases"]) for row in check_rows if row.get("common_harness")),
        "mechanism_specific_check_memberships": sum(len(row["cases"]) for row in check_rows if row.get("mechanism_specific")),
        "checks_unresolved": sum(1 for row in check_rows if row["check_status"] != "observed"),
        "macro_sites_observed": len(macro_rows_by_id),
        "unresolved_macro_sites": sum(1 for row in macro_rows_by_id.values() if row["resolution_status"] == "unresolved"),
        "rtos_api_target_relationships": rtos_relationships,
        "matched_rtos_api_targets": matched_rtos,
        "distinct_rtos_api_targets": distinct_rtos,
        "rtos_api_c_only_targets": sum(1 for row in rtos_rows if row["match_status"] == "c_only_rtos_api"),
        "rtos_api_rust_only_targets": sum(1 for row in rtos_rows if row["match_status"] == "rust_only_rtos_api"),
        "selected_c_bridge_relationships": bridge_definitions,
        "selected_distinct_c_bridge_definitions": len({row["target_name"] for row in bridge_rows}),
        "selected_matched_c_bridge_relationships": matched_bridges,
        "selected_matched_distinct_c_bridge_definitions": len({row["target_name"] for row in bridge_rows if row["match_status"] == "matched_c_bridge_definition"}),
        "selected_unused_c_bridge_definitions": unused_bridges,
        "c_bridge_inventory_scope": "selected/configured/reached Stage 2 bridge definitions",
        "c_bridge_inventory_includes_all_c_support_definitions": False,
        "callback_registrations": len(callback_rows),
        "resolved_callback_registrations": sum(1 for row in callback_rows if row["resolution_status"] == "resolved_callback_definition"),
        "partial_callback_registrations": sum(1 for row in callback_rows if row["resolution_status"] != "resolved_callback_definition"),
        "partial_path_cases": sum(1 for row in coverage_rows if row["coverage_status"] == "partial"),
        "partial_callback_cases": sum(1 for row in coverage_rows if row["coverage_status"] == "partial_external_callback"),
        "case_coverage_partial": sum(1 for row in case_coverage_rows if row["exposure_status"] == "partial"),
        "case_coverage_common_only": sum(1 for row in case_coverage_rows if row["exposure_status"] == "common_only"),
        "case_coverage_comparable": sum(1 for row in case_coverage_rows if row["exposure_status"] == "comparable"),
        "external_entry_partial_cases": sum(1 for row in case_coverage_rows if row["external_entry_status"] == "partial"),
        "resolved_external_callback_cases": sum(1 for row in coverage_rows if row["coverage_status"] == "complete_with_resolved_external_callback"),
        "parse_failures": len([row for row in c_bench["diagnostics"] + c_support["diagnostics"] if row["severity"] in ("3", "4")]),
        "limitations": [
            "Syntactic call paths are not complete runtime reachability.",
            "C macro calls record source invocation and resolved target separately; macro arity transformations may leave argument mapping partially unresolved.",
            "Rust resolution is syntax-based and conservative; aliases, generics and non-self method targets can remain unresolved.",
            "FFI symbol matching proves source-level pairing only, not ABI or behavioural compatibility.",
            "Checks are observed patterns within supported expressions; absence is not evidence that validation is missing.",
            "Unsafe-call context is lexical and is not intrinsic unsafety.",
            "Wrapper-internal operations are not application-level operations.",
            "Shared C ISR implementation in case 16 is not a Rust-native ISR.",
            "Callback registration does not prove runtime execution.",
        ],
    }
    if profile:
        coverage.update({
            "profile": profile["id"],
            "rtos": profile["rtos"],
            "board": profile["board"],
            "target_alias_relationships": len(alias_rows),
            "verified_target_aliases": sum(1 for row in alias_rows if row["verification_status"] == "source_backed"),
            "absent_case_19": any(int(row["thesis_case"]) == 19 and row["source_status"] == "absent" for row in availability_rows),
            "absent_cases": [int(row["thesis_case"]) for row in availability_rows if row["source_status"] == "absent"],
            "implemented_unassessed_cases": [int(row["thesis_case"]) for row in availability_rows if row["source_status"] == "not_assessed"],
            "absent_case_count": sum(1 for row in availability_rows if row["source_status"] == "absent"),
            "implemented_unassessed_count": sum(1 for row in availability_rows if row["source_status"] == "not_assessed"),
            "availability_rows": len(availability_rows),
        })

    # FFI selected-path attribution.
    reached_ffi = {(edge.get("raw_target_name") or edge["target_name"]) for edge in all_edges.values() if edge.get("language") == "rust" and edge.get("target_kind") == "rust_ffi"}
    for row in ffi:
        row["in_selected_path"] = row["rust_name"] in reached_ffi
    coverage["resolved_ffi_symbols_in_selected_path"] = sum(1 for row in ffi if row["in_selected_path"] and row["resolution_status"].startswith("resolved"))

    # Outputs.
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    root_fields = ("benchmark_case", "thesis_name", "mechanism", "c_enum", "c_enum_value", "c_wire_id", "rust_variant", "rust_discriminant", "rust_wire_id", "c_root_functions", "rust_application_methods", "c_application_methods", "common_c_application_methods", "common_rust_application_methods", "c_bridge_roots", "interrupt_roots", "c_port_function_map", "resolution_method", "match_status", "thesis_spec_line", "c_enum_header_line", "c_wire_line", "rust_enum_line", "rust_wire_line", "checks")
    root_rows_out = [{**row, "benchmark_case": row["thesis_case"]} for row in registry["rows"]]
    _write_csv(output / "benchmark-roots.csv", root_fields, root_rows_out)
    call_site_fields = ("call_site_id", "source_occurrence_id", "configuration", "language", "role", "roles", "level_membership", "scope", "scopes", "caller", "callee_kind", "target_category", "target_name") + (("raw_target_name",) if profile else ()) + ("source_file", "source_line", "source_column", "source_expression", "source_args", "call_context", "resolution_status", "return_handling", "in_unsafe_context", "unsafe_context_ids", "common_harness", "case_count", "levels")
    call_site_rows = [
        {**edge, "case_count": len(edge.get("cases", [])), "role": edge["roles"][0] if edge.get("roles") else ""}
        for edge in edge_rows
    ]
    call_site_rows.sort(key=lambda row: (row["language"], row["source_file"] or "", row["source_line"] or 0, row["source_column"] or 0, row["target_name"] or ""))
    _write_csv(output / "call-sites.csv", call_site_fields, call_site_rows)
    membership_fields = ("call_site_id", "source_occurrence_id", "benchmark_case", "thesis_name", "configuration", "language", "roles", "levels", "scope", "scopes", "target_name", "source_file", "source_line", "source_column")
    membership_rows = []
    for edge in edge_rows:
        for case_number in edge.get("cases", []):
            membership_rows.append({
                **edge,
                "benchmark_case": case_number,
                "thesis_name": next((row["thesis_name"] for row in registry["rows"] if row["thesis_case"] == case_number), ""),
                "roles": edge.get("roles", []),
                "levels": edge.get("levels", []),
            })
    membership_rows.sort(key=lambda row: (row["benchmark_case"], row["language"], row["source_file"] or "", row["source_line"] or 0, row["target_name"] or ""))
    _write_csv(output / "case-memberships.csv", membership_fields, membership_rows)
    edge_fields = ("call_site_id", "source_occurrence_id", "configuration", "language", "caller_id", "callee_id", "caller", "target_name") + (("raw_target_name",) if profile else ()) + ("source_file", "source_line", "source_column", "callee_kind", "target_category", "resolution_status", "evidence_method", "call_context", "return_handling", "roles", "levels", "level_a", "level_b", "level_c", "scopes", "common_harness", "root_kinds", "in_unsafe_context", "unsafe_context_ids", "bridge_root", "callback_registration_id")
    _write_csv(output / "call-edges.csv", edge_fields, edge_rows)
    function_fields = ("function_id", "configuration", "language", "name", "file", "line", "kind", "is_project", "unsafe", "active", "signature", "params", "return_type", "c_target_file", "c_target_line", "resolution_status", "evidence_method")
    _write_csv(output / "function-signatures.csv", function_fields, function_rows)
    ffi_fields = ("configuration", "rust_name", "in_selected_path", "rust_file", "rust_line", "rust_abi", "rust_params", "rust_return", "c_target_name", "c_target_file", "c_target_line", "c_target_signature", "c_target_kind", "resolution_status", "candidate_count", "evidence_method")
    _write_csv(output / "ffi-symbols.csv", ffi_fields, ffi)
    argument_fields = ("call_site_id", "source_occurrence_id", "cases", "roles", "levels", "scopes", "language", "position", "source_expression", "parameter_name", "parameter_type", "parameter_type_kind", "mapping_status", "role", "raw_pointer", "raw_data_pointer", "address_of", "explicit_cast", "pointer_typed", "buffer_pointer", "handle", "dispatch_context", "output_pointer", "callback", "explicit_size", "derived_size", "timeout_or_flags", "flag_or_type", "unknown_role", "semantic_claim")
    _write_csv(output / "call-arguments.csv", argument_fields, argument_rows)
    check_fields = ("check_id", "source_check_id", "cases", "configuration", "language", "function", "roles", "scope", "common_harness", "mechanism_specific", "source_file", "source_line", "source_column", "end_line", "end_column", "kind", "condition", "condition_shape", "check_kind", "null_check", "zero_comparison", "numeric_zero_check", "size_or_bound_check", "status_or_error_check", "call_result_check", "ordering_check", "flag_check", "logical_check", "dispatch_check", "check_status", "evidence_method", "level_membership")
    _write_csv(output / "checks.csv", check_fields, check_rows)
    rtos_fields = ("benchmark_case", "thesis_name", "target_category", "target_name", "c_target_api", "rust_target_api") + (("raw_c_target_names", "raw_rust_target_names") if profile else ()) + ("match_status", "c_call_path_ids", "rust_language_path_ids", "rust_cross_language_path_ids", "c_path_origins", "rust_path_origins", "unresolved_edges")
    _write_csv(output / "rtos-target-relationships.csv", rtos_fields, rtos_rows)
    bridge_fields = ("benchmark_case", "thesis_name", "target_category", "target_name", "match_status", "c_definition_file", "c_definition_line", "c_call_site_ids", "rust_language_path_ids", "rust_cross_language_path_ids", "body_rtos_targets", "body_traversed", "reachable_by", "inventory_scope", "c_path_origins", "rust_path_origins", "unresolved_edges")
    _write_csv(output / "c-bridge-definitions.csv", bridge_fields, bridge_rows)
    matched_rows = rtos_rows + bridge_rows
    matched_fields = ("benchmark_case", "thesis_name", "target_category", "target_name", "c_target_api", "rust_target_api", "match_status", "c_call_path_ids", "rust_language_path_ids", "rust_cross_language_path_ids", "c_definition_file", "c_definition_line", "body_rtos_targets", "body_traversed", "reachable_by", "c_path_origins", "rust_path_origins", "unresolved_edges")
    _write_csv(output / "matched-operations.csv", matched_fields, matched_rows)
    exposure_fields = ("benchmark_case", "thesis_name", "role", "scope", "case_call_sites", "distinct_source_call_sites", "raw_pointer_arguments", "raw_data_pointer_arguments", "dispatch_context_arguments", "output_pointer_arguments", "buffer_pointer_arguments", "handle_arguments", "function_pointer_callback_arguments", "explicit_size_memberships", "derived_size_memberships", "unsafe_context_memberships", "check_memberships", "unresolved_call_sites")
    _write_csv(output / "exposure-summary.csv", exposure_fields, exposure_rows)
    comparison_fields = ("benchmark_case", "thesis_name", "view", "level", "language", "call_sites", "distinct_source_call_sites", "raw_pointer_arguments", "raw_data_pointer_arguments", "dispatch_context_arguments", "output_pointer_arguments", "buffer_pointer_arguments", "handle_arguments", "function_pointer_callback_arguments", "explicit_size_memberships", "derived_size_memberships", "callback_registrations", "project_wrapper_operations", "underlying_rtos_calls", "unsafe_context_memberships", "check_memberships", "unresolved_call_sites", "partial_call_sites")
    _write_csv(output / "exposure-comparison.csv", comparison_fields, comparison_rows)
    case_coverage_fields = ("thesis_case", "mechanism", "c_root_status", "rust_root_status", "c_call_path_status", "rust_call_path_status", "callback_status", "ffi_status", "rtos_target_status", "exposure_status", "external_entry_status", "unresolved_evidence", "analysis_boundary") + (("semantic_limitations", "external_entry_evidence") if profile else ())
    _write_csv(output / "case-coverage.csv", case_coverage_fields, case_coverage_rows)
    case_comparison_fields = (
        "thesis_case", "mechanism",
        "c_application_raw_pointer_arguments", "c_dispatch_context_arguments", "c_output_pointer_arguments", "c_buffer_pointer_arguments",
        "rust_application_raw_pointer_arguments", "rust_buffer_pointer_arguments",
        "c_application_explicit_size_memberships", "rust_application_explicit_size_memberships",
        "c_application_derived_size_memberships", "rust_application_derived_size_memberships",
        "c_all_level_explicit_size_memberships", "rust_all_level_explicit_size_memberships",
        "c_all_level_derived_size_memberships", "rust_all_level_derived_size_memberships",
        "callback_registrations", "rtos_target_relationships", "selected_bridge_relationships",
        "c_wrapper_operations", "rust_wrapper_operations", "unsafe_context_memberships",
        "common_harness_check_memberships", "mechanism_specific_check_memberships",
        "coverage_status", "exposure_status", "units",
    ) + (("semantic_limitations",) if profile else ())
    _write_csv(output / "case-comparison.csv", case_comparison_fields, case_comparison_rows)
    callback_fields = ("benchmark_case", "language", "configuration", "source_scope", "bridge_root", "registration_call_site_id", "application_registration_call_site_id", "caller", "source_file", "source_line", "api", "callback_symbol", "callback_file", "callback_line", "forwarding_parameter", "invocation_kind", "resolution_status", "body_root_status", "call_site_ids")
    if profile:
        _write_csv(output / "target-aliases.csv", ("profile", "rtos", "c_symbol", "rust_symbol", "canonical_target", "verification_status", "justification", "source_locations"), alias_rows)
        _write_csv(output / "semantic-limitations.csv", ("thesis_case", "thesis_name", "limitation", "source_evidence", "effect"), semantic_rows)
        _write_csv(output / "case-availability.csv", ("thesis_case", "mechanism", "c_enum", "c_wire_id", "source_status", "pilot_status", "evidence"), availability_rows)
    _write_csv(output / "callback-registrations.csv", callback_fields, callback_rows)
    coverage_fields = ("benchmark_case", "thesis_name", "coverage_status", "external_entry_status", "exposure_status", "callback_status", "callback_registrations", "resolved_callbacks", "partial_callbacks", "external_isr_roots", "shared_c_isr", "unresolved_call_sites", "common_harness_calls")
    _write_csv(output / "path-coverage.csv", coverage_fields, coverage_rows)

    if profile:
        def _configuration_source(config: dict[str, Any]) -> dict[str, Any]:
            if "translation_units" in config:
                return {
                    "board": config.get("board"),
                    "kernel": config.get("kernel"),
                    "rust_mode": config.get("rust_mode"),
                    "compile_commands": config.get("compile_commands"),
                    "compile_commands_sha256": config.get("compile_commands_sha256"),
                    "build_id": config.get("build_id"),
                    "source_scope": config.get("source_scope"),
                    "rebuild_evidence": config.get("rebuild_evidence"),
                    "target": config.get("target"),
                    "files": [{"path": unit["path"], "sha256": _sha256(repo_root / unit["path"]), "has_bench_language_rust": unit.get("has_bench_language_rust"), "derived_compile_unit": unit.get("derived_compile_unit")} for unit in config["translation_units"]],
                }
            return {
                "board": config.get("board"),
                "kernel": config.get("kernel"),
                "target": config.get("target"),
                "crate_roots": config.get("crate_roots"),
                "source_scope": config.get("source_scope"),
                "files": [{"path": path, "sha256": _sha256(repo_root / path)} for path in config.get("files", [])],
            }

        sources = {
            "schema": "uastw-matched-api-sources-v3",
            "source_revision": discovery.get("source_revision"),
            "profile": profile.get("id"),
            "rtos": profile.get("rtos"),
            "board": profile.get("board"),
            "external_source": discovery.get("external_source"),
            "worktree_dirty": discovery.get("worktree_dirty"),
            "worktree_status_count": discovery.get("worktree_status_count"),
            "configurations": {
                c_bench_config["configuration"]: _configuration_source(c_bench_config),
                c_support_config["configuration"]: _configuration_source(c_support_config),
                rust_config["configuration"]: _configuration_source(rust_config),
            },
            "generated_headers": discovery.get("generated_headers"),
            "config_provenance": discovery.get("config_provenance"),
            "stage1": _verify_stage1(repo_root),
            "tools": discovery.get("tools", tool_versions(repo_root)),
        }
    else:
        sources = {
            "schema": "uastw-matched-api-sources-v2",
            "source_revision": discovery.get("source_revision"),
            "configurations": {
                c_bench_config["configuration"]: {"files": [{"path": unit["path"], "sha256": _sha256(repo_root / unit["path"])} for unit in c_bench_config["translation_units"]]},
                c_support_config["configuration"]: {"files": [{"path": unit["path"], "sha256": _sha256(repo_root / unit["path"])} for unit in c_support_config["translation_units"]]},
                rust_config["configuration"]: {"files": [{"path": path, "sha256": _sha256(repo_root / path)} for path in rust_config["files"]], "target": rust_config["target"]},
            },
            "stage1": _verify_stage1(repo_root),
            "tools": tool_versions(repo_root),
        }
    _write_json(output / "sources.json", sources)
    _write_json(output / "coverage.json", coverage)
    summary = {
        "schema": "uastw-matched-api-summary-v3" if profile else "uastw-matched-api-summary-v2",
        "version": spec["version"],
        "selected_cases": [row["thesis_case"] for row in registry["rows"]],
        "matched_roots": {row["thesis_case"]: row["match_status"] for row in registry["rows"]},
        "rtos_api_target_relationships": rtos_relationships,
        "matched_rtos_api_targets": matched_rtos,
        "distinct_rtos_api_targets": distinct_rtos,
        "selected_c_bridge_relationships": bridge_definitions,
        "selected_distinct_c_bridge_definitions": len({row["target_name"] for row in bridge_rows}),
        "selected_matched_c_bridge_relationships": matched_bridges,
        "selected_matched_distinct_c_bridge_definitions": len({row["target_name"] for row in bridge_rows if row["match_status"] == "matched_c_bridge_definition"}),
        "selected_unused_c_bridge_definitions": unused_bridges,
        "c_bridge_inventory_scope": "selected/configured/reached Stage 2 bridge definitions",
        "c_bridge_inventory_includes_all_c_support_definitions": False,
        "call_site_occurrences": len(all_edges),
        "configuration_specific_call_sites": len(all_edges),
        "distinct_source_call_sites": distinct_source_sites,
        "per_case_call_memberships": coverage["per_case_call_memberships"],
        "common_harness_call_sites": common_edges,
        "mechanism_specific_call_sites": mechanism_edges,
        "argument_rows": len(argument_rows),
        "unknown_argument_roles": coverage["unknown_argument_roles"],
        "c_application_explicit_size_occurrences": coverage["c_application_explicit_size_occurrences"],
        "rust_application_explicit_size_occurrences": coverage["rust_application_explicit_size_occurrences"],
        "c_application_explicit_size_memberships": coverage["c_application_explicit_size_memberships"],
        "rust_application_explicit_size_memberships": coverage["rust_application_explicit_size_memberships"],
        "c_application_derived_size_occurrences": coverage["c_application_derived_size_occurrences"],
        "rust_application_derived_size_occurrences": coverage["rust_application_derived_size_occurrences"],
        "c_application_derived_size_memberships": coverage["c_application_derived_size_memberships"],
        "rust_application_derived_size_memberships": coverage["rust_application_derived_size_memberships"],
        "c_all_level_explicit_size_occurrences": coverage["c_all_level_explicit_size_occurrences"],
        "rust_all_level_explicit_size_occurrences": coverage["rust_all_level_explicit_size_occurrences"],
        "c_all_level_explicit_size_memberships": coverage["c_all_level_explicit_size_memberships"],
        "rust_all_level_explicit_size_memberships": coverage["rust_all_level_explicit_size_memberships"],
        "c_all_level_derived_size_occurrences": coverage["c_all_level_derived_size_occurrences"],
        "rust_all_level_derived_size_occurrences": coverage["rust_all_level_derived_size_occurrences"],
        "c_all_level_derived_size_memberships": coverage["c_all_level_derived_size_memberships"],
        "rust_all_level_derived_size_memberships": coverage["rust_all_level_derived_size_memberships"],
        "unsafe_context_call_occurrences": coverage["unsafe_context_call_occurrences"],
        "unsafe_context_per_case_memberships": coverage["unsafe_context_per_case_memberships"],
        "unsafe_context_distinct_source_identities": coverage["unsafe_context_distinct_source_identities"],
        "unsafe_context_scope": coverage["unsafe_context_scope"],
        "check_occurrences": coverage["check_occurrences"],
        "distinct_source_checks": distinct_source_checks,
        "common_harness_check_occurrences": coverage["common_harness_check_occurrences"],
        "mechanism_specific_check_occurrences": coverage["mechanism_specific_check_occurrences"],
        "common_harness_check_memberships": coverage["common_harness_check_memberships"],
        "mechanism_specific_check_memberships": coverage["mechanism_specific_check_memberships"],
        "callback_registrations": len(callback_rows),
        "partial_path_cases": coverage["partial_path_cases"],
        "case_coverage_partial": coverage["case_coverage_partial"],
        "case_coverage_comparable": coverage["case_coverage_comparable"],
        "case_coverage_common_only": coverage["case_coverage_common_only"],
        "external_entry_partial_cases": coverage["external_entry_partial_cases"],
        "unresolved_macro_sites": coverage["unresolved_macro_sites"],
        "ffi_symbols": len(ffi),
        "unresolved_ffi_symbols": len(coverage["unresolved_ffi_symbols"]),
        "exposure": comparison_rows,
        "note": "Counts are source-level interface observations. They are not runtime operations, vulnerability counts or safety scores.",
    }
    if profile:
        summary.update({
            "profile": profile["id"],
            "rtos": profile["rtos"],
            "board": profile["board"],
            "target_aliases": alias_rows,
            "absence_regression": next((row for row in availability_rows if int(row["thesis_case"]) == 19 and row["source_status"] == "absent"), None),
            "semantic_limitations": semantic_rows,
            "external_source": discovery.get("external_source"),
        })
    _write_json(output / "summary.json", summary)
    macro_fields = ("macro_site_id", "case_attribution", "cases", "configuration", "language", "function", "source_file", "source_line", "source_column", "macro", "macro_effect", "common_harness", "resolution_status", "reason")
    _write_csv(output / "macro-sites.csv", macro_fields, sorted(macro_rows_by_id.values(), key=lambda row: (row["source_file"] or "", row["source_line"] or 0, row["function"] or "", row["macro"] or "")))

    # Validation.
    errors = []
    if coverage["selected_cases"] != coverage["expected_cases"]:
        errors.append("selected case count does not match the requested profile")
    if coverage["verified_roots"] != coverage["selected_cases"]:
        errors.append("not all selected benchmark roots verified")
    if not sources["source_revision"]:
        errors.append("source revision missing")
    if len({edge["call_site_id"] for edge in edge_rows}) != len(edge_rows):
        errors.append("duplicate call site ids")
    if len({row["check_id"] for row in check_rows}) != len(check_rows):
        errors.append("duplicate check ids")
    if any(row["target_category"] == "rtos_api" and row["target_name"] in {bridge["target_name"] for bridge in bridge_rows} for row in rtos_rows):
        errors.append("bridge symbol counted as RTOS target")
    if any(row["match_status"] == "unused_c_bridge_definition" and (row["c_call_site_ids"] or row["body_traversed"]) for row in bridge_rows):
        errors.append("unused bridge contaminates a reachable path")
    if any(row["match_status"] == "matched_rtos_api" and not (row["c_call_path_ids"] and (row["rust_language_path_ids"] or row["rust_cross_language_path_ids"])) for row in rtos_rows):
        errors.append("matched RTOS target without both path sides")
    if any(row["match_status"] == "matched_c_bridge_definition" and not (row["c_definition_file"] and row["rust_language_path_ids"]) for row in bridge_rows):
        errors.append("matched bridge lacks definition or Rust call")
    if not any(row["common_harness"] and row["target_name"] in ("now_ticks", "port_now_ticks") for row in edge_rows):
        errors.append("common now_ticks calls not represented")
    if len({row["thesis_case"] for row in registry["rows"]}) != expected_cases:
        errors.append("selected case count mismatch")
    if not sources["stage1"]["manifest_verified"]:
        errors.append("Stage 1 manifest is not verified")
    if profile:
        absent_numbers = {int(item["thesis_case"]) for item in profile.get("absent_cases", [])}
        selected_numbers = {int(row["thesis_case"]) for row in registry["rows"]}
        if any(row["thesis_case"] in absent_numbers for row in case_coverage_rows):
            errors.append("an absent case entered quantitative results")
        if coverage["availability_rows"] != 26:
            errors.append("26-case availability regression is incomplete")
        if coverage.get("absent_case_count") != len(absent_numbers):
            errors.append("absent case count does not match the profile")
        if coverage["selected_cases"] + coverage.get("implemented_unassessed_count", 0) + coverage.get("absent_case_count", 0) != 26:
            errors.append("availability populations do not reconcile to 26 thesis cases")
        if any(row["verification_status"] != "source_backed" for row in alias_rows):
            errors.append("an RTOS target alias is not source-backed")
        if any(str(diagnostic.get("severity", "")).lower() in ("3", "error", "diagnosticseverity.error") or "error" in str(diagnostic.get("severity", "")).lower() for config in (c_bench, c_support) for diagnostic in config.get("diagnostics", [])):
            errors.append("C parse failures remain")
        if any(root["status"] != "resolved_root" for root in root_rows):
            errors.append("an external or application root is unresolved")
        if not any(row["language"] == "c" and row["invocation_kind"] == "thread_entry" and row["resolution_status"] == "resolved_callback_definition" for row in callback_rows):
            errors.append("no resolved C thread callback")
        if not any(row["language"] == "rust" and row["invocation_kind"] == "thread_entry" and row["resolution_status"] == "resolved_callback_definition" for row in callback_rows):
            errors.append("no resolved Rust thread callback")
        if not any(row["rust_cross_language_path_ids"] for row in rtos_rows):
            errors.append("no C-owned external entry is represented as cross-language evidence")
    validation = {
        "schema": "uastw-matched-api-validation-v3" if profile else "uastw-matched-api-validation-v2",
        "status": "passed" if not errors else "failed",
        "errors": errors[:20],
        "all_selected_roots_verified": coverage["verified_roots"] == coverage["selected_cases"] == coverage["expected_cases"],
        "all_26_roots_verified": coverage["selected_cases"] == 26 and coverage["verified_roots"] == 26,
        "case_coverage_rows": len(case_coverage_rows),
        "source_revision_recorded": bool(sources["source_revision"]),
        "call_site_ids_unique": len({edge["call_site_id"] for edge in edge_rows}) == len(edge_rows),
        "checks_unique": len({row["check_id"] for row in check_rows}) == len(check_rows),
        "rtos_targets_separate_from_bridges": not any(row["target_category"] == "rtos_api" and row["target_name"] in {bridge["target_name"] for bridge in bridge_rows} for row in rtos_rows),
        "unused_bridges_not_traversed": not any(row["match_status"] == "unused_c_bridge_definition" and (row["c_call_site_ids"] or row["body_traversed"]) for row in bridge_rows),
        "matched_rtos_targets_have_both_paths": not any(row["match_status"] == "matched_rtos_api" and not (row["c_call_path_ids"] and (row["rust_language_path_ids"] or row["rust_cross_language_path_ids"])) for row in rtos_rows),
        "matched_bridges_have_definition_and_rust_call": not any(row["match_status"] == "matched_c_bridge_definition" and not (row["c_definition_file"] and row["rust_language_path_ids"]) for row in bridge_rows),
        "common_now_ticks_represented": any(row["common_harness"] and row["target_name"] in ("now_ticks", "port_now_ticks") for row in edge_rows),
        "callback_registrations": len(callback_rows),
        "partial_path_cases": coverage["partial_path_cases"],
        "case_coverage_partial": coverage["case_coverage_partial"],
        "case_coverage_comparable": coverage["case_coverage_comparable"],
        "case_coverage_common_only": coverage["case_coverage_common_only"],
        "external_entry_partial_cases": coverage["external_entry_partial_cases"],
        "stage1_manifest_verified": sources["stage1"]["manifest_verified"],
        "unresolved_records_visible": {
            "unresolved_call_sites": coverage["unresolved_call_sites"],
            "unresolved_ffi_symbols": len(coverage["unresolved_ffi_symbols"]),
            "ambiguous_ffi_symbols": len(coverage["ambiguous_ffi_symbols"]),
            "unresolved_root_rows": len(coverage["unresolved_root_rows"]),
            "partial_callback_registrations": coverage["partial_callback_registrations"],
        },
    }
    if profile:
        validation.update({
            "profile": profile["id"],
            "selected_case_count": coverage["selected_cases"],
            "case_19_absent_not_zero": (19 in absent_numbers) or (19 in selected_numbers),
            "case_19_status": "absent" if 19 in absent_numbers else ("selected" if 19 in selected_numbers else "unavailable"),
            "availability_population": {
                "selected": coverage["selected_cases"],
                "implemented_unassessed": coverage.get("implemented_unassessed_count"),
                "absent": coverage.get("absent_case_count"),
            },
            "target_aliases_source_backed": all(row["verification_status"] == "source_backed" for row in alias_rows),
            "profile_parse_failures": sum(1 for config in (c_bench, c_support) for diagnostic in config.get("diagnostics", []) if str(diagnostic.get("severity", "")).lower() in ("3", "error", "diagnosticseverity.error") or "error" in str(diagnostic.get("severity", "")).lower()),
            "profile_configuration_count": len(discovery["configurations"]),
        })
    _write_json(output / "validation.json", validation)
    if errors:
        raise SystemExit("matched-api validation failed:\n" + "\n".join(errors[:20]))

    # Manifest.
    outputs = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "manifest.json":
            outputs.append({"path": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size})
    manifest = {
        "schema": "uastw-matched-api-manifest-v3" if profile else "uastw-matched-api-manifest-v2",
        "source_revision": sources["source_revision"],
        "version": spec["version"],
        "outputs": outputs,
        "notes": ["Canonical rows and JSON keys are sorted; timestamps are omitted."],
    }
    _write_json(output / "manifest.json", manifest)
    return {
        "output": str(output),
        "summary": summary,
        "coverage": coverage,
        "validation": validation,
        "rtos_rows": len(rtos_rows),
        "bridge_rows": len(bridge_rows),
        "call_sites": len(all_edges),
    }
