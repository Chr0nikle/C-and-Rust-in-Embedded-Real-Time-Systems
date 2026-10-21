"""C call-path extraction for the Stage 2 matched API pilot."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import clang.cindex as clang

from checks import c_extract as base_c_extract
from checks.c_extract import _canonical_type, _is_project, _rel, _tokens, type_spec
from inventory.c_ops import (
    COMPILER_GENERATED,
    EXPLICIT_MACRO_ARGUMENT,
    EXPLICIT_SOURCE,
    MACRO_ARGUMENT_REFERENCE,
    MACRO_PROVENANCE,
    PROJECT_MACRO,
    THIRD_PARTY_MACRO,
    UNRESOLVED_PROVENANCE,
    _invocation_token,
    _location,
    _macro_invocation_range,
    _source_text,
    _provenance,
)

RTOS_RE = re.compile(r"^(x|v|ux|pv|ul|pc)(Queue|Semaphore|Task|Timer|EventGroup|Port|Notify)")
ALLOC_NAMES = {"pvPortMalloc", "vPortFree", "malloc", "calloc", "realloc", "free"}
SOURCE_CACHE: dict[str, str] = {}


def _text(path: str | None) -> str | None:
    if not path:
        return None
    if path not in SOURCE_CACHE:
        SOURCE_CACHE[path] = _source_text(path) or ""
    return SOURCE_CACHE[path]


def _slice(path: str | None, start: int | None, end: int | None) -> str:
    text = _text(path)
    if text is None or start is None or end is None:
        return ""
    return text[max(0, start):max(0, min(len(text), end))].strip()


def _split_args(text: str) -> list[str]:
    open_at = text.find("(")
    if open_at < 0:
        return []
    depth = 0
    parts: list[str] = []
    current: list[str] = []
    index = open_at
    quote: str | None = None
    while index < len(text):
        char = text[index]
        if quote:
            current.append(char)
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "(":
            depth += 1
            if depth > 1:
                current.append(char)
        elif char == ")":
            depth -= 1
            if depth == 0:
                break
            current.append(char)
        elif char == "," and depth == 1:
            parts.append("".join(current).strip())
            current = []
        else:
            if depth >= 1:
                current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail or parts:
        parts.append(tail)
    return parts


def _source_call(node: clang.Cursor, repo_root: Path) -> dict[str, Any]:
    provenance, provenance_details = _provenance(node, repo_root)
    expansion = _location(node, spell=False)
    invocation_range = _macro_invocation_range(expansion)
    source_name = node.spelling
    source_text = ""
    if invocation_range and expansion and expansion.get("file"):
        source_name = _invocation_token(expansion) or source_name
        source_text = _slice(expansion["file"], invocation_range[0], invocation_range[1])
    if not source_text or "(" not in source_text:
        if node.extent:
            source_text = _slice(expansion["file"] if expansion and expansion.get("file") else None,
                                 node.extent.start.offset, node.extent.end.offset)
        if not source_text:
            source_text = " ".join(_tokens(node, 120))
    return {
        "source_name": source_name or node.spelling or "<indirect>",
        "source_expression": source_text,
        "source_args": _split_args(source_text),
        "provenance": provenance,
        "provenance_details": provenance_details,
    }


def _is_function_type(t: clang.Type) -> bool:
    try:
        canonical = _canonical_type(t)
        if canonical.kind in (clang.TypeKind.FUNCTIONPROTO, clang.TypeKind.FUNCTIONNOPROTO):
            return True
        if canonical.kind == clang.TypeKind.POINTER:
            pointee = _canonical_type(canonical.get_pointee())
            return pointee.kind in (clang.TypeKind.FUNCTIONPROTO, clang.TypeKind.FUNCTIONNOPROTO)
    except Exception:
        pass
    return False


def _function_fact(node: clang.Cursor, repo_root: Path, configuration: str) -> dict[str, Any]:
    params = []
    for argument in node.get_arguments():
        params.append({
            "name": argument.spelling,
            "type": type_spec(argument.type),
            "type_text": argument.type.spelling,
        })
    return {
        "configuration": configuration,
        "language": "c",
        "name": node.spelling,
        "usr": node.get_usr(),
        "file": _rel(node.location.file.name if node.location.file else None, repo_root),
        "line": node.location.line,
        "column": node.location.column,
        "is_definition": node.is_definition(),
        "is_project": _is_project(node.location.file.name if node.location.file else None, repo_root),
        "signature": node.type.spelling,
        "return_type": type_spec(node.result_type),
        "return_type_text": node.result_type.spelling,
        "params": params,
    }


def _call_fact(node: clang.Cursor, caller: dict[str, Any], repo_root: Path, configuration: str, context: str,
               rtos_symbols: set[str] | None = None) -> dict[str, Any]:
    source = _source_call(node, repo_root)
    referenced = node.referenced
    target = None
    target_kind = "unresolved"
    resolution = "unresolved"
    member = None
    if referenced is not None and referenced.kind == clang.CursorKind.FUNCTION_DECL:
        target = _function_fact(referenced, repo_root, configuration)
        if target["is_project"] and target["is_definition"]:
            target_kind = "project_function"
            resolution = "resolved_project_definition"
        elif target["is_project"]:
            target_kind = "project_declaration"
            resolution = "resolved_project_declaration"
        elif RTOS_RE.match(target["name"]) or target["name"] in ALLOC_NAMES or target["name"] in (rtos_symbols or set()) or target["name"] in {
            "vTaskDelay", "xTaskGetTickCount", "uxTaskPriorityGet", "vTaskDelete",
            "xTaskCreate", "xTaskCreatePinnedToCore", "vPortYieldFromISR",
        }:
            target_kind = "rtos_api"
            resolution = "resolved_external_api"
        else:
            target_kind = "external_function"
            resolution = "resolved_external_function"
    elif referenced is not None and referenced.kind == clang.CursorKind.FIELD_DECL:
        member = referenced.spelling
        target_kind = "function_pointer_member"
        resolution = "unresolved_member_target"
    else:
        # A function pointer variable or an unresolved callee.
        target_kind = "indirect_or_unresolved"
        resolution = "unresolved"
        source_name = source["source_name"]
        if "->" in source["source_expression"]:
            match = re.search(r"->\s*([A-Za-z_][A-Za-z0-9_]*)", source["source_expression"])
            if match:
                member = match.group(1)

    if target_kind == "function_pointer_member" and member:
        source["source_name"] = member
    children = list(node.get_children())
    argument_children = children[1:] if children and _is_function_type(children[0].type) else children
    argument_types = [type_spec(child.type) for child in argument_children]
    source_args = source["source_args"]
    target_params = target["params"] if target else []
    call_id_material = "|".join((
        configuration, "c", caller["name"], caller["file"], str(node.location.line),
        str(node.location.column), source["source_name"], target["name"] if target else (member or ""),
        source["source_expression"],
    ))
    return {
        "call_site_id": "c-call-" + hashlib.sha256(call_id_material.encode()).hexdigest()[:24],
        "configuration": configuration,
        "language": "c",
        "caller": caller["name"],
        "caller_usr": caller["usr"],
        "caller_file": caller["file"],
        "source_file": _rel(node.location.file.name if node.location.file else None, repo_root),
        "source_line": node.location.line,
        "source_column": node.location.column,
        "source_name": source["source_name"],
        "source_expression": source["source_expression"],
        "source_args": source_args,
        "argument_types": argument_types,
        "target_name": target["name"] if target else (member or source["source_name"]),
        "target_usr": target["usr"] if target else None,
        "target_file": target["file"] if target else None,
        "target_line": target["line"] if target else None,
        "target_definition": bool(target and target["is_definition"]),
        "target_kind": target_kind,
        "target_params": target_params,
        "target_return_type": target["return_type"] if target else None,
        "member": member,
        "macro_provenance": source["provenance"],
        "macro_provenance_details": source["provenance_details"],
        "call_context": context,
        "resolution_status": resolution,
        "evidence_method": "libclang_referenced_cursor+source_expansion_locations",
    }


def _source_for_check(node: clang.Cursor) -> str:
    file_path = node.location.file.name if node.location.file else None
    if node.extent:
        text = _slice(file_path, node.extent.start.offset, node.extent.end.offset)
        if text:
            return text
    return " ".join(_tokens(node, 160))


def _cursor_expression_text(node: clang.Cursor) -> str:
    text = _source_for_check(node)
    return text or " ".join(_tokens(node, 160))


def _normalized_operand(text: str) -> str:
    value = text.strip()
    while value.startswith("(") and value.endswith(")"):
        value = value[1:-1].strip()
    return value


def _is_zero_text(text: str) -> bool:
    import re
    return bool(re.fullmatch(r"0(?:[uUlL]+)?", _normalized_operand(text)))


def _is_null_text(text: str) -> bool:
    return _normalized_operand(text).lower() in ("null", "nullptr")


def _canonical_kind(cursor: clang.Cursor) -> str | None:
    if cursor.type is None:
        return None
    try:
        return cursor.type.get_canonical().kind.name
    except Exception:
        return cursor.type.kind.name


def _condition_shape(node: clang.Cursor) -> dict[str, Any]:
    """Return the top-level operator and operands, not the full argument text."""
    children = list(node.get_children())
    if node.kind in (clang.CursorKind.BINARY_OPERATOR, clang.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR) and len(children) >= 2:
        left, right = children[0], children[1]
        left_text = _cursor_expression_text(left)
        right_text = _cursor_expression_text(right)
        whole = _cursor_expression_text(node)
        operator = None
        if whole.startswith(left_text) and len(whole) > len(left_text):
            operator = whole[len(left_text):].strip().split()[0] if whole[len(left_text):].strip() else None
        return {
            "kind": "binary",
            "operator": operator,
            "left": left_text,
            "right": right_text,
            "left_call": left.kind == clang.CursorKind.CALL_EXPR,
            "right_call": right.kind == clang.CursorKind.CALL_EXPR,
            "left_kind": left.kind.name,
            "right_kind": right.kind.name,
            "left_type_kind": _canonical_kind(left),
            "right_type_kind": _canonical_kind(right),
            "left_zero": _is_zero_text(left_text),
            "right_zero": _is_zero_text(right_text),
            "left_null": _is_null_text(left_text),
            "right_null": _is_null_text(right_text),
        }
    if node.kind == clang.CursorKind.UNARY_OPERATOR:
        child = children[0] if children else node
        return {
            "kind": "unary",
            "operator": " ".join(_tokens(node, 40)).replace(" ".join(_tokens(child, 160)), "").strip(),
            "left": _cursor_expression_text(child),
            "right": None,
            "left_call": child.kind == clang.CursorKind.CALL_EXPR,
            "right_call": False,
            "left_kind": child.kind.name,
            "right_kind": None,
            "left_type_kind": _canonical_kind(child),
            "right_type_kind": None,
            "left_zero": _is_zero_text(_cursor_expression_text(child)),
            "right_zero": False,
            "left_null": _is_null_text(_cursor_expression_text(child)),
            "right_null": False,
        }
    if node.kind == clang.CursorKind.CALL_EXPR:
        return {
            "kind": "call",
            "operator": None,
            "left": _cursor_expression_text(node),
            "right": None,
            "left_call": True,
            "right_call": False,
            "left_kind": "CALL_EXPR",
            "right_kind": None,
            "left_type_kind": node.type.kind.name if node.type is not None else None,
            "right_type_kind": None,
        }
    return {
        "kind": "other",
        "operator": None,
        "left": _cursor_expression_text(node),
        "right": None,
        "left_call": False,
        "right_call": False,
        "left_kind": node.kind.name,
        "right_kind": None,
        "left_type_kind": node.type.kind.name if node.type is not None else None,
        "right_type_kind": None,
    }


def _check_fact(node: clang.Cursor, caller: dict[str, Any], repo_root: Path, configuration: str,
                kind: str) -> dict[str, Any] | None:
    condition = " ".join(_tokens(node, 160))
    if not condition:
        condition = _source_for_check(node)
    if not condition:
        return None
    provenance, _ = _provenance(node, repo_root)
    if provenance not in (EXPLICIT_SOURCE, EXPLICIT_MACRO_ARGUMENT):
        return None
    file_path = node.location.file.name if node.location.file else None
    return {
        "configuration": configuration,
        "language": "c",
        "function": caller["name"],
        "source_file": _rel(file_path, repo_root),
        "source_line": node.location.line,
        "source_column": node.location.column,
        "end_line": node.extent.end.line if node.extent else node.location.line,
        "end_column": node.extent.end.column if node.extent else node.location.column,
        "kind": kind,
        "condition": condition,
        "condition_shape": _condition_shape(node),
        "macro_provenance": provenance,
        "evidence_method": "libclang_condition_ast",
    }


def _walk_calls(node: clang.Cursor, caller: dict[str, Any], repo_root: Path, configuration: str,
                calls: list[dict[str, Any]], checks: list[dict[str, Any]], context: str = "unclassified",
                rtos_symbols: set[str] | None = None) -> None:
    kind = node.kind
    if kind == clang.CursorKind.IF_STMT:
        children = list(node.get_children())
        condition_node = children[0] if children else node
        check = _check_fact(condition_node, caller, repo_root, configuration, "if")
        if check:
            checks.append(check)
        for child_index, child in enumerate(children):
            next_context = "condition_check" if child_index == 0 else "control"
            _walk_calls(child, caller, repo_root, configuration, calls, checks, next_context, rtos_symbols)
        return
    if kind == clang.CursorKind.WHILE_STMT:
        children = list(node.get_children())
        condition_node = children[0] if children else node
        check = _check_fact(condition_node, caller, repo_root, configuration, "while")
        if check:
            checks.append(check)
        for child_index, child in enumerate(children):
            next_context = "condition_check" if child_index == 0 else "control"
            _walk_calls(child, caller, repo_root, configuration, calls, checks, next_context, rtos_symbols)
        return
    if kind == clang.CursorKind.DO_STMT:
        children = list(node.get_children())
        condition_node = children[-1] if children else node
        check = _check_fact(condition_node, caller, repo_root, configuration, "do")
        if check:
            checks.append(check)
        for child_index, child in enumerate(children):
            next_context = "condition_check" if child_index == len(children) - 1 else "control"
            _walk_calls(child, caller, repo_root, configuration, calls, checks, next_context, rtos_symbols)
        return
    if kind == clang.CursorKind.FOR_STMT:
        children = list(node.get_children())
        condition_node = children[1] if len(children) >= 3 else None
        if condition_node is not None:
            check = _check_fact(condition_node, caller, repo_root, configuration, "for")
            if check:
                checks.append(check)
        for child_index, child in enumerate(children):
            if child_index == 1:
                next_context = "condition_check"
            elif child_index == 2:
                next_context = "stored"
            elif child_index == len(children) - 1:
                next_context = "control"
            else:
                next_context = "stored"
            _walk_calls(child, caller, repo_root, configuration, calls, checks, next_context, rtos_symbols)
        return
    if kind == clang.CursorKind.RETURN_STMT:
        for child in node.get_children():
            _walk_calls(child, caller, repo_root, configuration, calls, checks, "returned", rtos_symbols)
        return
    if kind == clang.CursorKind.VAR_DECL:
        for child in node.get_children():
            _walk_calls(child, caller, repo_root, configuration, calls, checks, "stored", rtos_symbols)
        return
    if kind in (clang.CursorKind.BINARY_OPERATOR, clang.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
        children = list(node.get_children())
        tokens = _tokens(node, 120)
        operator = None
        if len(children) >= 2:
            left = _tokens(children[0], 120)
            if tokens[:len(left)] == left and len(tokens) > len(left):
                operator = tokens[len(left)]
        if operator == "=":
            for index, child in enumerate(children):
                _walk_calls(child, caller, repo_root, configuration, calls, checks, "assigned_target" if index == 0 else "assigned", rtos_symbols)
            return
        for child in children:
            _walk_calls(child, caller, repo_root, configuration, calls, checks, context, rtos_symbols)
        return
    if kind == clang.CursorKind.COMPOUND_STMT:
        for child in node.get_children():
            _walk_calls(child, caller, repo_root, configuration, calls, checks, "discarded", rtos_symbols)
        return
    if kind == clang.CursorKind.CALL_EXPR:
        calls.append(_call_fact(node, caller, repo_root, configuration, context, rtos_symbols))
        for child in node.get_children():
            _walk_calls(child, caller, repo_root, configuration, calls, checks, "argument", rtos_symbols)
        return
    for child in node.get_children():
        _walk_calls(child, caller, repo_root, configuration, calls, checks, context, rtos_symbols)

def extract_c_index(repo_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    configuration = config["configuration"]
    index = clang.Index.create()
    functions: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    symbols: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen_functions: set[tuple[Any, ...]] = set()
    seen_calls: dict[str, dict[str, Any]] = {}

    previous_source_roots = base_c_extract.PROJECT_SOURCE_ROOTS
    previous_header_roots = base_c_extract.PROJECT_HEADER_ROOTS
    if config.get("project_source_roots"):
        base_c_extract.PROJECT_SOURCE_ROOTS = tuple(config["project_source_roots"])
        base_c_extract.PROJECT_HEADER_ROOTS = tuple(config.get("project_header_roots", config["project_source_roots"]))
    try:
        for unit in config["translation_units"]:
            tu = index.parse(unit["abs_path"], args=unit["clang_args"], options=clang.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
            diagnostics.extend({
                "severity": str(diagnostic.severity), "message": diagnostic.spelling,
                "file": _rel(diagnostic.location.file.name if diagnostic.location.file else None, repo_root),
                "line": diagnostic.location.line,
            } for diagnostic in tu.diagnostics)
            for node in tu.cursor.walk_preorder():
                if node.kind == clang.CursorKind.FUNCTION_DECL:
                    fact = _function_fact(node, repo_root, configuration)
                    symbols.append(fact)
                    key = (fact["name"], fact["file"], fact["line"], fact["is_definition"])
                    if key not in seen_functions:
                        seen_functions.add(key)
                        functions.append(fact)
                    if fact["is_project"] and fact["is_definition"]:
                        local_calls: list[dict[str, Any]] = []
                        local_checks: list[dict[str, Any]] = []
                        for child in node.get_children():
                            _walk_calls(child, fact, repo_root, configuration, local_calls, local_checks, "unclassified", set(config.get("rtos_symbols", [])))
                        for call in local_calls:
                            existing = seen_calls.get(call["call_site_id"])
                            if existing is None:
                                seen_calls[call["call_site_id"]] = call
                                calls.append(call)
                        checks.extend(local_checks)
    finally:
        base_c_extract.PROJECT_SOURCE_ROOTS = previous_source_roots
        base_c_extract.PROJECT_HEADER_ROOTS = previous_header_roots

    definitions_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for function in functions:
        if function["is_definition"] and function["is_project"]:
            definitions_by_name[function["name"]].append(function)
    if config.get("follow_project_declarations"):
        for call in calls:
            if call.get("target_kind") != "project_declaration":
                continue
            candidates = definitions_by_name.get(call.get("target_name") or "", [])
            if len(candidates) == 1:
                definition = candidates[0]
                call["target_kind"] = "project_function"
                call["target_file"] = definition["file"]
                call["target_line"] = definition["line"]
                call["target_usr"] = definition.get("usr")
                call["resolution_status"] = "resolved_project_definition"
                call["evidence_method"] = (call.get("evidence_method") or "") + "+unique_project_definition"
    symbols_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol in symbols:
        symbols_by_name[symbol["name"]].append(symbol)
    calls_by_function: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        calls_by_function[(call["caller"], call["caller_file"])].append(call)
    result = {
        "configuration": configuration,
        "functions": functions,
        "symbols": symbols,
        "definitions_by_name": definitions_by_name,
        "symbols_by_name": symbols_by_name,
        "calls": calls,
        "calls_by_function": calls_by_function,
        "checks": checks,
        "diagnostics": diagnostics,
    }
    result["callback_registrations"] = callback_registrations(
        result,
        apis=set(config.get("callback_apis", {})) or None,
        callback_apis=config.get("callback_apis", {}),
    )
    return result


TASK_CREATE_APIS = {"xTaskCreate", "xTaskCreateStatic"}


def callback_registrations(index_data: dict[str, Any], apis: set[str] | None = None,
                          callback_apis: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Record explicit function-item arguments passed to supported APIs.

    This is registration evidence, not a synchronous call edge and not proof of
    runtime invocation. The pipeline may traverse a uniquely resolved callback
    definition as a separate external-entry path.
    """
    callback_apis = callback_apis or {}
    supported = apis or TASK_CREATE_APIS
    rows: list[dict[str, Any]] = []
    for call in index_data["calls"]:
        if call.get("target_name") not in supported:
            continue
        api_config = callback_apis.get(call.get("target_name"), {})
        invocation_kind = api_config.get("invocation_kind", "task_entry")
        params = call.get("target_params", [])
        callback_index = api_config.get("callback_parameter")
        if callback_index is None:
            for index, param in enumerate(params):
                kind = str((param.get("type") or {}).get("kind", ""))
                name = str(param.get("name") or "")
                if kind == "function_pointer" or name in ("pxTaskCode", "task", "taskCode"):
                    callback_index = index
                    break
        if callback_index is None or callback_index >= len(call.get("source_args", [])):
            rows.append({
                "configuration": call["configuration"],
                "registration_call_site_id": call["call_site_id"],
                "caller": call["caller"],
                "source_file": call["source_file"],
                "source_line": call["source_line"],
                "api": call["target_name"],
                "callback_symbol": None,
                "callback_file": None,
                "callback_line": None,
                "invocation_kind": invocation_kind,
                "resolution_status": "unresolved_callback_argument",
                "evidence_method": "libclang_call+function_pointer_parameter_position",
            })
            continue
        expression = call["source_args"][callback_index]
        identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression)
        symbol = next((item for item in reversed(identifiers) if item not in {"NULL", "TaskFunction_t"}), None)
        definitions = index_data["definitions_by_name"].get(symbol or "", [])
        if len(definitions) == 1:
            definition = definitions[0]
            status = "resolved_callback_definition"
            file, line = definition["file"], definition["line"]
        elif len(definitions) > 1:
            status = "ambiguous_callback_definition"
            file = line = None
        else:
            status = "unresolved_callback_definition"
            file = line = None
        rows.append({
            "configuration": call["configuration"],
            "registration_call_site_id": call["call_site_id"],
            "caller": call["caller"],
            "source_file": call["source_file"],
            "source_line": call["source_line"],
            "api": call["target_name"],
            "callback_symbol": symbol,
            "callback_file": file,
            "callback_line": line,
            "invocation_kind": invocation_kind,
            "resolution_status": status,
            "evidence_method": "libclang_call+function_pointer_parameter_position",
        })
    return rows

