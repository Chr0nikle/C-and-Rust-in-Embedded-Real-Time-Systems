"""CHECK-03: compare C-owned array capacity with Rust pointer-index bounds."""

from __future__ import annotations

import re
from typing import Any

from common.io import contract


def _strip_expr(expr: str) -> str:
    value = expr.strip()
    value = re.sub(r"\s+as\s+\w+\s*$", "", value)
    value = re.sub(r"^\((?:[^()]|\\([^()]*\\))*\)\s*$", lambda m: m.group(0)[1:-1].strip(), value)
    return value.strip()


def _parse_int(expr: str) -> int | None:
    value = _strip_expr(expr).replace("_", "")
    value = re.sub(r"(u|i)(8|16|32|64|128|size)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[uUlL]+$", "", value)
    try:
        return int(value, 0)
    except ValueError:
        return None


def _function_map(rust_facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {function["name"]: function for function in rust_facts.get("functions", [])}


def _global_constants(rust_facts: dict[str, Any]) -> dict[str, str]:
    return {constant["name"]: constant.get("value_expr", "") for constant in rust_facts.get("constants", [])}


def _resolve_int(
    expr: str,
    function: str | None,
    functions: dict[str, dict[str, Any]],
    globals_: dict[str, str],
    seen: set[tuple[str | None, str]],
) -> tuple[int | None, str | None]:
    text = _strip_expr(expr)
    key = (function, text)
    if key in seen:
        return None, None
    seen.add(key)
    literal = _parse_int(text)
    if literal is not None:
        return literal, f"literal {text}"
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        if function and function in functions:
            facts = functions[function]
            for constant in facts.get("local_consts", []):
                if constant.get("name") == text:
                    value, reason = _resolve_int(constant.get("value_expr", ""), function, functions, globals_, seen)
                    if value is not None:
                        return value, f"{text} at {constant.get('line')}" + (f" ({reason})" if reason else "")
        if text in globals_:
            value, reason = _resolve_int(globals_[text], None, functions, globals_, seen)
            if value is not None:
                return value, f"global {text}" + (f" ({reason})" if reason else "")
    return None, None


def _infer_ident_bound(
    function_name: str,
    ident: str,
    functions: dict[str, dict[str, Any]],
    globals_: dict[str, str],
    seen: set[tuple[str, str]],
) -> tuple[int | None, list[str]]:
    key = (function_name, ident)
    if key in seen or function_name not in functions:
        return None, []
    seen.add(key)
    facts = functions[function_name]
    evidence: list[str] = []
    candidates: list[int] = []

    for loop in facts.get("loops", []):
        if loop.get("kind") == "for" and loop.get("var") == ident:
            end = loop.get("end")
            if end:
                value, reason = _resolve_int(end, function_name, functions, globals_, set())
                if value is None:
                    inner, inner_evidence = _infer_ident_bound(function_name, _strip_expr(end), functions, globals_, seen)
                    if inner is not None:
                        value = inner
                        evidence.extend(inner_evidence)
                if value is not None and not loop.get("inclusive"):
                    candidates.append(value)
                    evidence.append(f"for {ident} in ..{end} at line {loop.get('line')}")
                elif value is not None and loop.get("inclusive"):
                    candidates.append(value + 1)
                    evidence.append(f"for {ident} in ..={end} at line {loop.get('line')}")
        elif loop.get("kind") == "while":
            condition = loop.get("cond") or ""
            match = re.search(
                rf"\b{re.escape(ident)}\s*(<=|<)\s*([^&|]+)",
                condition,
            )
            if match:
                operator, rhs = match.groups()
                rhs = rhs.strip()
                value, reason = _resolve_int(rhs, function_name, functions, globals_, set())
                if value is None:
                    inner, inner_evidence = _infer_ident_bound(function_name, _strip_expr(rhs), functions, globals_, seen)
                    if inner is not None:
                        value = inner
                        evidence.extend(inner_evidence)
                if value is not None:
                    candidates.append(value + (1 if operator == "<=" else 0))
                    evidence.append(f"while {ident} {operator} {rhs} at line {loop.get('line')}")

    if not candidates:
        return None, evidence
    return max(candidates), evidence


def _bound_for_offset(
    function_name: str,
    offset: str,
    functions: dict[str, dict[str, Any]],
    globals_: dict[str, str],
) -> tuple[int | None, list[str]]:
    text = _strip_expr(offset)
    literal = _parse_int(text)
    if literal is not None:
        return literal + 1, [f"literal offset {text} -> exclusive bound {literal + 1}"]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text):
        return None, [f"offset expression {text!r} is not a simple identifier or literal"]

    facts = functions.get(function_name, {})
    params = {param.get("name") for param in facts.get("params", [])}
    if text in params:
        bounds: list[int] = []
        evidence: list[str] = []
        for call in [call for function in functions.values() for call in function.get("calls", [])]:
            if call.get("callee") != function_name:
                continue
            args = call.get("args", [])
            if not args:
                continue
            caller = call.get("function")
            arg = _strip_expr(args[0])
            value, why = _infer_ident_bound(caller, arg, functions, globals_, set())
            if value is not None:
                bounds.append(value)
                evidence.append(f"call {function_name}({arg}) at line {call.get('line')}: bound {value}")
                evidence.extend(why)
        if not bounds:
            return None, evidence or [f"no call-site bound found for parameter {text}"]
        return max(bounds), evidence

    value, evidence = _infer_ident_bound(function_name, text, functions, globals_, set())
    if value is None:
        return None, evidence or [f"no loop/guard bound found for {text}"]
    return value, evidence


