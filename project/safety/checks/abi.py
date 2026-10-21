"""CHECK-01: C/Rust source-type agreement and ABI representation.

The check deliberately separates source-type agreement (including signedness)
from ABI register representation.  A signedness disagreement is not reported as
an ARM calling-convention violation.  Value-domain compatibility is not
mechanically checked in Phase 1 and remains UNKNOWN.
"""

from __future__ import annotations

from typing import Any

from common.io import contract


RUST_INTEGERS = {
    "u8": (8, False), "u16": (16, False), "u32": (32, False),
    "u64": (64, False), "u128": (128, False), "usize": (None, False),
    "i8": (8, True), "i16": (16, True), "i32": (32, True),
    "i64": (64, True), "i128": (128, True), "isize": (None, True),
    "c_char": (8, None), "c_schar": (8, True), "c_uchar": (8, False),
    "c_short": (16, True), "c_ushort": (16, False),
    "c_int": (32, True), "c_uint": (32, False),
    "c_long": (32, True), "c_ulong": (32, False),
    "c_longlong": (64, True), "c_ulonglong": (64, False),
}
RUST_FLOATS = {"f32": 32, "f64": 64}
STATUS_ORDER = {"PASS": 0, "FAIL": 1, "UNKNOWN": 2}


def _rust_shape(spec: dict[str, Any], pointer_width: int) -> dict[str, Any]:
    kind = spec.get("kind")
    if kind == "void":
        return {"kind": "void"}
    if kind == "named":
        name = spec.get("name")
        if name in ("void", "c_void"):
            return {"kind": "void"}
        if name in RUST_INTEGERS:
            bits, signed = RUST_INTEGERS[name]
            if bits is None:
                bits = pointer_width
            if signed is None:
                return {"kind": "unsupported", "reason": "target-dependent Rust c_char width/signedness"}
            return {"kind": "integer", "bits": bits, "signed": signed}
        if name in RUST_FLOATS:
            return {"kind": "float", "bits": RUST_FLOATS[name]}
        if name == "bool":
            return {"kind": "bool"}
        return {"kind": "unsupported", "reason": f"unmapped Rust type {name}"}
    if kind == "pointer":
        return _pointer_abi(spec, pointer_width, "rust")
    if kind == "function_pointer":
        if spec.get("abi") not in ("C", None):
            return {"kind": "unsupported", "reason": f"Rust ABI {spec.get('abi')!r}"}
        return {
            "kind": "function_pointer",
            "params": [_rust_shape(param, pointer_width) for param in spec.get("params", [])],
            "ret": _rust_shape(spec.get("ret") or {"kind": "void"}, pointer_width),
        }
    return {"kind": "unsupported", "reason": f"Rust type kind {kind} is outside scalar scope"}


def _pointer_abi(spec: dict[str, Any], pointer_width: int, language: str) -> dict[str, Any]:
    depth = 0
    current = spec
    while current.get("kind") == "pointer":
        depth += 1
        current = current.get("to") or {"kind": "unsupported"}
    if current.get("kind") in ("function", "function_pointer"):
        shaped = _rust_shape(current, pointer_width) if language == "rust" else _c_function_shape(current)
        if shaped.get("kind") == "function_pointer":
            return {"kind": "pointer_to_function", "depth": depth, "function": shaped}
    if current.get("kind") == "unsupported":
        return {"kind": "unsupported", "reason": "pointer pointee could not be resolved"}
    return {"kind": "pointer", "depth": depth, "leaf": "data"}


