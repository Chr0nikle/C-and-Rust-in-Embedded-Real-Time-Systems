"""C operation inventory using libclang's target-aware AST.

The inventory deliberately separates source-level operations.  It does not
classify an operation as a defect and it does not count macro-expanded kernel
internals as separate application operations.

Operation provenance is resolved from Clang's spelling and expansion
locations.  A node whose spelling location is the macro definition rather than
the invocation site is *not* an explicit source-level operation.  In
particular, pointer conversions introduced by macro expansion are retained in
the descriptive ``macro_pointer_conversion`` category and are never folded into
the explicit ``pointer_conversion`` count.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import clang.cindex as clang

import checks.c_extract as _cextract_module
from checks.c_extract import (
    INTEGER_KINDS,
    _canonical_type,
    _is_project,
    _rel,
    _tokens,
    type_spec,
)


ALLOC_NAMES = {"pvPortMalloc", "vPortFree", "malloc", "calloc", "realloc", "free"}
KERNEL_RE = re.compile(r"^(x|v|ux|pv|ul|pc)(Queue|Semaphore|Task|Timer|EventGroup|Port|Notify)")
UNION_KIND = getattr(clang.CursorKind, "UNION_DECL", None)
ARRAY_KINDS = {
    getattr(clang.TypeKind, name)
    for name in ("CONSTANTARRAY", "INCOMPLETEARRAY", "VARIABLEARRAY", "DEPENDENTSIZEDARRAY")
    if hasattr(clang.TypeKind, name)
}

EXPLICIT_SOURCE = "explicit_source"
EXPLICIT_MACRO_ARGUMENT = "explicit_macro_argument"
MACRO_ARGUMENT_REFERENCE = "macro_argument_reference"
PROJECT_MACRO = "project_macro_expansion"
THIRD_PARTY_MACRO = "third_party_macro_expansion"
COMPILER_GENERATED = "compiler_generated"
UNRESOLVED_PROVENANCE = "unresolved_provenance"
MACRO_PROVENANCE = {PROJECT_MACRO, THIRD_PARTY_MACRO}

ASSIGNMENT_OPS = {
    "=", "+=", "-=", "*=", "/=", "%=", "<<=", ">>=", "&=", "^=", "|=",
}
COMPOUND_OPS = ASSIGNMENT_OPS - {"="}
BINARY_OPS = {
    "+", "-", "*", "/", "%", "<<", ">>", "&", "^", "|",
    "==", "!=", "<", ">", "<=", ">=", "&&", "||",
}

GLOBAL_CATEGORY = {
    "read": "mutable_global_read",
    "write": "mutable_global_write",
    "readwrite": "mutable_global_readwrite",
    "address_of": "mutable_global_address",
}

_SOURCE_CACHE: dict[str, str | None] = {}
_LOCATION_API_READY = False
_SPELLING_LOCATION = None
_EXPANSION_LOCATION = None


def _prepare_location_api() -> None:
    """Register the libclang location APIs missing from the Python binding."""
    global _LOCATION_API_READY, _SPELLING_LOCATION, _EXPANSION_LOCATION
    if _LOCATION_API_READY:
        return
    try:
        library = clang.conf.lib
        spelling = getattr(library, "clang_getSpellingLocation", None)
        expansion = getattr(library, "clang_getExpansionLocation", None)
        argument_types = [
            clang.SourceLocation,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        if spelling is not None and expansion is not None:
            spelling.argtypes = argument_types
            spelling.restype = None
            expansion.argtypes = argument_types
            expansion.restype = None
            # CXFile is an opaque void pointer.  The Python binding registers
            # clang_getFileName with a wrapper type, so set the real C signature
            # explicitly before using it with the location APIs.
            get_file_name = getattr(library, "clang_getFileName", None)
            if get_file_name is not None:
                get_file_name.argtypes = [ctypes.c_void_p]
            _SPELLING_LOCATION = spelling
            _EXPANSION_LOCATION = expansion
    except Exception:
        _SPELLING_LOCATION = None
        _EXPANSION_LOCATION = None
    _LOCATION_API_READY = True


def _location(node: clang.Cursor, spell: bool) -> dict[str, Any] | None:
    _prepare_location_api()
    function = _SPELLING_LOCATION if spell else _EXPANSION_LOCATION
    if function is None:
        return None
    file_pointer = ctypes.c_void_p()
    line = ctypes.c_uint()
    column = ctypes.c_uint()
    offset = ctypes.c_uint()
    try:
        function(
            node.location,
            ctypes.byref(file_pointer),
            ctypes.byref(line),
            ctypes.byref(column),
            ctypes.byref(offset),
        )
    except Exception:
        return None
    name: str | None = None
    if file_pointer.value:
        try:
            cx_string = clang.conf.lib.clang_getFileName(file_pointer)
            name = getattr(cx_string, "spelling", None) or None
            if isinstance(name, bytes):
                name = name.decode("utf-8", errors="replace")
        except Exception:
            name = None
    return {
        "file": name,
        "line": line.value,
        "column": column.value,
        "offset": offset.value,
    }


def _source_text(path: str | None) -> str | None:
    if not path:
        return None
    if path not in _SOURCE_CACHE:
        try:
            _SOURCE_CACHE[path] = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            _SOURCE_CACHE[path] = None
    return _SOURCE_CACHE[path]


def _invocation_token(location: dict[str, Any] | None) -> str | None:
    """Return the identifier spelled at a macro expansion location."""
    if not location:
        return None
    text = _source_text(location.get("file"))
    if text is None:
        return None
    offset = max(0, min(len(text), int(location.get("offset") or 0)))
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[offset:])
    return match.group(0) if match else None


def _macro_invocation_range(location: dict[str, Any] | None) -> tuple[int, int] | None:
    """Return the source offset range of a macro invocation, when spellable.

    ``libclang`` supplies the expansion location at the macro name.  The range
    is extended over the balanced argument list (if any), skipping strings and
    comments.  This is used only to distinguish an explicit cast written in a
    macro argument from a cast generated by a macro body.
    """
    if not location:
        return None
    text = _source_text(location.get("file"))
    if text is None:
        return None
    start = max(0, min(len(text), int(location.get("offset") or 0)))
    match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[start:])
    if not match:
        return None
    position = start + match.end()
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] != "(":
        return start, start + match.end()
    depth = 0
    index = position
    while index < len(text):
        character = text[index]
        if character in ('"', "'"):
            quote = character
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            end_comment = text.find("*/", index + 2)
            index = len(text) if end_comment < 0 else end_comment + 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return start, index + 1
        index += 1
    return start, len(text)


def _provenance(node: clang.Cursor, repo_root: Path) -> tuple[str, dict[str, Any]]:
    expansion = _location(node, spell=False)
    spelling = _location(node, spell=True)
    if expansion is None or spelling is None:
        return UNRESOLVED_PROVENANCE, {"macro_definition_status": "unresolved"}
    same = (
        expansion["file"] == spelling["file"]
        and expansion["line"] == spelling["line"]
        and expansion["column"] == spelling["column"]
    )
    if same:
        if _tokens(node, 1):
            return EXPLICIT_SOURCE, {}
        return COMPILER_GENERATED, {"macro_definition_status": "compiler_generated"}

    invocation_details: dict[str, Any] = {
        "macro_invocation_file": _rel(expansion["file"], repo_root),
        "macro_invocation_line": expansion["line"],
        "macro_invocation_column": expansion["column"],
    }
    token = _invocation_token(expansion)
    if token:
        invocation_details["macro_invocation"] = token

    # An argument token has its spelling location inside the invocation's
    # balanced argument list.  That is a source-written expression even though
    # it is passed through a macro.  Do not treat the argument location as a
    # macro definition.
    invocation_range = _macro_invocation_range(expansion)
    if (
        spelling["file"] == expansion["file"]
        and invocation_range is not None
        and invocation_range[0] < int(spelling["offset"]) < invocation_range[1]
    ):
        details = dict(invocation_details)
        details.update({
            "macro_argument_file": _rel(spelling["file"], repo_root),
            "macro_argument_line": spelling["line"],
            "macro_argument_column": spelling["column"],
            "macro_argument_offset": spelling["offset"],
            "macro_definition_status": "argument_not_definition",
        })
        if node.kind in (
            clang.CursorKind.CSTYLE_CAST_EXPR,
            getattr(clang.CursorKind, "CXX_FUNCTIONAL_CAST_EXPR", None),
        ):
            return EXPLICIT_MACRO_ARGUMENT, details
        return MACRO_ARGUMENT_REFERENCE, details

    if not spelling["file"]:
        details = dict(invocation_details)
        details["macro_definition_status"] = "unresolved"
        return UNRESOLVED_PROVENANCE, details

    definition_file = _rel(spelling["file"], repo_root)
    external_definition = bool(definition_file and Path(definition_file).is_absolute())
    details = dict(invocation_details)
    details.update({
        "macro_definition_file": Path(definition_file).name if external_definition else definition_file,
        "macro_definition_line": spelling["line"],
        "macro_definition_column": spelling["column"],
        "macro_definition_status": "definition",
    })
    if external_definition:
        details["macro_definition_scope"] = "external_toolchain_or_system"
    if _is_project(spelling["file"], repo_root):
        return PROJECT_MACRO, details
    return THIRD_PARTY_MACRO, details

def _is_pointer(t: clang.Type) -> bool:
    try:
        return _canonical_type(t).kind == clang.TypeKind.POINTER
    except Exception:
        return False


def _is_integer(t: clang.Type) -> bool:
    try:
        return _canonical_type(t).kind in INTEGER_KINDS
    except Exception:
        return False


def _is_array(t: clang.Type) -> bool:
    try:
        return _canonical_type(t).kind in ARRAY_KINDS
    except Exception:
        return False


def _is_const(t: clang.Type) -> bool:
    try:
        return bool(t.is_const_qualified() or _canonical_type(t).is_const_qualified())
    except Exception:
        return False


TRANSPARENT_KINDS = {
    getattr(clang.CursorKind, name)
    for name in ("UNEXPOSED_EXPR", "PAREN_EXPR", "CXX_FUNCTIONAL_CAST_EXPR")
    if hasattr(clang.CursorKind, name)
}


def _declared_type(node: clang.Cursor) -> clang.Type:
    """Return the declared type behind implicit array-to-pointer wrappers."""
    if node.kind in TRANSPARENT_KINDS:
        children = list(node.get_children())
        if len(children) == 1:
            return _declared_type(children[0])
    if node.kind in (clang.CursorKind.DECL_REF_EXPR, clang.CursorKind.MEMBER_REF_EXPR):
        referenced = node.referenced
        if referenced is not None:
            return referenced.type
    return node.type


def _expression(node: clang.Cursor, provenance: str = EXPLICIT_SOURCE) -> str:
    if provenance in (PROJECT_MACRO, THIRD_PARTY_MACRO, MACRO_ARGUMENT_REFERENCE, COMPILER_GENERATED):
        try:
            return f"<{node.kind.name}@{node.location.line}:{node.location.column}>"
        except Exception:
            return f"<{node.kind.name}>"
    text = " ".join(_tokens(node, 80)).strip()
    if text:
        return text
    try:
        return f"<{node.kind.name}@{node.location.line}:{node.location.column}>"
    except Exception:
        return f"<{node.kind.name}>"


def _occurrence_id(configuration: str, category: str, file: str | None, line: int, column: int | None, ast_kind: str, expression: str, function: str | None, start: int | None = None) -> str:
    material = "|".join((configuration, category, file or "", str(line), str(column or ""), ast_kind, expression, function or "", str(start or "")))
    return "c-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _source_identity(category: str, file: str | None, line: int, column: int | None, ast_kind: str, expression: str, function: str | None, start: int | None = None) -> str:
    """Identify one source occurrence independently of build configuration."""
    material = "|".join(("c", category, file or "", str(line), str(column or ""), ast_kind, expression, function or "", str(start or "")))
    return "c-src-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _function_name(node: clang.Cursor) -> str | None:
    parent = node.semantic_parent
    while parent is not None:
        if parent.kind == clang.CursorKind.FUNCTION_DECL:
            return parent.spelling
        parent = parent.semantic_parent
    return None


def _binary_operator(node: clang.Cursor) -> str | None:
    """Return the operator that joins the two operands of a binary node."""
    children = list(node.get_children())
    tokens = _tokens(node, 120)
    if len(children) >= 2 and tokens:
        left_tokens = _tokens(children[0], 120)
        if left_tokens and tokens[: len(left_tokens)] == left_tokens and len(tokens) > len(left_tokens):
            candidate = tokens[len(left_tokens)]
            if candidate in ASSIGNMENT_OPS or candidate in BINARY_OPS:
                return candidate
    depth = 0
    for token in tokens:
        if token in ("(", "[", "{"):
            depth += 1
        elif token in (")", "]", "}"):
            depth = max(0, depth - 1)
        elif depth == 0 and (token in ASSIGNMENT_OPS or token in BINARY_OPS):
            return token
    return None


def _global_declarations(tu: clang.TranslationUnit, repo_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    globals_: dict[str, dict[str, Any]] = {}
    unions: dict[str, dict[str, Any]] = {}
    for cursor in tu.cursor.walk_preorder():
        if not _is_project(cursor.location.file.name if cursor.location.file else None, repo_root):
            continue
        if cursor.kind == clang.CursorKind.VAR_DECL and cursor.semantic_parent and cursor.semantic_parent.kind == clang.CursorKind.TRANSLATION_UNIT:
            if _is_const(cursor.type):
                continue
            key = cursor.get_usr() or f"{cursor.location.file}:{cursor.location.line}:{cursor.location.column}:{cursor.spelling}"
            globals_[key] = {
                "name": cursor.spelling,
                "file": _rel(cursor.location.file.name if cursor.location.file else None, repo_root),
                "line": cursor.location.line,
                "column": cursor.location.column,
                "type": type_spec(cursor.type),
            }
        if UNION_KIND is not None and cursor.kind == UNION_KIND and cursor.is_definition():
            key = cursor.get_usr() or f"{cursor.location.file}:{cursor.location.line}:{cursor.spelling}"
            unions[key] = {
                "name": cursor.spelling,
                "file": _rel(cursor.location.file.name if cursor.location.file else None, repo_root),
                "line": cursor.location.line,
            }
    return globals_, unions


def _extract_c_scope(repo_root: Path, configuration: dict[str, Any], layer_default: str = "unclassified") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Return (operations, functions, kernel_calls) for one C configuration."""
    index = clang.Index.create()
    operations: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    kernel_calls: list[dict[str, Any]] = []
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    config_id = configuration["configuration"]
    diagnostics: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    def add_operation(node: clang.Cursor, category: str, expression: str, *, resolution: str = "resolved", details: dict[str, Any] | None = None, function: str | None = None, provenance: str = EXPLICIT_SOURCE, provenance_details: dict[str, Any] | None = None, location_override: dict[str, Any] | None = None) -> None:
        if location_override:
            file = location_override.get("file")
            if file and Path(file).is_absolute():
                file = _rel(file, repo_root)
            line = int(location_override.get("line") or 0)
            column = int(location_override.get("column") or 0)
            start = location_override.get("offset")
        else:
            file = _rel(node.location.file.name if node.location.file else None, repo_root)
            line = node.location.line
            column = node.location.column
            start = node.extent.start.offset if node.extent else None
        key = (file, line, column, category, str(node.kind), expression, function, start)
        merged_details = dict(details or {})
        if provenance_details:
            merged_details.update(provenance_details)
        if key in by_key:
            by_key[key]["included_by"] = sorted(set(by_key[key]["included_by"]) | {unit_rel})
            return
        record = {
            "occurrence_id": _occurrence_id(config_id, category, file, line, column, str(node.kind), expression, function, start),
            "source_identity_key": _source_identity(category, file, line, column, str(node.kind), expression, function, start),
            "language": "c",
            "configuration": config_id,
            "scope": "C benchmark" if configuration["rust_mode"] == 0 else "C support linked with Rust",
            "layer": layer_default,
            "category": category,
            "ast_kind": str(node.kind),
            "source_file": file,
            "source_line": line,
            "source_column": column,
            "function": function or _function_name(node),
            "expression": expression,
            "activity_status": "true",
            "resolution_status": resolution,
            "provenance": provenance,
            "macro_status": provenance,
            "details": merged_details,
            "included_by": [unit_rel],
        }
        operations.append(record)
        by_key[key] = record

    for unit in configuration["translation_units"]:
        unit_rel = unit["path"]
        tu = index.parse(unit["abs_path"], args=unit["clang_args"], options=clang.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
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
        files.append({"path": unit["path"], "diagnostics": len(file_diags), "parse_failed": any(item["severity"] in ("3", "4") for item in file_diags)})
        globals_, unions = _global_declarations(tu, repo_root)
        global_names = {item["name"] for item in globals_.values()}

        def global_pointer_root(node: clang.Cursor) -> str | None:
            """Return the project global that roots a pointer-valued expression."""
            if node.kind in TRANSPARENT_KINDS and node.kind not in (
                clang.CursorKind.CSTYLE_CAST_EXPR,
                getattr(clang.CursorKind, "CXX_FUNCTIONAL_CAST_EXPR", None),
            ):
                children = list(node.get_children())
                if len(children) == 1:
                    return global_pointer_root(children[0])
                return None
            if node.kind in (clang.CursorKind.CSTYLE_CAST_EXPR, getattr(clang.CursorKind, "CXX_FUNCTIONAL_CAST_EXPR", None)):
                value_children = [
                    child for child in node.get_children()
                    if child.kind != getattr(clang.CursorKind, "TYPE_REF", None)
                ]
                return global_pointer_root(value_children[0]) if value_children else None
            if node.kind == clang.CursorKind.DECL_REF_EXPR:
                referenced = node.referenced
                if referenced is not None and referenced.spelling in global_names:
                    key = referenced.get_usr() or f"{referenced.location.file}:{referenced.location.line}:{referenced.location.column}:{referenced.spelling}"
                    if key in globals_:
                        return referenced.spelling
                return None
            if node.kind in (clang.CursorKind.MEMBER_REF_EXPR, clang.CursorKind.ARRAY_SUBSCRIPT_EXPR):
                children = list(node.get_children())
                return global_pointer_root(children[0]) if children else None
            if node.kind in (clang.CursorKind.BINARY_OPERATOR, clang.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
                for child in node.get_children():
                    name = global_pointer_root(child)
                    if name:
                        return name
            return None

        def walk(node: clang.Cursor, function: str | None = None, access: str = "read", lvalue: bool = False) -> None:
            location_file = node.location.file.name if node.location.file else None
            project = _is_project(location_file, repo_root)
            if node.kind == clang.CursorKind.FUNCTION_DECL:
                if project:
                    functions.append({
                        "configuration": config_id,
                        "language": "c",
                        "name": node.spelling,
                        "file": _rel(location_file, repo_root),
                        "line": node.location.line,
                        "column": node.location.column,
                        "is_definition": node.is_definition(),
                        "active": "true",
                        "layer": layer_default,
                        "included_by": [unit_rel],
                    })
                next_function = node.spelling if node.is_definition() else function
                for child in node.get_children():
                    walk(child, next_function, "read", False)
                return

            if not project and node.kind != clang.CursorKind.TRANSLATION_UNIT:
                # Third-party header bodies are excluded from project operation
                # counts, but project translation units still descend through
                # their declarations.
                return

            tokens = _tokens(node, 120)
            provenance, provenance_details = _provenance(node, repo_root)
            text = _expression(node, provenance)
            children = list(node.get_children())

            if node.kind == clang.CursorKind.DECL_REF_EXPR:
                referenced = node.referenced
                if referenced is not None and referenced.kind == clang.CursorKind.VAR_DECL and referenced.spelling in global_names:
                    key = referenced.get_usr() or f"{referenced.location.file}:{referenced.location.line}:{referenced.location.column}:{referenced.spelling}"
                    if key in globals_:
                        value_access = access if access in GLOBAL_CATEGORY else "read"
                        category = GLOBAL_CATEGORY[value_access]
                        add_operation(
                            node,
                            category,
                            text,
                            details={"global": referenced.spelling, "access": value_access, "global_type": type_spec(referenced.type), "lvalue": lvalue},
                            function=function,
                            provenance=provenance,
                            provenance_details=provenance_details,
                        )
                return

            if node.kind == clang.CursorKind.UNARY_OPERATOR:
                operator = tokens[0] if tokens and tokens[0] in ("*", "&", "++", "--", "!", "~", "+", "-") else None
                if operator is None and tokens and tokens[-1] in ("++", "--"):
                    operator = tokens[-1]
                if operator == "*":
                    if children and _is_pointer(children[0].type):
                        add_operation(node, "pointer_dereference", text, function=function, provenance=provenance, provenance_details=provenance_details)
                        if lvalue and access in ("write", "readwrite"):
                            root_global = global_pointer_root(children[0])
                            if root_global:
                                add_operation(
                                    node,
                                    "mutable_global_unresolved",
                                    text,
                                    resolution="unresolved",
                                    details={"global": root_global, "access": access, "via": "deref", "reason": "pointee write through a global-derived pointer; mutated object identity is not tracked"},
                                    function=function,
                                    provenance=provenance,
                                    provenance_details=provenance_details,
                                )
                    for child in children:
                        walk(child, function, "read", False)
                    return
                if operator == "&":
                    for child in children:
                        walk(child, function, "address_of", True)
                    return
                if operator in ("++", "--"):
                    if children and _is_pointer(children[0].type):
                        add_operation(
                            node,
                            "pointer_arithmetic",
                            text,
                            details={"operator": operator, "form": "postfix" if tokens[-1] in ("++", "--") else "prefix"},
                            function=function,
                            provenance=provenance,
                            provenance_details=provenance_details,
                        )
                    for child in children:
                        walk(child, function, "readwrite", True)
                    return
                for child in children:
                    walk(child, function, "read", False)
                return

            if node.kind in (clang.CursorKind.BINARY_OPERATOR, clang.CursorKind.COMPOUND_ASSIGNMENT_OPERATOR):
                operator = _binary_operator(node)
                if operator == "=":
                    if children:
                        walk(children[0], function, "write", True)
                    for child in children[1:]:
                        walk(child, function, "read", False)
                    return
                if operator in COMPOUND_OPS:
                    if operator in ("+=", "-=") and any(_is_pointer(child.type) for child in children):
                        add_operation(
                            node,
                            "pointer_arithmetic",
                            text,
                            details={"operator": operator, "form": "compound"},
                            function=function,
                            provenance=provenance,
                            provenance_details=provenance_details,
                        )
                    if children:
                        walk(children[0], function, "readwrite", True)
                    for child in children[1:]:
                        walk(child, function, "read", False)
                    return
                if operator in ("+", "-") and any(_is_pointer(child.type) for child in children):
                    add_operation(
                        node,
                        "pointer_arithmetic",
                        text,
                        details={"operator": operator, "form": "binary"},
                        function=function,
                        provenance=provenance,
                        provenance_details=provenance_details,
                    )
                for child in children:
                    walk(child, function, "read", False)
                return

            if node.kind == clang.CursorKind.ARRAY_SUBSCRIPT_EXPR:
                add_operation(node, "array_index", text, function=function, provenance=provenance, provenance_details=provenance_details)
                if children:
                    base = children[0]
                    if lvalue and access in ("write", "readwrite") and _is_pointer(_declared_type(base)):
                        root_global = global_pointer_root(base)
                        if root_global:
                            add_operation(
                                node,
                                "mutable_global_unresolved",
                                text,
                                resolution="unresolved",
                                details={"global": root_global, "access": access, "via": "pointer_index", "reason": "indexed pointee write through a global-derived pointer; mutated object identity is not tracked"},
                                function=function,
                                provenance=provenance,
                                provenance_details=provenance_details,
                            )
                    if lvalue and access in ("write", "readwrite", "address_of") and _is_array(_declared_type(base)):
                        walk(base, function, access, True)
                    else:
                        walk(base, function, "read", False)
                    for child in children[1:]:
                        walk(child, function, "read", False)
                return

            if node.kind == clang.CursorKind.MEMBER_REF_EXPR:
                referenced = node.referenced
                if referenced is not None and referenced.kind == clang.CursorKind.FIELD_DECL:
                    parent = referenced.semantic_parent
                    if parent is not None and UNION_KIND is not None and parent.kind == UNION_KIND:
                        add_operation(node, "union_field_access", text, function=function, details={"union": parent.spelling, "field": referenced.spelling}, provenance=provenance, provenance_details=provenance_details)
                arrow = "->" in tokens
                if arrow and children and _is_pointer(children[0].type):
                    add_operation(node, "pointer_field_access", text, function=function, provenance=provenance, provenance_details=provenance_details)
                    if lvalue and access in ("write", "readwrite"):
                        root_global = global_pointer_root(children[0])
                        if root_global:
                            add_operation(
                                node,
                                "mutable_global_unresolved",
                                text,
                                resolution="unresolved",
                                details={"global": root_global, "access": access, "via": "pointer_member", "reason": "field write through a global-derived pointer; pointee identity is not tracked"},
                                function=function,
                                provenance=provenance,
                                provenance_details=provenance_details,
                            )
                if children:
                    base = children[0]
                    if lvalue and access in ("write", "readwrite", "address_of") and not arrow:
                        walk(base, function, access, True)
                    else:
                        walk(base, function, "read", False)
                    for child in children[1:]:
                        walk(child, function, "read", False)
                return

            if node.kind in (clang.CursorKind.CSTYLE_CAST_EXPR, clang.CursorKind.CXX_FUNCTIONAL_CAST_EXPR):
                target_pointer = _is_pointer(node.type)
                value_children = [
                    child for child in children
                    if child.kind != getattr(clang.CursorKind, "TYPE_REF", None)
                ]
                source_node = value_children[0] if value_children else None
                source_type = source_node.type if source_node is not None else None
                source_pointer = _is_pointer(source_type) if source_type is not None else False
                source_integer = _is_integer(source_type) if source_type is not None else False
                direction = "pointer_to_pointer" if source_pointer else ("integer_to_pointer" if source_integer else "unresolved_source")
                if target_pointer and (source_pointer or source_integer):
                    details = {
                        "target": type_spec(node.type),
                        "source": type_spec(source_type) if source_type is not None else None,
                        "direction": direction,
                    }
                    if provenance in (EXPLICIT_SOURCE, EXPLICIT_MACRO_ARGUMENT):
                        override = None
                        if provenance == EXPLICIT_MACRO_ARGUMENT and provenance_details:
                            override = {
                                "file": provenance_details.get("macro_argument_file"),
                                "line": provenance_details.get("macro_argument_line"),
                                "column": provenance_details.get("macro_argument_column"),
                                "offset": provenance_details.get("macro_argument_offset"),
                            }
                        add_operation(
                            node, "pointer_conversion", text, function=function,
                            details=details, provenance=provenance,
                            provenance_details=provenance_details,
                            location_override=override,
                        )
                    elif provenance in MACRO_PROVENANCE:
                        add_operation(node, "macro_pointer_conversion", text, function=function, details=details, provenance=provenance, provenance_details=provenance_details)
                    elif provenance == UNRESOLVED_PROVENANCE:
                        unresolved_details = dict(details)
                        unresolved_details["reason"] = "pointer conversion provenance could not be established"
                        add_operation(
                            node, "unresolved_pointer_conversion", text,
                            resolution="unresolved", function=function,
                            details=unresolved_details, provenance=provenance,
                            provenance_details=provenance_details,
                        )
                    elif provenance == COMPILER_GENERATED:
                        generated_details = dict(details)
                        generated_details["reason"] = "compiler-generated or implicit cast node; not an explicit source operation"
                        add_operation(
                            node, "compiler_generated_pointer_conversion", text,
                            resolution="unresolved", function=function,
                            details=generated_details, provenance=provenance,
                            provenance_details=provenance_details,
                        )
                for child in children:
                    walk(child, function, "read", False)
                return

            if node.kind == clang.CursorKind.CALL_EXPR:
                name = node.spelling
                referenced = node.referenced
                direct = referenced is not None and referenced.kind == clang.CursorKind.FUNCTION_DECL
                if not name or not direct:
                    callee = name or "<indirect>"
                    add_operation(
                        node,
                        "kernel_api_call",
                        text,
                        resolution="unresolved",
                        details={"callee": callee, "callee_kind": "indirect_or_unresolved"},
                        function=function,
                        provenance=provenance,
                        provenance_details=provenance_details,
                    )
                    kernel_calls.append({
                        "configuration": config_id, "language": "c",
                        "source_file": _rel(location_file, repo_root), "source_line": node.location.line,
                        "source_column": node.location.column, "caller": function,
                        "callee": callee, "callee_kind": "indirect_or_unresolved",
                        "resolution_status": "unresolved", "activity_status": "true",
                        "expression": text,
                    })
                elif name in ALLOC_NAMES:
                    category = "dynamic_allocation" if name in ("pvPortMalloc", "malloc", "calloc", "realloc") else "deallocation"
                    add_operation(node, category, text, function=function, details={"callee": name}, provenance=provenance, provenance_details=provenance_details)
                elif KERNEL_RE.match(name) or name.startswith(("port_", "bench_rust_", "rust_")):
                    kind = "rtos_api" if KERNEL_RE.match(name) else "project_wrapper_or_ffi"
                    add_operation(node, "kernel_api_call", text, function=function, details={"callee": name, "callee_kind": kind}, provenance=provenance, provenance_details=provenance_details)
                    kernel_calls.append({
                        "configuration": config_id,
                        "language": "c",
                        "source_file": _rel(location_file, repo_root),
                        "source_line": node.location.line,
                        "source_column": node.location.column,
                        "caller": function,
                        "callee": name,
                        "callee_kind": kind,
                        "resolution_status": "resolved",
                        "activity_status": "true",
                        "expression": text,
                    })
                for child in children:
                    walk(child, function, "read", False)
                return

            if node.kind in TRANSPARENT_KINDS and node.kind not in (
                clang.CursorKind.CSTYLE_CAST_EXPR,
                getattr(clang.CursorKind, "CXX_FUNCTIONAL_CAST_EXPR", None),
            ):
                if lvalue and len(children) == 1:
                    walk(children[0], function, access, True)
                else:
                    for child in children:
                        walk(child, function, "read", False)
                return

            for child in node.get_children():
                walk(child, function, "read", False)

        walk(tu.cursor)

    # A cast written explicitly in a macro argument may share its invocation
    # with pointer casts generated by the macro body.  The extractor's counting
    # unit is a deduplicated macro-expansion cast site, so keep the explicit
    # source cast and record any collapsed macro-body casts in its details
    # rather than adding an extra record for the same invocation.
    explicit_argument_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in operations:
        if row["category"] != "pointer_conversion" or row["provenance"] != EXPLICIT_MACRO_ARGUMENT:
            continue
        details = row["details"]
        key = (
            row["configuration"],
            details.get("macro_invocation_file"),
            details.get("macro_invocation_line"),
            details.get("macro_invocation_column"),
        )
        explicit_argument_groups.setdefault(key, []).append(row)
    for row in list(operations):
        if row["category"] != "macro_pointer_conversion":
            continue
        details = row["details"]
        key = (
            row["configuration"],
            details.get("macro_invocation_file"),
            details.get("macro_invocation_line"),
            details.get("macro_invocation_column"),
        )
        owners = explicit_argument_groups.get(key)
        if not owners:
            continue
        owner = sorted(owners, key=lambda item: item["source_column"] or 0)[0]
        owner["details"].setdefault("collapsed_macro_pointer_conversions", []).append({
            "target": details.get("target"),
            "source": details.get("source"),
            "direction": details.get("direction"),
            "macro_definition_file": details.get("macro_definition_file"),
            "macro_definition_line": details.get("macro_definition_line"),
            "macro_definition_column": details.get("macro_definition_column"),
            "ast_kind": row["ast_kind"],
            "expression": row["expression"],
        })
        operations.remove(row)

    operations.sort(key=lambda row: (row["function"] or "", row["source_file"] or "", row["source_line"], row["source_column"] or 0, row["category"], row["expression"]))
    functions.sort(key=lambda row: (row["file"] or "", row["line"], row["name"]))
    kernel_calls.sort(key=lambda row: (row["caller"] or "", row["source_file"] or "", row["source_line"], row["source_column"] or 0, row["callee"]))
    return operations, functions, kernel_calls, {
        "files": files,
        "diagnostics": diagnostics,
        "parse_failures": [item for item in files if item["parse_failed"]],
    }


def extract_c_scope(repo_root: Path, configuration: dict[str, Any], layer_default: str = "unclassified") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Extract one C scope using the configuration's project roots.

    The shared extractor historically hard-codes the STM32F4 FreeRTOS roots.
    Profile and inventory scopes for other boards must use their own source and
    header roots; this wrapper supplies them for the duration of the extraction.
    """
    source_roots = tuple(configuration.get("project_source_roots") or _cextract_module.PROJECT_SOURCE_ROOTS)
    header_roots = list(configuration.get("project_header_roots") or source_roots)
    for path in ("firmware/common/c/include", *(str(Path(root) / "include") for root in source_roots)):
        if (repo_root / path).is_dir() and path not in header_roots:
            header_roots.append(path)
    old_source = _cextract_module.PROJECT_SOURCE_ROOTS
    old_header = _cextract_module.PROJECT_HEADER_ROOTS
    _cextract_module.PROJECT_SOURCE_ROOTS = source_roots
    _cextract_module.PROJECT_HEADER_ROOTS = tuple(header_roots)
    try:
        return _extract_c_scope(repo_root, configuration, layer_default)
    finally:
        _cextract_module.PROJECT_SOURCE_ROOTS = old_source
        _cextract_module.PROJECT_HEADER_ROOTS = old_header