def _simple_ident(value: str) -> str | None:
    text = re.sub(r"\s+", "", value)
    return text if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) else None


def _accessor_uses(
    accessor: str,
    functions: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve direct accessor roots and simple local aliases.

    Returns (resolved uses, unresolved accessor-derived use descriptions).  An
    unresolved use must force UNKNOWN; a PASS is never emitted when one exists.
    """
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for function in functions.values():
        locals_ = {local.get("name"): local for local in function.get("locals", [])}
        assignments: dict[str, list[str]] = {}
        for assignment in function.get("assignments", []):
            lhs = _simple_ident(assignment.get("lhs", ""))
            if lhs:
                assignments.setdefault(lhs, []).append(assignment.get("rhs", ""))

        roots: set[str] = set()
        for name, local in locals_.items():
            init = local.get("init") or ""
            if accessor in init:
                roots.add(name)
        # Fixed-point simple alias propagation: let q = b; let r = q;
        changed = True
        while changed:
            changed = False
            for name, local in locals_.items():
                if name in roots:
                    continue
                init_ident = _simple_ident(local.get("init") or "")
                if init_ident and init_ident in roots:
                    roots.add(name)
                    changed = True
                elif accessor in (local.get("init") or "") and "(" in (local.get("init") or ""):
                    roots.add(name)
                    changed = True

        poisoned: set[str] = set()
        for name, rhs_values in assignments.items():
            if name not in roots:
                continue
            for rhs in rhs_values:
                rhs_ident = _simple_ident(rhs)
                if rhs_ident and rhs_ident in roots:
                    continue
                if accessor in rhs:
                    continue
                poisoned.add(name)

        # Any local/assignment expression containing an accessor root but not
        # reducible to a simple alias is an unresolved accessor-derived path.
        derived_unresolved: set[str] = set()
        for name, local in locals_.items():
            init = local.get("init") or ""
            if name in roots:
                continue
            if any(re.search(rf"\b{re.escape(root)}\b", init) for root in roots):
                derived_unresolved.add(name)
        for name, rhs_values in assignments.items():
            if name in roots:
                continue
            if any(any(re.search(rf"\b{re.escape(root)}\b", rhs) for root in roots) for rhs in rhs_values):
                derived_unresolved.add(name)

        for use in function.get("add_uses", []):
            if use.get("active") == "false":
                continue
            receiver = (use.get("receiver") or "").strip()
            ident = _simple_ident(receiver)
            name = ident or receiver
            if ident and ident in roots and ident not in poisoned:
                resolved.append({**use, "receiver_resolution": "direct or simple alias"})
                continue
            if ident and ident in poisoned:
                unresolved.append(f"{function.get('name')}:{use.get('line')} alias {ident} has a non-accessor reassignment")
                continue
            if ident and ident in derived_unresolved:
                unresolved.append(f"{function.get('name')}:{use.get('line')} alias {ident} has unresolved accessor-derived provenance")
                continue
            if not ident and accessor in receiver:
                resolved.append({**use, "receiver_resolution": "direct accessor expression"})
                continue
            if not ident and any(re.search(rf"\b{re.escape(root)}\b", receiver) for root in roots):
                unresolved.append(f"{function.get('name')}:{use.get('line')} complex receiver contains an accessor root")
    return resolved, unresolved


def check_03(
    c_facts: dict[str, Any],
    rust_facts: dict[str, Any],
    *,
    configuration: str,
    tool_version: str,
) -> list[dict[str, Any]]:
    arrays = {array["name"]: array for array in c_facts.get("arrays", []) if array.get("function") is None}
    functions = _function_map(rust_facts)
    globals_ = _global_constants(rust_facts)
    foreign = {declaration["name"]: declaration for declaration in rust_facts.get("foreign_fns", [])}
    rows: list[dict[str, Any]] = []

    for c_function in c_facts.get("functions", []):
        for returned in c_function.get("array_returns", []):
            if returned.get("return_kind") != "array_base":
                continue
            accessor = c_function["name"]
            rust_declaration = foreign.get(accessor)
            if rust_declaration is None:
                continue
            array = arrays.get(returned["array"])
            if array is None:
                rows.append(
                    contract(
                        check_id="CHECK-03",
                        configuration=configuration,
                        language="cross-language",
                        interface=accessor,
                        property_name="C array capacity versus Rust pointer index bound",
                        status="UNKNOWN",
                        source_file=c_function.get("file"),
                        source_line=c_function.get("line"),
                        evidence={"returned_array": returned["array"], "array_fact": None},
                        assumptions={},
                        tool="libclang AST + syn",
                        tool_version=tool_version,
                        reason="C accessor returns an array symbol whose declaration was not resolved",
                    )
                )
                continue
            if rust_declaration.get("active") == "false":
                status = "NOT_CHECKED"
                reason = "Rust accessor declaration is inactive under the selected target"
                uses: list[dict[str, Any]] = []
                unresolved: list[str] = []
                bounds: list[tuple[int, list[str]]] = []
            elif rust_declaration.get("active") == "unknown":
                status = "UNKNOWN"
                reason = "Rust accessor cfg predicate could not be evaluated"
                uses = []
                unresolved = []
                bounds = []
            else:
                uses, unresolved = _accessor_uses(accessor, functions)
                bounds = []
                for use in uses:
                    value, evidence = _bound_for_offset(use.get("function"), use.get("offset") or "", functions, globals_)
                    if value is not None:
                        bounds.append((value, evidence))
                if unresolved:
                    status = "UNKNOWN"
                    reason = "unresolved accessor-derived pointer path: " + "; ".join(sorted(unresolved)[:3])
                elif not uses:
                    status = "UNKNOWN"
                    reason = "Rust accessor call was found but no indexed pointer use was identified"
                elif not bounds:
                    status = "UNKNOWN"
                    reason = "Rust pointer index bound could not be derived mechanically"
                else:
                    max_bound = max(value for value, _ in bounds)
                    capacity = array.get("elements")
                    if capacity is None:
                        status = "UNKNOWN"
                        reason = "C array capacity is not a resolved constant"
                    elif max_bound > capacity:
                        status = "FAIL"
                        reason = f"Rust exclusive index bound {max_bound} exceeds C capacity {capacity}"
                    else:
                        status = "PASS"
                        reason = f"all resolved accessor-derived uses have exclusive bound <= {capacity} in this revision"
            evidence = {
                "c_accessor": f"{c_function.get('file')}:{c_function.get('line')}",
                "c_array": {
                    "name": array.get("name"),
                    "file": array.get("file"),
                    "line": array.get("line"),
                    "capacity_elements": array.get("elements"),
                    "element_size": (array.get("type") or {}).get("elem", {}).get("size"),
                },
                "rust_declaration": f"{rust_declaration.get('file')}:{rust_declaration.get('line')}",
                "rust_index_uses": uses,
                "unresolved_uses": unresolved,
                "bounds": [{"value": value, "evidence": why} for value, why in bounds],
            }
            rows.append(
                contract(
                    check_id="CHECK-03",
                    configuration=configuration,
                    language="cross-language",
                    interface=accessor,
                    property_name="C array capacity versus resolved Rust pointer index bounds",
                    status=status,
                    source_file=rust_declaration.get("file") or c_function.get("file"),
                    source_line=rust_declaration.get("line") or c_function.get("line"),
                    evidence=evidence,
                    assumptions={
                        "capacity": "from libclang's target-resolved constant array type",
                        "accessor": "C return expression must resolve to the array base, not an element",
                        "bound": "from Rust loop guards and one-level call-site propagation; simple local aliases are followed; unresolved aliases force UNKNOWN",
                        "scope": "all resolved accessor-derived pointer indexing operations in the analysed modules",
                        "does_not_establish": "complete array-bounds safety, aliasing, concurrency or writes outside the analysed model",
                    },
                    tool="libclang AST + syn",
                    tool_version=tool_version,
                    reason=reason,
                    applicability="APPLICABLE" if status in ("PASS", "FAIL", "UNKNOWN") else status,
                )
            )

    if not rows:
        rows.append(
            contract(
                check_id="CHECK-03",
                configuration=configuration,
                language="cross-language",
                interface="(no C-owned arrays exposed through Rust pointer accessors)",
                property_name="C array capacity versus resolved Rust pointer index bounds",
                status="NOT_CHECKED",
                source_file=None,
                source_line=None,
                evidence={"c_arrays": len(arrays)},
                assumptions={"scope": "C accessor must return the array base and have an active Rust foreign declaration"},
                tool="libclang AST + syn",
                tool_version=tool_version,
                reason="no applicable C-owned array exposed through a Rust array-base accessor was discovered",
            )
        )
    return rows