def _c_function_shape(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("kind") == "function_pointer":
        return {
            "kind": "function_pointer",
            "params": [_c_shape(param) for param in spec.get("params", [])],
            "ret": _c_shape(spec.get("ret") or {"kind": "void"}),
        }
    return {"kind": "unsupported", "reason": "not a function pointer"}


def _c_shape(spec: dict[str, Any]) -> dict[str, Any]:
    kind = spec.get("kind")
    if kind == "void":
        return {"kind": "void"}
    if kind == "integer":
        if spec.get("name") == "bool":
            return {"kind": "bool"}
        return {"kind": "integer", "bits": spec.get("bits"), "signed": spec.get("signed")}
    if kind == "float":
        return {"kind": "float", "bits": spec.get("bits")}
    if kind == "pointer":
        return _pointer_abi(spec, 32, "c")
    if kind == "function_pointer":
        return _c_function_shape(spec)
    if kind == "record":
        return {"kind": "unsupported", "reason": "record passed by value outside Phase 1 scalar scope"}
    return {"kind": "unsupported", "reason": f"C type kind {kind}"}


def _combine(statuses: list[tuple[str, str]]) -> tuple[str, str]:
    if not statuses:
        return "PASS", "compatible"
    if any(status == "UNKNOWN" for status, _ in statuses):
        reason = next(reason for status, reason in statuses if status == "UNKNOWN")
        return "UNKNOWN", reason
    for status, reason in statuses:
        if status == "FAIL":
            return "FAIL", reason
    return "PASS", "compatible"


def _compatible(c: dict[str, Any], rust: dict[str, Any], *, ignore_signedness: bool = False) -> tuple[str, str]:
    if c.get("kind") == "unsupported" or rust.get("kind") == "unsupported":
        return "UNKNOWN", c.get("reason") or rust.get("reason") or "unsupported type"
    if c.get("kind") != rust.get("kind"):
        return "FAIL", f"kind mismatch: C {c.get('kind')} vs Rust {rust.get('kind')}"
    kind = c["kind"]
    if kind == "integer":
        if c.get("bits") != rust.get("bits"):
            return "FAIL", f"integer width mismatch: C {c.get('bits')} vs Rust {rust.get('bits')}"
        if not ignore_signedness and c.get("signed") != rust.get("signed"):
            return "FAIL", f"integer signedness mismatch: C signed={c.get('signed')} vs Rust signed={rust.get('signed')}"
    elif kind == "float":
        if c.get("bits") != rust.get("bits"):
            return "FAIL", f"float width mismatch: C {c.get('bits')} vs Rust {rust.get('bits')}"
    elif kind == "bool":
        pass
    elif kind == "pointer":
        if c.get("depth") != rust.get("depth"):
            return "FAIL", f"pointer depth mismatch: C {c.get('depth')} vs Rust {rust.get('depth')}"
    elif kind == "pointer_to_function":
        if c.get("depth") != rust.get("depth"):
            return "FAIL", f"function-pointer depth mismatch: C {c.get('depth')} vs Rust {rust.get('depth')}"
        return _compatible_function(c.get("function", {}), rust.get("function", {}), ignore_signedness=ignore_signedness)
    elif kind == "function_pointer":
        return _compatible_function(c, rust, ignore_signedness=ignore_signedness)
    return "PASS", "compatible"


def _compatible_function(c: dict[str, Any], rust: dict[str, Any], *, ignore_signedness: bool = False) -> tuple[str, str]:
    c_params = c.get("params", [])
    rust_params = rust.get("params", [])
    if len(c_params) != len(rust_params):
        return "FAIL", f"parameter count mismatch: C {len(c_params)} vs Rust {len(rust_params)}"
    statuses = [
        _compatible(c_param, rust_param, ignore_signedness=ignore_signedness)
        for c_param, rust_param in zip(c_params, rust_params)
    ]
    status, reason = _combine(statuses)
    if status != "PASS":
        return status, f"parameter: {reason}"
    return _compatible(
        c.get("ret", {"kind": "void"}),
        rust.get("ret", {"kind": "void"}),
        ignore_signedness=ignore_signedness,
    )


def _pick_c_symbol(symbols: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not symbols:
        return None
    project_definitions = [row for row in symbols if row.get("origin") == "project" and row.get("is_definition")]
    if project_definitions:
        return project_definitions[0]
    project = [row for row in symbols if row.get("origin") == "project"]
    if project:
        return project[0]
    definitions = [row for row in symbols if row.get("is_definition")]
    if definitions:
        return definitions[0]
    return symbols[0]


def _dimension_contract(
    *,
    configuration: str,
    name: str,
    declaration: dict[str, Any],
    source_status: str,
    abi_status: str,
    calling_status: str,
    reason: str,
    evidence: dict[str, Any],
    tool_version: str,
 ) -> dict[str, Any]:
    status = source_status
    return contract(
        check_id="CHECK-01",
        configuration=configuration,
        language="cross-language",
        interface=name,
        property_name="C/Rust source scalar type agreement (width, signedness, pointer depth, return type, C ABI)",
        status=status,
        source_file=declaration.get("file"),
        source_line=declaration.get("line"),
        evidence=evidence,
        assumptions={
            "target": "FreeRTOS STM32F4, arm-none-eabi, 32-bit pointers",
            "pointer_pointee": "pointer pointee identity is not compared; data pointers compare depth only",
            "source_type": "width, signedness, pointer depth, return shape and declared C ABI",
            "abi_representation": "integer/pointer register representation; signedness is not treated as an ABI difference",
            "value_domain": "not mechanically checked",
            "does_not_establish": "pointer validity, lifetime, ownership, buffer length or value-domain compatibility",
        },
        tool="libclang AST + syn",
        tool_version=tool_version,
        reason=reason,
        applicability="APPLICABLE" if status in ("PASS", "FAIL", "UNKNOWN") else "NOT_CHECKED",
        source_type_status=source_status,
        abi_representation_status=abi_status,
        value_domain_status="UNKNOWN",
        calling_convention_status=calling_status,
    )


def check_01(
    c_facts: dict[str, Any],
    rust_facts: dict[str, Any],
    *,
    configuration: str,
    pointer_width: int,
    tool_version: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    symbols = c_facts.get("function_symbols", {})
    merged: dict[str, list[dict[str, Any]]] = {}
    for declaration in rust_facts.get("foreign_fns", []):
        merged.setdefault(declaration["name"], []).append(declaration)

    for name in sorted(merged):
        declarations = merged[name]
        active_declarations = [item for item in declarations if item.get("active") == "true"]
        declaration = active_declarations[0] if active_declarations else declarations[0]
        inactive = not active_declarations and any(item.get("active") == "false" for item in declarations)
        unknown_cfg = not active_declarations and any(item.get("active") == "unknown" for item in declarations)
        calls = [
            call for function in rust_facts.get("functions", [])
            for call in function.get("calls", [])
            if call.get("callee") == name
        ]
        active_calls = [call for call in calls if call.get("active") != "false"]
        inactive_calls = [call for call in calls if call.get("active") == "false"]
        abi_blocks = {item.get("abi_block") for item in declarations}
        evidence = {
            "rust_declaration": f"{declaration.get('file')}:{declaration.get('line')}",
            "rust_signature": {
                "params": [item.get("text") for item in declaration.get("params", [])],
                "ret": (declaration.get("ret") or {}).get("text"),
            },
            "call_sites": len(active_calls),
            "call_sites_inactive": len(inactive_calls),
            "duplicate_rust_declarations": len(declarations),
            "dimensions": {
                "source_type": "NOT_CHECKED",
                "abi_representation": "NOT_CHECKED",
                "value_domain": "UNKNOWN",
                "calling_convention": "NOT_CHECKED",
            },
        }
        if unknown_cfg or inactive:
            status = "NOT_CHECKED" if inactive else "UNKNOWN"
            reason = "Rust declaration is inactive under the selected target configuration" if inactive else "Rust cfg predicate could not be evaluated"
            rows.append(_dimension_contract(
                configuration=configuration, name=name, declaration=declaration,
                source_status=status, abi_status=status, calling_status=status,
                reason=reason, evidence=evidence, tool_version=tool_version,
            ))
            continue
        if abi_blocks != {"C"}:
            rows.append(_dimension_contract(
                configuration=configuration, name=name, declaration=declaration,
                source_status="UNKNOWN", abi_status="UNKNOWN", calling_status="UNKNOWN",
                reason=f"non-C ABI block(s): {sorted(abi_blocks)}",
                evidence=evidence, tool_version=tool_version,
            ))
            continue
        c_symbol = _pick_c_symbol(symbols.get(name, []))
        if c_symbol is None:
            rows.append(_dimension_contract(
                configuration=configuration, name=name, declaration=declaration,
                source_status="UNKNOWN", abi_status="UNKNOWN", calling_status="UNKNOWN",
                reason="no C declaration was found in the analysed C-support configuration",
                evidence=evidence, tool_version=tool_version,
            ))
            continue
        c_function = c_symbol.get("type", {})
        if c_function.get("kind") != "function":
            rows.append(_dimension_contract(
                configuration=configuration, name=name, declaration=declaration,
                source_status="UNKNOWN", abi_status="UNKNOWN", calling_status="UNKNOWN",
                reason="C symbol did not resolve to a function type",
                evidence=evidence, tool_version=tool_version,
            ))
            continue
        c_params = c_function.get("params", [])
        rust_params = declaration.get("params", [])
        c_ret = _c_shape(c_function.get("ret") or {"kind": "void"})
        rust_ret = _rust_shape(declaration.get("ret") or {"kind": "void"}, pointer_width)
        if len(c_params) != len(rust_params):
            source_status, source_reason = "FAIL", f"parameter count mismatch: C {len(c_params)} vs Rust {len(rust_params)}"
            abi_status, abi_reason = "FAIL", source_reason
        else:
            source_statuses = [
                _compatible(_c_shape(c_param), _rust_shape(rust_param.get("type", {}), pointer_width))
                for c_param, rust_param in zip(c_params, rust_params)
            ]
            source_status, source_reason = _combine(source_statuses)
            abi_statuses = [
                _compatible(_c_shape(c_param), _rust_shape(rust_param.get("type", {}), pointer_width), ignore_signedness=True)
                for c_param, rust_param in zip(c_params, rust_params)
            ]
            abi_status, abi_reason = _combine(abi_statuses)
        if source_status == "PASS":
            source_status, source_reason = _compatible(c_ret, rust_ret)
        if abi_status == "PASS":
            abi_status, abi_reason = _compatible(c_ret, rust_ret, ignore_signedness=True)
        calling_status = "PASS" if abi_blocks == {"C"} else "UNKNOWN"
        evidence.update({
            "c_symbol": f"{c_symbol.get('file')}:{c_symbol.get('line')}",
            "c_signature": c_symbol.get("type", {}).get("text"),
            "c_origin": c_symbol.get("origin"),
            "resolved_c_shape": {"params": [_c_shape(item) for item in c_params], "ret": c_ret},
            "resolved_rust_shape": {"params": [_rust_shape(item.get("type", {}), pointer_width) for item in rust_params], "ret": rust_ret},
            "dimensions": {
                "source_type": source_status,
                "abi_representation": abi_status,
                "value_domain": "UNKNOWN",
                "calling_convention": calling_status,
            },
        })
        if source_status == "FAIL" and abi_status == "PASS":
            reason = f"source type mismatch: {source_reason}; ABI representation agrees; value domain not checked"
        elif source_status == "FAIL":
            reason = f"source type mismatch: {source_reason}; ABI representation: {abi_reason}; value domain not checked"
        elif source_status == "UNKNOWN":
            reason = f"source type could not be established: {source_reason}; ABI={abi_status}; value domain not checked"
        else:
            reason = "source scalar types and C ABI agree; ABI representation agrees; value domain not checked"
        rows.append(_dimension_contract(
            configuration=configuration, name=name, declaration=declaration,
            source_status=source_status, abi_status=abi_status, calling_status=calling_status,
            reason=reason, evidence=evidence, tool_version=tool_version,
        ))
    return rows
