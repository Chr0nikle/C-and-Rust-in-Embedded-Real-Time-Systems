"""Mechanical argument, check and return-handling classification.

The rules are deliberately conservative.  They describe source-level evidence
and do not assert buffer validity, ownership correctness or wrapper soundness.
"""

from __future__ import annotations

import re
from typing import Any

SIZE_NAMES = {
    "size", "size_bytes", "length", "len", "capacity", "item_size", "itemsize",
    "uxitemsize", "uxqueuelength", "uxqueue_length", "queue_length", "stack",
    "stack_depth", "uxstackdepth", "stacksize", "stack_size", "bytes", "count",
    "n", "total", "allocated", "xwantedsize", "wanted_size",
}
TIMEOUT_NAMES = {
    "ticks", "tickstowait", "timeout", "wait", "portmax_delay", "block",
}
FLAG_NAMES = {
    "action", "flags", "position", "copy_position", "copyposition", "queue_type",
    "queuetype", "active",
}
HANDLE_NAMES = {"queue", "semaphore", "task", "timer", "handle", "eventgroup", "notify"}
BUFFER_NAMES = {
    "item", "buffer", "buf", "data", "payload", "pvitemtoqueue", "pvbuffer",
    "ptr", "pv", "pcname", "pvparameters",
}
OUTPUT_NAMES = {
    "out", "output", "ticks", "elapsed", "elapsed_ticks", "end_ticks",
    "start_ticks", "created", "pxcreatedtask", "received", "received_payload",
}
CALLBACK_NAMES = {"task", "pxtaskcode", "taskcode", "entry", "callback", "handler"}
POINTER_CONVERSION_RE = re.compile(r"\(\s*[A-Za-z_][A-Za-z0-9_\s]*\*+\s*\)|as\s*\*")
DERIVED_SIZE_RE = re.compile(r"\b(sizeof|size_of)\b|\.len\s*\(|\blen\s*\(")
NULL_RE = re.compile(r"\bnull\b|\bnullptr\b|\bNULL\b", re.IGNORECASE)
ZERO_RE = re.compile(r"(?<![A-Za-z0-9_])0(?:[uUlL]+)?(?![A-Za-z0-9_])")
TEMPORAL_RE = re.compile(r"(wake|entry|start|end|elapsed|tick|time|order|before|after)", re.IGNORECASE)
SIZE_WORD_RE = re.compile(r"(size|len|length|capacity|total|allocated|margin|free|heap|bound|depth|count)", re.IGNORECASE)
FLAG_WORD_RE = re.compile(r"(\bflag|\btype\b|position|action|status|state|mask|\bSR\b|\bUIF\b|&)", re.IGNORECASE)
STATUS_WORD_RE = re.compile(r"(pdpass|pd_true|pd_false|bench_|status|err|corrupt|port)", re.IGNORECASE)


def _type_kind(param: dict[str, Any] | None) -> str:
    return str((param or {}).get("type", {}).get("kind", "unknown"))


def _param_name(param: dict[str, Any] | None) -> str:
    return str((param or {}).get("name", "") or "").lower()


def _param_text(param: dict[str, Any] | None) -> str:
    return str((param or {}).get("type_text", "") or (param or {}).get("text", "") or "")


def _is_pointer(param: dict[str, Any] | None) -> bool:
    return _type_kind(param) in ("pointer", "reference") or "*" in _param_text(param)


def _is_function_pointer(param: dict[str, Any] | None) -> bool:
    return _type_kind(param) == "function_pointer" or "fn (" in _param_text(param)


