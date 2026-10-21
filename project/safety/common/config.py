"""Source and configuration discovery for the FreeRTOS/STM32F4 scopes.

The C benchmark, Rust benchmark, and C-support-for-Rust scopes are discovered
separately.  No configuration is inferred from a previous run or from a
hard-coded result.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any


FREE_RTOS_MAKE_DIR = Path("firmware/c-kernels/freertos/c/stm32f4")
RUST_APP_DIR = Path("firmware/c-kernels/freertos/rust")
RUST_SHARED_DIR = Path("firmware/common/rust")
RUST_TARGET = "thumbv7em-none-eabihf"
C_PROJECT_SOURCE_ROOTS = (
    Path("firmware/common/c/src"),
    Path("firmware/c-kernels/freertos/c/stm32f4/src"),
)


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "project" / "firmware").is_dir():
            return candidate / "project"
        if (candidate / "firmware").is_dir():
            return candidate
    raise RuntimeError(f"could not find repository root from {start}")


def resolve_repo_root(start: Path) -> Path:
    """Resolve the repository root, honouring an explicit environment override.

    The override is used by the test runner and by isolated tooling copies.
    Normal discovery remains the fallback and never overrides an explicit root.
    """
    supplied = os.environ.get("UASTW_REPO_ROOT")
    if supplied:
        return find_repo_root(Path(supplied))
    return find_repo_root(start)


def _run(command: list[str], cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout}")
    return completed.stdout


def _toolchain_include(repo_root: Path) -> Path | None:
    base = repo_root / ".local/toolchains/arm-none-eabi"
    if not base.is_dir():
        return None
    matches = sorted(base.glob("*/arm-none-eabi/include"))
    return matches[0] if matches else None


def _resolve_include_flag(flag: str, cwd: Path) -> str:
    if flag.startswith("-I") and len(flag) > 2:
        return "-I" + os.path.realpath(cwd / flag[2:])
    if flag.startswith("-isystem") and len(flag) > 8:
        return "-isystem" + os.path.realpath(cwd / flag[8:])
    return flag


def _relative_path(path: str | None, repo_root: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(repo_root))
    except (ValueError, OSError):
        return path


def discover_c(repo_root: Path, rust_mode: int) -> dict[str, Any]:
    """Discover one C configuration: 0 = benchmark, 1 = support-for-Rust."""
    if rust_mode not in (0, 1):
        raise ValueError("rust_mode must be 0 or 1")
    make_dir = repo_root / FREE_RTOS_MAKE_DIR
    if not make_dir.is_dir():
        raise RuntimeError(f"FreeRTOS build directory not found: {make_dir}")
    make_argv = ["make", "-n", "-B", f"RUST={rust_mode}", "all"]
    output = _run(make_argv, make_dir)
    compile_commands: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        if " -c " not in raw_line:
            continue
        try:
            tokens = shlex.split(raw_line)
        except ValueError:
            continue
        if not tokens or not tokens[0].endswith("gcc"):
            continue
        try:
            c_index = tokens.index("-c")
        except ValueError:
            continue
        if c_index + 1 >= len(tokens):
            continue
        source_token = tokens[c_index + 1]
        source = Path(os.path.realpath(make_dir / source_token))
        if source.suffix.lower() != ".c":
            continue
        rel = source.relative_to(repo_root)
        flags = tokens[1:c_index]
        cleaned: list[str] = []
        index = 0
        while index < len(flags):
            flag = flags[index]
            if flag in ("-I", "-isystem") and index + 1 < len(flags):
                cleaned.extend([flag, os.path.realpath(make_dir / flags[index + 1])])
                index += 2
            elif flag.startswith(("-I", "-isystem")):
                cleaned.append(_resolve_include_flag(flag, make_dir))
                index += 1
            elif flag in ("-c", "-o"):
                index += 2
            elif flag.startswith("-DBENCH_BUILD_ID="):
                value = flag.split("=", 1)[1].strip('"')
                cleaned.append(f'-DBENCH_BUILD_ID="{value}"')
                index += 1
            else:
                cleaned.append(flag)
                index += 1
        include = _toolchain_include(repo_root)
        clang_args = ["-target", "arm-none-eabi", *cleaned, "-fno-color-diagnostics"]
        if include is not None and not any(arg == str(include) or arg.endswith(str(include)) for arg in clang_args):
            clang_args.extend(["-isystem", str(include)])
        project = any(str(rel).startswith(str(root) + "/") for root in C_PROJECT_SOURCE_ROOTS)
        compile_commands.append(
            {
                "path": str(rel),
                "abs_path": str(source),
                "project_written": project,
                "source_root": str(next((root for root in C_PROJECT_SOURCE_ROOTS if str(rel).startswith(str(root) + "/")), "")),
                "clang_args": clang_args,
                "gcc_command": raw_line,
                "preprocessor_definitions": [
                    arg for arg in cleaned if arg.startswith("-D")
                ],
                "has_bench_language_rust": "-DBENCH_LANGUAGE_RUST" in cleaned,
            }
        )
    if not compile_commands:
        raise RuntimeError("no C compile commands were discovered from the FreeRTOS Makefile")
    project_units = [unit for unit in compile_commands if unit["project_written"]]
    excluded = [unit["path"] for unit in compile_commands if not unit["project_written"]]
    project_units.sort(key=lambda unit: unit["path"])
    configuration = (
        "freertos-stm32f4-c-benchmark"
        if rust_mode == 0
        else "freertos-stm32f4-c-support-rust"
    )
    return {
        "schema": "uastw-c-configuration-v1",
        "configuration": configuration,
        "make_directory": str(make_dir.relative_to(repo_root)),
        "make_argv": make_argv,
        "make_command": " ".join(make_argv),
        "rust_mode": rust_mode,
        "toolchain_include": str(include.relative_to(repo_root)) if include and include.is_relative_to(repo_root) else (str(include) if include else None),
        "target": {
            "compiler": "arm-none-eabi-gcc",
            "clang_target": "arm-none-eabi",
            "cpu": "cortex-m4",
            "thumb": True,
            "fpu": "fpv4-sp-d16",
            "float_abi": "hard",
            "pointer_width": 32,
        },
        "translation_units": project_units,
        "excluded_translation_units": excluded,
        "all_compile_commands": compile_commands,
    }


def _rust_target_facts(repo_root: Path, target: str = RUST_TARGET) -> dict[str, Any]:
    rustc = shutil.which("rustc")
    if not rustc:
        return {"available": False, "target": target, "arch": "arm", "os": "none", "family": "none", "pointer_width": "32"}
    try:
        output = _run([rustc, "--print", "cfg", "--target", target], repo_root)
    except RuntimeError as error:
        return {"available": False, "target": target, "error": str(error)}
    values: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"')
    return {
        "available": True,
        "target": target,
        "arch": values.get("target_arch", "arm"),
        "os": values.get("target_os", "none"),
        "family": values.get("target_family", "none"),
        "pointer_width": values.get("target_pointer_width", "32"),
        "endian": values.get("target_endian"),
        "cfg": values,
    }


def _discover_rust_modules(start: Path) -> list[Path]:
    """Follow non-cfg `mod name;` declarations from a crate root."""
    files: list[Path] = []
    pending = [start]
    seen: set[Path] = set()
    while pending:
        path = pending.pop(0)
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        files.append(path)
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"(?m)^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", text):
            name = match.group(1)
            candidates = [path.parent / f"{name}.rs", path.parent / name / "mod.rs"]
            for candidate in candidates:
                if candidate.is_file():
                    pending.append(candidate)
                    break
    return files


def discover_rust(repo_root: Path) -> dict[str, Any]:
    roots = [
        repo_root / RUST_SHARED_DIR / "src/lib.rs",
        repo_root / RUST_APP_DIR / "src/lib.rs",
    ]
    files: list[Path] = []
    for root in roots:
        if not root.is_file():
            raise RuntimeError(f"Rust crate root not found: {root}")
        files.extend(_discover_rust_modules(root))
    normalized = sorted({str(path.resolve().relative_to(repo_root)) for path in files})
    excluded = [
        "firmware/common/rust/tests",
        "firmware/c-kernels/freertos/rust/tests",
        "firmware/c-kernels/*/rust/target",
        "firmware/rust-kernels",
    ]
    return {
        "schema": "uastw-rust-configuration-v1",
        "configuration": "freertos-stm32f4-rust",
        "target": _rust_target_facts(repo_root),
        "crate_roots": [
            "firmware/common/rust/Cargo.toml",
            "firmware/c-kernels/freertos/rust/Cargo.toml",
        ],
        "files": normalized,
        "excluded_scopes": excluded,
    }


def _version_line(name: str, output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if name == "clang_tidy":
        for line in lines:
            if "version" in line.lower():
                return line
    return lines[0] if lines else ""


def tool_versions(repo_root: Path) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for name, command in (
        ("clang", ["clang", "--version"]),
        ("clang_tidy", ["clang-tidy", "--version"]),
        ("rustc", ["rustc", "--version"]),
        ("cargo", ["cargo", "--version"]),
        ("python", ["python3", "--version"]),
    ):
        executable = shutil.which(command[0])
        if not executable:
            versions[name] = {"available": False}
            continue
        try:
            output = _run(command, repo_root)
            versions[name] = {
                "available": True,
                "version": _version_line(name, output),
                "command": " ".join(command),
            }
        except RuntimeError as error:
            versions[name] = {"available": True, "error": str(error)}
    try:
        import clang.cindex as clang

        library = getattr(clang.conf, "lib", None)
        library_name = getattr(library, "_name", None) or getattr(library, "name", None) or "libclang.so"
        binding = getattr(clang, "__file__", None)
        versions["libclang"] = {
            "available": True,
            "python_binding": Path(binding).name if binding else None,
            "library": Path(library_name).name,
        }
    except Exception as error:
        versions["libclang"] = {"available": False, "error": str(error)}
    clippy = shutil.which("clippy-driver")
    codeql = shutil.which("codeql")
    versions["clippy"] = {"available": bool(clippy), "path": Path(clippy).name if clippy else None}
    versions["codeql"] = {"available": bool(codeql), "path": Path(codeql).name if codeql else None}
    return versions


def discover(repo_root: Path) -> dict[str, Any]:
    c_benchmark = discover_c(repo_root, 0)
    c_support = discover_c(repo_root, 1)
    rust = discover_rust(repo_root)
    # Ignore untracked outputs so deterministic analysis is not invalidated by
    # caches or concurrently produced files; campaign provenance records full status.
    status = _run(["git", "status", "--porcelain", "--untracked-files=no"], repo_root).splitlines()
    return {
        "schema": "uastw-safety-config-v2",
        "board": "STM32F407VG (stm32f4discovery)",
        "kernel": "FreeRTOS (third-party kernel excluded from project metrics)",
        "source_revision": _run(["git", "rev-parse", "HEAD"], repo_root).strip(),
        "worktree_dirty": bool(status),
        "worktree_status_count": len(status),
        "configurations": {
            c_benchmark["configuration"]: c_benchmark,
            rust["configuration"]: rust,
            c_support["configuration"]: c_support,
        },
        "tools": tool_versions(repo_root),
        "scope": {
            "included": {
                c_benchmark["configuration"]: [unit["path"] for unit in c_benchmark["translation_units"]],
                "freertos-stm32f4-rust": rust["files"],
                c_support["configuration"]: [unit["path"] for unit in c_support["translation_units"]],
            },
            "excluded": [
                "FreeRTOS-Kernel sources and headers as project-written application metrics",
                "STM32CubeF4 HAL and CMSIS sources",
                "startup assembly",
                "other RTOS kernels and boards",
                "Rust-native RTOS kernels",
            ],
        },
    }


def write_discovered_config(repo_root: Path, path: Path) -> dict[str, Any]:
    config = discover(repo_root)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return config
