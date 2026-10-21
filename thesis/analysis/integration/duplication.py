#!/usr/bin/env python3
"""C<->Rust duplication / drift analysis for the thesis.

The shared workload runner is maintained by hand in both C and
Rust. This script pairs the corresponding files and compares the sets of
identifiers they define or use (comments and string literals removed, language
keywords excluded). The Jaccard similarity of those identifier sets is a
deterministic drift-risk proxy: it is high when the two implementations name the
same operations and structures, and drops as they diverge. It locates parallel
code to review; it is not a defect or quality score.

Run from this directory::

    python3 duplication.py
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2] / "project"
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"

FIXED_PAIRS = [
    ("firmware/common/c/src/runner.c", "firmware/common/rust/src/lib.rs"),
]

KEYWORDS = {
    k.lower()
    for k in (
        "fn let mut pub use mod const static unsafe extern as in ref move where self Self "
        "super crate dyn impl trait struct enum if else match for while loop return break "
        "continue true false async await "
        "void int char long short unsigned signed float double sizeof goto inline volatile "
        "register auto NULL default do switch case typedef union static_assert"
    ).split()
}


def discover_pairs() -> list[tuple[str, str]]:
    pairs = list(FIXED_PAIRS)
    firmware = PROJECT_ROOT / "firmware"
    for system in sorted(p for p in firmware.iterdir() if p.is_dir()):
        if system.name == "common":
            continue
        c_dir = system / "c"
        rust_dirs = [d for d in system.iterdir() if d.is_dir() and d.name.startswith("rust")]
        c_files = [f for f in c_dir.rglob("*") if f.suffix in (".c", ".h")] if c_dir.is_dir() else []
        rust_files = [f for d in rust_dirs for f in d.rglob("*.rs")]
        by_stem = {f.stem: f for f in rust_files}
        for c in c_files:
            r = by_stem.get(c.stem)
            if r is not None:
                pairs.append((str(c.relative_to(PROJECT_ROOT)), str(r.relative_to(PROJECT_ROOT))))
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for pair in pairs:
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def strip_source(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            out.append(" ")
        elif c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def identifier_set(path: Path) -> set[str]:
    text = strip_source(path.read_text(encoding="utf-8", errors="replace"))
    return {w for w in WORD.findall(text) if w.lower() not in KEYWORDS}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    pairs = []
    inputs = {}
    for c_rel, r_rel in discover_pairs():
        c_path, r_path = PROJECT_ROOT / c_rel, PROJECT_ROOT / r_rel
        if not (c_path.is_file() and r_path.is_file()):
            continue
        c_ids, r_ids = identifier_set(c_path), identifier_set(r_path)
        union = c_ids | r_ids
        shared = c_ids & r_ids
        similarity = (len(shared) / len(union)) if union else 0.0
        pairs.append(
            {
                "c_path": c_rel,
                "rust_path": r_rel,
                "c_identifiers": len(c_ids),
                "rust_identifiers": len(r_ids),
                "shared_identifiers": len(shared),
                "similarity": round(similarity, 4),
                "examples": sorted(shared)[:8],
            }
        )
        inputs[c_rel] = sha256(c_path)
        inputs[r_rel] = sha256(r_path)

    report = {
        "schema": 1,
        "generated_by": "analysis/integration/duplication.py",
        "method": "jaccard(identifier sets, comments/strings removed, keywords excluded)",
        "pairs": pairs,
        "provenance": {"script_sha256": sha256(Path(__file__)), "inputs": inputs},
    }
    DATA.mkdir(parents=True, exist_ok=True)
    TYP.mkdir(parents=True, exist_ok=True)
    (DATA / "duplication.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    rows = "\n".join(
        f"  [{p['c_path'].split('/')[-1]} \u2194 {p['rust_path'].split('/')[-1]}], "
        f"[{p['c_identifiers']}], [{p['rust_identifiers']}], [{p['shared_identifiers']}], "
        f"[{p['similarity']:.2f}],"
        for p in pairs
    )
    typ = (
        "#figure(\n"
        "  text(size: 7.5pt, table(\n"
        "    columns: (1fr, 1fr, 1fr, 1fr, 1fr),\n"
        "    stroke: (x: none, y: 0.25pt + luma(210)),\n"
        "    inset: (x: 3pt, y: 1.5pt),\n"
        "    column-gutter: 2pt,\n"
        "    align: (left, right, right, right, right),\n"
        "    table.header(\n"
        "      table.cell(fill: luma(235))[*Pair*],\n"
        "      table.cell(fill: luma(235))[*C idents*],\n"
        "      table.cell(fill: luma(235))[*Rust idents*],\n"
        "      table.cell(fill: luma(235))[*Shared*],\n"
        "      table.cell(fill: luma(235))[*Similarity*],\n"
        "    ),\n"
        "    table.hline(stroke: 0.6pt + luma(150)),\n"
        f"{rows}\n"
        "  )),\n"
        "  caption: [Runner identifier overlap (Jaccard similarity)],\n"
        ") <duplication>\n"
    )
    (TYP / "duplication.typ").write_text(typ, encoding="utf-8")

    for p in pairs:
        print(
            f"{p['similarity']:.2f}  shared={p['shared_identifiers']:3d}  "
            f"{p['c_path']}  <->  {p['rust_path']}"
        )
    print(f"saved {DATA / 'duplication.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
