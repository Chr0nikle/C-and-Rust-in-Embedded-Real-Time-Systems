#!/usr/bin/env python3
"""Integration-surface extraction for the thesis (RQ3).

For every integrated system this reports the concrete cost of adding a language
or operating-system path: adapter source size relative to the shared runner,
conditional-compilation branches, staged SDK patches, and build-file count.
Counts are deterministic but heuristic proxies that locate integration effort;
they are not productivity measurements. The port-primitive counts (the Rust
`Port` trait methods and the C `bench_port` function pointers) describe the
shared contract every path implements.

Run from this directory::

    python3 surface.py
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "uastw-thesis"
import matplotlib.pyplot as plt
import numpy as np

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2] / "project"
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"
FIRMWARE = PROJECT_ROOT / "firmware"
PLUGINS = PROJECT_ROOT / "plugins"

SOURCE_SUFFIXES = (".c", ".h", ".rs")
BUILD_FILE_NAMES = ("Cargo.toml", "Makefile", "CMakeLists.txt", "prj.conf", "Kconfig")
# Directories that are not hand-maintained integration code. The Zephyr tree
# also retains early blink examples; they are not part of the benchmark image.
EXCLUDED_DIRECTORY_NAMES = {"build", "target", "vendor", ".git", "blink", "blink-bare", "cube-blink"}


def code_lines(path: Path) -> int:
    """Non-blank, non-comment lines (line and block comments)."""
    count = 0
    in_block = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if in_block:
            if "*/" in line:
                in_block = False
            continue
        if line.startswith("/*"):
            if "*/" not in line:
                in_block = True
            continue
        if line.startswith("//") or line.startswith("*") or not line:
            continue
        count += 1
    return count


def excluded(path: Path, root: Path) -> bool:
    return any(part in EXCLUDED_DIRECTORY_NAMES for part in path.relative_to(root).parts)


def source_files(root: Path, adapter_roots: list[Path]):
    """Yield source files only from explicit adapter roots."""
    for path in sorted(root.rglob("*")):
        if (path.is_file() and path.suffix in SOURCE_SUFFIXES
                and not excluded(path, root)
                and any(path.is_relative_to(adapter) for adapter in adapter_roots)):
            yield path


def build_files(root: Path, build_roots: list[Path]):
    """Yield recognised build files from the same hand-maintained roots."""
    for path in sorted(root.rglob("*")):
        if (path.is_file() and path.name in BUILD_FILE_NAMES
                and not excluded(path, root)
                and any(path.is_relative_to(adapter) for adapter in build_roots)):
            yield path


def adapter_roots(fw: Path) -> tuple[list[Path], list[Path]]:
    """Return (source roots, build roots) for one system's project code."""
    roots: list[Path] = []
    builds: list[Path] = []
    c_root = fw / "c"
    if c_root.is_dir():
        # Include direct files: NuttX keeps port.c/isr.c/main.c at this level.
        roots.append(c_root)
        builds.append(c_root)
    for directory in sorted(fw.iterdir()):
        if not directory.is_dir() or not directory.name.startswith("rust"):
            continue
        crates = [directory] if (
            (directory / "Cargo.toml").is_file() or (directory / "src").is_dir()
        ) else [
            child for child in sorted(directory.iterdir())
            if child.is_dir() and (child / "Cargo.toml").is_file()
        ]
        for crate in crates:
            src = crate / "src"
            roots.append(src if src.is_dir() else crate)
            builds.append(crate)
    return roots, builds


def port_primitives() -> dict:
    lib = (FIRMWARE / "common/rust/src/lib.rs").read_text(encoding="utf-8", errors="replace")
    rust_methods = 0
    if "pub trait Port" in lib:
        body = lib.split("pub trait Port", 1)[1]
        depth = 0
        started = False
        for line in body.splitlines():
            depth += line.count("{") - line.count("}")
            if "{" in line:
                started = True
            if re.match(r"\s*fn\s", line):
                rust_methods += 1
            if started and depth <= 0:
                break
    header = (FIRMWARE / "common/c/include/rtos_bench.h").read_text(encoding="utf-8", errors="replace")
    c_pointers = 0
    if "} bench_port;" in header:
        struct = header.split("} bench_port;", 1)[0]
        struct = struct.rsplit("typedef struct {", 1)[-1]
        c_pointers = struct.count("(*")
    return {"rust_port_methods": rust_methods, "c_port_pointers": c_pointers}


