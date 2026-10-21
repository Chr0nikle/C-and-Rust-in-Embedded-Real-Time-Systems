"""Rust call-path extraction for the Stage 2 matched API pilot."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from common.layers import LayerClassifier
from inventory.rust_ops import extract_rust_scope
from common.extractor import extract_rust
from matched.c_extract import ALLOC_NAMES, RTOS_RE

BUILTIN_CALLS = {
    "Ok", "Err", "Some", "None", "from", "from_u32", "from_usize",
    "size_of", "size_of_val", "as_ptr", "as_mut_ptr", "as_ref", "as_mut",
    "from_raw_parts", "null", "null_mut", "is_null", "len", "wrapping_add",
    "saturating_sub", "abs_diff", "checked_sub", "ok_or", "unwrap", "unwrap_or",
    "div_ceil", "add", "offset", "sub", "wrapping_sub", "iter", "position", "write",
}
BUILTIN_METHODS = {
    "is_null", "len", "as_ptr", "as_mut_ptr", "as_ref", "as_mut", "wrapping_add",
    "saturating_sub", "abs_diff", "checked_sub", "ok_or", "unwrap", "unwrap_or",
    "div_ceil", "add", "offset", "sub", "wrapping_sub", "iter", "position", "write",
}


def _call_id(configuration: str, language: str, caller: str, file: str | None, line: int, column: int, source: str, target: str) -> str:
    material = "|".join((configuration, language, caller, file or "", str(line), str(column), source, target))
    return f"{language}-call-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def _contains(line: int, column: int, context: dict[str, Any]) -> bool:
    start_line = context.get("start_line")
    start_column = context.get("start_column")
    end_line = context.get("end_line")
    end_column = context.get("end_column")
    if start_line is None or end_line is None:
        return False
    if line < start_line or line > end_line:
        return False
    if line == start_line and column < start_column:
        return False
    if line == end_line and column > end_column:
        return False
    return True


def build_rust_index(repo_root: Path, config: dict[str, Any], extractor: Path, classifier: LayerClassifier) -> dict[str, Any]:
    operations, _, _, contexts, meta = extract_rust_scope(repo_root, config, extractor, classifier)
    facts = meta["facts"]
    functions = facts.get("functions", [])
    by_qualified: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_simple: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_impl: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for function in functions:
        qualified = function.get("name") or ""
        simple = function.get("simple_name") or qualified.split("::")[-1]
        by_qualified[qualified].append(function)
        by_simple[simple].append(function)
        impl_type = function.get("impl_type")
        if impl_type:
            by_impl[(impl_type, simple)].append(function)
    foreign = {item["name"]: item for item in facts.get("foreign_fns", []) if item.get("active") != "false"}
    result = {
        "configuration": config["configuration"],
        "functions": functions,
        "by_qualified": by_qualified,
        "by_simple": by_simple,
        "by_impl": by_impl,
        "foreign": foreign,
        "facts": facts,
        "contexts": contexts,
        "operations": operations,
    }
    result["builtin_methods"] = set(BUILTIN_METHODS) | set(config.get("language_builtin_methods", []))
    result["builtin_calls"] = set(BUILTIN_CALLS) | set(config.get("language_builtin_calls", []))
    result["callback_registrations"] = callback_registrations(
        result,
        apis=set(config.get("callback_apis", {})) or None,
        callback_apis=config.get("callback_apis", {}),
    )
    return result


def _unsafe_for(index: dict[str, Any], file: str | None, function: str | None, line: int, column: int) -> dict[str, Any]:
    blocks = []
    for context in index["contexts"]:
        if context["source_file"] != file or context["function"] != function:
            continue
        if _contains(line, column, context):
            blocks.append(context)
    in_block = any(context["context_type"] == "unsafe_block" for context in blocks)
    in_function = any(context["context_type"] == "unsafe_function" for context in blocks)
    return {
        "in_unsafe_block": "true" if in_block else "false",
        "in_unsafe_function": "true" if in_function else "false",
        "in_unsafe_context": "true" if (in_block or in_function) else "false",
        "unsafe_context_ids": sorted(context["context_id"] for context in blocks),
        "unsafe_context_status": "resolved" if function else "unknown",
    }


def _resolve_direct(index: dict[str, Any], callee: str) -> tuple[str, dict[str, Any] | None]:
    if callee in index["foreign"]:
        return "rust_ffi", index["foreign"][callee]
    if callee in index.get("builtin_calls", BUILTIN_CALLS):
        return "language_builtin", None
    candidates = index["by_qualified"].get(callee) or index["by_simple"].get(callee) or []
    candidates = [candidate for candidate in candidates if candidate.get("active") != "false"]
    if len(candidates) == 1:
        return "project_function", candidates[0]
    if len(candidates) > 1:
        return "ambiguous_project_function", None
    return "unresolved", None


def _resolve_call(index: dict[str, Any], call: dict[str, Any], caller_file: str | None = None) -> tuple[str, dict[str, Any] | None]:
    """Resolve a call, with profile-only qualified/same-file disambiguation."""
    if not index.get("profile_mode"):
        return _resolve_direct(index, call.get("callee") or "")
    expression = call.get("expression") or ""
    match = re.search(r"(?:(?:crate|self|super)\s*::\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*::\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression)
    if match:
        module, name = match.groups()
        candidates = [
            candidate for candidate in index["by_simple"].get(name, [])
            if candidate.get("active") != "false"
            and Path(candidate.get("file") or "").stem == module
        ]
        if len(candidates) == 1:
            return "project_function", candidates[0]
    name = call.get("callee") or ""
    if name and caller_file:
        same_file = [
            candidate for candidate in index["by_simple"].get(name, [])
            if candidate.get("active") != "false" and candidate.get("file") == caller_file
        ]
        if len(same_file) == 1:
            return "project_function", same_file[0]
    return _resolve_direct(index, name)


def _edge(index: dict[str, Any], caller: dict[str, Any], source_kind: str, source_name: str,
          args: list[str], line: int, column: int, context: str, callee_kind: str,
          target: dict[str, Any] | None, target_kind: str, resolution: str,
          member: str | None = None, receiver: str | None = None,
          expression: str | None = None) -> dict[str, Any]:
    file = caller.get("file")
    unsafe = _unsafe_for(index, file, caller.get("name"), line, column)
    target_name = target.get("name") if target else source_name
    target_params = target.get("params", []) if target else []
    target_return = target.get("ret") if target else None
    return {
        "call_site_id": _call_id(index["configuration"], "rust", caller.get("name") or "", file, line, column, source_kind, target_name),
        "configuration": index["configuration"],
        "language": "rust",
        "caller": caller.get("name"),
        "caller_file": file,
        "source_file": file,
        "source_line": line,
        "source_column": column,
        "source_name": source_name,
        "source_expression": expression or source_name,
        "source_args": args,
        "target_name": target_name,
        "target_file": target.get("file") if target else None,
        "target_line": target.get("line") if target else None,
        "target_kind": target_kind,
        "target_params": target_params,
        "target_return_type": target_return,
        "callee_kind": callee_kind,
        "caller_kind": "unsafe_fn" if caller.get("unsafe") else "safe_fn",
        "member": member,
        "receiver": receiver,
        "call_context": context,
        "resolution_status": resolution,
        "evidence_method": "syn_call_and_method_facts+foreign_declarations",
        **unsafe,
    }


TASK_CREATE_APIS = {"xTaskCreate", "xTaskCreateStatic"}


def _function_param_names(function: dict[str, Any]) -> list[str]:
    return [str((param or {}).get("name") or "") for param in function.get("params", [])]


def _incoming_calls(index: dict[str, Any], callee: str, param_index: int) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    result = []
    for function in index["functions"]:
        for call in function.get("calls", []):
            kind, target = _resolve_call(index, call, function.get("file"))
            if kind != "project_function" or target is None or target.get("name") != callee:
                continue
            args = call.get("args", [])
            if param_index < len(args):
                result.append((function, call, args[param_index]))
    return result


def _is_callable_type(kind: dict[str, Any]) -> bool:
    if kind.get("kind") == "function_pointer":
        return True
    if kind.get("kind") == "named" and kind.get("name") == "Option":
        args = kind.get("args") or []
        return bool(args) and _is_callable_type(args[0])
    return False


def callback_registrations(index: dict[str, Any], apis: set[str] | None = None,
                           callback_apis: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Resolve explicit function-item callback arguments to task creation.

    One level of project-wrapper parameter forwarding is supported, which covers
    the selected Rust `handoff_create -> task_create -> xTaskCreate` shape. This
    is registration evidence, not proof of runtime invocation.
    """
    callback_apis = callback_apis or {}
    supported = apis or TASK_CREATE_APIS
    rows: list[dict[str, Any]] = []
    for function in index["functions"]:
        for call in function.get("calls", []):
            if call.get("callee") not in supported:
                continue
            target = index["foreign"].get(call.get("callee") or "")
            if target is None:
                continue
            api_config = callback_apis.get(call.get("callee"), {})
            invocation_kind = api_config.get("invocation_kind", "task_entry")
            params = target.get("params", [])
            callback_index = api_config.get("callback_parameter")
            if callback_index is None:
                for position, param in enumerate(params):
                    kind = param.get("type") or {}
                    name = str((param or {}).get("name") or "")
                    if _is_callable_type(kind) or name in ("task", "pxTaskCode", "taskCode"):
                        callback_index = position
                        break
            args = call.get("args", [])
            expression = args[callback_index] if callback_index is not None and callback_index < len(args) else None
            registration = _edge(index, function, "direct_call", call.get("callee") or "", args,
                                 call["line"], call.get("column", 0), call.get("context", "unclassified"),
                                 "direct_rust_call", target, "rust_ffi", "resolved",
                                 member=call.get("callee"), expression=call.get("expression"))
            symbol = None
            if expression is not None:
                identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)
                symbol = next((item for item in reversed(identifiers) if item not in {"core", "ptr", "null", "NULL", "c_void"}), None)

            def forwarded_candidates(fn: dict[str, Any], symbol: str | None, depth: int, seen: set[tuple[str, str | None]]) -> list[tuple[str | None, dict[str, Any] | None, str | None]]:
                if symbol is None:
                    return [(None, None, None)]
                param_names = _function_param_names(fn)
                if symbol not in param_names or depth >= 4:
                    return [(symbol, None, None)]
                key = (fn.get("name") or "", symbol)
                if key in seen:
                    return [(symbol, None, None)]
                seen = seen | {key}
                position = param_names.index(symbol)
                incoming = _incoming_calls(index, fn.get("name") or "", position)
                if not incoming:
                    return [(symbol, None, None)]
                resolved: list[tuple[str | None, dict[str, Any] | None, str | None]] = []
                for caller_function, application_call, application_expression in incoming:
                    application_registration = _edge(
                        index, caller_function, "direct_call", application_call.get("callee") or "",
                        application_call.get("args", []), application_call["line"],
                        application_call.get("column", 0), application_call.get("context", "unclassified"),
                        "direct_rust_call", fn, "project_function", "resolved",
                        member=application_call.get("callee"), expression=application_call.get("expression"),
                    )
                    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", application_expression)
                    forwarded_symbol = next(
                        (item for item in reversed(identifiers) if item not in {"core", "ptr", "null", "NULL", "c_void"}),
                        symbol,
                    )
                    nested = forwarded_candidates(caller_function, forwarded_symbol, depth + 1, seen)
                    if nested and any(candidate[1] is not None for candidate in nested):
                        resolved.extend((candidate[0], candidate[1], symbol) for candidate in nested)
                    else:
                        resolved.append((forwarded_symbol, application_registration, symbol))
                return resolved

            candidates = forwarded_candidates(function, symbol, 0, set()) if expression is not None else [(None, None, None)]
            if not candidates:
                candidates.append((None, None, None))
            for symbol, application_registration, forwarding in candidates:
                definitions = [candidate for candidate in index["by_simple"].get(symbol or "", []) if candidate.get("active") != "false"]
                if len(definitions) == 1:
                    definition = definitions[0]
                    status = "resolved_callback_definition"
                    file, line = definition.get("file"), definition.get("line")
                elif len(definitions) > 1:
                    status = "ambiguous_callback_definition"
                    file = line = None
                else:
                    status = "unresolved_callback_definition"
                    file = line = None
                rows.append({
                    "configuration": index["configuration"],
                    "registration_call_site_id": registration["call_site_id"],
                    "application_registration_call_site_id": application_registration["call_site_id"] if application_registration else None,
                    "caller": function.get("name"),
                    "source_file": function.get("file"),
                    "source_line": call["line"],
                    "api": call.get("callee"),
                    "callback_symbol": symbol,
                    "callback_file": file,
                    "callback_line": line,
                    "forwarding_parameter": forwarding,
                    "invocation_kind": invocation_kind,
                    "resolution_status": status,
                    "evidence_method": "syn_call+foreign_function_pointer_parameter+bounded_recursive_project_forwarding",
                })
    return rows


