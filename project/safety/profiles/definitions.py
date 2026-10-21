"""Source/build profiles for bounded non-FreeRTOS Stage 2 pilots.

The FreeRTOS registry and discovery stay unchanged.  A pilot profile names an
existing board build database, selects project-written C units, and records the
small parser configurations that differ between FreeRTOS, NuttX and Zephyr.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from common.config import _discover_rust_modules, _rust_target_facts, _run, tool_versions

PROFILE_DIR = Path(__file__).resolve().parent / "data"

ZEPHYR_DROP_FLAGS = {
    "-fno-printf-return-value",
    "-fno-reorder-functions",
    "-mfp16-format=ieee",
    "-fno-common",
    "-fdiagnostics-color=always",
    "-gdwarf-4",
}
ZEPHYR_DROP_PREFIXES = ("-W", "-Wa,", "--param=", "-fdiagnostics")
CLANG_DROP_PREFIXES = ("-W", "-Wa,", "--param=", "-fdiagnostics")
GCC_INTERNAL_MARKERS = ("/lib/gcc/arm-none-eabi/", "/lib/gcc/arm-none-eabi")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_profile(repo_root: Path, name_or_path: str) -> dict[str, Any]:
    path = Path(name_or_path)
    if not path.is_absolute():
        candidate = PROFILE_DIR / path.name
        path = candidate if candidate.is_file() else repo_root / path
    document = json.loads(path.read_text())
    document["_profile_path"] = str(path)
    return document


def _source_relative(repo_root: Path, path: str | Path) -> str | None:
    try:
        return str(Path(path).resolve().relative_to(repo_root))
    except (ValueError, OSError):
        return None


def _resolve_flag_path(repo_root: Path, flag: str) -> str:
    """Resolve an include flag to an existing path without changing its meaning."""
    for prefix in ("-I", "-isystem"):
        if flag.startswith(prefix) and len(flag) > len(prefix):
            value = flag[len(prefix):]
            path = Path(value)
            if not path.is_absolute():
                path = repo_root / path
            return prefix + str(path.resolve())
    return flag


def _clang_builtin_include() -> Path | None:
    clang = shutil.which("clang")
    if not clang:
        return None
    try:
        out = subprocess.run([clang, "-print-resource-dir"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if out.returncode == 0:
            candidate = Path(out.stdout.strip()) / "include"
            if candidate.is_dir():
                return candidate
    except OSError:
        pass
    for candidate in sorted(Path("/usr/lib/clang").glob("*/include")):
        if (candidate / "stddef.h").is_file():
            return candidate
    return None


def _command_tokens(entry: dict[str, Any]) -> list[str]:
    raw = [str(item) for item in entry["arguments"]] if entry.get("arguments") else shlex.split(entry.get("command", ""))
    expanded: list[str] = []
    for token in raw:
        if token.startswith("@") and len(token) > 1 and Path(token[1:]).is_file():
            expanded.extend(shlex.split(Path(token[1:]).read_text()))
        else:
            expanded.append(token)
    return expanded


def _normalized_clang_args(repo_root: Path, entry: dict[str, Any], *, zephyr: bool,
                            remove_defines: list[str], add_defines: list[str],
                            replace_build_id: str | None,
                            source_path: str) -> list[str]:
    tokens = _command_tokens(entry)
    output: list[str] = []
    index = 1  # Drop the compiler executable.
    while index < len(tokens):
        token = tokens[index]
        if token == "-c":
            index += 1
            continue
        if token == "-o":
            index += 2
            continue
        if token == source_path or token == entry.get("file"):
            index += 1
            continue
        if token in ("-MD", "-MMD", "-MP"):
            index += 1
            continue
        if token in ("-MF", "-MT", "-MQ"):
            index += 2
            continue
        if token.startswith(GCC_INTERNAL_MARKERS) or (token in ("-isystem", "-I") and index + 1 < len(tokens) and any(marker in tokens[index + 1] for marker in GCC_INTERNAL_MARKERS)):
            index += 2 if token in ("-isystem", "-I") else 1
            continue
        if zephyr and (token in ZEPHYR_DROP_FLAGS or token.startswith(ZEPHYR_DROP_PREFIXES) or token.startswith("--sysroot=")):
            index += 1
            continue
        if token in {"-fno-shrink-wrap", "-fstrict-volatile-bitfields", "-fno-tree-switch-conversion", "-fno-defer-pop"}:
            index += 1
            continue
        if not zephyr and token.startswith(CLANG_DROP_PREFIXES):
            index += 1
            continue
        if token in ("-I", "-isystem") and index + 1 < len(tokens):
            output.extend([token, str((repo_root / tokens[index + 1]).resolve()) if not Path(tokens[index + 1]).is_absolute() else tokens[index + 1]])
            index += 2
            continue
        if token.startswith(("-I", "-isystem")):
            output.append(_resolve_flag_path(repo_root, token))
            index += 1
            continue
        if token.startswith("-DBENCH_BUILD_ID=") and replace_build_id:
            output.append(f'-DBENCH_BUILD_ID="{replace_build_id}"')
            index += 1
            continue
        if (token.startswith("-D") and token[2:].split("=", 1)[0] in remove_defines) or token in remove_defines:
            index += 1
            continue
        output.append(token)
        index += 1
    for define in add_defines:
        flag = define if define.startswith("-D") else f"-D{define}"
        if flag not in output:
            output.append(flag)
    if not zephyr and "riscv32-esp-elf" in Path(tokens[0]).name:
        toolchain_include = Path(tokens[0]).resolve().parent.parent / "riscv32-esp-elf/include"
        if toolchain_include.is_dir():
            output.extend(["-isystem", str(toolchain_include)])
    if not any(arg == "-target" or arg.startswith("-target=") for arg in output):
        target = "arm-none-eabi"
        if any("riscv" in token.lower() for token in tokens[:3]) or any(token.startswith("-march=rv32") for token in tokens):
            target = "riscv32-unknown-elf"
        output[:0] = ["-target", target]
    builtin = _clang_builtin_include()
    if builtin and not any(str(builtin) in arg for arg in output):
        output.extend(["-isystem", str(builtin)])
    output.append("-fno-color-diagnostics")
    return output


def _entry_for(repo_root: Path, database: Path, source: str) -> dict[str, Any]:
    data = json.loads(database.read_text())
    for entry in data:
        file_name = entry.get("file", "")
        if file_name == source or file_name.endswith("/" + source):
            return entry
    raise RuntimeError(f"source unit not found in {database}: {source}")


def _unit(repo_root: Path, entry: dict[str, Any], relative_path: str, args: list[str],
          source_roots: list[str]) -> dict[str, Any]:
    source = Path(relative_path)
    project = any(relative_path == root or relative_path.startswith(root.rstrip("/") + "/") for root in source_roots)
    return {
        "path": relative_path,
        "abs_path": str((repo_root / relative_path).resolve()),
        "project_written": project,
        "source_root": next((root for root in source_roots if relative_path == root or relative_path.startswith(root.rstrip("/") + "/")), ""),
        "clang_args": args,
        "gcc_command": entry.get("command", ""),
        "preprocessor_definitions": [arg for arg in args if arg.startswith("-D")],
        "has_bench_language_rust": any(arg == "-DBENCH_LANGUAGE_RUST" for arg in args),
    }


def _external_revision(repo_root: Path, path: str) -> dict[str, Any]:
    checkout = repo_root / path
    if not (checkout / ".git").exists():
        return {"path": path, "available": False}
    revision = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], repo_root).strip()
    status = _run(["git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=no"], repo_root).splitlines()
    return {
        "path": path,
        "available": True,
        "revision": revision,
        "worktree_dirty": bool(status),
        "worktree_status_count": len(status),
        "worktree_status": status[:80],
    }


def discover_profile_configs(repo_root: Path, profile: dict[str, Any]) -> dict[str, Any]:
    zephyr = profile["rtos"].lower() == "zephyr"
    source_roots = list(profile["source_roots"])
    configurations: dict[str, Any] = {}
    for key in ("c_benchmark", "c_support"):
        spec = profile["source_configs"][key]
        database = repo_root / spec["compile_commands"]
        if not database.is_file():
            raise RuntimeError(f"compile database not found for {key}: {database}")
        units = []
        for relative_path in spec["source_units"]:
            entry = _entry_for(repo_root, database, relative_path)
            args = _normalized_clang_args(
                repo_root, entry, zephyr=zephyr,
                remove_defines=list(spec.get("remove_defines", [])),
                add_defines=list(spec.get("add_defines", [])) or (["BENCH_LANGUAGE_RUST"] if key == "c_support" else []),
                replace_build_id=spec.get("replace_build_id"),
                source_path=relative_path,
            )
            units.append(_unit(repo_root, entry, relative_path, args, source_roots))
        for relative_path in spec.get("extra_units", []):
            template = units[0]["clang_args"] if units else []
            units.append({
                "path": relative_path,
                "abs_path": str((repo_root / relative_path).resolve()),
                "project_written": True,
                "source_root": next((root for root in source_roots if relative_path == root or relative_path.startswith(root.rstrip("/") + "/")), ""),
                "clang_args": list(template),
                "gcc_command": "",
                "preprocessor_definitions": [arg for arg in template if arg.startswith("-D")],
                "has_bench_language_rust": any(arg == "-DBENCH_LANGUAGE_RUST" for arg in template),
                "derived_compile_unit": "reuses the selected board's project C flags; extra source is compiled by the standalone C build",
            })
        configuration = {
            "schema": "uastw-c-configuration-v1",
            "configuration": spec["configuration"],
            "board": profile["board"],
            "kernel": profile["rtos"],
            "rust_mode": 0 if key == "c_benchmark" else 1,
            "compile_commands": _source_relative(repo_root, database),
            "compile_commands_sha256": _sha256(database),
            "build_id": spec.get("build_id"),
            "rebuild_evidence": spec.get("rebuild_evidence"),
            "source_scope": spec.get("source_scope"),
            "target": {
                "compiler": profile["compiler"],
                "clang_target": "arm-none-eabi",
                "cpu": "cortex-m4",
                "thumb": True,
                "fpu": "fpv4-sp-d16",
                "float_abi": "soft",
                "pointer_width": 32,
            },
            "translation_units": units,
            "excluded_translation_units": [],
            "all_compile_commands": [],
            "rtos_symbols": list(profile.get("api", {}).get("rtos_symbols", [])),
            "allocation_symbols": list(profile.get("api", {}).get("allocation_symbols", [])),
            "callback_apis": profile.get("api", {}).get("callback_apis", {}),
            "profile": profile["id"],
            "project_source_roots": list(profile["source_roots"]),
            "project_header_roots": list(profile["source_roots"]),
            "follow_project_declarations": True,
            "source_hashes": {unit["path"]: _sha256(repo_root / unit["path"]) for unit in units},
        }
        configurations[spec["configuration"]] = configuration
    rust_spec = profile["source_configs"]["rust"]
    roots = [repo_root / path for path in rust_spec["crate_roots"]]
    files: list[Path] = []
    for root in roots:
        if not root.is_file():
            raise RuntimeError(f"Rust crate root not found: {root}")
        start = root.parent / "src/lib.rs" if root.name == "Cargo.toml" else root
        if not start.is_file():
            raise RuntimeError(f"Rust module root not found: {start}")
        files.extend(_discover_rust_modules(start))
    rust_files = sorted({str(path.resolve().relative_to(repo_root)) for path in files})
    rust_config = {
        "schema": "uastw-rust-configuration-v1",
        "configuration": rust_spec["configuration"],
        "board": profile["board"],
        "kernel": profile["rtos"],
        "target": _rust_target_facts(repo_root, rust_spec["target"]),
        "crate_roots": rust_spec["crate_roots"],
        "files": rust_files,
        "excluded_scopes": rust_spec.get("excluded_scopes", []),
        "source_scope": rust_spec.get("source_scope"),
        "rtos_symbols": list(profile.get("api", {}).get("rtos_symbols", [])),
        "allocation_symbols": list(profile.get("api", {}).get("allocation_symbols", [])),
        "callback_apis": profile.get("api", {}).get("callback_apis", {}),
        "language_builtin_methods": list(profile.get("api", {}).get("language_builtin_methods", [])),
        "language_builtin_calls": list(profile.get("api", {}).get("language_builtin_calls", [])),
        "profile": profile["id"],
        "source_hashes": {relative: _sha256(repo_root / relative) for relative in rust_files},
    }
    configurations[rust_config["configuration"]] = rust_config
    if profile["rtos"].lower() == "zephyr":
        generated_candidates = [
            repo_root / ".local/west/build-test/zephyr/include/generated/zephyr/autoconf.h",
            repo_root / ".local/west/build-test/zephyr/include/generated/zephyr/syscalls/kernel.h",
        ]
    else:
        generated_candidates = [repo_root / ".local/source/nuttx/build-bench/include/nuttx/config.h"]
    generated_headers = [
        {"path": str(path.relative_to(repo_root)), "sha256": _sha256(path)}
        for path in generated_candidates if path.is_file()
    ]
    gcc = shutil.which("arm-none-eabi-gcc")
    if not gcc:
        candidates = sorted((repo_root / ".local/toolchains/arm-none-eabi").glob("*/bin/arm-none-eabi-gcc"))
        gcc = str(candidates[0]) if candidates else None
    gcc_version = None
    if gcc:
        try:
            gcc_version = _run([gcc, "--version"], repo_root).splitlines()[0]
        except Exception:
            gcc_version = None
    config_provenance = None
    config_spec = profile.get("config_provenance")
    if config_spec:
        external = repo_root / config_spec["external_path"]
        project = repo_root / config_spec["project_path"]
        external_hash = _sha256(external) if external.is_file() else None
        project_hash = _sha256(project) if project.is_file() else None
        config_provenance = {
            "external_path": config_spec["external_path"],
            "external_sha256": external_hash,
            "project_path": config_spec["project_path"],
            "project_sha256": project_hash,
            "byte_identical": bool(external_hash and project_hash and external_hash == project_hash),
            "relationship": config_spec.get("relationship"),
        }
    status = _run(["git", "status", "--porcelain"], repo_root).splitlines()
    return {
        "schema": "uastw-safety-config-v2",
        "profile": profile["id"],
        "rtos": profile["rtos"],
        "board": profile["board"],
        "source_revision": _run(["git", "rev-parse", "HEAD"], repo_root).strip(),
        "worktree_dirty": bool(status),
        "worktree_status_count": len(status),
        "external_source": _external_revision(repo_root, profile["external_source"]),
        "configurations": configurations,
        "tools": {**tool_versions(repo_root), "arm_none_eabi_gcc": {"available": bool(gcc), "version": gcc_version}},
        "generated_headers": generated_headers,
        "config_provenance": config_provenance,
        "scope": {
            "included": {name: (config.get("translation_units") and [unit["path"] for unit in config["translation_units"]]) or config.get("files", []) for name, config in configurations.items()},
            "excluded": [
                "RTOS kernel, HAL and CMSIS implementation bodies",
                "other boards and architecture branches",
                "Rust-native RTOS kernels",
                "runtime measurements and benchmark archives",
            ],
        },
    }