def shared_code_lines(path: Path) -> int:
    """Non-blank, non-comment lines of the shared runner, one counting method.

    The shared runner is the protocol session and case dispatch, so the
    thesis reports one comparable size for it. The retired case-28 portability control is excluded.
    """
    text = re.sub(r"/\*.*?\*/", "", path.read_text(), flags=re.S)
    return sum(1 for line in text.splitlines()
               if line.strip() and not line.strip().startswith("//"))



def write_integration_figure():
    # Hand-written benchmark code per system: the case implementation and its
    # kernel boundary, excluding the staged kernel, SDK and HAL templates. The C
    # figure is the case-implementation module (bench.c / port.c); the Rust
    # figure is the application plus boundary modules (lib.rs + boundary.rs).
    # Both come from complexity.json, which measures C and Rust with one tool and
    # one scope. The Rust-only kernels are not in that file, so they fall back to
    # the integration-surface count for their Rust adapter.
    modules = json.load(open(DATA / "complexity.json"))["modules"]
    hand = {}  # system -> {"c": int, "rust": int}
    for m in modules:
        scope = m["scope"]
        if scope == "Shared runner":
            continue
        sys = scope.split()[0].lower()
        if m["language"] == "C":
            hand.setdefault(sys, {})["c"] = m["loc"]
        else:
            slot = hand.setdefault(sys, {})
            slot["rust"] = slot.get("rust", 0) + m["loc"]
    systems = json.load(open(DATA / "integration-surface.json"))["systems"]
    systems = [s for s in systems
               if (s.get("adapter_c_lines") or 0) or (s.get("adapter_rust_lines") or 0)]
    names = [s["system"] for s in systems]
    c_loc = [hand.get(s["system"], {}).get("c", 0) for s in systems]
    r_loc = [hand.get(s["system"], {}).get("rust", 0)
             or (s.get("adapter_rust_lines") or 0) for s in systems]
    x = np.arange(len(names))
    width = 0.38
    fig, ax = plt.subplots(figsize=(7.0, 3.2))
    ax.bar(x - width / 2, c_loc, width, color="#4c72b0", label="C adapter")
    ax.bar(x + width / 2, r_loc, width, color="#dd8452", label="Rust adapter")
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontsize=8)
    ax.set_ylabel("code lines")
    ax.set_title(
        "Hand-written benchmark code per system\n"
        "(case implementation and kernel boundary)",
        fontsize=10.5,
        pad=6,
    )
    ax.legend(fontsize=8)
    fig.subplots_adjust(top=0.82)
    fig.savefig(FIGS / "fig-integration.svg", metadata={"Date": None})
    plt.close(fig)




