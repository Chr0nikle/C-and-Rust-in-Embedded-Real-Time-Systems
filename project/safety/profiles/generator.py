"""Generate Stage 2 matched-API profiles from repository source and build evidence.

The generator deliberately treats the shared benchmark runner records and the
board build databases as authoritative.  It does not contain per-case method
tables; those are recovered from the shared C/Rust dispatchers, the selected
board port initializer, and the source extractors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from common.layers import LayerClassifier
from matched.c_extract import extract_c_index, reachable_edges
from profiles.definitions import discover_profile_configs
from matched.r_extract import build_rust_index, ffi_rows, reachable_rust_edges, reachable_rust_functions
from common.config import resolve_repo_root, _discover_rust_modules
from common.paths import ARTIFACTS, SAFETY_DIR

PROFILE_DIR = Path(__file__).resolve().parent / "data"
GENERATED_DIR = ARTIFACTS / "profiles"
SCHEMA = "uastw-matched-api-profile-v1"
GENERATOR_SCHEMA = "uastw-matched-api-profile-generator-v1"

STANDARD_LIBRARY_HEADERS = {
    "string.h", "stdio.h", "stdlib.h", "unistd.h", "termios.h", "fcntl.h",
    "errno.h", "ctype.h", "math.h",
}
ALLOCATION_EXCEPTIONS = {"malloc", "calloc", "realloc", "free"}

BOARD_INFO = {
    "stm32f4discovery": {"short": "stm32f4", "display": "STM32F4 Discovery", "arm": True},
    "stm32h723": {"short": "stm32h723", "display": "STM32H723", "arm": True},
    "nrf52840dk": {"short": "nrf52840", "display": "nRF52840 DK", "arm": True},
    "esp32c6": {"short": "esp32c6", "display": "ESP32-C6", "arm": False},
}


def layout_for(rtos: str, board: str) -> dict[str, str]:
    if rtos not in ("freertos", "nuttx", "zephyr"):
        raise ProfileGenerationError(f"unsupported RTOS: {rtos}")
    if board not in BOARD_INFO:
        raise ProfileGenerationError(f"unsupported board: {board}")
    info = BOARD_INFO[board]
    short = info["short"]
    layout: dict[str, str] = {
        "rtos": rtos,
        "board": board,
        "id": f"{rtos}-{short}",
        "display": {"freertos": "FreeRTOS", "nuttx": "NuttX", "zephyr": "Zephyr"}[rtos],
        "board_display": info["display"],
        "rust_impl": f"firmware/c-kernels/{rtos}/rust/src/lib.rs",
        "external_source": {"freertos": ".local/source/FreeRTOS-Kernel", "nuttx": ".local/source/nuttx", "zephyr": ".local/west/zephyr"}[rtos],
        "compile_commands": None,
        "rust_target": "riscv32imac-unknown-none-elf" if not info["arm"] else (
            "thumbv7em-none-eabihf" if rtos == "freertos" or (rtos == "nuttx" and board == "stm32h723")
            else "thumbv7em-none-eabi"
        ),
    }
    if rtos == "freertos":
        if board == "esp32c6":
            layout.update({
                "c_dir": "firmware/c-kernels/freertos/c",
                "c_impl": "firmware/c-kernels/freertos/c/main/bench.c",
                "c_wire": "firmware/c-kernels/freertos/c/main/bench.c",
                "c_wire_function": "protocol_id",
                "c_root_files": ["firmware/c-kernels/freertos/c/main/bench.c", "firmware/c-kernels/freertos/c/main/main.c"],
                "compile_commands": "firmware/c-kernels/freertos/c/build/compile_commands.json",
            })
        else:
            board_dir = "stm32f4" if board == "stm32f4discovery" else board
            base = f"firmware/c-kernels/freertos/c/{board_dir}/src"
            it = {"stm32f4discovery": "stm32f4xx_it.c", "stm32h723": "stm32h7xx_it.c", "nrf52840dk": "startup_nrf52840.c"}[board]
            layout.update({
                "c_dir": f"firmware/c-kernels/freertos/c/{board_dir}",
                "c_impl": f"{base}/bench.c",
                "c_wire": f"{base}/bench.c",
                "c_wire_function": "sink_case_id",
                "c_root_files": [f"{base}/bench.c", f"{base}/main.c", f"{base}/{it}"],
            })
    elif rtos == "nuttx":
        db = {
            "stm32f4discovery": ".local/source/nuttx/build-bench/compile_commands.json",
            "stm32h723": ".local/source/nuttx/build-bench-h723/compile_commands.json",
            "nrf52840dk": ".local/source/nuttx/build-nrf52840dk-bench/compile_commands.json",
            "esp32c6": ".local/source/nuttx/build-c6-bench/compile_commands.json",
        }[board]
        layout.update({
            "c_dir": "firmware/c-kernels/nuttx/c",
            "c_impl": "firmware/c-kernels/nuttx/c/port.c",
            "c_wire": "firmware/c-kernels/nuttx/c/main.c",
            "c_wire_function": "protocol_id",
            "c_root_files": ["firmware/c-kernels/nuttx/c/port.c", "firmware/c-kernels/nuttx/c/isr.c"],
            "compile_commands": db,
        })
    else:
        db = {
            "stm32f4discovery": ".local/west/build-test/compile_commands.json",
            "stm32h723": ".local/west/build-stm32h723/compile_commands.json",
            "nrf52840dk": ".local/west/build-nrf52840dk/compile_commands.json",
            "esp32c6": ".local/west/build-zephyr-c6-bench/compile_commands.json",
        }[board]
        base = f"firmware/c-kernels/zephyr/c/{board if board != 'stm32f4discovery' else 'stm32f4'}"
        layout.update({
            "c_dir": base,
            "c_impl": f"{base}/port.c",
            "c_wire": f"{base}/main.c",
            "c_wire_function": "protocol_id",
            "c_root_files": [f"{base}/port.c"],
            "compile_commands": db,
        })
    return layout


# Backwards-compatible STM32F4 aliases used by older imports.
LAYOUTS = {rtos: layout_for(rtos, "stm32f4discovery") for rtos in ("freertos", "nuttx", "zephyr")}


NATIVE_LAYOUTS = {
    "freertos-in-rust-stm32f4": {
        "id": "native-freertos-in-rust-stm32f4", "rtos": "FreeRTOS-in-Rust",
        "board": "STM32F4 Discovery", "crate": "firmware/rust-kernels/freertos-in-rust/rust",
        "target": "thumbv7em-none-eabihf", "features": ["f4"], "matched_analysis": "not_applicable",
    },
    "freertos-in-rust-stm32h723": {
        "id": "native-freertos-in-rust-stm32h723", "rtos": "FreeRTOS-in-Rust",
        "board": "STM32H723", "crate": "firmware/rust-kernels/freertos-in-rust/rust",
        "target": "thumbv7em-none-eabihf", "features": ["stm32h723"], "matched_analysis": "not_applicable",
    },
    "rivet-stm32f4": {
        "id": "native-rivet-stm32f4", "rtos": "Rivet", "board": "STM32F4 Discovery",
        "crate": "firmware/rust-kernels/rivet/rust/stm32f4", "target": "thumbv7em-none-eabi",
        "matched_analysis": "not_applicable",
    },
}


class ProfileGenerationError(RuntimeError):
    pass


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise ProfileGenerationError(f"cannot read {path}: {exc}") from exc


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rel(repo: Path, path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(repo))
    except ValueError:
        return os.path.relpath(resolved, repo)


def _git_short(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=8", "HEAD"],
                          text=True, capture_output=True, check=True).stdout.strip()


def _tool_version(repo: Path) -> str:
    candidates = sorted((repo / ".local/toolchains/arm-none-eabi").glob("*/bin/arm-none-eabi-gcc"))
    exe = str(candidates[0]) if candidates else "arm-none-eabi-gcc"
    try:
        first = subprocess.run([exe, "--version"], text=True, capture_output=True, check=False).stdout.splitlines()[0]
        match = re.search(r"(\d+\.\d+(?:\.\d+)?)", first)
        if match:
            return f"arm-none-eabi-gcc {match.group(1)}"
    except OSError:
        pass
    return "arm-none-eabi-gcc (version not available)"


def parse_thesis_cases(repo: Path) -> list[dict[str, Any]]:
    path = repo.parent / "thesis/sections/benchmark-specification.typ"
    text = _read(path)
    cases: list[dict[str, Any]] = []
    mechanism = ""
    for line in text.splitlines():
        section = re.match(r"^==\s+(.+?)\s*$", line)
        if section and not line.startswith("==="):
            mechanism = section.group(1).strip()
            continue
        match = re.match(r"^===\s+(\d+)\.\s+`([a-z0-9_]+)`\s*$", line)
        if match:
            cases.append({
                "thesis_case": int(match.group(1)),
                "thesis_name": match.group(2),
                "mechanism": mechanism,
            })
    if not cases:
        # The appendix also has a prose form: one ordered bullet per case.
        for index, match in enumerate(
            re.finditer(r"^\+ `([a-z0-9_]+)`", text, re.MULTILINE), start=1
        ):
            cases.append({
                "thesis_case": index,
                "thesis_name": match.group(1),
                "mechanism": "",
            })
    if not cases:
        raise ProfileGenerationError(f"no thesis cases found in {_rel(repo, path)}")
    return cases


def parse_c_enum(repo: Path) -> list[dict[str, Any]]:
    text = _read(repo / "firmware/common/c/include/rtos_bench.h")
    matches = list(re.finditer(r"typedef\s+enum\s*\{([^{}]*)\}\s*bench_case\s*;", text, re.S))
    if not matches:
        raise ProfileGenerationError("bench_case enum not found in rtos_bench.h")
    body = matches[-1]
    value = -1
    result = []
    for item in body.group(1).split(","):
        item = re.sub(r"/\*.*?\*/", "", item, flags=re.S).strip()
        if not item:
            continue
        match = re.match(r"(BENCH_[A-Z0-9_]+)(?:\s*=\s*(-?\d+))?", item)
        if not match:
            continue
        value = int(match.group(2)) if match.group(2) is not None else value + 1
        result.append({"name": match.group(1), "value": value})
    return result


def parse_rust_enum(repo: Path) -> list[dict[str, Any]]:
    text = _read(repo / "firmware/common/rust/src/lib.rs")
    body = re.search(r"pub\s+enum\s+Case\s*\{(.*?)\n\}", text, re.S)
    if not body:
        raise ProfileGenerationError("Rust Case enum not found")
    value = -1
    result = []
    for line in body.group(1).splitlines():
        line = re.sub(r"//.*", "", line).strip().rstrip(",")
        if not line:
            continue
        match = re.match(r"([A-Z][A-Za-z0-9_]*)(?:\s*=\s*(-?\d+))?", line)
        if not match:
            continue
        value = int(match.group(2)) if match.group(2) is not None else value + 1
        result.append({"name": match.group(1), "value": value})
    return result


def _function_body(text: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*(?:<[^>]+>)?\s*\([^;{{]*\)\s*(?:->\s*[^{{]+)?\{{", text)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def parse_c_wire_map(repo: Path, layout: dict[str, str]) -> dict[str, int]:
    text = _read(repo / layout["c_wire"])
    body = _function_body(text, layout["c_wire_function"])
    if body is None:
        raise ProfileGenerationError(f"C wire function not found: {layout['c_wire_function']}")
    result: dict[str, int] = {}
    for match in re.finditer(r"case\s+(BENCH_[A-Z0-9_]+)\s*:\s*(?:\n\s*)?return\s+([0-9]+)U?\s*;", body):
        result[match.group(1)] = int(match.group(2))
    for match in re.finditer(r"if\s*\(\s*c\s*==\s*(BENCH_[A-Z0-9_]+)\s*\)\s*\{\s*return\s+([0-9]+)U?\s*;", body):
        result[match.group(1)] = int(match.group(2))
    return result


def parse_c_wire_default(repo: Path, layout: dict[str, str]) -> bool:
    text = _read(repo / layout["c_wire"])
    body = _function_body(text, layout["c_wire_function"])
    if body is None:
        return False
    match = re.search(r"default\s*:\s*return\s+(?:\(\s*uint32_t\s*\)\s*)?c\s*;", body)
    return match is not None


def parse_supported_cases(repo: Path, layout: dict[str, str]) -> set[str] | None:
    text = _read(repo / layout["c_wire"])
    match = re.search(r"s_supported_cases\s*\[\s*\]\s*=\s*\{(.*?)\};", text, re.S)
    if not match:
        return None
    return set(re.findall(r"BENCH_[A-Z0-9_]+", match.group(1)))


def parse_rust_wire_map(repo: Path, layout: dict[str, str]) -> dict[str, int]:
    text = _read(repo / layout["rust_impl"])
    body = _function_body(text, "case_id_for")
    if body is None:
        raise ProfileGenerationError("Rust case_id_for not found")
    result: dict[str, int] = {}
    for match in re.finditer(r"Case::([A-Za-z0-9_]+)\s*=>\s*([0-9]+)\s*,", body):
        result[match.group(1)] = int(match.group(2))
    return result


def _normalise_tokens(value: str) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return [part for part in re.split(r"[^a-z0-9]+", value.lower()) if part]


def map_names(thesis: list[dict[str, Any]], c_enum: list[dict[str, Any]],
              rust_enum: list[dict[str, Any]], c_wire: dict[str, int],
              rust_wire: dict[str, int]) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    """Map thesis names to C/Rust identifiers using wire IDs and token evidence."""
    c_by_wire = {wire: name for name, wire in c_wire.items()}
    rust_by_wire = {wire: name for name, wire in rust_wire.items()}
    c_values = {item["name"]: item["value"] for item in c_enum}
    rust_values = {item["name"]: item["value"] for item in rust_enum}
    result: dict[int, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    for case in thesis:
        tokens = _normalise_tokens(case["thesis_name"])
        def candidates(names: Iterable[str], enum_tokens: dict[str, list[str]]) -> tuple[list[str], list[str]]:
            subset, prefix = [], []
            for name in names:
                current = enum_tokens[name]
                if all(token in current for token in tokens):
                    subset.append(name)
                if current == tokens[:len(current)]:
                    prefix.append(name)
            exact = [name for name in subset if enum_tokens[name] == tokens]
            return exact or prefix or subset, subset

        c_tokens = {name: [token for token in _normalise_tokens(name) if token != "bench"] for name in c_values}
        rust_tokens = {name: _normalise_tokens(name) for name in rust_values}
        c_pool, c_subset = candidates(list(c_values), c_tokens)
        rust_pool, rust_subset = candidates(list(rust_values), rust_tokens)
        c_name = c_pool[0] if len(c_pool) == 1 else None
        rust_name = rust_pool[0] if len(rust_pool) == 1 else None
        explicit_c_wire = c_wire.get(c_name) if c_name else None
        c_wire_id = explicit_c_wire if explicit_c_wire is not None else (c_values.get(c_name) if c_name else None)
        rust_wire_id = rust_wire.get(rust_name, rust_values.get(rust_name)) if rust_name else None
        # Only an explicit C wire entry is authoritative; the enum fallback is
        # an internal case identifier and may not equal the protocol ID.
        if explicit_c_wire is not None and explicit_c_wire in rust_by_wire:
            rust_name = rust_by_wire[explicit_c_wire]
            rust_wire_id = explicit_c_wire
        if c_name is None and rust_wire_id is not None and rust_wire_id in c_by_wire:
            c_name = c_by_wire[rust_wire_id]
            c_wire_id = rust_wire_id
        if c_name is None or rust_name is None:
            unresolved.append({
                "thesis_case": case["thesis_case"],
                "thesis_name": case["thesis_name"],
                "reason": "no unique C enum/Rust variant match from name and wire evidence",
                "c_candidates": c_pool,
                "rust_candidates": rust_pool,
            })
            continue
        result[case["thesis_case"]] = {
            **case,
            "c_enum": c_name,
            "c_enum_value": c_values.get(c_name),
            "c_wire_id": c_wire_id,
            "rust_variant": rust_name,
            "rust_discriminant": rust_values.get(rust_name),
            "rust_wire_id": rust_wire_id,
        }
    return result, unresolved


def parse_c_case_methods(repo: Path) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    text = _read(repo / "firmware/common/c/src/runner.c")
    predicates: dict[str, list[str]] = {}
    for match in re.finditer(r"const\s+int\s+(is_\w+)\s*=\s*([^;]+);", text):
        enums = re.findall(r"BENCH_[A-Z0-9_]+", match.group(2))
        if enums:
            predicates[match.group(1)] = enums

    def enums_for(expression: str) -> list[str]:
        enums: list[str] = []
        for name in re.findall(r"\bis_\w+\b", expression):
            for enum in predicates.get(name, []):
                if enum not in enums:
                    enums.append(enum)
        return enums

    create: dict[str, list[str]] = {}
    for match in re.finditer(r"if\s*\(([^)]*)\)\s*\{\s*status\s*=\s*port->(\w+)\(", text):
        for enum in enums_for(match.group(1)):
            create.setdefault(enum, []).append(match.group(2))
    sample_body = _function_body(text, "sample")
    sample: dict[str, list[str]] = {}
    if sample_body:
        for match in re.finditer(
                r"(?:if|else\s+if)\s*\((case_id\s*==\s*BENCH_[A-Z0-9_]+(?:\s*\|\|\s*case_id\s*==\s*BENCH_[A-Z0-9_]+)*)\)\s*\{",
                sample_body):
            enums = re.findall(r"BENCH_[A-Z0-9_]+", match.group(1))
            start_index = match.end() - 1
            depth = 0
            end_index = start_index
            for index in range(start_index, len(sample_body)):
                if sample_body[index] == "{":
                    depth += 1
                elif sample_body[index] == "}":
                    depth -= 1
                    if depth == 0:
                        end_index = index
                        break
            arm = sample_body[start_index:end_index + 1]
            methods = re.findall(r"port->(\w+)\(", arm)
            for enum in enums:
                sample.setdefault(enum, []).extend(methods)
    cleanup: dict[str, list[str]] = {}
    for match in re.finditer(r"if\s*\(([^)]*)\)\s*\{\s*bench_status\s+cleanup\s*=\s*port->(\w+)\(", text):
        for enum in enums_for(match.group(1)):
            cleanup.setdefault(enum, []).append(match.group(2))
    optional: dict[str, list[str]] = {}
    sweep_map = {"is_peer_count_sweep": "BENCH_YIELD_ROUNDTRIP", "is_pjit_load_sweep": "BENCH_PERIODIC_JITTER_LOADED"}
    for variable, enum in sweep_map.items():
        for match in re.finditer(rf"if\s*\({variable}\)\s*\{{\s*status\s*=\s*port->(\w+)\(", text):
            optional.setdefault(enum, []).append(match.group(1))
    combined: dict[str, list[str]] = {}
    for enum in set(create) | set(sample) | set(cleanup) | set(optional):
        seen: list[str] = []
        for group in (create.get(enum, []), sample.get(enum, []), cleanup.get(enum, []), optional.get(enum, [])):
            for method in group:
                if method not in seen:
                    seen.append(method)
        combined[enum] = seen
    return combined, optional


def parse_rust_case_methods(repo: Path, c_methods: dict[str, list[str]] | None = None,
                           impl_methods: set[str] | None = None) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Recover Rust case methods from the shared dispatcher and adapter impl.

    The shared Rust runner mirrors the C case path.  We derive the candidate
    method set from the C dispatcher, retain only methods overridden by the
    selected Rust adapter, and require each retained name to appear as a
    ``port.<method>(`` call in the shared Rust core.
    """
    text = _read(repo / "firmware/common/rust/src/lib.rs")
    c_methods = c_methods or {}
    impl_methods = impl_methods or set()
    combined: dict[str, list[str]] = {}
    for enum, methods in c_methods.items():
        candidates = [method for method in methods if method in impl_methods]
        verified = [method for method in candidates if re.search(rf"port\.{re.escape(method)}\s*\(", text)]
        combined[enum] = verified
    optional = {
        enum: [method for method in methods if method in {"peers_set", "pjit_set_load"} and method in impl_methods]
        for enum, methods in c_methods.items()
    }
    return combined, optional


