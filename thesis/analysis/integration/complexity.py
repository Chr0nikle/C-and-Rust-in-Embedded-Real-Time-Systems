#!/usr/bin/env python3
"""Complexity and maintainability metrics for this work's own modules.

Four metrics, per module, one tool for both languages (Mozilla
`rust-code-analysis-cli`, tree-sitter based):

    size        non-blank, non-comment code lines per module
    cyclomatic  median, mean and max per function
    cognitive   median, mean and max per function
    coupling    dependency edges per module (imports/includes)

Scope is the language-specific code of each path (shared runner and the three
per-kernel ports), the same scope as the safety metric; the vendored kernels are
excluded. Deterministic: the tool version is recorded and the output is
regenerated from the sources.

Writes generated/complexity.json and generated/complexity.typ.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2] / "project"
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"
RCA = Path.home() / ".cargo" / "bin" / "rust-code-analysis-cli"

MODULES = [
    # scope, language, root file, extra files counted for LOC only.
    # The C side is a single translation unit that holds both the case logic and
    # the timer/ISR shims, so it stays one "port" row. The Rust side is split the
    # same way the safety metric splits it: "app" is the case logic (lib.rs) and
    # "FFI" is the kernel-object boundary module (boundary.rs).
    ("Shared runner", "C", "firmware/common/c/src/runner.c",
     ["firmware/common/c/include/rtos_bench.h"]),
    ("Shared runner", "Rust", "firmware/common/rust/src/lib.rs", []),
    ("FreeRTOS port", "C", "firmware/c-kernels/freertos/c/stm32f4/src/bench.c", []),
    ("FreeRTOS app", "Rust", "firmware/c-kernels/freertos/rust/src/lib.rs", []),
    ("FreeRTOS FFI", "Rust", "firmware/c-kernels/freertos/rust/src/boundary.rs", []),
    ("NuttX port", "C", "firmware/c-kernels/nuttx/c/port.c", []),
    ("NuttX app", "Rust", "firmware/c-kernels/nuttx/rust/src/lib.rs", []),
    ("NuttX FFI", "Rust", "firmware/c-kernels/nuttx/rust/src/boundary.rs", []),
    ("Zephyr port", "C", "firmware/c-kernels/zephyr/c/stm32f4/port.c", []),
    ("Zephyr app", "Rust", "firmware/c-kernels/zephyr/rust/src/lib.rs", []),
    ("Zephyr FFI", "Rust", "firmware/c-kernels/zephyr/rust/src/boundary.rs", []),
]


def code_lines(path: Path) -> int:
    """Non-blank, non-comment lines, the same count used in the runner tables."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(errors="replace"), flags=re.S)
    return sum(1 for line in text.splitlines()
               if line.strip() and not line.strip().startswith("//"))


def functions(space, out):
    """Collect (cyclomatic, cognitive) for every function space, recursively."""
    metrics = space.get("metrics", {})
    if space.get("kind") == "function":
        cyc = metrics.get("cyclomatic", {}).get("sum")
        cog = metrics.get("cognitive", {}).get("sum")
        if cyc is not None and cog is not None:
            out.append((float(cyc), float(cog)))
    for child in space.get("spaces", []):
        functions(child, out)


PROJECT_ROOTS = {"crate", "self", "super", "rtos_bench"}


def dependencies(path: Path, lang: str) -> dict:
    """Distinct components the module depends on, split internal vs external.

    Internal is this work's own code (quoted C includes; Rust `crate`/`self`/
    `super`/`rtos_bench` uses and `mod`); external is everything else (kernel and
    SDK headers, `core`/`std`, other crates) plus the C kernel FFI boundary.
    """
    text = path.read_text(errors="replace")
    if lang == "C":
        quoted = {Path(m).name for m in re.findall(r'#\s*include\s+"([^"]+)"', text)}
        angled = set(re.findall(r'#\s*include\s*<([^>]+)>', text))
        return {"internal": len(quoted), "external": len(angled),
                "total": len(quoted) + len(angled)}
    uses = set(re.findall(r'^\s*(?:pub\s+)?use\s+([A-Za-z_]\w*)', text, re.M))
    mods = set(re.findall(r'^\s*(?:pub\s+)?mod\s+([A-Za-z_]\w*)', text, re.M))
    extern_c = 0  # the FFI boundary is one component per module
    if re.search(r'extern\s+"C"', text):
        extern_c = 1
    internal = len({u for u in uses if u in PROJECT_ROOTS} | mods)
    external = len({u for u in uses if u not in PROJECT_ROOTS}) + extern_c
    return {"internal": internal, "external": external, "total": internal + external}


