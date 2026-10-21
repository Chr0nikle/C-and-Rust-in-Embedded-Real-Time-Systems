"""Syntax-aware C extraction using libclang's target-aware AST.

The extractor records facts only. Contract decisions live in the check modules.
Compiler flags are supplied by source discovery and come from the real project
Makefile; this module never parses C with host defaults.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import clang.cindex as clang


PROJECT_HEADER_ROOTS = (
    "firmware/common/c/include",
    "firmware/c-kernels/freertos/c/stm32f4/include",
    "firmware/c-kernels/freertos/c/stm32f4/src",
)
PROJECT_SOURCE_ROOTS = (
    "firmware/common/c/src",
    "firmware/c-kernels/freertos/c/stm32f4/src",
)


def _rel(path: str | None, repo_root: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(repo_root))
    except (ValueError, OSError):
        return path


def _origin(path: str | None, repo_root: Path) -> str:
    rel = _rel(path, repo_root)
    if rel and (rel.startswith(PROJECT_SOURCE_ROOTS) or rel.startswith(PROJECT_HEADER_ROOTS)):
        return "project"
    return "third_party_or_system"


def _canonical_type(t: clang.Type) -> clang.Type:
    try:
        return t.get_canonical()
    except Exception:
        return t


INTEGER_KINDS = {
    clang.TypeKind.BOOL: (1, False, "bool"),
    clang.TypeKind.CHAR_S: (8, True, "char"),
    clang.TypeKind.CHAR_U: (8, False, "char"),
    clang.TypeKind.SCHAR: (8, True, "signed char"),
    clang.TypeKind.UCHAR: (8, False, "unsigned char"),
    clang.TypeKind.SHORT: (16, True, "short"),
    clang.TypeKind.USHORT: (16, False, "unsigned short"),
    clang.TypeKind.INT: (32, True, "int"),
    clang.TypeKind.UINT: (32, False, "unsigned int"),
    clang.TypeKind.LONG: (32, True, "long"),
    clang.TypeKind.ULONG: (32, False, "unsigned long"),
    clang.TypeKind.LONGLONG: (64, True, "long long"),
    clang.TypeKind.ULONGLONG: (64, False, "unsigned long long"),
    clang.TypeKind.INT128: (128, True, "__int128"),
    clang.TypeKind.UINT128: (128, False, "unsigned __int128"),
    clang.TypeKind.ENUM: (32, True, "enum"),
    clang.TypeKind.FLOAT: (32, True, "float"),
    clang.TypeKind.DOUBLE: (64, True, "double"),
    clang.TypeKind.LONGDOUBLE: (64, True, "long double"),
}


def type_spec(t: clang.Type, depth: int = 0) -> dict[str, Any]:
    """Return a target-resolved ABI shape for a Clang type."""
    if depth > 16:
        return {"kind": "unsupported", "text": t.spelling, "reason": "type nesting limit"}
    canon = _canonical_type(t)
    kind = canon.kind
    size = canon.get_size()
    align = canon.get_align()
    text = canon.spelling or t.spelling

    if kind == clang.TypeKind.VOID:
        return {"kind": "void", "size": None, "align": None, "text": text}
    if kind in INTEGER_KINDS:
        bits, signed, name = INTEGER_KINDS[kind]
        return {
            "kind": "integer",
            "name": name,
            "bits": bits,
            "size": size if size >= 0 else None,
            "align": align if align >= 0 else None,
            "signed": signed,
            "text": text,
        }
    if kind == clang.TypeKind.POINTER:
        pointee = canon.get_pointee()
        pointee_kind = _canonical_type(pointee).kind
        if pointee_kind in (clang.TypeKind.FUNCTIONPROTO, clang.TypeKind.FUNCTIONNOPROTO):
            function = type_spec(pointee, depth + 1)
            return {
                "kind": "function_pointer",
                "abi": "C",
                "params": function.get("params", []),
                "ret": function.get("ret"),
                "variadic": function.get("variadic", False),
                "size": size if size >= 0 else None,
                "align": align if align >= 0 else None,
                "text": text,
            }
        return {
            "kind": "pointer",
            "to": type_spec(pointee, depth + 1),
            "size": size if size >= 0 else None,
            "align": align if align >= 0 else None,
            "text": text,
        }
    if kind in (clang.TypeKind.FUNCTIONPROTO, clang.TypeKind.FUNCTIONNOPROTO):
        params = []
        for arg in canon.argument_types() or []:
            params.append(type_spec(arg, depth + 1))
        return {
            "kind": "function",
            "abi": "C",
            "params": params,
            "ret": type_spec(canon.get_result(), depth + 1),
            "variadic": kind == clang.TypeKind.FUNCTIONNOPROTO,
            "size": size if size >= 0 else None,
            "align": align if align >= 0 else None,
            "text": text,
        }
    if kind in (clang.TypeKind.CONSTANTARRAY, clang.TypeKind.INCOMPLETEARRAY):
        return {
            "kind": "array",
            "elements": canon.get_array_size() if kind == clang.TypeKind.CONSTANTARRAY else None,
            "elem": type_spec(canon.get_array_element_type(), depth + 1),
            "size": size if size >= 0 else None,
            "align": align if align >= 0 else None,
            "text": text,
        }
    if kind == clang.TypeKind.RECORD:
        return {
            "kind": "record",
            "name": canon.get_declaration().spelling,
            "size": size if size >= 0 else None,
            "align": align if align >= 0 else None,
            "text": text,
        }
    if kind == clang.TypeKind.UNEXPOSED:
        declaration = canon.get_declaration()
        underlying = declaration.underlying_typedef_type if declaration.kind == clang.CursorKind.TYPEDEF_DECL else None
        if underlying is not None:
            return type_spec(underlying, depth + 1)
    return {
        "kind": "unsupported",
        "size": size if size >= 0 else None,
        "align": align if align >= 0 else None,
        "text": text,
    }


def _tokens(cursor: clang.Cursor, limit: int = 80) -> list[str]:
    try:
        return [token.spelling for token in cursor.get_tokens()][:limit]
    except Exception:
        return []


def _descendant_literals(cursor: clang.Cursor) -> list[int]:
    values: list[int] = []
    if cursor.kind == clang.CursorKind.INTEGER_LITERAL:
        text = "".join(token.spelling for token in cursor.get_tokens())
        try:
            values.append(int(text.replace("u", "").replace("U", "").replace("l", "").replace("L", ""), 0))
        except ValueError:
            pass
    for child in cursor.get_children():
        values.extend(_descendant_literals(child))
    return values


def _descendant_tokens(cursor: clang.Cursor, limit: int = 120) -> list[str]:
    result: list[str] = []
    for token in _tokens(cursor, limit):
        result.append(token)
    for child in cursor.get_children():
        result.extend(_descendant_tokens(child, limit))
    return result[:limit]


def _decl_refs(cursor: clang.Cursor) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if cursor.kind == clang.CursorKind.DECL_REF_EXPR:
        referenced = cursor.referenced
        if referenced is not None:
            t = referenced.type
            canon = _canonical_type(t)
            entry = {
                "name": referenced.spelling,
                "cursor_kind": str(referenced.kind),
                "type": type_spec(t),
                "declared_line": referenced.location.line,
                "array_elements": canon.get_array_size() if canon.kind == clang.TypeKind.CONSTANTARRAY else None,
                "type_size": canon.get_size() if canon.get_size() >= 0 else None,
            }
            if canon.kind == clang.TypeKind.POINTER:
                pointee = _canonical_type(canon.get_pointee())
                entry["pointee_size"] = pointee.get_size() if pointee.get_size() >= 0 else None
            refs.append(entry)
    for child in cursor.get_children():
        refs.extend(_decl_refs(child))
    return refs


def _sizeof_value(cursor: clang.Cursor) -> dict[str, Any] | None:
    tokens = _descendant_tokens(cursor, 40)
    if "sizeof" not in tokens:
        return None
    type_names: list[str] = []
    sizes: list[int] = []

    def visit(node: clang.Cursor) -> None:
        if node.kind == clang.CursorKind.TYPE_REF:
            type_names.append(node.spelling)
            size = node.type.get_size()
            if size >= 0:
                sizes.append(size)
        for child in node.get_children():
            visit(child)

    visit(cursor)
    if not type_names:
        return None
    return {"type_names": type_names, "sizes": sizes, "tokens": tokens, "pointer_operand": "*" in tokens}


def _expr_facts(cursor: clang.Cursor) -> dict[str, Any]:
    return {
        "tokens": _descendant_tokens(cursor),
        "literals": _descendant_literals(cursor),
        "declrefs": _decl_refs(cursor),
        "has_address_of": "&" in _descendant_tokens(cursor),
        "sizeof": _sizeof_value(cursor),
        "type": type_spec(cursor.type),
        "line": cursor.location.line,
    }


def _is_project(path: str | None, repo_root: Path) -> bool:
    return _origin(path, repo_root) == "project"


def _array_values(cursor: clang.Cursor) -> list[int]:
    values: list[int] = []
    for child in cursor.get_children():
        if child.kind in (clang.CursorKind.INIT_LIST_EXPR, clang.CursorKind.UNEXPOSED_EXPR):
            values.extend(_descendant_literals(child))
    return values


def _usr(cursor: clang.Cursor) -> str:
    try:
        return cursor.get_usr() or ""
    except Exception:
        return ""


def _column(cursor: clang.Cursor) -> int | None:
    return cursor.location.column if cursor.location.column else None


def _extent(cursor: clang.Cursor) -> tuple[int | None, int | None]:
    try:
        return cursor.extent.start.offset, cursor.extent.end.offset
    except Exception:
        return None, None


def _array_base_return(cursor: clang.Cursor) -> dict[str, Any] | None:
    """Return the array declaration only when the return expression is the
    array base itself, after stripping implicit/parenthesised wrappers.

    A return of an element (`slots[i]`) or an arbitrary expression is not an
    array-base accessor for CHECK-03.
    """
    children = list(cursor.get_children())
    if not children:
        return None
    expr = children[-1]
    while expr.kind in (clang.CursorKind.UNEXPOSED_EXPR, clang.CursorKind.PAREN_EXPR):
        expr_children = list(expr.get_children())
        if len(expr_children) != 1:
            return None
        expr = expr_children[0]
    if expr.kind != clang.CursorKind.DECL_REF_EXPR:
        return None
    referenced = expr.referenced
    if referenced is None:
        return None
    canon = _canonical_type(referenced.type)
    if canon.kind != clang.TypeKind.CONSTANTARRAY:
        return None
    return {
        "array": referenced.spelling,
        "elements": canon.get_array_size(),
        "line": cursor.location.line,
        "column": _column(cursor),
        "return_kind": "array_base",
    }


def _dedup(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(json.dumps(row.get(field), sort_keys=True) if isinstance(row.get(field), (dict, list)) else str(row.get(field)) for field in fields)
        if key not in merged:
            row["included_by"] = sorted(set(row.get("included_by", [])))
            merged[key] = row
        else:
            existing = merged[key]
            existing["included_by"] = sorted(set(existing.get("included_by", [])) | set(row.get("included_by", [])))
    return list(merged.values())


def extract_c(
    repo_root: Path,
    translation_units: list[dict[str, Any]],
    clang_args: list[str],
) -> dict[str, Any]:
    """Parse the discovered translation units and return source facts."""
    index = clang.Index.create()
    functions: list[dict[str, Any]] = []
    function_symbols: dict[str, list[dict[str, Any]]] = {}
    arrays: list[dict[str, Any]] = []
    variables: list[dict[str, Any]] = []
    structs: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    pointer_decls: list[dict[str, Any]] = []
    dereferences: list[dict[str, Any]] = []
    subscripts: list[dict[str, Any]] = []
    allocation_calls: list[dict[str, Any]] = []
    kernel_calls: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    parse_failures: list[dict[str, Any]] = []

    alloc_names = {"pvPortMalloc", "vPortFree", "malloc", "calloc", "realloc", "free"}
    kernel_prefix = re.compile(r"^(x|v|ux|pv|ul|pc)(Queue|Semaphore|Task|Timer|EventGroup|Port|Malloc|Free|Notify)")

    for unit in translation_units:
        source = unit["path"]
        tu_rel = str(Path(source).resolve().relative_to(repo_root))
        args = list(clang_args) + list(unit.get("args", []))
        tu = index.parse(
            source,
            args=args,
            options=clang.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
        )
        file_diags = [
            {
                "severity": str(diag.severity),
                "message": diag.spelling,
                "file": _rel(diag.location.file.name if diag.location.file else None, repo_root),
                "line": diag.location.line,
            }
            for diag in tu.diagnostics
        ]
        diagnostics.extend(file_diags)
        files.append(
            {
                "path": str(Path(source).resolve().relative_to(repo_root)),
                "clang_args": args,
                "diagnostics": len(file_diags),
                "parse_failed": any(d["severity"] in ("3", "4") for d in file_diags),
            }
        )
        if any(d["severity"] in ("3", "4") for d in file_diags):
            parse_failures.append({"file": source, "diagnostics": file_diags})

        def walk(node: clang.Cursor, current_function: str | None = None, in_project: bool = False) -> None:
            location_file = node.location.file.name if node.location.file else None
            project = _is_project(location_file, repo_root)
            resolved = str(Path(location_file).resolve()) if location_file else None

            if node.kind == clang.CursorKind.FUNCTION_DECL:
                sig = {
                    "name": node.spelling,
                    "file": _rel(location_file, repo_root),
                    "line": node.location.line,
                    "column": _column(node),
                    "usr": _usr(node),
                    "type": type_spec(node.type),
                    "params": [
                        {
                            "name": child.spelling,
                            "type": type_spec(child.type),
                            "line": child.location.line,
                            "column": _column(child),
                        }
                        for child in node.get_children()
                        if child.kind == clang.CursorKind.PARM_DECL
                    ],
                    "return": type_spec(node.type.get_result()) if node.type.kind in (clang.TypeKind.FUNCTIONPROTO, clang.TypeKind.FUNCTIONNOPROTO) else None,
                    "is_definition": node.is_definition(),
                    "origin": _origin(location_file, repo_root),
                    "included_by": [tu_rel],
                }
                function_symbols.setdefault(node.spelling, []).append(sig)
                if project and node.is_definition():
                    returns: list[dict[str, Any]] = []
                    def collect_returns(cursor: clang.Cursor) -> None:
                        if cursor.kind == clang.CursorKind.RETURN_STMT:
                            base = _array_base_return(cursor)
                            if base is not None:
                                returns.append(base)
                        for child in cursor.get_children():
                            collect_returns(child)
                    collect_returns(node)
                    sig["array_returns"] = returns
                if project and _origin(location_file, repo_root) == "project":
                    functions.append(sig)
                next_function = node.spelling if node.is_definition() else current_function
                for child in node.get_children():
                    walk(child, next_function, project)
                return

            if node.kind == clang.CursorKind.VAR_DECL and project:
                canon = _canonical_type(node.type)
                record = {
                    "name": node.spelling,
                    "file": _rel(location_file, repo_root),
                    "line": node.location.line,
                    "column": _column(node),
                    "usr": _usr(node),
                    "function": current_function,
                    "type": type_spec(node.type),
                    "is_array": canon.kind == clang.TypeKind.CONSTANTARRAY,
                    "elements": canon.get_array_size() if canon.kind == clang.TypeKind.CONSTANTARRAY else None,
                    "bytes": canon.get_size() if canon.get_size() >= 0 else None,
                    "values": _array_values(node),
                    "init": _expr_facts(node) if list(node.get_children()) else None,
                    "included_by": [tu_rel],
                }
                variables.append(record)
                if record["is_array"]:
                    arrays.append(record)
                if node.type.kind == clang.TypeKind.POINTER:
                    pointer_type = type_spec(node.type)
                    pointer_decls.append(
                        {
                            "name": node.spelling,
                            "file": record["file"],
                            "line": record["line"],
                            "column": record["column"],
                            "usr": record["usr"],
                            "function": current_function,
                            "type": pointer_type,
                            "kind": "function_pointer_declaration" if pointer_type.get("kind") == "function_pointer" else "pointer_declaration",
                            "included_by": [tu_rel],
                        }
                    )
                # VarDecl initializer is represented in children; walk them for calls.
                for child in node.get_children():
                    walk(child, current_function, project)
                return

            if node.kind == clang.CursorKind.PARM_DECL and project:
                if node.type.kind == clang.TypeKind.POINTER:
                    pointer_type = type_spec(node.type)
                    pointer_decls.append(
                        {
                            "name": node.spelling,
                            "file": _rel(location_file, repo_root),
                            "line": node.location.line,
                            "column": _column(node),
                            "usr": _usr(node),
                            "function": current_function,
                            "type": pointer_type,
                            "kind": "function_pointer_declaration" if pointer_type.get("kind") == "function_pointer" else "pointer_declaration",
                            "included_by": [tu_rel],
                        }
                    )
                for child in node.get_children():
                    walk(child, current_function, project)
                return

            if node.kind == clang.CursorKind.STRUCT_DECL and project and node.is_definition():
                layout = type_spec(node.type)
                if node.spelling:
                    fields = []
                    for child in node.get_children():
                        if child.kind == clang.CursorKind.FIELD_DECL:
                            field_name = child.spelling
                            offset_bits = None
                            try:
                                offset_bits = node.type.get_offset(field_name)
                            except Exception:
                                offset_bits = None
                            offset = None
                            if offset_bits is not None and offset_bits >= 0 and offset_bits % 8 == 0:
                                offset = offset_bits // 8
                            fields.append(
                                {
                                    "name": field_name,
                                    "type": type_spec(child.type),
                                    "line": child.location.line,
                                    "offset": offset,
                                    "offset_bits": offset_bits,
                                }
                            )
                    structs.append(
                        {
                            "name": node.spelling,
                            "file": _rel(location_file, repo_root),
                            "line": node.location.line,
                            "column": _column(node),
                            "usr": _usr(node),
                            "size": layout.get("size"),
                            "align": layout.get("align"),
                            "fields": fields,
                            "included_by": [tu_rel],
                        }
                    )
                for child in node.get_children():
                    walk(child, current_function, project)
                return

            if node.kind == clang.CursorKind.CALL_EXPR and project:
                name = node.spelling
                args = [_expr_facts(arg) for arg in node.get_arguments()]
                start, end = _extent(node)
                category = "allocation_or_deallocation" if name in alloc_names else ("kernel_api" if kernel_prefix.match(name) else "ordinary_call")
                call = {
                    "name": name,
                    "file": _rel(location_file, repo_root),
                    "line": node.location.line,
                    "column": _column(node),
                    "usr": _usr(node),
                    "function": current_function,
                    "args": args,
                    "return_type": type_spec(node.type),
                    "category": category,
                    "extent_start": start,
                    "extent_end": end,
                    "included_by": [tu_rel],
                }
                calls.append(call)

            if node.kind == clang.CursorKind.UNARY_OPERATOR and project:
                tokens = _tokens(node)
                if tokens and tokens[0] == "*":
                    start, end = _extent(node)
                    dereferences.append(
                        {
                            "file": _rel(location_file, repo_root),
                            "line": node.location.line,
                            "column": _column(node),
                            "function": current_function,
                            "expression": " ".join(tokens),
                            "extent_start": start,
                            "extent_end": end,
                            "included_by": [tu_rel],
                        }
                    )
                if tokens and tokens[0] == "&":
                    # Address-of is represented inside argument facts; no separate row.
                    pass

            if node.kind == clang.CursorKind.ARRAY_SUBSCRIPT_EXPR and project:
                start, end = _extent(node)
                subscripts.append(
                    {
                        "file": _rel(location_file, repo_root),
                        "line": node.location.line,
                        "column": _column(node),
                        "function": current_function,
                        "tokens": _tokens(node),
                        "extent_start": start,
                        "extent_end": end,
                        "included_by": [tu_rel],
                    }
                )

            for child in node.get_children():
                walk(child, current_function, project)

        walk(tu.cursor)

    functions = _dedup(functions, ("file", "line", "column", "usr", "name", "is_definition"))
    variables = _dedup(variables, ("file", "line", "column", "usr", "name", "function"))
    arrays = [variable for variable in variables if variable.get("is_array")]
    structs = _dedup(structs, ("file", "line", "column", "usr", "name"))
    calls = _dedup(calls, ("file", "line", "column", "name", "function", "extent_start", "extent_end"))
    pointer_decls = _dedup(pointer_decls, ("file", "line", "column", "usr", "name", "function", "kind"))
    dereferences = _dedup(dereferences, ("file", "line", "column", "function", "extent_start", "extent_end"))
    subscripts = _dedup(subscripts, ("file", "line", "column", "function", "extent_start", "extent_end"))
    allocation_calls = [call for call in calls if call.get("category") == "allocation_or_deallocation"]
    kernel_calls = [call for call in calls if call.get("category") == "kernel_api"]

    for collection in (functions, arrays, variables, structs, calls, pointer_decls, dereferences, subscripts, allocation_calls, kernel_calls):
        collection.sort(
            key=lambda row: (
                row.get("file") or "",
                row.get("line") or 0,
                row.get("column") or 0,
                row.get("extent_start") or 0,
                row.get("kind") or "",
                row.get("name") or row.get("expression") or "",
            )
        )

    return {
        "schema": "uastw-safety-c-facts-v2",
        "files": files,
        "parse_failures": parse_failures,
        "diagnostics": diagnostics,
        "functions": functions,
        "function_symbols": function_symbols,
        "arrays": arrays,
        "variables": variables,
        "structs": structs,
        "calls": calls,
        "pointer_decls": pointer_decls,
        "dereferences": dereferences,
        "subscripts": subscripts,
        "allocation_calls": allocation_calls,
        "kernel_calls": kernel_calls,
    }