def reachable_rust_edges(index: dict[str, Any], impl_type: str, methods: list[str], max_depth: int = 16) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edges: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []

    def add(edge: dict[str, Any]) -> None:
        existing = edges.get(edge["call_site_id"])
        if existing is None:
            edges[edge["call_site_id"]] = edge
        else:
            existing["roots"] = sorted(set(existing.get("roots", [])) | set(edge.get("roots", [])))

    for method in methods:
        candidates = [f for f in index["by_impl"].get((impl_type, method), []) if f.get("active") != "false"]
        if len(candidates) != 1:
            roots.append({"root": method, "status": "unresolved_root" if not candidates else "ambiguous_root", "file": None, "line": None})
            continue
        root = candidates[0]
        roots.append({"root": method, "status": "resolved_root", "file": root.get("file"), "line": root.get("line")})
        queue = [(root, 0, method)]
        visited: set[tuple[str, str | None]] = set()
        while queue:
            function, depth, root_name = queue.pop(0)
            key = (function.get("name") or "", function.get("file"))
            if key in visited or depth > max_depth:
                continue
            visited.add(key)
            for call in function.get("calls", []):
                if call.get("active") == "false":
                    continue
                callee = call.get("callee") or ""
                kind, target = _resolve_call(index, call, function.get("file"))
                edge = _edge(index, function, "direct_call", callee, call.get("args", []), call["line"],
                             call.get("column", 0), call.get("context", "unclassified"),
                             "direct_rust_call", target, kind,
                             "resolved" if kind in ("project_function", "rust_ffi", "language_builtin") else "unresolved",
                             member=callee, expression=call.get("expression"))
                edge["roots"] = [root_name]
                add(edge)
                if kind == "project_function" and target is not None:
                    queue.append((target, depth + 1, root_name))
            for call in function.get("method_calls", []):
                if call.get("active") == "false":
                    continue
                method_name = call.get("method") or ""
                receiver = call.get("receiver") or ""
                if method_name in index.get("builtin_methods", BUILTIN_METHODS):
                    edge = _edge(index, function, "method_call", method_name, call.get("args", []), call["line"],
                                 call.get("column", 0), call.get("context", "unclassified"),
                                 "language_method", None, "language_builtin", "resolved", member=method_name, receiver=receiver, expression=call.get("expression"))
                    edge["roots"] = [root_name]
                    add(edge)
                    continue
                target = None
                if receiver.strip() == "self":
                    candidates = [f for f in index["by_impl"].get((impl_type, method_name), []) if f.get("active") != "false"]
                    if len(candidates) == 1:
                        target = candidates[0]
                if target is None:
                    edge = _edge(index, function, "method_call", method_name, call.get("args", []), call["line"],
                                 call.get("column", 0), call.get("context", "unclassified"),
                                 "unresolved_method", None, "unresolved", "unresolved", member=method_name, receiver=receiver, expression=call.get("expression"))
                else:
                    edge = _edge(index, function, "method_call", method_name, call.get("args", []), call["line"],
                                 call.get("column", 0), call.get("context", "unclassified"),
                                 "project_method", target, "project_method", "resolved", member=method_name, receiver=receiver, expression=call.get("expression"))
                    queue.append((target, depth + 1, root_name))
                edge["roots"] = [root_name]
                add(edge)
    return sorted(edges.values(), key=lambda row: (row["caller_file"] or "", row["source_line"], row["source_column"], row["source_name"])), roots


