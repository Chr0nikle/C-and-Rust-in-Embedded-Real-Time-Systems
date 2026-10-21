"""CHECK-02: compare compiler-derived C and Rust structure layouts."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from common.io import contract


def _sanitize(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _compile_rust_probe(repo_root: Path, structs: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Compile `size_of`/`align_of`/`offset_of!` probes for real source structs."""
    rustc = shutil.which("rustc")
    if not rustc:
        return {}, "rustc is unavailable"
    if not structs:
        return {}, None
    by_file: dict[str, list[dict[str, Any]]] = {}
    for struct in structs:
        by_file.setdefault(struct["file"], []).append(struct)

    lines = ["#![no_std]"]
    metadata: list[tuple[str, dict[str, Any]]] = []
    index = 0
    for file, items in sorted(by_file.items()):
        module = f"fixture_{index}"
        index += 1
        absolute = (repo_root / file).resolve()
        lines.append(f"mod {module} {{")
        lines.append(f'    include!(r#"{absolute}"#);')
        for struct in items:
            key = f"{_sanitize(struct['name'])}_{len(metadata)}"
            name = struct["name"]
            lines.append(f"    #[no_mangle]")
            lines.append(f"    pub extern \"C\" fn __uastw_size_{key}() -> usize {{ core::mem::size_of::<{name}>() }}")
            lines.append(f"    #[no_mangle]")
            lines.append(f"    pub extern \"C\" fn __uastw_align_{key}() -> usize {{ core::mem::align_of::<{name}>() }}")
            field_keys: list[tuple[str, str]] = []
            for field_index, field in enumerate(struct.get("fields", [])):
                if not field.get("name"):
                    continue
                field_key = f"__uastw_offset_{key}_{field_index}"
                lines.append(f"    #[no_mangle]")
                lines.append(
                    f"    pub extern \"C\" fn {field_key}() -> usize {{ core::mem::offset_of!({name}, {field['name']}) }}"
                )
                field_keys.append((field["name"], field_key))
            metadata.append((key, {"struct": struct, "field_keys": field_keys, "module": module}))
        lines.append("}")

    with tempfile.TemporaryDirectory(prefix="uastw-layout-") as directory:
        source = Path(directory) / "probe.rs"
        output = Path(directory) / "probe.ll"
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        command = [
            rustc,
            "--edition",
            "2021",
            "--crate-type",
            "lib",
            "--emit=llvm-ir",
            "-O",
            "--target",
            "thumbv7em-none-eabihf",
            "-o",
            str(output),
            str(source),
        ]
        completed = subprocess.run(command, cwd=repo_root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if completed.returncode != 0:
            return {}, f"Rust layout probe failed: {completed.stderr.splitlines()[-1] if completed.stderr else 'unknown error'}"
        ir = output.read_text(encoding="utf-8", errors="replace")

    values: dict[str, int] = {}
    pattern = re.compile(
        r"@(__uastw_(?:size|align|offset)_[A-Za-z0-9_]+)\(\)[^\n]*\{\s*(?:[A-Za-z0-9_.]+:\s*)?ret i32 (\d+)",
        re.MULTILINE,
    )
    for symbol, value in pattern.findall(ir):
        values[symbol] = int(value)
    aliases = re.findall(
        r"^@(__uastw_(?:size|align|offset)_[A-Za-z0-9_]+)\s*=.*?alias i32 \(\), ptr @(__uastw_(?:size|align|offset)_[A-Za-z0-9_]+)",
        ir,
        re.MULTILINE,
    )
    for alias, target in aliases:
        if target in values:
            values[alias] = values[target]

    facts: dict[str, dict[str, Any]] = {}
    for key, item in metadata:
        size = values.get(f"__uastw_size_{key}")
        align = values.get(f"__uastw_align_{key}")
        offsets = {
            field_name: values.get(symbol)
            for field_name, symbol in item["field_keys"]
        }
        facts[item["struct"]["name"]] = {
            "size": size,
            "align": align,
            "offsets": offsets,
            "file": item["struct"]["file"],
            "line": item["struct"]["line"],
            "repr_c": item["struct"].get("repr_c"),
            "active": item["struct"].get("active"),
        }
    return facts, None


def check_02(
    c_facts: dict[str, Any],
    rust_facts: dict[str, Any],
    *,
    repo_root: Path,
    configuration: str,
    tool_version: str,
) -> list[dict[str, Any]]:
    c_structs = {struct["name"]: struct for struct in c_facts.get("structs", []) if struct.get("name")}
    rust_structs = {struct["name"]: struct for struct in rust_facts.get("structs", []) if struct.get("name")}
    pairs = sorted(set(c_structs) & set(rust_structs))
    rows: list[dict[str, Any]] = []

    if not pairs:
        return [
            contract(
                check_id="CHECK-02",
                configuration=configuration,
                language="cross-language",
                interface="(no paired FFI structures in configuration)",
                property_name="C/Rust structure size, alignment and field offsets",
                status="NOT_CHECKED",
                source_file=None,
                source_line=None,
                evidence={"c_structs": len(c_structs), "rust_structs": len(rust_structs), "paired": 0},
                assumptions={"scope": "only same-name project C and Rust #[repr(C)] structures are paired"},
                tool="libclang AST + rustc layout probe",
                tool_version=tool_version,
                reason="the selected FreeRTOS STM32F4 FFI surface passes opaque handles and scalars; no paired FFI-exposed structure was discovered",
            )
        ]

    rust_layouts, probe_error = _compile_rust_probe(
        repo_root,
        [rust_structs[name] for name in pairs if rust_structs[name].get("repr_c")],
    )
    for name in pairs:
        c_struct = c_structs[name]
        rust_struct = rust_structs[name]
        source_file = rust_struct.get("file") or c_struct.get("file")
        source_line = rust_struct.get("line") or c_struct.get("line")
        if not rust_struct.get("repr_c"):
            rows.append(
                contract(
                    check_id="CHECK-02",
                    configuration=configuration,
                    language="cross-language",
                    interface=name,
                    property_name="C/Rust structure size, alignment and field offsets",
                    status="UNKNOWN",
                    source_file=source_file,
                    source_line=source_line,
                    evidence={"rust_repr_c": False, "c_layout": c_struct},
                    assumptions={"scope": "only #[repr(C)] Rust structures have a defined C layout"},
                    tool="libclang AST + syn",
                    tool_version=tool_version,
                    reason="Rust structure does not carry #[repr(C)]",
                )
            )
            continue
        if rust_struct.get("active") == "false":
            rows.append(
                contract(
                    check_id="CHECK-02",
                    configuration=configuration,
                    language="cross-language",
                    interface=name,
                    property_name="C/Rust structure size, alignment and field offsets",
                    status="NOT_CHECKED",
                    source_file=source_file,
                    source_line=source_line,
                    evidence={"cfg": rust_struct.get("cfg")},
                    assumptions={},
                    tool="libclang AST + syn",
                    tool_version=tool_version,
                    reason="Rust structure is inactive under the selected target configuration",
                )
            )
            continue
        rust_layout = rust_layouts.get(name)
        if rust_layout is None or rust_layout.get("size") is None:
            rows.append(
                contract(
                    check_id="CHECK-02",
                    configuration=configuration,
                    language="cross-language",
                    interface=name,
                    property_name="C/Rust structure size, alignment and field offsets",
                    status="UNKNOWN",
                    source_file=source_file,
                    source_line=source_line,
                    evidence={"probe_error": probe_error, "rust_layout": rust_layout},
                    assumptions={"compiler_probe": "rustc --emit=llvm-ir on the selected target"},
                    tool="rustc layout probe",
                    tool_version=tool_version,
                    reason="Rust compiler-derived layout was unavailable",
                )
            )
            continue
        mismatches: list[str] = []
        if c_struct.get("size") != rust_layout.get("size"):
            mismatches.append(f"size C={c_struct.get('size')} Rust={rust_layout.get('size')}")
        if c_struct.get("align") != rust_layout.get("align"):
            mismatches.append(f"align C={c_struct.get('align')} Rust={rust_layout.get('align')}")
        c_offsets = {field.get("name"): field.get("offset") for field in c_struct.get("fields", [])}
        rust_offsets = rust_layout.get("offsets", {})
        if set(c_offsets) != set(rust_offsets):
            mismatches.append(f"field set differs: C={sorted(c_offsets)} Rust={sorted(rust_offsets)}")
        for field_name, rust_offset in rust_offsets.items():
            c_offset = c_offsets.get(field_name)
            if rust_offset is None or c_offset is None:
                mismatches.append(f"offset unresolved for field {field_name}")
            elif c_offset != rust_offset:
                mismatches.append(f"offset {field_name} C={c_offset} Rust={rust_offset}")
        status = "PASS" if not mismatches else "FAIL"
        rows.append(
            contract(
                check_id="CHECK-02",
                configuration=configuration,
                language="cross-language",
                interface=name,
                property_name="C/Rust structure size, alignment and field offsets",
                status=status,
                source_file=source_file,
                source_line=source_line,
                evidence={
                    "c": {"file": c_struct.get("file"), "line": c_struct.get("line"), "size": c_struct.get("size"), "align": c_struct.get("align"), "offsets": c_offsets},
                    "rust": {**rust_layout, "file": rust_struct.get("file"), "line": rust_struct.get("line")},
                    "mismatches": mismatches,
                },
                assumptions={
                    "compiler_facts": "C values from libclang for arm-none-eabi; Rust values from rustc LLVM IR for thumbv7em-none-eabihf",
                    "does_not_establish": "semantic compatibility, pointer validity or lifetime",
                },
                tool="libclang AST + rustc layout probe",
                tool_version=tool_version,
                reason="compiler-derived layouts agree" if status == "PASS" else "; ".join(mismatches),
            )
        )
    return rows
