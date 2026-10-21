"""CHECK-04: queue item size versus supplied send/receive buffer capacity."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from common.io import contract


CREATE_FUNCTIONS = {"xQueueGenericCreate", "xQueueCreate"}
SEND_FUNCTIONS = {"xQueueGenericSend", "xQueueSend", "xQueueSendToBack", "xQueueSendToFront", "xQueueOverwrite"}
RECEIVE_FUNCTIONS = {"xQueueReceive", "xQueuePeek"}
QUEUE_FUNCTIONS = CREATE_FUNCTIONS | SEND_FUNCTIONS | RECEIVE_FUNCTIONS


def _strip_expr(expr: str) -> str:
    return expr.strip()


def _parse_int(expr: str) -> int | None:
    value = _strip_expr(expr).replace("_", "")
    value = re.sub(r"\s+as\s+[A-Za-z0-9_:<>* ]+$", "", value).strip()
    value = re.sub(r"\((?:u|i)(?:8|16|32|64|128|size)\)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"(u|i)(?:8|16|32|64|128|size)$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[uUlL]+$", "", value)
    try:
        return int(value, 0)
    except ValueError:
        return None


def _rust_type_size(spec: dict[str, Any] | None, pointer_width: int = 4) -> int | None:
    if not spec:
        return None
    kind = spec.get("kind")
    if kind == "named":
        name = spec.get("name")
        sizes = {
            "u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4,
            "u64": 8, "i64": 8, "u128": 16, "i128": 16, "usize": pointer_width,
            "isize": pointer_width, "f32": 4, "f64": 8, "bool": 1,
            "c_void": None, "void": None,
        }
        return sizes.get(name)
    if kind in ("pointer", "function_pointer", "reference"):
        return pointer_width
    if kind == "array":
        elem = _rust_type_size(spec.get("elem"), pointer_width)
        length = _parse_int(str(spec.get("len", "")))
        if length is None:
            length = spec.get("len") if isinstance(spec.get("len"), int) else None
        return elem * length if elem is not None and length is not None else None
    return None


def _base_name(value: str) -> str:
    text = re.sub(r"\s+", "", value)
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = text.replace("->", ".")
    text = text.strip(".&*")
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)", text)
    return match.group(1) if match else text


def _array_len(array: dict[str, Any] | None) -> int | None:
    if not array:
        return None
    value = array.get("len")
    if isinstance(value, int):
        return value
    elements = array.get("elements")
    if isinstance(elements, list) and elements:
        return len(elements)
    return _parse_int(str(array.get("len_expr", "")))


def _array_values(array: dict[str, Any] | None) -> list[int]:
    if not array:
        return []
    values: list[int] = []
    raw = array.get("values")
    if not isinstance(raw, list):
        raw = array.get("elements")
    if isinstance(raw, list):
        for element in raw:
            if isinstance(element, int):
                values.append(element)
                continue
            parsed = _parse_int(str(element))
            if parsed is not None:
                values.append(parsed)
    return values


def _c_variable(c_facts: dict[str, Any], function: str | None, name: str) -> dict[str, Any] | None:
    candidates = [
        variable
        for variable in c_facts.get("variables", [])
        if variable.get("name") == name and (function is None or variable.get("function") == function)
    ]
    if not candidates:
        candidates = [variable for variable in c_facts.get("variables", []) if variable.get("name") == name]
    return candidates[0] if candidates else None


def _resolve_c_item_size(
    expr: dict[str, Any],
    c_facts: dict[str, Any],
    function: str | None,
    seen: set[str] | None = None,
) -> tuple[int | None, str]:
    seen = seen or set()
    sizeof = expr.get("sizeof")
    if sizeof and sizeof.get("sizes"):
        if sizeof.get("pointer_operand"):
            return 4, f"compiler pointer width for sizeof {sizeof.get('type_names')}"
        for value in sizeof["sizes"]:
            if isinstance(value, int) and value > 0:
                return value, f"compiler sizeof {sizeof.get('type_names')} = {value}"
    literals = expr.get("literals") or []
    declrefs = expr.get("declrefs") or []
    if literals and not declrefs:
        return max(literals), f"literal expression values {literals}"
    for ref in declrefs:
        name = ref.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        if ref.get("array_elements") is not None:
            candidates = [
                array for array in c_facts.get("arrays", []) if array.get("name") == name
            ]
            for array in candidates:
                values = _array_values(array)
                if values:
                    return max(values), f"max of indexed C array {name} values {values}"
            return None, f"C array {name} has no statically resolvable element values"
        variable = _c_variable(c_facts, function, name)
        if variable and variable.get("init"):
            init = variable["init"]
            for init_ref in init.get("declrefs", []):
                array_name = init_ref.get("name")
                if init_ref.get("array_elements") is not None:
                    for array in c_facts.get("arrays", []):
                        if array.get("name") == array_name:
                            values = _array_values(array)
                            if values:
                                return max(values), f"max of indexed C array {array_name} values {values} reached through {name}"
            if init.get("literals"):
                return max(init["literals"]), f"initializer of {name}: {init['literals']}"
            value, reason = _resolve_c_item_size(init, c_facts, function, seen)
            if value is not None:
                return value, f"initializer of {name}: {reason}"
    if len(literals) == 1:
        return literals[0], f"single literal {literals[0]}"
    return None, "item-size expression could not be resolved mechanically"


def _c_buffer_capacity(
    expr: dict[str, Any],
    c_facts: dict[str, Any],
    function: str | None,
) -> tuple[int | None, str]:
    sizeof = expr.get("sizeof")
    if sizeof and sizeof.get("sizes"):
        return sizeof["sizes"][0], f"compiler sizeof {sizeof.get('type_names')}"
    declrefs = expr.get("declrefs") or []
    has_address = bool(expr.get("has_address_of"))
    if not declrefs:
        tokens = "".join(expr.get("tokens") or []).lower()
        if "null" in tokens:
            return 0, "null item pointer"
        return None, "no object declaration was resolved for the buffer"
    ref = declrefs[0]
    type_spec = ref.get("type") or {}
    array_elements = ref.get("array_elements")
    if type_spec.get("kind") == "array" and array_elements is not None:
        elem = type_spec.get("elem") or {}
        elem_size = elem.get("size")
        if elem_size is not None:
            return array_elements * elem_size, f"C array {ref.get('name')} has {array_elements} elements of {elem_size} bytes"
    if has_address and ref.get("type_size") is not None:
        return ref["type_size"], f"address-of {ref.get('name')} has object size {ref['type_size']}"
    if type_spec.get("kind") == "pointer" and ref.get("pointee_size") is not None:
        return ref["pointee_size"], f"pointee of pointer parameter {ref.get('name')} has size {ref['pointee_size']}"
    if ref.get("type_size") is not None:
        return ref["type_size"], f"object {ref.get('name')} has size {ref['type_size']}"
    return None, "buffer capacity could not be resolved"


def _rust_local(rust_facts: dict[str, Any], function: str | None, name: str) -> dict[str, Any] | None:
    for candidate in rust_facts.get("functions", []):
        if candidate.get("name") != function:
            continue
        params = {parameter.get("name"): parameter for parameter in candidate.get("params", [])}
        locals_ = {local.get("name"): local for local in candidate.get("locals", [])}
        if name in locals_:
            local = locals_[name]
            if not local.get("type"):
                init = (local.get("init") or "").strip()
                target = params.get(init) or locals_.get(init)
                if target and target.get("type"):
                    return {**local, "type": target["type"]}
            return local
        if name in params:
            return {"name": name, "type": params[name].get("type"), "param": True}
    return None


def _rust_constant(rust_facts: dict[str, Any], function: str | None, name: str) -> dict[str, Any] | None:
    for candidate in rust_facts.get("functions", []):
        if candidate.get("name") != function:
            continue
        for constant in candidate.get("local_consts", []):
            if constant.get("name") == name:
                return constant
    return next((constant for constant in rust_facts.get("constants", []) if constant.get("name") == name), None)


def _resolve_rust_item_size(
    expr: str,
    rust_facts: dict[str, Any],
    function: str | None,
    seen: set[str] | None = None,
) -> tuple[int | None, str]:
    seen = seen or set()
    text = expr.strip()
    compact = re.sub(r"\s+", "", text)
    cast = re.match(r"^(.*?)\s+as\s+[A-Za-z0-9_:<>* ]+$", text)
    if cast:
        text = cast.group(1).strip()
        compact = re.sub(r"\s+", "", text)
    indexed = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\[", text)
    lookup_name = indexed.group(1) if indexed else text
    constant = _rust_constant(rust_facts, function, lookup_name)
    if constant and lookup_name not in seen:
        seen.add(lookup_name)
        array = constant.get("array")
        values = _array_values(array)
        if values:
            return max(values), f"max of Rust const array {lookup_name} values {values}"
        value, reason = _resolve_rust_item_size(constant.get("value_expr", ""), rust_facts, function, seen)
        if value is not None:
            return value, f"{lookup_name}: {reason}"
    local = _rust_local(rust_facts, function, text)
    if local and text not in seen:
        seen.add(text)
        array = local.get("array")
        values = _array_values(array)
        if values:
            return max(values), f"max of local array {text} values {values}"
        if local.get("init"):
            value, reason = _resolve_rust_item_size(local["init"], rust_facts, function, seen)
            if value is not None:
                return value, f"{text}: {reason}"
    literal = _parse_int(text)
    if literal is not None:
        return literal, f"literal {literal}"
    size_match = re.match(r"core::mem::size_of::<(.+)>\(\)", compact)
    if size_match:
        type_name = size_match.group(1).strip()
        if type_name.startswith("*"):
            return 32 // 8, "size_of pointer on 32-bit target"
        sizes = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "u64": 8, "i64": 8, "usize": 4, "isize": 4, "c_void": None}
        value = sizes.get(type_name)
        if value is not None:
            return value, f"size_of::<{type_name}>() = {value}"
    return None, "Rust item-size expression could not be resolved mechanically"


def _rust_static_array(rust_facts: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((array for array in rust_facts.get("static_arrays", []) if array.get("name") == name), None)


def _rust_buffer_capacity(
    expr: str,
    rust_facts: dict[str, Any],
    function: str | None,
) -> tuple[int | None, str]:
    text = expr.strip()
    compact = re.sub(r"\s+", "", text)
    if "core::ptr::null" in compact or compact == "null":
        return 0, "null item pointer"
    array_match = re.search(r"addr_of(?:_mut)?!\(([A-Za-z_][A-Za-z0-9_]*)\)", compact)
    if array_match:
        name = array_match.group(1)
        array = _rust_static_array(rust_facts, name)
        if array:
            length = _array_len(array.get("array"))
            elem_size = _rust_type_size((array.get("array") or {}).get("elem"))
            if length is not None and elem_size is not None:
                return length * elem_size, f"static array {name} has {length} elements of {elem_size} bytes"
    pointer_method = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\.(?:as_mut_ptr|as_ptr)\(\)", compact)
    if pointer_method:
        name = pointer_method.group(1)
        local = _rust_local(rust_facts, function, name)
        if local and local.get("array"):
            length = _array_len(local["array"])
            elem_size = _rust_type_size(local["array"].get("elem"))
            if length is not None and elem_size is not None:
                return length * elem_size, f"local array {name} has {length} elements of {elem_size} bytes"
        static = _rust_static_array(rust_facts, name)
        if static:
            length = _array_len(static.get("array"))
            elem_size = _rust_type_size((static.get("array") or {}).get("elem"))
            if length is not None and elem_size is not None:
                return length * elem_size, f"static array {name} has {length} elements of {elem_size} bytes"
    address = re.match(r"^&\s*(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\b", text)
    if address:
        name = address.group(1)
        local = _rust_local(rust_facts, function, name)
        if local:
            value = _rust_type_size(local.get("type"))
            if value is not None:
                return value, f"address-of local {name} has size {value}"
    return None, "Rust buffer capacity could not be resolved mechanically"


def _assignment_for_line(repo_root: Path, file: str | None, line: int | None, call_name: str) -> str | None:
    if not file or not line:
        return None
    path = repo_root / file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for candidate_line in range(min(len(lines) - 1, line - 1), max(-1, line - 4), -1):
        text = lines[candidate_line]
        if "=" not in text:
            continue
        marker = re.search(r"(?<![=!<>])=\s*(?!>)", text)
        if not marker:
            continue
        lhs = text[: marker.start()].strip()
        if not lhs or any(word in lhs for word in ("if", "while", "for", "switch", "return")):
            continue
        lhs = lhs.split()[-1] if " " in lhs else lhs
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\s*\[[^\]]+\])?$", lhs):
            return lhs
    return None


def _source_line(repo_root: Path, file: str | None, line: int | None) -> str:
    if not file or not line:
        return ""
    try:
        return (repo_root / file).read_text(encoding="utf-8").splitlines()[line - 1]
    except (OSError, IndexError):
        return ""


def _create_map(
    c_facts: dict[str, Any],
    rust_facts: dict[str, Any],
    repo_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    c_map: dict[str, dict[str, Any]] = {}
    r_map: dict[str, dict[str, Any]] = {}

    for call in c_facts.get("calls", []):
        if call.get("name") not in CREATE_FUNCTIONS or len(call.get("args", [])) < 2:
            continue
        assigned = _assignment_for_line(repo_root, call.get("file"), call.get("line"), call.get("name", ""))
        if not assigned:
            continue
        key = _base_name(assigned)
        size, reason = _resolve_c_item_size(call["args"][1], c_facts, call.get("function"))
        line_text = _source_line(repo_root, call.get("file"), call.get("line"))
        if "xSemaphoreCreateBinary" in line_text:
            kind = "binary_semaphore"
        elif "xSemaphoreCreateCounting" in line_text:
            kind = "counting_semaphore"
        elif "xSemaphoreCreateMutex" in line_text or "xSemaphoreCreateRecursiveMutex" in line_text:
            kind = "mutex"
        else:
            kind = "message_queue"
        entry = c_map.setdefault(key, {"sizes": [], "evidence": [], "kind": kind})
        if size is not None:
            entry["sizes"].append(size)
            entry["evidence"].append({"line": call.get("line"), "item_size": size, "reason": reason})
    for call in [call for function in rust_facts.get("functions", []) for call in function.get("calls", [])]:
        if call.get("callee") not in CREATE_FUNCTIONS or len(call.get("args", [])) < 2:
            continue
        assigned = _assignment_for_line(repo_root, _file_for_rust_function(rust_facts, call.get("function")), call.get("line"), call.get("callee", ""))
        if not assigned:
            continue
        key = _base_name(assigned)
        size, reason = _resolve_rust_item_size(call["args"][1], rust_facts, call.get("function"))
        entry = r_map.setdefault(key, {"sizes": [], "evidence": [], "kind": "message_queue"})
        if size is not None:
            entry["sizes"].append(size)
            entry["evidence"].append({"line": call.get("line"), "item_size": size, "reason": reason})
    return c_map, r_map


def _file_for_rust_function(rust_facts: dict[str, Any], name: str) -> str | None:
    for function in rust_facts.get("functions", []):
        if function.get("name") == name:
            return function.get("file")
    return None


def _rust_helper_item_size(
    call: dict[str, Any],
    rust_facts: dict[str, Any],
    create_map: dict[str, dict[str, Any]],
) -> tuple[int | None, str]:
    function_name = call.get("function")
    args = call.get("args", [])
    if not args:
        return None, "queue call has no queue argument"
    queue_text = args[0]
    base = _base_name(queue_text)
    function = next((item for item in rust_facts.get("functions", []) if item.get("name") == function_name), None)
    param_names = {param.get("name") for param in (function or {}).get("params", [])}
    if base not in param_names:
        create = create_map.get(base)
        if create and create.get("sizes"):
            sizes = sorted(set(create["sizes"]))
            if len(sizes) == 1:
                return sizes[0], f"queue handle {base} connected to one creation item size"
            return None, f"queue handle {base} has multiple creation item sizes {sizes}"
        return None, f"queue handle {base} was not connected to a creation site"
    # Narrow helper propagation: all direct call sites of the helper must
    # resolve to creation sites with one common item size.
    sizes: set[int] = set()
    evidence: list[str] = []
    calls = [item for candidate in rust_facts.get("functions", []) for item in candidate.get("calls", []) if item.get("callee") == function_name]
    if not calls:
        return None, f"helper {function_name} has no visible call sites"
    for caller in calls:
        caller_args = caller.get("args", [])
        if not caller_args:
            return None, f"helper {function_name} call has no queue argument"
        caller_base = _base_name(caller_args[0])
        create = create_map.get(caller_base)
        if not create or not create.get("sizes"):
            return None, f"helper call queue {caller_base!r} has no creation item size"
        values = sorted(set(create["sizes"]))
        if len(values) != 1:
            return None, f"helper call queue {caller_base!r} has multiple item sizes"
        sizes.add(values[0])
        evidence.append(f"{caller_base}={values[0]}")
    if len(sizes) != 1:
        return None, f"helper calls resolve to different item sizes: {sorted(sizes)}"
    return next(iter(sizes)), "helper call sites agree: " + ", ".join(evidence)


def _c_semaphore_like(
    call: dict[str, Any],
    create: dict[str, Any] | None,
    repo_root: Path,
) -> tuple[bool, str]:
    line_text = _source_line(repo_root, call.get("file"), call.get("line"))
    if "xSemaphoreGive" in line_text or "xSemaphoreTake" in line_text:
        return True, "source call expands a semaphore API"
    if create and create.get("kind") in ("binary_semaphore", "counting_semaphore", "mutex"):
        return True, f"queue creation is a {create['kind']}"
    return False, "message queue"


def _rust_semaphore_like(call: dict[str, Any], rust_facts: dict[str, Any]) -> tuple[bool, str]:
    function_name = call.get("function") or ""
    if function_name in ("semaphore_give", "semaphore_take"):
        return True, "call is inside a semaphore helper"
    for function in rust_facts.get("functions", []):
        if function.get("name") == function_name:
            for item in function.get("calls", []):
                if item is call:
                    break
    return False, "message queue"


def check_04(
    c_facts: dict[str, Any],
    rust_facts: dict[str, Any],
    *,
    repo_root: Path,
    c_configuration: str,
    rust_configuration: str,
    tool_version: str,
) -> list[dict[str, Any]]:
    c_create, r_create = _create_map(c_facts, rust_facts, repo_root)
    rows: list[dict[str, Any]] = []

    for call in c_facts.get("calls", []):
        name = call.get("name")
        if name not in SEND_FUNCTIONS | RECEIVE_FUNCTIONS:
            continue
        direction = "send" if name in SEND_FUNCTIONS else "receive"
        args = call.get("args", [])
        queue_base = _base_name((args[0].get("declrefs") or [{}])[0].get("name", "")) if args else ""
        create = c_create.get(queue_base)
        not_applicable, kind_reason = _c_semaphore_like(call, create, repo_root)
        if not_applicable:
            rows.append(
                contract(
                    check_id="CHECK-04",
                    configuration=c_configuration,
                    language="c",
                    interface=f"{name}:{queue_base}:{direction}",
                    property_name="queue item size versus supplied message-buffer capacity",
                    status="NOT_APPLICABLE",
                    source_file=call.get("file"),
                    source_line=call.get("line"),
                    evidence={
                        "call": name,
                        "direction": direction,
                        "queue_handle_base": queue_base,
                        "operation_kind": create.get("kind") if create else "semaphore_api",
                        "reason": kind_reason,
                        "creation_sizes": create.get("sizes") if create else [],
                    },
                    assumptions={"scope": "zero-item semaphore/mutex operations do not transfer ordinary message buffers"},
                    tool="libclang AST",
                    tool_version=tool_version,
                    reason="not applicable: zero-item semaphore/mutex operation, not a message-buffer capacity check",
                    applicability="NOT_APPLICABLE",
                )
            )
            continue
        if len(args) < 2:
            status, reason, item_size, capacity = "UNKNOWN", "queue call does not expose the expected queue and item/buffer arguments", None, None
        else:
            item_size, item_reason = _resolve_c_item_size(args[1], c_facts, call.get("function"))
            capacity, capacity_reason = _c_buffer_capacity(args[1], c_facts, call.get("function"))
            if create and create.get("sizes"):
                item_size = max(create["sizes"])
                item_reason = "configured item size from the connected creation site"
            elif create:
                item_size = None
                item_reason = "queue creation exists but its item size is unresolved"
            else:
                item_size = None
                item_reason = f"queue handle {queue_base!r} was not connected to a creation site"
            if item_size is None:
                status, reason = "UNKNOWN", item_reason
            elif capacity is None:
                status, reason = "UNKNOWN", capacity_reason
            elif capacity < item_size:
                status, reason = "FAIL", f"buffer capacity {capacity} bytes is smaller than queue item size {item_size} bytes"
            else:
                status, reason = "PASS", f"buffer capacity {capacity} bytes accommodates queue item size {item_size} bytes"
        rows.append(
            contract(
                check_id="CHECK-04",
                configuration=c_configuration,
                language="c",
                interface=f"{name}:{queue_base or '?'}:{direction}",
                property_name="queue item size versus supplied message-buffer capacity",
                status=status,
                source_file=call.get("file"),
                source_line=call.get("line"),
                evidence={
                    "call": name, "direction": direction, "queue_handle_base": queue_base,
                    "operation_kind": "message_queue",
                    "item_expression": args[1].get("tokens") if len(args) > 1 else None,
                    "resolved_item_size": item_size, "item_reason": item_reason if len(args) > 1 else None,
                    "buffer_capacity": capacity, "capacity_reason": capacity_reason if len(args) > 1 else None,
                    "creation_sizes": create.get("sizes") if create else [],
                    "creation_evidence": create.get("evidence") if create else [],
                },
                assumptions={
                    "item_size": "configured item size at the connected creation site; indexed queue arrays use the maximum configured item size",
                    "capacity": "object/array capacity in bytes; pointer size is used for pointer-valued items",
                    "scope": "explicit message-buffer relationship at the call site",
                    "does_not_establish": "correct queue use, ownership, lifetime, blocking or absence of other users",
                },
                tool="libclang AST",
                tool_version=tool_version,
                reason=reason,
                applicability="APPLICABLE",
            )
        )

    for call in [item for function in rust_facts.get("functions", []) for item in function.get("calls", [])]:
        name = call.get("callee")
        if name not in SEND_FUNCTIONS | RECEIVE_FUNCTIONS or call.get("active") == "false":
            continue
        direction = "send" if name in SEND_FUNCTIONS else "receive"
        args = call.get("args", [])
        queue_base = _base_name(args[0]) if args else ""
        not_applicable, kind_reason = _rust_semaphore_like(call, rust_facts)
        if not_applicable:
            rows.append(
                contract(
                    check_id="CHECK-04",
                    configuration=rust_configuration,
                    language="rust",
                    interface=f"{name}:{queue_base}:{direction}",
                    property_name="queue item size versus supplied message-buffer capacity",
                    status="NOT_APPLICABLE",
                    source_file=_file_for_rust_function(rust_facts, call.get("function")),
                    source_line=call.get("line"),
                    evidence={"call": name, "direction": direction, "queue_handle_base": queue_base, "operation_kind": "semaphore", "reason": kind_reason},
                    assumptions={"scope": "zero-item semaphore/mutex operations do not transfer ordinary message buffers"},
                    tool="syn",
                    tool_version=tool_version,
                    reason="not applicable: zero-item semaphore operation, not a message-buffer capacity check",
                    applicability="NOT_APPLICABLE",
                )
            )
            continue
        if len(args) < 2:
            rows.append(
                contract(
                    check_id="CHECK-04", configuration=rust_configuration, language="rust",
                    interface=f"{name}:{queue_base or '?'}:{direction}",
                    property_name="queue item size versus supplied message-buffer capacity",
                    status="UNKNOWN", source_file=_file_for_rust_function(rust_facts, call.get("function")),
                    source_line=call.get("line"), evidence={"call": call},
                    assumptions={}, tool="syn", tool_version=tool_version,
                    reason="queue call does not expose the expected queue and buffer arguments",
                )
            )
            continue
        item_size, item_reason = _rust_helper_item_size(call, rust_facts, r_create)
        capacity, capacity_reason = _rust_buffer_capacity(args[1], rust_facts, call.get("function"))
        if item_size is None:
            status, reason = "UNKNOWN", item_reason
        elif capacity is None:
            status, reason = "UNKNOWN", capacity_reason
        elif capacity < item_size:
            status, reason = "FAIL", f"buffer capacity {capacity} bytes is smaller than queue item size {item_size} bytes"
        else:
            status, reason = "PASS", f"buffer capacity {capacity} bytes accommodates queue item size {item_size} bytes"
        rows.append(
            contract(
                check_id="CHECK-04", configuration=rust_configuration, language="rust",
                interface=f"{name}:{queue_base or '?'}:{direction}",
                property_name="queue item size versus supplied message-buffer capacity",
                status=status, source_file=_file_for_rust_function(rust_facts, call.get("function")),
                source_line=call.get("line"),
                evidence={
                    "call": name, "direction": direction, "queue_handle_base": queue_base,
                    "item_expression": args[1], "resolved_item_size": item_size,
                    "item_reason": item_reason, "buffer_capacity": capacity,
                    "capacity_reason": capacity_reason,
                    "creation_sizes": r_create.get(queue_base, {}).get("sizes", []),
                },
                assumptions={
                    "item_size": "connected creation-site item size, or common item size across visible helper call sites",
                    "capacity": "visible buffer object/array capacity in bytes",
                    "scope": "explicit message-buffer relationship at the call site",
                    "does_not_establish": "correct queue use, ownership, lifetime, blocking or absence of other users",
                },
                tool="syn", tool_version=tool_version, reason=reason,
                applicability="APPLICABLE",
            )
        )

    if not rows:
        rows.append(
            contract(
                check_id="CHECK-04", configuration=f"{c_configuration};{rust_configuration}",
                language="c/rust", interface="(no queue send/receive call sites)",
                property_name="queue item size versus supplied message-buffer capacity",
                status="NOT_CHECKED", source_file=None, source_line=None,
                evidence={"c_calls": len(c_facts.get("calls", [])), "rust_functions": len(rust_facts.get("functions", []))},
                assumptions={"scope": "supported FreeRTOS queue send/receive APIs"},
                tool="libclang AST + syn", tool_version=tool_version,
                reason="no supported queue send/receive call site was discovered",
            )
        )
    return rows
