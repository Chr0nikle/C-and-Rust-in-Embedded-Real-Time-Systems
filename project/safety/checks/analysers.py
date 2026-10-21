"""Established static analysers, kept separate from contract checks."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from common.io import contract


def _rel(path: str | None, root: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return path


def _scope(path: str | None, repo_root: Path) -> str:
    if not path:
        return "unknown"
    try:
        rel = Path(path).resolve().relative_to(repo_root)
    except (ValueError, OSError):
        return "external"
    if str(rel).startswith("firmware/common/c/") or str(rel).startswith("firmware/c-kernels/freertos/c/stm32f4/"):
        return "project"
    return "dependency_or_system"


def run_clang_analyzer(
    repo_root: Path,
    translation_units: list[dict[str, Any]],
    *,
    output_root: Path,
    configuration: str,
    report_name: str,
) -> dict[str, Any]:
    clang = shutil.which("clang")
    if not clang:
        return {"tool": "clang-static-analyzer", "available": False, "status": "unavailable", "diagnostics": []}
    diagnostics: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for unit in translation_units:
        with tempfile.TemporaryDirectory(prefix="uastw-clang-analyzer-") as directory:
            plist = Path(directory) / "report.plist"
            command = [
                clang,
                "--analyze",
                "-Xanalyzer",
                "-analyzer-output=plist",
                "-o",
                str(plist),
                *unit["args"],
                unit["abs_path"],
            ]
            completed = subprocess.run(
                command,
                cwd=repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            run = {
                "configuration": configuration,
                "source": unit.get("rel", unit["path"]),
                "command_excluding_temporary_report_path": [part.replace(str(repo_root), ".") for part in command if part not in (str(plist), "-o")],
                "returncode": completed.returncode,
                "stderr": completed.stderr.strip(),
                "diagnostic_count": 0,
            }
            if completed.returncode == 0 and plist.is_file():
                try:
                    report = plistlib.loads(plist.read_bytes())
                    for item in report.get("diagnostics", []):
                        location = (item.get("location") or {})
                        file = location.get("file")
                        diagnostics.append(
                            {
                                "tool": "clang-static-analyzer",
                                "tool_version": subprocess.run([clang, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout.splitlines()[0],
                                "severity": "warning",
                                "rule_id": item.get("category") or item.get("type") or "clang-analyzer",
                                "file": _rel(file, repo_root),
                                "line": location.get("line"),
                                "column": location.get("col"),
                                "message": item.get("description") or item.get("type") or "",
                                "scope": _scope(file, repo_root),
                                "run_status": "completed",
                            }
                        )
                    run["diagnostic_count"] = len(report.get("diagnostics", []))
                except Exception as error:
                    run["parse_error"] = str(error)
            else:
                run["status"] = "failed" if completed.returncode != 0 else "no_report"
            runs.append(run)
    diagnostics.sort(key=lambda row: (row.get("file") or "", row.get("line") or 0, row.get("column") or 0, row.get("rule_id") or "", row.get("message") or ""))
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / report_name).write_text(
        json.dumps({"configuration": configuration, "runs": runs, "diagnostics": diagnostics}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "configuration": configuration,
        "tool": "clang-static-analyzer",
        "available": True,
        "version": subprocess.run([clang, "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout.splitlines()[0],
        "status": "completed" if all(run.get("returncode") == 0 for run in runs) else "partial_or_failed",
        "runs": runs,
        "diagnostics": diagnostics,
        "scope_note": "default Clang Static Analyzer checks; no custom FreeRTOS allocator/semaphore model",
    }


def run_clippy(repo_root: Path, *, output_root: Path) -> dict[str, Any]:
    cargo = shutil.which("cargo")
    if not cargo or not shutil.which("clippy-driver"):
        return {"configuration": "freertos-stm32f4-rust", "tool": "clippy", "available": False, "status": "unavailable", "diagnostics": []}
    source_shared = repo_root / "firmware/common/rust"
    source_app = repo_root / "firmware/c-kernels/freertos/rust"
    if not source_shared.is_dir() or not source_app.is_dir():
        return {"configuration": "freertos-stm32f4-rust", "tool": "clippy", "available": True, "status": "scope_missing", "diagnostics": []}
    with tempfile.TemporaryDirectory(prefix="uastw-clippy-") as directory:
        temp = Path(directory)
        target_shared = temp / "firmware/common/rust"
        target_app = temp / "firmware/c-kernels/freertos/rust"
        shutil.copytree(source_shared, target_shared)
        shutil.copytree(source_app, target_app)
        command = [
            cargo,
            "clippy",
            "--offline",
            "--target",
            "thumbv7em-none-eabihf",
            "--message-format=json",
        ]
        completed = subprocess.run(
            command,
            cwd=target_app,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**dict(os.environ), "CARGO_TARGET_DIR": str(temp / "target")},
        )
        diagnostics: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("reason") != "compiler-message":
                continue
            message = record.get("message") or {}
            spans = message.get("spans") or []
            primary = next((span for span in spans if span.get("is_primary")), spans[0] if spans else {})
            diagnostics.append(
                {
                    "configuration": "freertos-stm32f4-rust",
                    "tool": "clippy",
                    "tool_version": subprocess.run([shutil.which("clippy-driver") or "clippy-driver", "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout.splitlines()[0],
                    "severity": message.get("level", "warning"),
                    "rule_id": (message.get("code") or {}).get("code") or message.get("level", "clippy"),
                    "file": primary.get("file_name"),
                    "line": primary.get("line_start"),
                    "column": primary.get("column_start"),
                    "message": message.get("message", ""),
                    "scope": "project",
                    "run_status": "completed",
                }
            )
        diagnostics.sort(key=lambda row: (row.get("file") or "", row.get("line") or 0, row.get("rule_id") or "", row.get("message") or ""))
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "clippy.json").write_text(
            json.dumps(
                {
                    "configuration": "freertos-stm32f4-rust",
                    "command": command,
                    "cwd": "temporary copy of firmware/common/rust and firmware/c-kernels/freertos/rust",
                    "returncode": completed.returncode,
                    "stderr": (
                        completed.stderr.strip().replace(str(temp), "<temporary-copy>")
                        if completed.returncode != 0
                        else ""
                    ),
                    "diagnostics": diagnostics,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "configuration": "freertos-stm32f4-rust",
            "tool": "clippy",
            "available": True,
            "version": subprocess.run([shutil.which("clippy-driver") or "clippy-driver", "--version"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False).stdout.splitlines()[0],
            "status": "completed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "diagnostics": diagnostics,
            "scope_note": "isolated copy; target thumbv7em-none-eabihf; no source mutation",
        }


def check_05(
    analyser_runs: dict[str, Any],
    *,
    configuration: str,
    language: str,
    clang_key: str | None,
    clippy: bool,
    tool_version: str,
) -> dict[str, Any]:
    clang_run = analyser_runs.get(clang_key or "", {}) if clang_key else {}
    clippy_run = analyser_runs.get("clippy", {}) if clippy else {}
    evidence = {
        "clang_analyzer_status": clang_run.get("status"),
        "clang_analyzer_scope": clang_run.get("scope_note"),
        "clippy_status": clippy_run.get("status"),
        "diagnostics_are_not_contract_results": True,
    }
    return contract(
        check_id="CHECK-05",
        configuration=configuration,
        language=language,
        interface="resource and cleanup diagnostics",
        property_name="missing/double release and null-use after allocation failure",
        status="NOT_CHECKED",
        source_file=None,
        source_line=None,
        evidence=evidence,
        assumptions={
            "reason": "Clang Static Analyzer and Clippy have no validated FreeRTOS ownership model in this phase",
            "policy": "a clean run is not PASS; absence of warnings is not verified cleanup",
        },
        tool="clang-static-analyzer, clippy" if clippy else "clang-static-analyzer",
        tool_version=tool_version,
        reason="resource-cleanup ownership verification remains outside the validated Phase 1 checker set; analyser diagnostics are reported separately",
        applicability="NOT_CHECKED",
    )