def reachable_rust_functions(index: dict[str, Any], names: list[str], max_depth: int = 16) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Traverse free-function callback bodies without a general call graph."""
    edges: dict[str, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []
    for name in names:
        candidates = [candidate for candidate in index["by_simple"].get(name, []) if candidate.get("active") != "false"]
        if len(candidates) != 1:
            roots.append({"root": name, "status": "unresolved_root" if not candidates else "ambiguous_root", "file": None, "line": None})
            continue
        root = candidates[0]
        roots.append({"root": name, "status": "resolved_root", "file": root.get("file"), "line": root.get("line")})
        queue = [(root, 0, name)]
        visited: set[tuple[str, str | None]] = set()
        while queue:
            function, depth, root_name = queue.pop(0)
            key = (function.get("name") or "", function.get("file"))
            if key in visited or depth > max_depth:
                continue
            visited.add(key)
            for call in function.get("calls", []):
                if call.get("active") == "false":
                    continue
                callee = call.get("callee") or ""
                kind, target = _resolve_call(index, call, function.get("file"))
                edge = _edge(index, function, "direct_call", callee, call.get("args", []), call["line"],
                             call.get("column", 0), call.get("context", "unclassified"),
                             "direct_rust_call", target, kind,
                             "resolved" if kind in ("project_function", "rust_ffi", "language_builtin") else "unresolved",
                             member=callee, expression=call.get("expression"))
                edge["roots"] = [root_name]
                existing = edges.get(edge["call_site_id"])
                if existing is None:
                    edges[edge["call_site_id"]] = edge
                else:
                    existing["roots"] = sorted(set(existing["roots"]) | {root_name})
                if kind == "project_function" and target is not None:
                    queue.append((target, depth + 1, root_name))
            for call in function.get("method_calls", []):
                if call.get("active") == "false":
                    continue
                method_name = call.get("method") or ""
                if method_name in index.get("builtin_methods", BUILTIN_METHODS):
                    continue
                edge = _edge(index, function, "method_call", method_name, call.get("args", []), call["line"],
                             call.get("column", 0), call.get("context", "unclassified"),
                             "unresolved_method", None, "unresolved", "unresolved",
                             member=method_name, receiver=call.get("receiver"), expression=call.get("expression"))
                edge["roots"] = [root_name]
                edges.setdefault(edge["call_site_id"], edge)
    return sorted(edges.values(), key=lambda row: (row["caller_file"] or "", row["source_line"], row["source_column"], row["source_name"])), roots


def application_method_calls(index: dict[str, Any], impl_type: str, methods: list[str], case_number: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for function in index["functions"]:
        if function.get("name") not in ("run_case", "sample"):
            continue
        for call in function.get("method_calls", []):
            method = call.get("method")
            if method not in methods:
                continue
            candidates = [f for f in index["by_impl"].get((impl_type, method), []) if f.get("active") != "false"]
            target = candidates[0] if len(candidates) == 1 else None
            row = _edge(index, function, "method_call", method, call.get("args", []), call["line"],
                        call.get("column", 0), call.get("context", "unclassified"),
                        "rust_trait_method", target, "project_method" if target else "unresolved",
                        "resolved" if target else "unresolved", member=method, receiver=call.get("receiver"), expression=call.get("expression"))
            row["benchmark_case"] = case_number
            row["level"] = "A_application"
            rows.append(row)
    return rows


def ffi_rows(repo_root: Path, index: dict[str, Any], c_index: dict[str, Any],
             rtos_symbols: set[str] | None = None,
             allocation_symbols: set[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for name, foreign in sorted(index["foreign"].items()):
        candidates = c_index["symbols_by_name"].get(name, [])
        project_defs = [candidate for candidate in candidates if candidate["is_project"] and candidate["is_definition"]]
        project_decls = [candidate for candidate in candidates if candidate["is_project"] and not candidate["is_definition"]]
        external = [candidate for candidate in candidates if not candidate["is_project"]]
        chosen = None
        status = "unresolved"
        kind = "unresolved"
        if len(project_defs) == 1:
            chosen = project_defs[0]; status = "resolved_project_definition"; kind = "c_bridge_function"
        elif len(project_defs) > 1:
            status = "ambiguous_c_symbol"; kind = "ambiguous"
        elif len(project_decls) == 1:
            chosen = project_decls[0]; status = "resolved_project_declaration"; kind = "c_bridge_declaration"
        elif len(project_decls) > 1:
            status = "ambiguous_c_symbol"; kind = "ambiguous"
        elif external:
            signatures = {candidate["signature"] for candidate in external}
            if len(signatures) == 1:
                chosen = sorted(external, key=lambda item: (item["file"] or "", item["line"]))[0]
                status = "resolved_external_declaration"
                kind = "rtos_api" if (RTOS_RE.match(name) or name in ALLOC_NAMES or name in (allocation_symbols or set()) or name in (rtos_symbols or set()) or name.startswith(("x", "v", "ux", "pv"))) else "external_function"
            else:
                status = "ambiguous_c_symbol"; kind = "ambiguous"
        rows.append({
            "configuration": index["configuration"],
            "rust_name": name,
            "rust_file": foreign.get("file"),
            "rust_line": foreign.get("line"),
            "rust_abi": foreign.get("abi_block"),
            "rust_params": foreign.get("params", []),
            "rust_return": foreign.get("ret"),
            "c_target_name": chosen["name"] if chosen else None,
            "c_target_file": chosen["file"] if chosen else None,
            "c_target_line": chosen["line"] if chosen else None,
            "c_target_signature": chosen["signature"] if chosen else None,
            "c_target_kind": kind,
            "resolution_status": status,
            "candidate_count": len(candidates),
            "evidence_method": "exact_symbol_match+libclang_declarations",
        })
    return rows