def parse_port_map(repo: Path, path: str) -> dict[str, str]:
    text = _read(repo / path)
    result = {match.group(1): match.group(2) for match in re.finditer(r"port->(\w+)\s*=\s*(\w+)\s*;", text)}
    initializer = re.search(r"bench_port\s+\w+\s*=\s*\{(.*?)\n\s*\};", text, re.S)
    if initializer:
        for match in re.finditer(r"\.(\w+)\s*=\s*([A-Za-z_]\w*)", initializer.group(1)):
            result.setdefault(match.group(1), match.group(2))
    return result


def parse_rust_impl_methods(repo: Path, path: str, impl_type: str) -> set[str]:
    text = _read(repo / path)
    body = re.search(rf"impl\s+Port\s+for\s+{re.escape(impl_type)}\s*\{{(.*?)\n\}}", text, re.S)
    if not body:
        body = re.search(r"impl\s+Port\s+for\s+(\w+)\s*\{(.*?)\n\}", text, re.S)
    if not body:
        raise ProfileGenerationError(f"impl Port for {impl_type} not found")
    content = body.group(2) if body.lastindex == 2 else body.group(1)
    return set(re.findall(r"\bfn\s+([a-z_][a-z0-9_]*)\s*\(", content))


def _source_units_from_db(repo: Path, db: Path, roots: list[str]) -> list[str]:
    data = json.loads(_read(db))
    units: list[str] = []
    for entry in data:
        file_name = Path(entry.get("file", "")).resolve()
        try:
            relative = _rel(repo, file_name)
        except ValueError:
            continue
        if not relative.endswith(".c") or "/build/" in relative or Path(relative).name.startswith("project_elf_src_"):
            continue
        if any(relative == root or relative.startswith(root.rstrip("/") + "/") for root in roots):
            units.append(relative)
    return sorted(set(units))