def reachable_edges(index_data: dict[str, Any], roots: list[str], max_depth: int = 16) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edges_by_id: dict[str, dict[str, Any]] = {}
    root_rows: list[dict[str, Any]] = []
    for root in roots:
        definitions = index_data["definitions_by_name"].get(root, [])
        if not definitions:
            root_rows.append({"root": root, "status": "unresolved_root", "definition_file": None, "definition_line": None})
            continue
        if len(definitions) > 1:
            root_rows.append({"root": root, "status": "ambiguous_root", "definition_file": None, "definition_line": None, "candidates": len(definitions)})
            continue
        definition = definitions[0]
        root_rows.append({"root": root, "status": "resolved_root", "definition_file": definition["file"], "definition_line": definition["line"]})
        queue = [(definition, 0, root)]
        visited: set[tuple[str, str]] = set()
        while queue:
            function, depth, root_name = queue.pop(0)
            function_key = (function["name"], function["file"])
            if function_key in visited or depth > max_depth:
                continue
            visited.add(function_key)
            for call in index_data["calls_by_function"].get((function["name"], function["file"]), []):
                edge = dict(call)
                edge["root"] = root_name
                edge["depth"] = depth
                edge["reachability"] = "resolved_direct_path" if edge["resolution_status"].startswith("resolved") else "unresolved_edge"
                existing = edges_by_id.get(edge["call_site_id"])
                if existing is None:
                    edge["roots"] = [root_name]
                    edges_by_id[edge["call_site_id"]] = edge
                else:
                    existing["roots"] = sorted(set(existing["roots"]) | {root_name})
                if call["target_kind"] == "project_function":
                    candidates = index_data["definitions_by_name"].get(call["target_name"], [])
                    target = next((candidate for candidate in candidates if candidate["usr"] == call["target_usr"]), None)
                    if target is None and len(candidates) == 1:
                        target = candidates[0]
                    if target is not None:
                        queue.append((target, depth + 1, root_name))
    return sorted(edges_by_id.values(), key=lambda row: (row["caller_file"], row["source_line"], row["source_column"], row["source_name"])), root_rows


def application_calls(index_data: dict[str, Any], method_map: dict[str, str], case_number: int) -> list[dict[str, Any]]:
    """Find calls from runner.c sample/bench_run_case to bench_port method pointers."""
    rows: list[dict[str, Any]] = []
    for call in index_data["calls"]:
        if call["caller"] not in ("sample", "bench_run_case"):
            continue
        member = call.get("member")
        if not member or member not in method_map:
            continue
        row = dict(call)
        row["benchmark_case"] = case_number
        row["level"] = "A_application"
        row["target_name"] = method_map[member]
        row["target_kind"] = "port_function_pointer"
        target_params = method_map[member]
        row["resolution_status"] = "resolved_from_bench_port_initializer"
        row["evidence_method"] = "runner_member_call+bench_port_initializer"
        rows.append(row)
    return rows
