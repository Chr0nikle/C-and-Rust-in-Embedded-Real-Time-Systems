"""Rust operation inventory from the existing syntax-aware syn facts.

The model is deliberately conservative.  It resolves direct project-function
return types when the declaration is unambiguous and active, models the two
well-defined ``addr_of!``/``addr_of_mut!`` forms for direct ``static mut``
paths, and keeps every other macro expression visible as unresolved.  It does
not perform type inference, alias analysis or macro expansion.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from common.extractor import extract_rust


ALLOC_NAMES = {"pvPortMalloc", "malloc", "calloc", "realloc"}
FREE_NAMES = {"vPortFree", "free"}
KERNEL_RE = re.compile(r"^(x|v|ux|pv|ul|pc)(Queue|Semaphore|Task|Timer|EventGroup|Port|Notify)")
WRAPPER_PREFIXES = ("port_", "bench_", "semaphore_", "task_", "queue_", "timer_", "notify_", "jitter_", "rust_")
ADDR_OF_MACROS = {"addr_of", "addr_of_mut"}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CALL_RE = re.compile(r"(?:(?:[A-Za-z_][A-Za-z0-9_]*)::)*([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", re.S)
MACRO_RE = re.compile(r"(?:(?:[A-Za-z_][A-Za-z0-9_]*)::)*([A-Za-z_][A-Za-z0-9_]*)!\((.*)\)", re.S)

GLOBAL_ACCESS_CATEGORY = {
    "read": "mutable_global_read",
    "write": "mutable_global_write",
    "readwrite": "mutable_global_readwrite",
    "address_of": "mutable_global_address",
    "field_write": "mutable_global_write",
    "field_readwrite": "mutable_global_readwrite",
    "index_write": "mutable_global_write",
    "index_readwrite": "mutable_global_readwrite",
}


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _simple_ident(text: str) -> str | None:
    value = _compact(text)
    return value if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) else None


def _is_pointer(spec: dict[str, Any] | None) -> bool:
    return bool(spec and spec.get("kind") == "pointer")


PRIMITIVE_RETURN_NAMES = {
    "u8", "u16", "u32", "u64", "usize", "i8", "i16", "i32", "i64", "isize",
    "f32", "f64", "bool", "char", "c_void",
}


def _unique_simple(items: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    simple: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for item in items:
        name = item.get("simple_name") or (item.get("name") or "").split("::")[-1]
        if not name:
            continue
        if name in ambiguous:
            continue
        if name in simple:
            simple.pop(name, None)
            ambiguous.add(name)
        else:
            simple[name] = item
    return simple, ambiguous


def _static_types(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    active = [item for item in facts.get("static_muts", []) if item.get("active") != "false"]
    simple, _ambiguous = _unique_simple(active)
    return {name: (item.get("type") or {"kind": "unknown"}) for name, item in simple.items()}


def _static_index(facts: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    active = [item for item in facts.get("static_muts", []) if item.get("active") != "false"]
    by_qualified: dict[str, dict[str, Any]] = {}
    for item in active:
        name = item.get("name")
        module = item.get("module_path") or ""
        if name:
            by_qualified[f"{module}::{name}" if module else name] = item
    simple, ambiguous = _unique_simple(active)
    return by_qualified, simple, ambiguous


def _return_index(facts: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str], set[str]]:
    """Index unambiguous active project and foreign return types.

    Qualified names distinguish same-named functions in different modules.
    Simple names are accepted only when they are unambiguous across the active
    source set.  Aliases and unsupported named types remain unresolved.
    """
    entries: list[tuple[str, str, dict[str, Any]]] = []
    for function in [*facts.get("functions", []), *facts.get("foreign_fns", [])]:
        if function.get("active") == "false":
            continue
        qualified = function.get("name")
        if not qualified:
            continue
        simple = function.get("simple_name") or qualified.split("::")[-1]
        entries.append((qualified, simple, function.get("ret") or {}))
    by_qualified: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for qualified, _simple, spec in entries:
        by_qualified[qualified].append(spec)
    returns: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for qualified, specs in by_qualified.items():
        if len(specs) != 1:
            ambiguous.add(qualified)
            continue
        spec = specs[0]
        kind = spec.get("kind")
        if kind in ("pointer", "reference", "void"):
            returns[qualified] = spec
        elif kind == "named" and spec.get("name") in PRIMITIVE_RETURN_NAMES:
            returns[qualified] = spec
        else:
            ambiguous.add(qualified)
    by_simple: dict[str, list[str]] = defaultdict(list)
    for qualified, simple, _spec in entries:
        by_simple[simple].append(qualified)
    simple_returns: dict[str, dict[str, Any]] = {}
    simple_ambiguous: set[str] = set()
    for simple, qualified_names in by_simple.items():
        unique_names = sorted(set(qualified_names))
        if len(unique_names) != 1:
            simple_ambiguous.add(simple)
            continue
        qualified = unique_names[0]
        if qualified in returns:
            simple_returns[simple] = returns[qualified]
        elif qualified in ambiguous:
            simple_ambiguous.add(simple)
    return returns, simple_returns, ambiguous, simple_ambiguous


def _type_for_expression(
    expr: str,
    function: dict[str, Any],
    returns: dict[str, dict[str, Any]],
    simple_returns: dict[str, dict[str, Any]],
    ambiguous: set[str],
    simple_ambiguous: set[str],
    static_mut_types: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    text = _compact(expr)
    params = {param.get("name"): param.get("type") for param in function.get("params", [])}
    locals_ = {local.get("name"): local for local in function.get("locals", [])}
    static_mut_types = static_mut_types or {}
    module_path = function.get("module_path") or ""

    def resolve_call(callee: str) -> dict[str, Any] | None:
        if "::" in callee:
            candidates = [callee]
        else:
            candidates = []
            if module_path:
                candidates.append(f"{module_path}::{callee}")
            candidates.append(callee)
        for candidate in candidates:
            if candidate in returns:
                return returns[candidate]
            if candidate in ambiguous:
                return None
        if callee in simple_ambiguous:
            return None
        return simple_returns.get(callee)

    def resolve_ident(ident: str, depth: int = 0) -> dict[str, Any] | None:
        if depth > 8:
            return None
        if ident in params:
            return params[ident]
        if ident in static_mut_types:
            return static_mut_types[ident]
        local = locals_.get(ident)
        if not local:
            return None
        if local.get("type"):
            return local["type"]
        init = _compact(local.get("init") or "")
        if not init:
            return None
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", init):
            return resolve_ident(init, depth + 1)
        call = CALL_RE.fullmatch(init)
        if call:
            return resolve_call(call.group(1))
        if "as*mut" in init or "as*const" in init:
            return {"kind": "pointer", "to": {"kind": "unknown"}}
        receiver = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)", init)
        if receiver:
            base = resolve_ident(receiver.group(1), depth + 1)
            if base and base.get("kind") == "pointer":
                return base
        return None

    ident = _simple_ident(text)
    if ident:
        resolved = resolve_ident(ident)
        if resolved:
            return resolved
    call = CALL_RE.fullmatch(text)
    if call:
        resolved = resolve_call(call.group(1))
        if resolved:
            return resolved
    macro = MACRO_RE.fullmatch(text)
    if macro and macro.group(1) in ADDR_OF_MACROS:
        argument = _simple_ident(macro.group(2))
        if argument and argument in static_mut_types:
            return {"kind": "pointer", "to": static_mut_types[argument], "mut": macro.group(1) == "addr_of_mut"}
    receiver = text.split(".", 1)[0] if "." in text else None
    if receiver:
        base = resolve_ident(receiver)
        if base and base.get("kind") == "pointer":
            return base
    cast = re.search(r"as(\*mut|\*const)", text)
    if cast:
        return {"kind": "pointer", "to": {"kind": "unknown"}}
    return None

def _source_identity(category: str, file: str | None, line: int, column: int, ast_kind: str, expression: str, function: str | None) -> str:
    material = "|".join(("rust", category, file or "", str(line), str(column), ast_kind, expression, function or ""))
    return "r-src-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _context_id(configuration: str, context_type: str, file: str | None, line: int, column: int, function: str | None, start_line: int | None, start_column: int | None) -> str:
    material = "|".join((configuration, context_type, file or "", str(line), str(column), function or "", str(start_line or ""), str(start_column or "")))
    return "u-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _contains(line: int, column: int, start_line: int | None, start_column: int | None, end_line: int | None, end_column: int | None) -> bool:
    if start_line is None or end_line is None:
        return False
    if line < start_line or line > end_line:
        return False
    if line == start_line and start_column is not None and column < start_column:
        return False
    if line == end_line and end_column is not None and column > end_column:
        return False
    return True


def _macro_parts(tokens: str) -> tuple[str | None, str | None]:
    match = MACRO_RE.fullmatch(_compact(tokens))
    if not match:
        return None, None
    return match.group(1), match.group(2)


def extract_rust_scope(repo_root: Path, config: dict[str, Any], extractor: Path, classifier) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    facts = extract_rust(repo_root, config["files"], config["target"], extractor, config.get("features", []))
    config_id = config["configuration"]
    foreign = {
        item["name"]: item
        for item in facts.get("foreign_fns", [])
        if item.get("active") != "false"
    }
    foreign_simple, foreign_simple_ambiguous = _unique_simple(list(foreign.values()))
    foreign_names = set(foreign) | set(foreign_simple)
    static_by_qualified, static_simple, static_ambiguous = _static_index(facts)
    static_types = _static_types(facts)
    returns, simple_returns, return_ambiguous, simple_return_ambiguous = _return_index(facts)

    def resolve_static(name: str, function: dict[str, Any]) -> dict[str, Any] | None:
        module_path = function.get("module_path") or ""
        if "::" in name:
            return static_by_qualified.get(name)
        if module_path:
            qualified = f"{module_path}::{name}"
            if qualified in static_by_qualified:
                return static_by_qualified[qualified]
        if name in static_ambiguous:
            return None
        return static_simple.get(name)
    operations: list[dict[str, Any]] = []
    kernel_calls: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    function_info: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    block_contexts: dict[tuple[str | None, str | None], list[dict[str, Any]]] = defaultdict(list)

    def add_operation(function: dict[str, Any], category: str, ast_kind: str, line: int, column: int, expression: str, resolution: str, active: str, layer: str, details: dict[str, Any] | None = None, provenance: str = "explicit_source") -> None:
        file = function.get("file")
        key = (file, line, column, category, ast_kind, expression, function.get("name"), active, resolution)
        if key in by_key:
            return
        record = {
            "occurrence_id": "r-" + hashlib.sha256("|".join((config_id, category, file or "", str(line), str(column), ast_kind, expression, function.get("name") or "", active, resolution)).encode()).hexdigest()[:24],
            "source_identity_key": _source_identity(category, file, line, column, ast_kind, expression, function.get("name")),
            "language": "rust",
            "configuration": config_id,
            "scope": "rust",
            "scope_label": "Rust benchmark",
            "layer": layer,
            "category": category,
            "ast_kind": ast_kind,
            "source_file": file,
            "source_line": line,
            "source_column": column,
            "function": function.get("name"),
            "expression": expression,
            "activity_status": active,
            "resolution_status": resolution,
            "provenance": provenance,
            "macro_status": provenance,
            "details": details or {},
            "in_unsafe_block": "unknown",
            "in_unsafe_function": "unknown",
            "in_unsafe_context": "unknown",
            "unsafe_context_ids": [],
            "unsafe_context_status": "unknown",
        }
        operations.append(record)
        by_key[key] = record

    for function in facts.get("functions", []):
        layer = classifier.classify("rust", function.get("file"))
        active = function.get("active", "unknown")
        function_key = (function.get("file"), function.get("name"))
        function_info[function_key] = {
            "unsafe": bool(function.get("unsafe")),
            "body_start_line": function.get("body_start_line"),
            "body_start_column": function.get("body_start_column"),
            "body_end_line": function.get("body_end_line"),
            "body_end_column": function.get("body_end_column"),
        }
        for record in function.get("deref_records", []):
            spec = _type_for_expression(record.get("operand", ""), function, returns, simple_returns, return_ambiguous, simple_return_ambiguous, static_types)
            if _is_pointer(spec):
                add_operation(function, "raw_pointer_dereference", "ExprUnary(Deref)", record["line"], record["column"], record["operand"], "resolved", active, layer)
            elif spec is None:
                add_operation(function, "raw_pointer_dereference", "ExprUnary(Deref)", record["line"], record["column"], record["operand"], "unresolved", active, layer, {"reason": "operand type not resolved by syntax-only extraction"})
            # A resolved non-pointer operand is an ordinary reference dereference.
        for record in function.get("cast_records", []):
            target = record.get("target_type") or {}
            if target.get("kind") == "pointer":
                add_operation(function, "pointer_conversion", "ExprCast", record["line"], record["column"], record["source"], "resolved", active, layer, {"target": target})
        for record in function.get("add_uses", []):
            spec = _type_for_expression(record.get("receiver", ""), function, returns, simple_returns, return_ambiguous, simple_return_ambiguous, static_types)
            resolution = "resolved" if _is_pointer(spec) else "unresolved"
            add_operation(function, "pointer_arithmetic", f"ExprMethodCall({record.get('method')})", record["line"], record["column"], record.get("receiver", ""), resolution, record.get("active", active), layer, {"offset": record.get("offset")})
        for call in function.get("calls", []):
            name = call.get("callee", "")
            call_active = call.get("active", active)
            if name in ALLOC_NAMES:
                add_operation(function, "dynamic_allocation", "ExprCall", call["line"], call.get("column", 0), " ".join(call.get("args", [])), "resolved", call_active, layer, {"callee": name})
            elif name in FREE_NAMES:
                add_operation(function, "deallocation", "ExprCall", call["line"], call.get("column", 0), " ".join(call.get("args", [])), "resolved", call_active, layer, {"callee": name})
            if name in foreign_names or KERNEL_RE.match(name) or name.startswith(WRAPPER_PREFIXES):
                if name in foreign or (name in foreign_simple and name not in foreign_simple_ambiguous):
                    kind = "rust_ffi_declaration"
                elif KERNEL_RE.match(name):
                    kind = "rtos_api"
                else:
                    kind = "project_wrapper"
                add_operation(function, "kernel_api_call", "ExprCall", call["line"], call.get("column", 0), " ".join(call.get("args", [])), "resolved", call_active, layer, {"callee": name, "callee_kind": kind})
                kernel_calls.append({
                    "configuration": config_id, "language": "rust",
                    "source_file": function.get("file"), "source_line": call["line"],
                    "source_column": call.get("column", 0), "caller": function.get("name"), "callee": name,
                    "callee_kind": kind, "resolution_status": "resolved",
                    "activity_status": call_active, "expression": " ".join(call.get("args", [])),
                })
        local_names = {item.get("name") for item in function.get("locals", [])} | {item.get("name") for item in function.get("params", [])}
        for record in function.get("path_refs", []):
            name = record.get("name")
            static_item = resolve_static(name, function)
            if static_item is None or name in local_names:
                continue
            access = record.get("access", "read")
            spec = static_item.get("type") or {}
            category = GLOBAL_ACCESS_CATEGORY.get(access)
            if access in ("index_write", "index_readwrite") and spec.get("kind") == "pointer":
                # The static-mut pointer value is read; the pointee mutation
                # cannot be attributed to a named global object.
                add_operation(function, "mutable_global_read", "ExprPath", record["line"], record["column"], name, "resolved", record.get("active", active), layer, {"global": name, "access": "read", "via": "index_base"})
                add_operation(function, "mutable_global_unresolved", "ExprPath", record["line"], record["column"], name, "unresolved", record.get("active", active), layer, {"global": name, "access": access, "reason": "indexed mutation through a static-mut pointer; pointee identity is not tracked"})
                continue
            if category is None:
                category = "mutable_global_unresolved"
            details = {"global": name, "access": access, "static_type": spec}
            if access.startswith("field_"):
                details["via"] = "field"
            elif access.startswith("index_"):
                details["via"] = "index"
            resolution = "unresolved" if category == "mutable_global_unresolved" else "resolved"
            add_operation(function, category, "ExprPath", record["line"], record["column"], name, resolution, record.get("active", active), layer, details)
        for record in function.get("alias_writes", []):
            pointer = record.get("pointer")
            if resolve_static(pointer, function) is not None:
                add_operation(function, "mutable_global_unresolved", "ExprUnary(Deref)", record["line"], record["column"], pointer, "unresolved", record.get("active", active), layer, {"global": pointer, "pointer": pointer, "access": record.get("access"), "reason": "write through a raw pointer; pointee identity is not tracked"})
        for record in function.get("macro_uses", []):
            macro_name, arguments = _macro_parts(record.get("tokens", ""))
            argument = _simple_ident(arguments or "")
            if macro_name in ADDR_OF_MACROS and argument and resolve_static(argument, function) is not None and argument not in local_names:
                add_operation(function, "mutable_global_address", "ExprMacro", record["line"], record["column"], record.get("macro", ""), "resolved", record.get("active", active), layer, {"global": argument, "access": "address_of", "macro": macro_name, "model": "known_addr_of_form"}, provenance="known_macro_model")
            else:
                add_operation(function, "unresolved_macro", "ExprMacro", record["line"], record["column"], record.get("macro", ""), "unresolved", record.get("active", active), layer, {"macro": record.get("macro"), "tokens": record.get("tokens"), "model": "not_expanded"}, provenance="macro_not_expanded")
        for record in function.get("unsafe_locations", []):
            start_line = record.get("start_line", record.get("line"))
            start_column = record.get("start_column", record.get("column"))
            end_line = record.get("end_line")
            end_column = record.get("end_column")
            context = {
                "configuration": config_id,
                "language": "rust",
                "context_id": _context_id(config_id, "unsafe_block", function.get("file"), start_line, start_column, function.get("name"), start_line, start_column),
                "context_type": "unsafe_block",
                "source_file": function.get("file"),
                "source_line": record["line"],
                "source_column": record["column"],
                "start_line": start_line,
                "start_column": start_column,
                "end_line": end_line,
                "end_column": end_column,
                "function": function.get("name"),
                "layer": layer,
                "activity_status": record.get("active", active),
            }
            contexts.append(context)
            block_contexts[function_key].append(context)
        if function.get("unsafe"):
            start_line = function.get("body_start_line") or function.get("line")
            start_column = function.get("body_start_column") or 0
            end_line = function.get("body_end_line")
            end_column = function.get("body_end_column")
            contexts.append({
                "configuration": config_id,
                "language": "rust",
                "context_id": _context_id(config_id, "unsafe_function", function.get("file"), start_line, start_column, function.get("name"), start_line, start_column),
                "context_type": "unsafe_function",
                "source_file": function.get("file"),
                "source_line": function.get("line"),
                "source_column": 0,
                "start_line": start_line,
                "start_column": start_column,
                "end_line": end_line,
                "end_column": end_column,
                "function": function.get("name"),
                "layer": layer,
                "activity_status": active,
            })

    for item in facts.get("unsafe_impls", []):
        contexts.append({
            "configuration": config_id,
            "language": "rust",
            "context_id": _context_id(config_id, "unsafe_impl", item.get("file"), item.get("start_line", item.get("line", 0)), item.get("start_column", item.get("column", 0)), None, item.get("start_line"), item.get("start_column")),
            "context_type": "unsafe_impl",
            "source_file": item.get("file"),
            "source_line": item.get("line"),
            "source_column": item.get("column", 0),
            "start_line": item.get("start_line", item.get("line")),
            "start_column": item.get("start_column", item.get("column", 0)),
            "end_line": item.get("end_line"),
            "end_column": item.get("end_column"),
            "function": None,
            "layer": classifier.classify("rust", item.get("file")),
            "activity_status": "true",
        })

    # Associate each operation with the lexical unsafe contexts that contain
    # it.  Membership is a syntactic-context property, not a claim that the
    # operation intrinsically requires unsafe.
    for operation in operations:
        function_key = (operation.get("source_file"), operation.get("function"))
        info = function_info.get(function_key)
        line = operation.get("source_line")
        column = operation.get("source_column")
        matching: list[dict[str, Any]] = []
        if isinstance(line, int) and isinstance(column, int):
            matching = [
                context for context in block_contexts.get(function_key, [])
                if _contains(line, column, context.get("start_line"), context.get("start_column"), context.get("end_line"), context.get("end_column"))
            ]
        in_block = bool(matching)
        in_function = bool(info and info.get("unsafe"))
        context_ids = [context["context_id"] for context in matching]
        if in_function:
            function_context_id = _context_id(
                config_id,
                "unsafe_function",
                operation.get("source_file"),
                info.get("body_start_line") or 0,
                info.get("body_start_column") or 0,
                operation.get("function"),
                info.get("body_start_line"),
                info.get("body_start_column"),
            )
            context_ids.append(function_context_id)
        operation["in_unsafe_block"] = "true" if in_block else "false"
        operation["in_unsafe_function"] = "true" if in_function else "false"
        operation["in_unsafe_context"] = "true" if (in_block or in_function) else "false"
        operation["unsafe_context_ids"] = sorted(set(context_ids))
        operation["unsafe_context_status"] = "resolved" if info is not None else "unknown"

    if facts.get("unions"):
        unsupported.append({"category": "union_field_access", "reason": "Rust union field access resolution is not implemented in the syntax-only inventory", "count": len(facts.get("unions", []))})
    for module in facts.get("out_of_line_modules", []):
        if not module.get("resolved_by_configuration"):
            unsupported.append({
                "category": "out_of_line_module",
                "reason": "out-of-line module declaration was not resolved by the configured file set",
                "module": module.get("name"),
                "qualified_name": module.get("qualified_name"),
                "file": module.get("file"),
                "line": module.get("line"),
                "active": module.get("active"),
            })
    functions = []
    for function in facts.get("functions", []):
        functions.append({
            "configuration": config_id, "language": "rust", "name": function.get("name"),
            "qualified_name": function.get("qualified_name") or function.get("name"),
            "simple_name": function.get("simple_name"),
            "module_path": function.get("module_path") or "",
            "file": function.get("file"), "line": function.get("line"), "column": None,
            "is_definition": True, "unsafe": function.get("unsafe", False),
            "active": function.get("active", "unknown"), "layer": classifier.classify("rust", function.get("file")),
        })
    operations.sort(key=lambda row: (row["function"] or "", row["source_file"] or "", row["source_line"], row["source_column"] or 0, row["category"], row["expression"]))
    kernel_calls.sort(key=lambda row: (row["caller"] or "", row["source_file"] or "", row["source_line"], row["callee"]))
    contexts.sort(key=lambda row: (row["source_file"] or "", row["source_line"], row["context_type"], row["function"] or ""))
    return operations, functions, kernel_calls, contexts, {"facts": facts, "unsupported": unsupported, "out_of_line_modules": facts.get("out_of_line_modules", [])}