def main() -> int:
    shared_c = shared_code_lines(FIRMWARE / "common/c/src/runner.c") + shared_code_lines(
        FIRMWARE / "common/c/include/rtos_bench.h")
    shared_rust = shared_code_lines(FIRMWARE / "common/rust/src/lib.rs")
    primitives = port_primitives()

    systems = []
    for plugin in sorted(p for p in PLUGINS.iterdir() if p.is_dir()):
        name = plugin.name
        fw = None
        for group in ("c-kernels", "rust-kernels"):
            candidate = FIRMWARE / group / name
            if candidate.is_dir():
                fw = candidate
                break
        if fw is None:
            continue
        c_lines = r_lines = cfg = patches = 0
        source_roots, build_roots = adapter_roots(fw)
        for f in source_files(fw, source_roots):
            text = f.read_text(encoding="utf-8", errors="replace")
            lines = code_lines(f)
            if f.suffix == ".rs":
                r_lines += lines
                cfg += len(re.findall(r"#\s*\[\s*cfg", text))
            else:
                c_lines += lines
                cfg += len(re.findall(r"#\s*(if|ifdef|ifndef)\b", text))
        build_file_paths = [
            str(f.relative_to(PROJECT_ROOT)) for f in build_files(fw, build_roots)
        ]
        src_dir = plugin / "src"
        patch_files = sorted(src_dir.glob("*.rs")) if src_dir.is_dir() else []
        for f in patch_files:
            patches += len(
                re.findall(
                    r"\b(patch|replace|stage|overlay)\b",
                    f.read_text(encoding="utf-8", errors="replace"),
                    re.I,
                )
            )
        systems.append(
            {
                "system": name,
                "adapter_c_lines": c_lines,
                "adapter_rust_lines": r_lines,
                "adapter_lines": c_lines + r_lines,
                "cfg_branches": cfg,
                "staged_patches": patches,
                "patch_keyword_scope": "case-insensitive patch/replace/stage/overlay matches in plugins/<system>/src/*.rs",
                "build_files": len(build_file_paths),
                "build_file_paths": build_file_paths,
            }
        )

    report = {
        "schema": 1,
        "generated_by": "analysis/integration/surface.py",
        "shared_runner": {"c_lines": shared_c, "rust_lines": shared_rust, "lines": shared_c + shared_rust},
        "port_primitives": primitives,
        "systems": systems,
        "notes": [
            "Adapter lines count non-blank, non-comment lines under firmware/<system>.",
            "cfg_branches counts #[cfg in Rust and #if/#ifdef/#ifndef in C.",
            "staged_patches is retained as a data key; it counts case-insensitive patch/replace/stage/overlay keyword matches in plugins/<system>/src/*.rs, including comments and strings.",
            "adapter_c_lines and adapter_rust_lines cover project-owned files under the system's C adapter root and Rust crate source; vendored SDKs, generated trees and retained blink examples are excluded.",
            "build_files counts recognised project-owned build/configuration files in those crate and adapter roots; it excludes vendor, target and build trees.",
            "Counts are review-oriented proxies, not effort measurements.",
        ],
        "provenance": {"script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
    }
    DATA.mkdir(parents=True, exist_ok=True)
    TYP.mkdir(parents=True, exist_ok=True)
    FIGS.mkdir(parents=True, exist_ok=True)
    (DATA / "integration-surface.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    rows = "\n".join(
        f"  [{s['system']}], [{s['adapter_c_lines']}], [{s['adapter_rust_lines']}], "
        f"[{s['cfg_branches']}], [{s['staged_patches']}],"
        for s in systems
    )
    typ = (
        "#figure(\n"
        "  text(size: 7.5pt, table(\n"
        "    columns: (auto, 1fr, 1fr, 1fr, 1fr),\n"
        "    stroke: (x: none, y: 0.25pt + luma(210)),\n"
        "    inset: (x: 3pt, y: 1.5pt),\n"
        "    column-gutter: 2pt,\n"
        "    align: (left, right, right, right, right),\n"
        "    table.header(\n"
        "      table.cell(fill: luma(235))[*System*],\n"
        "      table.cell(fill: luma(235))[*Adapter C*],\n"
        "      table.cell(fill: luma(235))[*Adapter Rust*],\n"
        "      table.cell(fill: luma(235))[*cfg branches*],\n"
        "      table.cell(fill: luma(235))[*Patch keyword matches*],\n"
        "    ),\n"
        "    table.hline(stroke: 0.6pt + luma(150)),\n"
        f"{rows}\n"
        "  )),\n"
        "  caption: [Integration surface by system. Adapter lines are project-owned code; patch counts are keyword matches, not applied patches.],\n"
        ") <integration-surface>\n"
    )
    (TYP / "integration-surface.typ").write_text(typ, encoding="utf-8")

    print(f"shared runner: {shared_c + shared_rust} code lines; primitives {primitives}")
    for s in systems:
        print(
            f"{s['system']:16s} c={s['adapter_c_lines']:5d} rust={s['adapter_rust_lines']:5d} "
            f"cfg={s['cfg_branches']:4d} patches={s['staged_patches']:3d} build={s['build_files']}"
        )
    write_integration_figure()
    print(f"saved {DATA / 'integration-surface.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