def main() -> int:
    if not RCA.exists():
        raise SystemExit(f"rust-code-analysis-cli not found at {RCA}")

    paths = [ROOT / rel for _scope, _lang, rel, _extra in MODULES]
    out_dir = Path(tempfile.mkdtemp(prefix="rca-"))
    try:
        cmd = [str(RCA), "-m", "-O", "json", "--pr", "-o", str(out_dir)]
        for path in paths:
            cmd += ["-p", str(path)]
        run = subprocess.run(cmd, capture_output=True, text=True)
        if run.returncode != 0:
            raise SystemExit(f"rca failed: {run.stderr.strip()[:400]}")

        rows = []
        all_cyc, all_cog = [], []
        for scope, lang, rel, extra in MODULES:
            path = ROOT / rel
            mirrored = out_dir / (str(path).lstrip("/") + ".json")
            data = json.loads(mirrored.read_text())
            fn_vals = []
            for space in data.get("spaces", []):
                functions(space, fn_vals)
            cyc = [c for c, _ in fn_vals]
            cog = [c for _, c in fn_vals]
            all_cyc += cyc
            all_cog += cog
            sloc = code_lines(path) + sum(code_lines(ROOT / e) for e in extra)
            dep = dependencies(path, lang)
            rows.append({
                "scope": scope, "language": lang, "module": rel,
                "loc": sloc, "functions": len(fn_vals), "coupling": dep["total"],
                "coupling_internal": dep["internal"], "coupling_external": dep["external"],
                "cyc_median": statistics.median(cyc) if cyc else 0,
                "cyc_mean": statistics.mean(cyc) if cyc else 0,
                "cyc_max": max(cyc) if cyc else 0,
                "cog_median": statistics.median(cog) if cog else 0,
                "cog_mean": statistics.mean(cog) if cog else 0,
                "cog_max": max(cog) if cog else 0,
            })
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    version = subprocess.run([str(RCA), "--version"], capture_output=True, text=True).stdout.strip()
    repo = ROOT.parent
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--", "project/firmware"],
        capture_output=True, text=True, check=False,
    ).stdout
    payload = {
        "schema": 1,
        "generated_by": "analysis/integration/complexity.py",
        "tool": version,
        "source_revision": revision or None,
        "source_dirty": bool(status.strip()),
        "source_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "source_scope": "project/firmware",
        "modules": rows,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    TYP.mkdir(parents=True, exist_ok=True)
    (DATA / "complexity.json").write_text(json.dumps(payload, indent=2) + "\n")

    out = ["// Generated by analysis/integration/complexity.py -- do not edit.",
           "// Tool: " + version,
           "#figure(",
           "  text(size: 7.5pt, table(",
           "    columns: (auto, auto, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr, 1fr),",
           "    stroke: (x: none, y: 0.25pt + luma(210)),",
           "    inset: (x: 2pt, y: 1.5pt),",
           "    column-gutter: 2pt,",
           "    align: (left, left, right, right, right, right, right, right, right, right, right),",
           "    table.header(",
           "      table.cell(fill: luma(235))[*Module*],",
           "      table.cell(fill: luma(235))[*Lang*],",
           "      table.cell(fill: luma(235))[*LOC*],",
           "      table.cell(fill: luma(235))[*Funcs*],",
           "      table.cell(fill: luma(235))[*cyc med*],",
           "      table.cell(fill: luma(235))[*cyc mean*],",
           "      table.cell(fill: luma(235))[*cyc max*],",
           "      table.cell(fill: luma(235))[*cog med*],",
           "      table.cell(fill: luma(235))[*cog mean*],",
           "      table.cell(fill: luma(235))[*cog max*],",
           "      table.cell(fill: luma(235))[*deps*],",
           "    ),",
           "    table.hline(stroke: 0.6pt + luma(150)),"]
    for r in rows:
        out.append(
            f"    [{r['scope']}], [{r['language']}], [{r['loc']}], [{r['functions']}], "
            f"[{r['cyc_median']:.0f}], [{r['cyc_mean']:.1f}], [{r['cyc_max']:.0f}], "
            f"[{r['cog_median']:.0f}], [{r['cog_mean']:.1f}], [{r['cog_max']:.0f}], "
            f"[{r['coupling']}],")
    out += [
        "  )),",
        "  caption: [Source complexity and coupling by module],",
        ") <complexity>",
    ]
    (TYP / "complexity.typ").write_text("\n".join(out) + "\n")

    for r in rows:
        print(f"{r['scope']:14s} {r['language']:4s} loc={r['loc']:5d} funcs={r['functions']:3d} "
              f"cyc={r['cyc_median']:.0f}/{r['cyc_mean']:.1f}/{r['cyc_max']:.0f} "
              f"cog={r['cog_median']:.0f}/{r['cog_mean']:.1f}/{r['cog_max']:.0f} deps={r['coupling']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