def classify_argument(language: str, source_expression: str, param: dict[str, Any] | None,
                      position: int, mapping_status: str,
                      allocator_size_positions: set[int] | None = None) -> dict[str, Any]:
    text = (source_expression or "").strip()
    name = _param_name(param)
    type_text = _param_text(param).lower()
    kind = _type_kind(param)
    pointer = _is_pointer(param)
    function_pointer = _is_function_pointer(param)
    address_of = text.startswith("&") or text.startswith("&mut") or bool(re.search(r"(?<!&)&\s*[A-Za-z_(]", text))
    explicit_cast = bool(POINTER_CONVERSION_RE.search(text))
    derived_expression = bool(DERIVED_SIZE_RE.search(text))
    size_name = name in SIZE_NAMES
    allocator_size = bool(allocator_size_positions and position in allocator_size_positions)
    callback = function_pointer or (name in CALLBACK_NAMES and not pointer)

    if callback:
        role = "function_pointer_callback"
    elif pointer and ("ctx" in name or "context" in name):
        role = "dispatch_context"
    elif pointer and (name in OUTPUT_NAMES or text.startswith("&") and name in OUTPUT_NAMES):
        role = "output_pointer"
    elif pointer and (name in BUFFER_NAMES or any(token in name for token in ("buffer", "buf", "payload", "item", "data"))):
        role = "buffer_pointer"
    elif size_name or allocator_size:
        role = "derived_size" if derived_expression else "explicit_size"
    elif pointer and (name in HANDLE_NAMES or "handle" in type_text or any(name.startswith(prefix) for prefix in ("queue", "semaphore", "task", "timer", "event_group"))):
        role = "resource_handle"
    elif name in TIMEOUT_NAMES or any(token in name for token in ("tick", "timeout")):
        role = "timeout_or_flag"
    elif name in FLAG_NAMES:
        role = "queue_type_or_flags"
    elif pointer:
        role = "raw_pointer"
    elif kind == "unknown" or not name:
        role = "unknown"
    else:
        role = "ordinary_scalar"

    raw_pointer = pointer or address_of or explicit_cast
    raw_data_pointer = role in ("output_pointer", "buffer_pointer", "raw_pointer")
    return {
        "language": language,
        "position": position,
        "source_expression": text,
        "parameter_name": (param or {}).get("name"),
        "parameter_type": _param_text(param) or None,
        "parameter_type_kind": kind,
        "mapping_status": mapping_status,
        "role": role,
        "raw_pointer": raw_pointer,
        "raw_data_pointer": raw_data_pointer,
        "address_of": address_of,
        "explicit_cast": explicit_cast,
        "pointer_typed": pointer,
        "buffer_pointer": role == "buffer_pointer",
        "handle": role == "resource_handle",
        "dispatch_context": role == "dispatch_context",
        "output_pointer": role == "output_pointer",
        "callback": role == "function_pointer_callback",
        "explicit_size": role == "explicit_size",
        "derived_size": role == "derived_size",
        "timeout_or_flags": role in ("timeout_or_flag", "queue_type_or_flags"),
        "flag_or_type": role == "queue_type_or_flags",
        "unknown_role": role == "unknown",
        "semantic_claim": "parameter_type_name_and_source_expression_only",
    }


def map_arguments(language: str, source_args: list[str], target_params: list[dict[str, Any]],
                  target_kind: str, target_name: str | None = None,
                  allocator_size_arguments: dict[str, list[int]] | None = None) -> tuple[list[dict[str, Any]], str, list[str]]:
    rows = []
    unmapped: list[str] = []
    if not source_args:
        mapping_status = "no_source_arguments"
    elif not target_params:
        mapping_status = "target_parameters_unresolved"
    elif len(source_args) == len(target_params):
        mapping_status = "positional"
    elif len(source_args) < len(target_params):
        mapping_status = "positional_with_extra_target_arguments"
    else:
        mapping_status = "positional_with_extra_source_arguments"
    allocator_positions = set((allocator_size_arguments or {}).get(target_name or "", []))
    for index, text in enumerate(source_args):
        param = target_params[index] if index < len(target_params) else None
        if index >= len(target_params):
            unmapped.append(f"source_argument_{index}")
        rows.append(classify_argument(language, text, param, index, mapping_status, allocator_positions))
    for index in range(len(source_args), len(target_params)):
        unmapped.append(str(target_params[index].get("name") or index))
    return rows, mapping_status, unmapped


def _shape_parts(check: dict[str, Any]) -> tuple[str, str, str, str, bool, bool]:
    shape = check.get("condition_shape") or {}
    return (
        str(shape.get("kind") or "other"),
        str(shape.get("operator") or ""),
        str(shape.get("left") or ""),
        str(shape.get("right") or ""),
        bool(shape.get("left_call")),
        bool(shape.get("right_call")),
    )