def _make_compile_database(repo: Path, rtos: str, board: str, rust: bool, out_dir: Path) -> Path:
    layout = layout_for(rtos, board)
    make_dir = repo / layout["c_dir"]
    command = ["make", "-B", "-n", "-C", str(make_dir), "all"]
    if rust:
        command.append("RUST=1")
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise ProfileGenerationError(f"make -n failed for {rtos}: {result.stderr[-400:]}")
    entries = []
    for line in result.stdout.splitlines():
        if "arm-none-eabi-gcc" not in line or " -c " not in line or " -o " not in line:
            continue
        tokens = shlex.split(line)
        try:
            source_index = tokens.index("-c") + 1
            output_index = tokens.index("-o") + 1
        except ValueError:
            continue
        source = Path(tokens[source_index])
        if not source.is_absolute():
            source = (make_dir / source).resolve()
        tokens[source_index] = str(source)
        for index, token in enumerate(tokens):
            if token.startswith("-I") and len(token) > 2 and not Path(token[2:]).is_absolute():
                tokens[index] = "-I" + str((make_dir / token[2:]).resolve())
            elif token == "-isystem" and index + 1 < len(tokens) and not Path(tokens[index + 1]).is_absolute():
                tokens[index + 1] = str((make_dir / tokens[index + 1]).resolve())
        entries.append({"directory": str(make_dir), "file": str(source), "output": str((make_dir / tokens[output_index]).resolve()), "command": shlex.join(tokens)})
    if not entries:
        raise ProfileGenerationError(f"no compiler commands recovered for {rtos}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{layout['id']}-{'rust' if rust else 'c'}-compile_commands.json"
    path.write_text(json.dumps(entries, indent=1, sort_keys=True) + "\n")
    return path


@dataclass
class GenerationContext:
    repo: Path
    rtos: str
    layout: dict[str, str]
    case_info: dict[int, dict[str, Any]]
    c_methods: dict[str, list[str]]
    rust_methods: dict[str, list[str]]
    port_map: dict[str, str]
    rust_impl_methods: set[str]
    unresolved: list[dict[str, Any]] = field(default_factory=list)


def _build_source_specs(repo: Path, ctx: GenerationContext, out_dir: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    layout = ctx.layout
    rust_build = bool(re.search(r"-DBENCH_LANGUAGE_RUST", _read(repo / layout["c_impl"]))) if False else False
    if layout["compile_commands"]:
        support_db = repo / layout["compile_commands"]
        benchmark_db = support_db
        text = _read(support_db)
        db_has_rust = "-DBENCH_LANGUAGE_RUST" in text
    else:
        support_db = _make_compile_database(repo, ctx.rtos, layout["board"], True, out_dir / "compile_commands")
        benchmark_db = _make_compile_database(repo, ctx.rtos, layout["board"], False, out_dir / "compile_commands")
        db_has_rust = True
    roots = [layout["c_dir"], "firmware/common/c/src"]
    support_units = _source_units_from_db(repo, support_db, [layout["c_dir"]])
    benchmark_units = _source_units_from_db(repo, benchmark_db, [layout["c_dir"]])
    runner = "firmware/common/c/src/runner.c"
    benchmark_units = [unit for unit in benchmark_units if unit != runner]
    support_units = [unit for unit in support_units if unit != runner]
    short = _git_short(repo)
    support = {
        "configuration": f"{layout['id']}-c-support-rust",
        "compile_commands": _rel(repo, support_db),
        "build_id": f"{layout['id']}-rust-{short}",
        "replace_build_id": None,
        "source_units": support_units,
        "extra_units": [],
        "remove_defines": [],
        "add_defines": [] if db_has_rust else ["BENCH_LANGUAGE_RUST"],
        "source_scope": f"{layout['display']} STM32F4 C support compiled into the Rust benchmark",
    }
    benchmark = {
        "configuration": f"{layout['id']}-c-benchmark",
        "compile_commands": _rel(repo, benchmark_db),
        "build_id": f"{layout['id']}-c-{short}",
        "replace_build_id": f"{layout['id']}-c-{short}",
        "source_units": benchmark_units,
        "extra_units": [runner],
        "remove_defines": ["BENCH_LANGUAGE_RUST"],
        "add_defines": [],
        "source_scope": f"{layout['display']} STM32F4 standalone C benchmark",
    }
    spec = {
        "c_benchmark": benchmark,
        "c_support": support,
        "rust": {
            "configuration": f"{layout['id']}-rust",
            "target": layout["rust_target"],
            "crate_roots": ["firmware/common/rust/Cargo.toml", f"firmware/c-kernels/{ctx.rtos}/rust/Cargo.toml"],
            "source_scope": f"{layout['display']} STM32F4 shared Rust core plus adapter",
        },
    }
    return spec, None


def _api_filter_name(name: str, target_file: str | None, c_index: dict[str, Any]) -> bool:
    if not name or name.startswith("__"):
        return False
    if "uptime_ticks" in name or name == "pthread_attr_setstacksize":
        return False
    if name in ALLOCATION_EXCEPTIONS:
        return True
    lowered_path = str(target_file or "").lower()
    if any(part in lowered_path for part in ("stm32f4xx_hal", "stm32f4xx_ll", "core_cm", "cmsis")):
        return False
    base = Path(target_file or "").name
    if base in STANDARD_LIBRARY_HEADERS:
        return False
    candidates = c_index.get("symbols_by_name", {}).get(name, [])
    if candidates and all(candidate.get("is_project") for candidate in candidates):
        return False
    return True


def _derive_api(repo: Path, ctx: GenerationContext, c_index: dict[str, Any], rust_index: dict[str, Any],
                ffi: dict[str, dict[str, Any]], case_bridges: dict[int, list[str]],
                case_interrupts: dict[int, list[str]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    roots: set[str] = set(ctx.port_map.values())
    for case in ctx.case_info.values():
        enum = case["c_enum"]
        roots.update(ctx.port_map.get(method, "") for method in ctx.c_methods.get(enum, []))
    roots.update(case_bridge for names in case_bridges.values() for case_bridge in names)
    roots.update(case_irq for names in case_interrupts.values() for case_irq in names)
    roots.discard("")
    edges, _ = reachable_edges(c_index, sorted(roots))
    api: set[str] = set()
    for edge in edges:
        name = edge.get("target_name") or ""
        source_name = edge.get("source_name") or ""
        if name == "arm_irq_priority_set" and "IRQ_CONNECT" in source_name:
            continue
        if "uptime_ticks" in name or "attr_setstacksize" in name:
            continue
        if re.match(r"^(enter|leave)_critical_section$|^irq_(enable|disable|lock|unlock|detach)$", source_name):
            api.add(source_name)
        if _api_filter_name(name, edge.get("target_file"), c_index):
            api.add(name)
    for case in ctx.case_info.values():
        methods = ctx.rust_methods.get(enum, [])
        r_edges, _ = reachable_rust_edges(rust_index, parse_rust_impl_type(repo, ctx.layout["rust_impl"]), methods)
        f_edges, _ = reachable_rust_functions(rust_index, methods)
        for edge in list(r_edges) + list(f_edges):
            if edge.get("target_kind") != "rust_ffi":
                continue
            name = edge["target_name"]
            row = ffi.get(name, {})
            if row.get("c_target_kind", "").startswith("c_bridge"):
                continue
            if name not in ALLOCATION_EXCEPTIONS and name.startswith("bench_"):
                continue
            api.add(name)
    for name in rust_index.get("foreign", {}):
        if name.startswith("wd_"):
            api.add(name)
    if ctx.rtos in ("freertos", "zephyr"):
        for name in rust_index.get("foreign", {}):
            row = ffi.get(name, {})
            if row.get("c_target_kind", "").startswith("c_bridge"):
                continue
            if name.startswith("bench_") or "uptime_ticks" in name:
                continue
            if name.startswith("wd_") or _api_filter_name(name, (c_index.get("symbols_by_name", {}).get(name) or [{}])[0].get("file"), c_index):
                api.add(name)
    # C macro-backed APIs are recorded through their source spelling.
    for edge in edges:
        source_name = edge.get("source_name") or ""
        candidates = c_index.get("symbols_by_name", {}).get(source_name, [])
        if source_name and candidates and not all(candidate.get("is_project") for candidate in candidates) and _api_filter_name(source_name, candidates[0].get("file"), c_index):
            api.add(source_name)
    allocation = {name for name in api if name in ALLOCATION_EXCEPTIONS}
    for name in list(api):
        if "malloc" in name.lower() or name.endswith("_alloc"):
            allocation.add(name)
            free_name = name.replace("malloc", "free")
            if free_name in api:
                allocation.add(free_name)
    allocator_args: dict[str, list[int]] = {}
    for name in sorted(allocation):
        candidates = c_index.get("symbols_by_name", {}).get(name, [])
        for candidate in candidates:
            params = candidate.get("params", [])
            indexes = [index for index, param in enumerate(params) if "size" in str(param.get("name", "")).lower() or "nbytes" in str(param.get("name", "")).lower()]
            if indexes:
                allocator_args[name] = indexes
        if name not in allocator_args and ("malloc" in name.lower() or name.endswith("_alloc")):
            allocator_args[name] = [0]
    return {
        "rtos_symbols": sorted(api),
        "allocation_symbols": sorted(allocation),
        "aliases": [],
        "callback_apis": {},
        "language_builtin_methods": sorted({
            call.get("method") for fn in rust_index.get("functions", []) for call in fn.get("method_calls", [])
            if call.get("method") and call.get("method") not in ctx.rust_impl_methods
            and call.get("method") not in {name.split("::")[-1] for name in rust_index.get("by_simple", {})}
            and call.get("method") not in {
                "is_null", "len", "as_ptr", "as_mut_ptr", "as_ref", "as_mut", "wrapping_add",
                "saturating_sub", "abs_diff", "checked_sub", "ok_or", "unwrap", "unwrap_or",
                "div_ceil", "add", "offset", "sub", "wrapping_sub", "iter", "position", "write",
            }
        }),
        "language_builtin_calls": sorted({
            call.get("callee") for fn in rust_index.get("functions", []) for call in fn.get("calls", [])
            if call.get("callee") and call.get("callee") not in rust_index.get("foreign", {})
            and call.get("callee") not in {name.split("::")[-1] for name in rust_index.get("by_simple", {})}
        }),
        "allocator_size_arguments": allocator_args,
    }, []


def parse_rust_impl_type(repo: Path, path: str) -> str:
    text = _read(repo / path)
    match = re.search(r"impl\s+Port\s+for\s+(\w+)", text)
    if not match:
        raise ProfileGenerationError(f"impl Port type not found in {path}")
    return match.group(1)


def _defined_functions(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r"^(?:static\s+)?(?:[A-Za-z_][A-Za-z0-9_\s*]*?)\s+(\w+)\s*\([^;{]*\)\s*\{", text, re.M):
        body = _function_body(text, match.group(1))
        if body:
            result[match.group(1)] = body
    return result


def _reachable_text_functions(roots: list[str], functions: dict[str, str]) -> list[str]:
    seen: set[str] = set()
    queue = list(roots)
    order: list[str] = []
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        body = functions.get(name, "")
        order.append(name)
        for callee in re.findall(r"\b([A-Za-z_]\w*)\s*\(", body):
            if callee in functions and callee not in seen:
                queue.append(callee)
    return order


def _c6_registered_interrupts(repo: Path, layout: dict[str, str], roots: list[str]) -> list[str]:
    text = "\n".join(_read(repo / path) for path in layout["c_root_files"])
    functions = _defined_functions(text)
    reachable = _reachable_text_functions(roots, functions)
    handlers: list[str] = []
    def split_args(value: str) -> list[str]:
        depth = 0
        current = ""
        args: list[str] = []
        for char in value:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                args.append(current.strip()); current = ""
            else:
                current += char
        if current.strip():
            args.append(current.strip())
        return args
    for name in reachable:
        body = functions.get(name, "")
        for match in re.finditer(r"\b(esp_setup_irq|esp_intr_alloc)\s*\(", body):
            start = match.end() - 1
            depth = 0
            end = start
            for index in range(start, len(body)):
                if body[index] == "(":
                    depth += 1
                elif body[index] == ")":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break
            args = split_args(body[start + 1:end])
            for arg in args:
                candidate = re.sub(r"[^A-Za-z0-9_].*$", "", arg.strip())
                if candidate in functions and candidate != name and candidate not in handlers:
                    handlers.append(candidate)
                    break
        for match in re.finditer(r"\birq_attach\s*\([^,]+,\s*(\w+)", body):
            candidate = match.group(1)
            if candidate in functions and candidate not in handlers:
                handlers.append(candidate)
    return handlers


def _freertos_interrupts(repo: Path, layout: dict[str, str], roots: list[str], tick_case: bool) -> list[str]:
    text = "\n".join(_read(repo / path) for path in layout["c_root_files"])
    functions = _defined_functions(text)
    reachable = _reachable_text_functions(roots, functions)
    if layout["board"] == "esp32c6":
        handlers = []
        for name in reachable:
            body = functions.get(name, "")
            for candidate in re.findall(r"\b([A-Za-z_]\w*)\b", body):
                lowered = candidate.lower()
                if (candidate in functions and candidate != name and not candidate.startswith("port_")
                        and ("isr" in lowered or "on_alarm" in lowered or "handler" in lowered)
                        and candidate not in handlers):
                    handlers.append(candidate)
        return handlers
    if layout["board"] == "nrf52840dk":
        startup = "\n".join(_read(repo / path) for path in layout["c_root_files"] if path.endswith("startup_nrf52840.c"))
        vector_map = {
            int(index): name for index, name in re.findall(r"\[IRQ\((\d+)\)\]\s*=\s*\(uint32_t\)([A-Za-z0-9_]+)", startup)
        }
        irq_by_name: dict[str, int] = {}
        for match in re.finditer(r"#define\s+(NRF_[A-Z0-9_]+_IRQ)\s+(\d+)", text):
            irq_by_name[match.group(1)] = int(match.group(2))
        handlers = []
        for name in reachable:
            body = functions.get(name, "")
            for token in re.findall(r"NVIC_EnableIRQ\s*\(\s*\(?\s*IRQn_Type\s*\)?\s*(\w+)\s*\)", body):
                irq = irq_by_name.get(token)
                handler = vector_map.get(irq) if irq is not None else None
                if handler and handler != "Default_Handler" and handler not in handlers:
                    handlers.append(handler)
        return handlers
    found: list[str] = []
    for root in roots:
        body = functions.get(root, "")
        for irq in re.findall(r"\b([A-Z][A-Z0-9_]+_IRQn)\b", body):
            handler = irq.removesuffix("_IRQn") + "_IRQHandler"
            if handler not in found and re.search(rf"\b{re.escape(handler)}\s*\(", text):
                found.append(handler)
        if tick_case and "SysTick" in body and "SysTick_Handler" not in found:
            found.append("SysTick_Handler")
    return found


def _freertos_trace_evidence(repo: Path, layout: dict[str, str], roots: list[str]) -> dict[str, Any]:
    if not any(name.startswith("port_tick_") or name.startswith("port_cpu_") for name in roots):
        return {}
    config = repo / layout["c_dir"] / "include/FreeRTOSConfig.h"
    if not config.is_file():
        return {}
    config_text = _read(config)
    if "traceISR_ENTER" not in config_text or "traceISR_EXIT" not in config_text:
        return {}
    portable = "ARM_CM7/r0p1" if layout["board"] == "stm32h723" else "ARM_CM4F"
    port = repo / ".local/source/FreeRTOS-Kernel/portable/GCC" / portable / "port.c"
    if not port.is_file():
        return {}
    port_text = _read(port)
    if "traceISR_ENTER();" not in port_text or "traceISR_EXIT();" not in port_text:
        return {}
    cpu = any(name.startswith("port_cpu_") for name in roots)
    if cpu and "g_cpu_isr_accum" not in config_text:
        return {}
    def line_of(text: str, needle: str) -> int:
        return text[:text.index(needle)].count("\n") + 1
    evidence = {
        "kind": "tick_isr_trace_hook" if not cpu else "cpu_share_tick_isr_trace_hook",
        "project_hook": {
            "path": _rel(repo, config),
            "trace_enter_line": line_of(config_text, "#define traceISR_ENTER"),
            "trace_exit_line": line_of(config_text, "#define traceISR_EXIT"),
            "cpu_accumulator": cpu,
        },
        "kernel_entry": {
            "path": _rel(repo, port),
            "handler": "xPortSysTickHandler",
            "trace_enter_line": line_of(port_text, "traceISR_ENTER();"),
            "trace_exit_line": line_of(port_text, "traceISR_EXIT();"),
        },
    }
    evidence["analysis_boundary"] = (
        "The benchmark root calls the project tick/CPU adapter; the timed body runs in "
        "FreeRTOS xPortSysTickHandler and invokes the project traceISR_* macros defined in "
        "FreeRTOSConfig.h. The kernel body beyond that documented hook boundary is outside "
        "the project source scope."
    )
    return evidence


def generate_profile(repo: Path, rtos: str, out_dir: Path, existing: dict[str, Any] | None = None,
                     board: str = "stm32f4discovery") -> dict[str, Any]:
    layout = layout_for(rtos, board)
    thesis = parse_thesis_cases(repo)
    c_enum = parse_c_enum(repo)
    rust_enum = parse_rust_enum(repo)
    c_wire = parse_c_wire_map(repo, layout)
    rust_wire = parse_rust_wire_map(repo, layout)
    case_info, unresolved_cases = map_names(thesis, c_enum, rust_enum, c_wire, rust_wire)
    c_methods, c_optional = parse_c_case_methods(repo)
    port_map = parse_port_map(repo, layout["c_impl"])
    impl_type = parse_rust_impl_type(repo, layout["rust_impl"])
    impl_methods = parse_rust_impl_methods(repo, layout["rust_impl"], impl_type)
    rust_methods, rust_optional = parse_rust_case_methods(repo, c_methods, impl_methods)
    ctx = GenerationContext(repo, rtos, layout, case_info, c_methods, rust_methods, port_map, impl_methods, unresolved_cases)
    source_specs, _ = _build_source_specs(repo, ctx, out_dir)
    profile_stub = {
        "schema": SCHEMA,
        "id": f"{layout['id']}-all",
        "rtos": layout["display"],
        "board": layout["board_display"],
        "thesis_spec": "thesis/sections/methodology.typ",
        "thesis_case_spec": "thesis/sections/benchmark-specification.typ",
        "c_enum_header": "firmware/common/c/include/rtos_bench.h",
        "c_wire_registry": layout["c_wire"],
        "c_wire_function": layout["c_wire_function"],
        "c_runner": "firmware/common/c/src/runner.c",
        "c_impl": layout["c_impl"],
        "c_port_initializer": "bench_port port = ",
        "c_root_search_files": layout["c_root_files"],
        "c_bridge_search_files": layout["c_root_files"],
        "interrupt_search_files": layout["c_root_files"],
        "rust_core": "firmware/common/rust/src/lib.rs",
        "rust_impl": layout["rust_impl"],
        "rust_impl_type": impl_type,
        "common_c_application_methods": ["now_ticks"],
        "common_rust_application_methods": ["now_ticks"],
        "compiler": _tool_version(repo),
        "external_source": layout["external_source"],
        "source_roots": [
            "firmware/common/c/src", layout["c_dir"],
            "firmware/common/rust/src", f"firmware/c-kernels/{rtos}/rust/src",
        ],
        "source_configs": source_specs,
        "api": {},
        "absent_cases": [],
    }
    configurations = discover_profile_configs(repo, profile_stub)["configurations"]
    c_support = configurations[source_specs["c_support"]["configuration"]]
    c_benchmark = configurations[source_specs["c_benchmark"]["configuration"]]
    rust_config = configurations[source_specs["rust"]["configuration"]]
    c_index = extract_c_index(repo, c_support)
    rust_index = build_rust_index(repo, rust_config,
                                  SAFETY_DIR / "rust_extractor/target/release/uastw-rust-extract",
                                  LayerClassifier(SAFETY_DIR / "specs/layers.json"))
    ffi = {row["rust_name"]: row for row in ffi_rows(repo, rust_index, c_index)}
    # C callback registrations and bridge membership are derived from the
    # shared runner and the Rust direct-call graph.
    cases: list[dict[str, Any]] = []
    absent: list[dict[str, Any]] = []
    case_bridges: dict[int, list[str]] = {}
    case_interrupts: dict[int, list[str]] = {}
    for number in sorted(case_info):
        case = case_info[number]
        enum = case["c_enum"]
        methods = [method for method in c_methods.get(enum, []) if method in port_map and method != "now_ticks"]
        roots = [port_map[method] for method in methods if method in port_map]
        r_methods = [method for method in rust_methods.get(enum, []) if method in impl_methods and method != "now_ticks"]
        # Case availability is based on advertised wire IDs and actual roots.
        if rtos == "freertos" and not parse_c_wire_default(repo, layout) and c_wire.get(enum) is None and case["c_enum_value"] != 0:
            absent.append({
                "thesis_case": number,
                "thesis_name": case["thesis_name"],
                "c_enum": enum,
                "c_wire_id": case["rust_wire_id"],
                "status": "absent",
                "reason": f"{layout['c_wire']} does not assign a protocol ID to {enum} on this board.",
            })
            continue
        if case["c_wire_id"] is None or (not roots and number != 1):
            reason = "wire ID or implementation roots are absent"
            absent.append({
                "thesis_case": number,
                "thesis_name": case["thesis_name"],
                "c_enum": enum,
                "c_wire_id": case["rust_wire_id"],
                "status": "absent",
                "reason": reason,
            })
            continue
        bridges: list[str] = []
        r_edges, _ = reachable_rust_edges(rust_index, impl_type, r_methods)
        f_edges, _ = reachable_rust_functions(rust_index, r_methods)
        for edge in list(r_edges) + list(f_edges):
            if edge.get("target_kind") != "rust_ffi":
                continue
            row = ffi.get(edge["target_name"], {})
            if edge["target_name"].endswith("now_ticks") or edge["target_name"].endswith("ticks_per_second"):
                continue
            if row.get("c_target_kind", "").startswith("c_bridge") and edge["target_name"] not in bridges:
                bridges.append(edge["target_name"])
        interrupts: list[str] = []
        c_edges, _ = reachable_edges(c_index, roots)
        for edge in c_edges:
            if edge.get("target_name") == "irq_attach":
                args = edge.get("source_args", [])
                if len(args) > 1 and re.fullmatch(r"[A-Za-z_]\w*", args[1]):
                    interrupts.append(args[1])
            elif edge.get("target_name") == "arm_irq_priority_set":
                args = edge.get("source_args", [])
                if len(args) > 2 and re.fullmatch(r"[A-Za-z_]\w*", args[2]):
                    interrupts.append(args[2])
        if rtos == "freertos":
            interrupts = _freertos_interrupts(repo, layout, roots, enum == "BENCH_TICK_OVERHEAD")
        elif layout["board"] == "esp32c6":
            interrupts = _c6_registered_interrupts(repo, layout, roots)
        else:
            interrupts = list(dict.fromkeys(interrupts))
        case_bridges[number] = bridges
        case_interrupts[number] = interrupts
        record = {
            "thesis_case": number,
            "thesis_name": case["thesis_name"],
            "mechanism": case["mechanism"],
            "c_enum": enum,
            "c_wire_id": case["c_wire_id"],
            "rust_variant": case["rust_variant"],
            "rust_wire_id": case["rust_wire_id"],
            "c_root_functions": roots,
            "c_application_methods": methods,
            "rust_application_methods": r_methods,
            "c_bridge_roots": bridges,
            "interrupt_roots": interrupts,
            "external_entry_limitations": [],
            "c_enum_value": case["c_enum_value"],
            "rust_discriminant": case["rust_discriminant"],
        }
        if number == 9 and not re.search(r"PRIO_INHERIT|priority_inherit", _read(repo / layout["c_impl"]) + _read(repo / layout["rust_impl"])):
            record["semantic_limitations"] = ["priority_inheritance_protocol_not_explicitly_established"]
        if rtos == "freertos":
            evidence = _freertos_trace_evidence(repo, layout, roots)
            if evidence:
                record["external_entry_evidence"] = evidence
                record["analysis_boundary"] = evidence["analysis_boundary"]
        cases.append(record)
    api, unresolved_api = _derive_api(repo, ctx, c_index, rust_index, ffi, case_bridges, case_interrupts)
    # Aliases are derived from inline wrappers in generated/public headers.
    api["aliases"] = _derive_aliases(repo, rtos, api["rtos_symbols"], set(rust_index.get("foreign", {})))
    api["callback_apis"] = _derive_callbacks(c_index, api["rtos_symbols"])
    unresolved = unresolved_cases + unresolved_api
    profile = dict(profile_stub)
    profile.update({
        "schema": SCHEMA,
        "description": f"All {len(cases)} implemented {layout['display']} STM32F4 C/Rust thesis cases",
        "api": api,
        "cases": cases,
        "absent_cases": absent,
        "generator": {
            "schema": GENERATOR_SCHEMA,
            "source_revision": _git_short(repo),
            "unresolved": unresolved,
        },
    })
    if rtos == "nuttx":
        profile["config_provenance"] = _config_provenance(repo)
    profile["generator"]["source_guards"] = _source_guards(repo, profile)
    return profile


def _source_guards(repo: Path, profile: dict[str, Any]) -> dict[str, str]:
    paths = {
        profile["c_enum_header"], profile["c_wire_registry"], profile["c_runner"],
        profile["c_impl"], profile["rust_core"], profile["rust_impl"],
        *profile.get("c_root_search_files", []),
        *profile.get("c_bridge_search_files", []),
        *profile.get("interrupt_search_files", []),
    }
    for spec in profile["source_configs"].values():
        paths.update(spec.get("source_units", []))
        paths.update(spec.get("extra_units", []))
    rust_roots = [profile["rust_core"], profile["rust_impl"]]
    for root in rust_roots:
        source_dir = (repo / root).parent
        paths.update(_rel(repo, path) for path in source_dir.glob("*.rs"))
    c_dir = (repo / profile["c_impl"]).parent
    build_names = ("Makefile", "CMakeLists.txt", "prj.conf", "Kconfig", "sdkconfig.defaults",
                   "bench_defconfig", "bench_h723_defconfig", "bench_nrf52840dk_defconfig", "bench_c6_defconfig")
    for base in (c_dir, c_dir.parent, c_dir.parent.parent):
        for name in build_names:
            candidate = base / name
            if candidate.is_file():
                paths.add(_rel(repo, candidate))
    for candidate in ("firmware/Cargo.toml", f"firmware/c-kernels/{profile['rtos']}/rust/Cargo.toml"):
        if (repo / candidate).is_file():
            paths.add(candidate)
    return {path: _sha(repo / path) for path in sorted(paths) if (repo / path).is_file()}


def validate_profile_sources(repo: Path, profile: dict[str, Any]) -> None:
    guards = profile.get("generator", {}).get("source_guards", {})
    if not guards:
        raise ProfileGenerationError("profile has no source guards")
    changed = [path for path, digest in guards.items()
               if not (repo / path).is_file() or _sha(repo / path) != digest]
    if changed:
        raise ProfileGenerationError("source definitions changed since profile generation: " + ", ".join(changed))


def _derive_aliases(repo: Path, rtos: str, api: set[str], rust_foreign: set[str]) -> list[dict[str, Any]]:
    paths: list[Path] = []
    if rtos == "zephyr":
        paths = [
            repo / ".local/west/build-test/zephyr/include/generated/zephyr/syscalls/kernel.h",
            repo / ".local/west/zephyr/include/zephyr/kernel.h",
        ]
    elif rtos == "nuttx":
        paths = [
            repo / ".local/source/nuttx/include/nuttx/wdog.h",
            repo / ".local/source/nuttx/include/nuttx/sched.h",
            repo / ".local/source/nuttx/include/pthread.h",
        ]
    texts = [(path, _read(path)) for path in paths if path.is_file()]
    combined = "\n".join(text for _, text in texts)

    def resolve(name: str, seen: set[str]) -> str | None:
        if name in rust_foreign:
            return name
        if name in seen:
            return None
        body = _function_body(combined, name)
        if body is None:
            return None
        seen = seen | {name}
        for callee in re.findall(r"\b([A-Za-z_]\w*)\s*\(", body):
            if callee == name:
                continue
            resolved = resolve(callee, seen)
            if resolved:
                return resolved
        return None

    aliases = []
    seen_pairs: set[tuple[str, str]] = set()
    for wrapper in sorted(api):
        body = _function_body(combined, wrapper)
        if body is None:
            continue
        for callee in sorted(set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", body))):
            target = resolve(callee, {wrapper})
            if target and (wrapper, target) not in seen_pairs:
                seen_pairs.add((wrapper, target))
                sources = [ _rel(repo, path) for path, text in texts if re.search(rf"\b{re.escape(wrapper)}\s*\(", text) ]
                aliases.append({
                    "c_symbol": wrapper,
                    "rust_symbol": target,
                    "canonical": wrapper,
                    "justification": f"Source-level wrapper chain starting at {wrapper} reaches {target}.",
                    "source_locations": sources,
                })
    return aliases



def _derive_callbacks(c_index: dict[str, Any], api: set[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(api):
        for candidate in c_index.get("symbols_by_name", {}).get(name, []):
            for index, param in enumerate(candidate.get("params", [])):
                kind = str((param.get("type") or {}).get("kind", ""))
                if kind != "function_pointer":
                    continue
                type_text = (param.get("type_text") or "") + " " + json.dumps(param.get("type") or {})
                lowered = (type_text + " " + name).lower()
                if "thread" in lowered or "task" in lowered:
                    invocation = "thread_entry"
                elif "timer" in lowered or "expiry" in lowered:
                    invocation = "timer_expiry"
                elif "wd" in lowered or "watchdog" in lowered:
                    invocation = "watchdog_callback"
                else:
                    continue
                result[name] = {"invocation_kind": invocation, "callback_parameter": index}
                break
    return result



def _config_provenance(repo: Path) -> dict[str, Any] | None:
    project = repo / "firmware/c-kernels/nuttx/c/bench_defconfig"
    if not project.is_file():
        return None
    digest = _sha(project)
    for candidate in (repo / ".local/source/nuttx/boards").rglob("defconfig"):
        if candidate.is_file() and _sha(candidate) == digest and "stm32f4discovery/configs/bench" in str(candidate):
            return {
                "external_path": _rel(repo, candidate),
                "project_path": _rel(repo, project),
                "relationship": "expected_byte_identical",
            }
    return None


def write_profile(profile: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n")


def generate_native_profile(repo: Path, native: str, out_dir: Path) -> dict[str, Any]:
    if native not in NATIVE_LAYOUTS:
        raise ProfileGenerationError(f"unsupported native configuration: {native}")
    layout = NATIVE_LAYOUTS[native]
    crate = repo / layout["crate"]
    files = sorted(str(path.relative_to(repo)) for path in (crate / "src").glob("**/*.rs"))
    roots = []
    for path in files:
        text = _read(repo / path)
        roots.extend(match.group(1) for match in re.finditer(r"\bfn\s+(main|rust_bench_run_case|rust_bench_case_count)\s*\(", text))
    profile = {
        "schema": "uastw-native-safety-profile-v1",
        "id": layout["id"],
        "rtos": layout["rtos"],
        "board": layout["board"],
        "configuration": native,
        "language": "rust",
        "target": layout["target"],
        "features": layout.get("features", []),
        "crate": layout["crate"],
        "source_files": files,
        "entry_roots": sorted(set(roots)),
        "matched_analysis": layout["matched_analysis"],
        "analysis_1": "operation_inventory",
        "analysis_2": "rust_only_source_trace",
        "note": "This profile does not define a C counterpart or a matched Level A/B/C comparison.",
        "generator": {
            "schema": "uastw-native-profile-generator-v1",
            "source_revision": _git_short(repo),
            "unresolved": [],
            "source_guards": {path: _sha(repo / path) for path in files if (repo / path).is_file()},
        },
    }
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root")
    parser.add_argument("--rtos", action="append", choices=["freertos", "nuttx", "zephyr"], help="RTOS to generate (repeatable; default all)")
    parser.add_argument("--board", action="append", choices=sorted(BOARD_INFO), help="board to generate (repeatable; default STM32F4)")
    parser.add_argument("--native", action="append", choices=sorted(NATIVE_LAYOUTS), help="Rust-native configuration to generate (repeatable)")
    parser.add_argument("--output", default=str(GENERATED_DIR))
    parser.add_argument("--check", action="store_true", help="compare generated output with existing profiles and exit nonzero on differences")
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve() if args.repo_root else resolve_repo_root(PROFILE_DIR)
    rtos_targets = args.rtos or ["freertos", "nuttx", "zephyr"]
    board_targets = args.board or ["stm32f4discovery"]
    out = Path(args.output)
    failures = 0
    for native in args.native or []:
        profile = generate_native_profile(repo, native, out)
        write_profile(profile, out / f"{profile['id']}.json")
    for rtos in rtos_targets:
        for board in board_targets:
            profile = generate_profile(repo, rtos, out, board=board)
            write_profile(profile, out / f"{profile['id']}.json")
            if args.check:
                existing_path = PROFILE_DIR / f"{profile['id']}.json"
                if not existing_path.is_file():
                    print(f"{profile['id']}: no existing profile at {existing_path}", file=sys.stderr)
                    failures += 1
                    continue
                existing = json.loads(existing_path.read_text())
                differences = compare_profiles(existing, profile)
                if differences:
                    failures += 1
                    print(f"{profile['id']}: {len(differences)} difference(s)")
                    for difference in differences:
                        print(f"  - {difference}")
    return 1 if failures else 0


def compare_profiles(existing: dict[str, Any], generated: dict[str, Any]) -> list[str]:
    differences = []
    for key in ("c_enum", "c_root_functions", "c_application_methods", "rust_application_methods",
                "c_bridge_roots", "interrupt_roots"):
        old = {case["thesis_case"]: case.get(key) for case in existing.get("cases", [])}
        new = {case["thesis_case"]: case.get(key) for case in generated.get("cases", [])}
        for number in sorted(set(old) | set(new)):
            if old.get(number) != new.get(number):
                differences.append(f"case {number} {key}: existing={old.get(number)!r} generated={new.get(number)!r}")
    for key in ("rtos_symbols", "allocation_symbols"):
        old = set(existing.get("api", {}).get(key, []))
        new = set(generated.get("api", {}).get(key, []))
        if old != new:
            differences.append(f"api.{key}: missing={sorted(old - new)} extra={sorted(new - old)}")
    if existing.get("absent_cases") != generated.get("absent_cases"):
        differences.append("absent_cases differ")
    return differences


if __name__ == "__main__":
    raise SystemExit(main())
