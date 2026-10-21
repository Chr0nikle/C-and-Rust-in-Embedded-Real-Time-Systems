"""Build and invoke the syntax-aware syn extractor."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


EXTRACTOR_DIR = Path(__file__).resolve().parent.parent / "rust_extractor"


def build_extractor(repo_root: Path) -> Path:
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError("cargo is unavailable; the syn extractor cannot be built")
    completed = subprocess.run(
        [cargo, "build", "--offline", "--release"],
        cwd=EXTRACTOR_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "syn extractor build failed; cached crates may be missing\n"
            + completed.stderr[-4000:]
        )
    binary = EXTRACTOR_DIR / "target" / "release" / "uastw-rust-extract"
    if not binary.is_file():
        raise RuntimeError(f"extractor binary not found at {binary}")
    return binary


def extract_rust(repo_root: Path, files: list[str], target: dict[str, Any], binary: Path, features: list[str] | None = None) -> dict[str, Any]:
    command = [
        str(binary),
        "--target-arch",
        str(target.get("arch", "arm")),
        "--target-os",
        str(target.get("os", "none")),
        "--target-family",
        str(target.get("family", "none")),
        "--target-pointer-width",
        str(target.get("pointer_width", "32")),
        *[item for feature in (features or []) for item in ("--feature", feature)],
        *files,
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Rust extractor failed: {completed.stderr.strip()}")
    facts = json.loads(completed.stdout)
    facts["tool"] = {
        "name": "uastw-rust-extract",
        "version": "0.1.0",
        "parser": "syn 2.0.119",
        "rustc": subprocess.run(
            [shutil.which("rustc") or "rustc", "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        ).stdout.strip(),
        "command": command,
    }
    # Function/file association is needed by the queue checker.
    facts["file_for_function"] = {
        function["name"]: function.get("file")
        for function in facts.get("functions", [])
        if function.get("name")
    }
    return facts
