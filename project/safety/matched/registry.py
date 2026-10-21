"""Verify the five-case Stage 2 registry against the actual source registries."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _repo_path(repo_root: Path, relative: str) -> Path:
    """Resolve a repository-relative path, including thesis-side paths."""
    path = Path(relative)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "thesis":
        return repo_root.parent / path
    return repo_root / path


def load_cases(repo_root: Path, safety: Path) -> dict[str, Any]:
    return json.loads((safety / "specs" / "cases.json").read_text())


def _line_of(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return None


def _section(text: str, start: str, end_markers: tuple[str, ...] | str) -> str:
    if isinstance(end_markers, str):
        end_markers = (end_markers,)
    start_at = text.find(start)
    if start_at < 0:
        return ""
    end_at = len(text)
    for marker in end_markers:
        candidate = text.find(marker, start_at + len(start))
        if candidate >= 0:
            end_at = min(end_at, candidate)
    return text[start_at:end_at]


def _brace_block(text: str, start: str) -> str:
    start_at = text.find(start)
    if start_at < 0:
        return ""
    open_at = text.find("{", start_at)
    if open_at < 0:
        return ""
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start_at:index + 1]
    return text[start_at:]


def verify_registry(repo_root: Path, safety: Path, cases_doc: dict[str, Any]) -> dict[str, Any]:
    thesis_path = _repo_path(repo_root, cases_doc["thesis_spec"])
    c_header_path = repo_root / cases_doc["c_enum_header"]
    c_bench_path = repo_root / cases_doc["c_wire_registry"]
    rust_core_path = repo_root / cases_doc["rust_core"]
    rust_impl_path = repo_root / cases_doc["rust_impl"]
    c_impl_path = repo_root / cases_doc["c_impl"]

    thesis = thesis_path.read_text()
    c_header = c_header_path.read_text()
    c_bench = c_bench_path.read_text()
    rust_core = rust_core_path.read_text()
    rust_impl = rust_impl_path.read_text()
    c_impl = c_impl_path.read_text()

    thesis_rows = {
        int(number): name
        for number, name in re.findall(r"\[(\d+)\],\s*\[`([^`]+)`\]", thesis)
    }
    c_enum_block = _section(c_header, "typedef enum { BENCH_TIMER_OVERHEAD", "} bench_case;")
    c_enum_names = re.findall(r"\b(BENCH_[A-Z0-9_]+)\b", c_enum_block)
    c_enum_values = {name: index for index, name in enumerate(dict.fromkeys(c_enum_names))}
    c_enums = set(c_enum_values)
    c_wire_block = _section(c_bench, "static int sink_case_id", "/* record():")
    c_wire = {
        enum: int(wire)
        for enum, wire in re.findall(r"case\s+(BENCH_[A-Z0-9_]+):\s*return\s+(\d+)U?;", c_wire_block)
    }
    c_wire.update({
        enum: int(wire)
        for enum, wire in re.findall(r"if\s*\(\s*c\s*==\s*(BENCH_[A-Z0-9_]+)\s*\)\s*\{\s*return\s+(\d+)U?;", c_wire_block)
    })
    rust_enum_block = _section(rust_core, "pub enum Case", "/// Case 18 payload")
    rust_variant_names = re.findall(r"^\s{4}([A-Z][A-Za-z0-9_]*),", rust_enum_block, re.MULTILINE)
    rust_discriminants = {name: index for index, name in enumerate(dict.fromkeys(rust_variant_names))}
    rust_variants = set(rust_discriminants)
    rust_wire_block = _section(rust_impl, "fn case_id_for", "fn record(")
    rust_wire = {
        variant: int(wire)
        for variant, wire in re.findall(r"Case::([A-Za-z0-9_]+)\s*=>\s*(\d+)", rust_wire_block)
    }
    rust_entry_block = _section(rust_impl, "pub unsafe extern \"C\" fn rust_bench_run_case", "pub extern \"C\" fn rust_bench_case_count")
    rust_entry = {
        int(wire): variant
        for wire, variant in re.findall(r"(\d+)\s*=>\s*Case::([A-Za-z0-9_]+)", rust_entry_block)
    }

    port_block = _brace_block(c_impl, "bench_port port = ")
    c_port_methods = {
        method: function
        for method, function in re.findall(r"\.([a-z_]+)\s*=\s*(port_[a-z_]+)", port_block)
    }
    rust_impl_block = _brace_block(rust_impl, "impl Port for FreertosPort")
    rust_impl_methods = set(re.findall(r"fn\s+([a-z_][a-z0-9_]*)\s*\(", rust_impl_block))

    rows: list[dict[str, Any]] = []
    for case in cases_doc["cases"]:
        checks: dict[str, Any] = {}
        number = int(case["thesis_case"])
        name = case["thesis_name"]
        checks["thesis"] = thesis_rows.get(number) == name
        case_spec = _repo_path(repo_root, cases_doc.get("thesis_case_spec", cases_doc["thesis_spec"])).read_text()
        checks["thesis_spec_section"] = (
            f"=== {number}. `{name}`" in case_spec or f"+ `{name}`" in case_spec
        )
        checks["c_enum"] = case["c_enum"] in c_enums
        checks["c_enum_value"] = c_enum_values.get(case["c_enum"]) is not None
        c_wire_value = c_wire.get(case["c_enum"])
        if c_wire_value is None and c_enum_values.get(case["c_enum"]) == 0:
            c_wire_value = 0  # sink_case_id's default maps the first enum to 0
        checks["c_wire"] = c_wire_value == int(case["c_wire_id"])
        checks["rust_variant"] = case["rust_variant"] in rust_variants
        checks["rust_discriminant"] = rust_discriminants.get(case["rust_variant"]) is not None
        checks["rust_wire"] = rust_wire.get(case["rust_variant"]) == int(case["rust_wire_id"])
        checks["rust_entry"] = rust_entry.get(int(case["rust_wire_id"])) == case["rust_variant"]
        checks["c_roots"] = all(_line_of(c_impl, f"{fn}(") is not None for fn in case["c_root_functions"])
        checks["rust_methods"] = all(method in rust_impl_methods for method in case["rust_application_methods"])
        checks["c_application_methods"] = all(method in c_port_methods for method in case["c_application_methods"])
        checks["wire_ids_agree"] = int(case["c_wire_id"]) == int(case["rust_wire_id"])
        checks["c_port_mapping"] = all(
            c_port_methods.get(method, "").replace("port_", "", 1) == method
            for method in case["c_application_methods"]
        )
        checks["c_bridge_roots"] = all(_line_of(c_impl, f"{fn}(") is not None for fn in case.get("c_bridge_roots", []))
        interrupt_text = c_impl
        if case.get("interrupt_source"):
            source_path = repo_root / case["interrupt_source"]
            interrupt_text = source_path.read_text() if source_path.is_file() else ""
        checks["interrupt_roots"] = all(_line_of(interrupt_text, f"{fn}(") is not None for fn in case.get("interrupt_roots", []))
        resolved = all(checks.values())
        rows.append({
            "thesis_case": number,
            "thesis_name": name,
            "mechanism": case["mechanism"],
            "c_enum": case["c_enum"],
            "c_enum_value": c_enum_values.get(case["c_enum"]),
            "c_wire_id": int(case["c_wire_id"]),
            "rust_variant": case["rust_variant"],
            "rust_discriminant": rust_discriminants.get(case["rust_variant"]),
            "rust_wire_id": int(case["rust_wire_id"]),
            "c_root_functions": case["c_root_functions"],
            "rust_application_methods": case["rust_application_methods"],
            "c_application_methods": case["c_application_methods"],
            "common_c_application_methods": cases_doc.get("common_c_application_methods", []),
            "common_rust_application_methods": cases_doc.get("common_rust_application_methods", []),
            "c_bridge_roots": case.get("c_bridge_roots", []),
            "interrupt_roots": case.get("interrupt_roots", []),
            "interrupt_source": case.get("interrupt_source"),
            "external_entry_limitations": case.get("external_entry_limitations", []),
            "c_port_function_map": {
                method: c_port_methods.get(method) for method in case["c_application_methods"]
            },
            "checks": checks,
            "resolution_method": "thesis_table+enum+wire_map+port_initializer+rust_case_map",
            "match_status": "verified_from_registry" if resolved else "unresolved",
            "thesis_spec_line": _line_of(thesis, f"[{number}], [`{name}`]"),
            "c_enum_header_line": _line_of(c_header, case["c_enum"]),
            "c_wire_line": _line_of(c_bench, case["c_enum"]),
            "rust_enum_line": _line_of(rust_core, f"{case['rust_variant']},"),
            "rust_wire_line": _line_of(rust_impl, f"Case::{case['rust_variant']} =>"),
        })
    return {
        "rows": rows,
        "thesis_rows": thesis_rows,
        "c_enums": sorted(c_enums),
        "c_enum_values": c_enum_values,
        "c_wire": c_wire,
        "rust_variants": sorted(rust_variants),
        "rust_discriminants": rust_discriminants,
        "rust_wire": rust_wire,
        "rust_entry": rust_entry,
        "c_port_methods": c_port_methods,
        "rust_impl_methods": sorted(rust_impl_methods),
    }


def _profile_source(repo_root: Path, relative: str) -> str:
    path = repo_root / relative
    if not path.is_file():
        raise RuntimeError(f"profile source not found: {relative}")
    return path.read_text()


def _profile_functions(text: str, names: list[str]) -> bool:
    return all(re.search(r"\b" + re.escape(name) + r"\s*\(", text) for name in names)


def verify_profile_registry(repo_root: Path, safety: Path, profile: dict[str, Any]) -> dict[str, Any]:
    """Verify a bounded non-FreeRTOS profile against its own source registry.

    It deliberately does not reuse FreeRTOS case roots or FreeRTOS wire names.
    A missing or ambiguous source fact marks the profile case unresolved.
    """
    thesis_path = _repo_path(repo_root, profile["thesis_spec"])
    case_spec_path = _repo_path(repo_root, profile.get("thesis_case_spec", profile["thesis_spec"]))
    c_header_path = repo_root / profile["c_enum_header"]
    c_wire_path = repo_root / profile["c_wire_registry"]
    rust_core_path = repo_root / profile["rust_core"]
    rust_impl_path = repo_root / profile["rust_impl"]
    c_impl_path = repo_root / profile["c_impl"]

    thesis = thesis_path.read_text()
    case_spec = case_spec_path.read_text()
    c_header = c_header_path.read_text()
    c_wire_text = c_wire_path.read_text()
    rust_core = rust_core_path.read_text()
    rust_impl = rust_impl_path.read_text()
    c_impl = c_impl_path.read_text()
    c_root_texts = {path: _profile_source(repo_root, path) for path in profile["c_root_search_files"]}
    c_bridge_texts = {path: _profile_source(repo_root, path) for path in profile["c_bridge_search_files"]}
    interrupt_texts = {path: _profile_source(repo_root, path) for path in profile.get("interrupt_search_files", [])}

    thesis_rows = {
        int(number): name
        for number, name in re.findall(r"\[(\d+)\],\s*\[`([^`]+)`\]", thesis)
    }
    c_enum_block = _section(c_header, "typedef enum { BENCH_TIMER_OVERHEAD", "} bench_case;")
    c_enum_names = re.findall(r"\b(BENCH_[A-Z0-9_]+)\b", c_enum_block)
    c_enum_values = {name: index for index, name in enumerate(dict.fromkeys(c_enum_names))}
    c_wire_block = _section(c_wire_text, profile["c_wire_function"], "static struct")
    if not c_wire_block:
        c_wire_block = _section(c_wire_text, profile["c_wire_function"], "/* record():")
    c_wire = {
        enum: int(wire)
        for enum, wire in re.findall(r"case\s+(BENCH_[A-Z0-9_]+):\s*return\s+(\d+)U?;", c_wire_block)
    }
    c_wire.update({
        enum: int(wire)
        for enum, wire in re.findall(r"if\s*\(\s*c\s*==\s*(BENCH_[A-Z0-9_]+)\s*\)\s*\{\s*return\s+(\d+)U?;", c_wire_block)
    })
    rust_enum_block = _section(rust_core, "pub enum Case", "/// Case 18 payload")
    rust_variant_names = re.findall(r"^\s{4}([A-Z][A-Za-z0-9_]*),", rust_enum_block, re.MULTILINE)
    rust_discriminants = {name: index for index, name in enumerate(dict.fromkeys(rust_variant_names))}
    rust_wire_block = _section(rust_impl, "fn case_id_for", "fn record(")
    rust_wire = {
        variant: int(wire)
        for variant, wire in re.findall(r"Case::([A-Za-z0-9_]+)\s*=>\s*(\d+)", rust_wire_block)
    }
    rust_entry_block = _section(rust_impl, 'pub unsafe extern "C" fn rust_bench_run_case', "pub extern \"C\" fn rust_bench_case_count")
    rust_entry = {
        int(wire): variant
        for wire, variant in re.findall(r"(\d+)\s*=>\s*Case::([A-Za-z0-9_]+)", rust_entry_block)
    }
    port_block = _brace_block(c_impl, profile.get("c_port_initializer", "bench_port port = "))
    if not port_block or "port->" not in port_block and "bench_port port =" not in port_block:
        port_block = c_impl
    c_port_methods = {
        method: function
        for method, function in re.findall(r"port->([a-z_]+)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*;", port_block)
    }
    if not c_port_methods:
        c_port_methods = {
            method: function
            for method, function in re.findall(r"\.([a-z_]+)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)", port_block)
        }
    rust_impl_block = _brace_block(rust_impl, f"impl Port for {profile['rust_impl_type']}")
    rust_impl_methods = set(re.findall(r"fn\s+([a-z_][a-z0-9_]*)\s*\(", rust_impl_block))

    rows: list[dict[str, Any]] = []
    for case in profile["cases"]:
        checks: dict[str, Any] = {}
        number = int(case["thesis_case"])
        name = case["thesis_name"]
        checks["thesis"] = thesis_rows.get(number) == name
        checks["thesis_spec_section"] = (
            f"=== {number}. `{name}`" in case_spec or f"+ `{name}`" in case_spec
        )
        checks["c_enum"] = case["c_enum"] in c_enum_values
        checks["c_enum_value"] = c_enum_values.get(case["c_enum"]) == int(case.get("c_enum_value", -1))
        c_wire_value = c_wire.get(case["c_enum"], c_enum_values.get(case["c_enum"]))
        checks["c_wire"] = c_wire_value == int(case["c_wire_id"])
        checks["rust_variant"] = case["rust_variant"] in rust_discriminants
        checks["rust_discriminant"] = rust_discriminants.get(case["rust_variant"]) == int(case.get("rust_discriminant", -1))
        checks["rust_wire"] = rust_wire.get(case["rust_variant"]) == int(case["rust_wire_id"])
        checks["rust_entry"] = rust_entry.get(int(case["rust_wire_id"])) == case["rust_variant"]
        checks["c_roots"] = any(_profile_functions(text, case["c_root_functions"]) for text in c_root_texts.values())
        checks["rust_methods"] = all(method in rust_impl_methods for method in case["rust_application_methods"])
        checks["c_application_methods"] = all(method in c_port_methods for method in case["c_application_methods"])
        checks["wire_ids_agree"] = int(case["c_wire_id"]) == int(case["rust_wire_id"])
        checks["c_port_mapping"] = all(c_port_methods.get(method) for method in case["c_application_methods"])
        checks["c_bridge_roots"] = all(
            any(_profile_functions(text, [name]) for text in c_bridge_texts.values())
            for name in case.get("c_bridge_roots", [])
        )
        checks["interrupt_roots"] = all(
            any(_profile_functions(text, [name]) for text in interrupt_texts.values())
            for name in case.get("interrupt_roots", [])
        )
        resolved = all(checks.values())
        rows.append({
            "thesis_case": number,
            "thesis_name": name,
            "mechanism": case["mechanism"],
            "c_enum": case["c_enum"],
            "c_enum_value": c_enum_values.get(case["c_enum"]),
            "c_wire_id": int(case["c_wire_id"]),
            "rust_variant": case["rust_variant"],
            "rust_discriminant": rust_discriminants.get(case["rust_variant"]),
            "rust_wire_id": int(case["rust_wire_id"]),
            "c_root_functions": case["c_root_functions"],
            "rust_application_methods": case["rust_application_methods"],
            "c_application_methods": case["c_application_methods"],
            "common_c_application_methods": profile.get("common_c_application_methods", []),
            "common_rust_application_methods": profile.get("common_rust_application_methods", []),
            "c_bridge_roots": case.get("c_bridge_roots", []),
            "interrupt_roots": case.get("interrupt_roots", []),
            "interrupt_source": ";".join(profile.get("interrupt_search_files", [])),
            "external_entry_limitations": case.get("external_entry_limitations", []),
            "semantic_limitations": case.get("semantic_limitations", []),
            "c_port_function_map": {method: c_port_methods.get(method) for method in case["c_application_methods"]},
            "checks": checks,
            "resolution_method": "thesis_table+profile_registry+enum+wire_map+port_initializer+rust_case_map",
            "match_status": "verified_from_registry" if resolved else "unresolved",
            "thesis_spec_line": _line_of(thesis, f"[{number}], [`{name}`]"),
            "c_enum_header_line": _line_of(c_header, case["c_enum"]),
            "c_wire_line": _line_of(c_wire_text, case["c_enum"]),
            "rust_enum_line": _line_of(rust_core, f"{case['rust_variant']},"),
            "rust_wire_line": _line_of(rust_impl, f"Case::{case['rust_variant']} =>"),
        })
    return {
        "rows": rows,
        "thesis_rows": thesis_rows,
        "c_enums": sorted(c_enum_values),
        "c_enum_values": c_enum_values,
        "c_wire": c_wire,
        "rust_variants": sorted(rust_discriminants),
        "rust_discriminants": rust_discriminants,
        "rust_wire": rust_wire,
        "rust_entry": rust_entry,
        "c_port_methods": c_port_methods,
        "rust_impl_methods": sorted(rust_impl_methods),
        "profile_id": profile["id"],
        "selected_case_count": len(rows),
        "absent_cases": profile.get("absent_cases", []),
    }