def classify_check(check: dict[str, Any]) -> dict[str, Any]:
    condition = (check.get("condition") or "").strip()
    shape_kind, operator, left, right, left_call, right_call = _shape_parts(check)
    shape = check.get("condition_shape") or {}
    left_type_kind = str(shape.get("left_type_kind") or "").lower()
    right_type_kind = str(shape.get("right_type_kind") or "").lower()
    combined = " ".join((left, right))
    null_check = False
    zero_comparison = False
    size_or_bound = False
    status_or_error = False
    call_result = False
    ordering = False
    flag = False
    logical = False
    unknown = False

    if shape_kind == "match":
        status_or_error = True
    elif shape_kind in ("call", "block"):
        call_result = True
    elif shape_kind == "method_call":
        method = str((check.get("condition_shape") or {}).get("method") or "").lower()
        if method in ("is_null", "is_none"):
            null_check = True
        elif method in ("len", "capacity", "count"):
            size_or_bound = True
        else:
            call_result = True
    elif shape_kind == "unary":
        if operator.strip() == "!":
            call_result = True
        else:
            unknown = True
    elif operator in ("==", "!="):
        left_null = bool(shape.get("left_null")) or (not left_call and bool(NULL_RE.search(left)))
        right_null = bool(shape.get("right_null")) or (not right_call and bool(NULL_RE.search(right)))
        left_zero = bool(shape.get("left_zero")) or (not left_call and bool(ZERO_RE.fullmatch(left.strip("() "))))
        right_zero = bool(shape.get("right_zero")) or (not right_call and bool(ZERO_RE.fullmatch(right.strip("() "))))
        if (left_null and not left_call) or (right_null and not right_call):
            null_check = True
        elif (left_zero or right_zero) and (left_type_kind == "pointer" or right_type_kind == "pointer"):
            null_check = True
        elif ("&" in left or "|" in left or "&" in right or "|" in right) and (left_zero or right_zero):
            flag = True
        elif (left_zero or right_zero) and SIZE_WORD_RE.search(combined):
            size_or_bound = True
        elif left_zero or right_zero:
            zero_comparison = True
        elif left_call or right_call or STATUS_WORD_RE.search(combined):
            status_or_error = True
        else:
            unknown = True
    elif operator in ("<", ">", "<=", ">="):
        if TEMPORAL_RE.search(combined):
            ordering = True
        elif SIZE_WORD_RE.search(combined):
            size_or_bound = True
        elif left_call or right_call:
            status_or_error = True
        else:
            ordering = True
    elif operator in ("&&", "||"):
        logical = True
        if NULL_RE.search(combined) and re.search(r"(==|!=)\s*(NULL|nullptr)", combined, re.IGNORECASE):
            null_check = True
        elif SIZE_WORD_RE.search(combined):
            size_or_bound = True
        elif left_call or right_call or STATUS_WORD_RE.search(combined):
            status_or_error = True
        else:
            unknown = True
    elif operator in ("&", "|", "^", "<<", ">>"):
        flag = True
    elif shape_kind == "binary":
        unknown = True
    else:
        unknown = True

    if null_check:
        kind = "null_check"
    elif size_or_bound:
        kind = "size_or_bound_check"
    elif status_or_error:
        kind = "status_or_error_check"
    elif call_result:
        kind = "call_result_check"
    elif ordering:
        kind = "ordering_check"
    elif flag:
        kind = "flag_check"
    elif zero_comparison:
        kind = "numeric_zero_check"
    elif logical:
        kind = "logical_check"
    else:
        kind = "unknown_condition"

    function = str(check.get("function") or "")
    dispatch_check = function in ("sample", "run_case") and bool(re.search(r"(case|BENCH_|Case::)", condition))
    return {
        **check,
        "check_kind": kind,
        "null_check": null_check,
        "zero_comparison": zero_comparison,
        "numeric_zero_check": zero_comparison,
        "size_or_bound_check": size_or_bound,
        "status_or_error_check": status_or_error,
        "call_result_check": call_result,
        "ordering_check": ordering,
        "flag_check": flag,
        "logical_check": logical,
        "dispatch_check": dispatch_check,
        "check_status": "observed" if condition else "unresolved",
        "not_observed": "not_claimed",
    }


def _within_check(edge: dict[str, Any], checks: list[dict[str, Any]]) -> bool:
    file = edge.get("source_file")
    line = edge.get("source_line")
    column = edge.get("source_column")
    function = edge.get("caller")
    for check in checks:
        if check.get("source_file") != file or check.get("function") != function:
            continue
        if line is None or column is None:
            continue
        start_line = check.get("source_line")
        start_column = check.get("source_column")
        end_line = check.get("end_line")
        end_column = check.get("end_column")
        if start_line is None or end_line is None:
            continue
        if (line > start_line or (line == start_line and column >= start_column)) and (line < end_line or (line == end_line and column <= end_column)):
            return True
    return False


def return_handling(edge: dict[str, Any], checks: list[dict[str, Any]]) -> str:
    context = edge.get("call_context")
    if context in ("condition_check", "checked"):
        return "checked_in_condition"
    if context in ("stored", "assigned"):
        return "stored_or_assigned"
    if context in ("returned", "function_tail", "propagated_via_question_mark"):
        return "propagated"
    if context in ("discarded",):
        return "discarded"
    if context in ("argument",):
        return "used_as_argument"
    if _within_check(edge, checks):
        return "checked_in_condition_span"
    return "unresolved"
