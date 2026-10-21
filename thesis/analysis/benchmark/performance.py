#!/usr/bin/env python3
"""Publication heatmap of matched C/Rust benchmark ratios.

The figure replaces ``figures/performance-comparison.svg``.  It reads the
pooled statistics in ``analysis/generated/replication-stats.json`` and plots
the median Rust/C ratio for all 26 thesis benchmark cases on the four boards of
FreeRTOS, NuttX and Zephyr.

The campaign is not hard-coded: the script finds the newest completed
``results/campaigns/campaign-replication-*`` bundle, checks that the generated statistics
refer to it (regenerating the statistics and thesis tables when they are stale),
and records the bundle, statistics hash and every plotted cell in
``figures/performance-comparison.provenance.json``.

The two higher-is-better cases, IPC throughput (14) and zero-copy IPC (15), are
marked with an upward arrow in the row label.  All other cases are
lower-is-better.  Cases that are not advertised by a port, or whose medians are
zero/undefined, are drawn as missing rather than as zero.

Run from the thesis root with the repository's analysis virtual environment::

    <project-root>/.local/venv/bin/python3 analysis/benchmark/performance.py
"""

from __future__ import annotations

import hashlib
import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
from matplotlib.patches import Rectangle

from selection import campaign_bundle

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[2] / "project"
REPO_ROOT = PROJECT_ROOT.parent
RESULTS = PROJECT_ROOT.parent / "results" / "campaigns"
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"
STATS = DATA / "replication-stats.json"
FIGURE = FIGS / "performance-comparison.svg"
PROVENANCE = DATA / "performance-comparison.provenance.json"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from campaign import (  # noqa: E402
    BOARD_LABEL,
    BOARD_ORDER,
    RTOS_LABEL,
    THESIS_ORDER,
)
from tables import CASE_GROUPS, CASE_ORDER, CASE_SHORT  # noqa: E402
from replication import ticks_to_throughput  # noqa: E402

MATCHED_RTOS = ("freertos", "nuttx", "zephyr")

# All thesis benchmark cases, in presentation order.  Cases 14 (throughput) and
# 15 (zero-copy saving) are higher-is-better; the remaining cases are
# lower-is-better.
ALL_CASES = list(range(1, 27))
HIGHER_IS_BETTER = {14, 15}

# Blue (lower Rust/C ratio) -> neutral -> red (higher Rust/C ratio).  The
# ratio is of the reported quantity, so rows marked higher-is-better (14 and
# 15) must be read with their arrow rather than as a universal latency ranking.
# The blue/red pair is distinguishable for the common colour-vision
# deficiencies and keeps the midpoint white rather than relying on hue alone.
CMAP = LinearSegmentedColormap.from_list(
    "rust_c_latency",
    ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"],
    N=256,
)
CMAP.set_bad((1, 1, 1, 0))

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8,
    "axes.linewidth": 0.6,
    "svg.fonttype": "path",
    "svg.hashsalt": "uastw-thesis",
})


def bundle_path(reference: str) -> Path:
    path = Path(reference)
    return path if path.is_absolute() else REPO_ROOT / path


def selected_bundle() -> Path:
    """Campaign selected by ``thesis/analysis/campaign.lock.json``."""
    return campaign_bundle()


def transform_native_throughput(stats: dict, old: dict | None,
                                provenance_path: Path) -> None:
    """Convert preserved Rust-native case-19 medians and drop unsupported p99.

    The supplementary raw bundle is missing.  Only the preserved median and p99
    elapsed durations, counter frequencies and published outputs remain.  A
    median throughput can be derived from the preserved median duration, but a
    p99 *rate* cannot be reconstructed from a p99 duration.  Mark p99 unavailable
    instead of presenting a percentile of elapsed time as a percentile of rates.
    """
    if not old:
        return
    try:
        provenance = json.loads(provenance_path.read_text())
    except (OSError, json.JSONDecodeError):
        provenance = {}
    for key in ("freertos-in-rust/stm32f4/rust",
                "freertos-in-rust/stm32h723/rust",
                "rivet/stm32f4/rust"):
        old_record = old.get("paths", {}).get(key, {}).get("cases", {}).get("19")
        record = stats.get("paths", {}).get(key, {}).get("cases", {}).get("19")
        if not old_record or not record or record.get("summary_derived"):
            continue
        tps_record = provenance.get(key, {}).get("cases", {}).get("19", {})
        tps = tps_record.get("ticks_per_second")
        raw_median = old_record.get("median")
        raw_p99 = old_record.get("p99")
        if not isinstance(tps, int) or tps <= 0 or not raw_median or raw_median <= 0:
            record["unit"] = "unavailable"
            record["median"] = None
            record["p99"] = None
            record["unavailable_reason"] = "preserved native counter frequency missing"
            continue
        record["raw_median_ticks"] = raw_median
        record["raw_p99_ticks"] = raw_p99
        record["raw_values"] = record.pop("values", [])
        record["raw_variants"] = record.pop("variants", {})
        record["ticks_per_second"] = tps
        record["median"] = ticks_to_throughput(raw_median, tps)
        record["unit"] = "B/s"
        record["min"] = None
        record["p95"] = None
        record["p99"] = None
        record["p999"] = None
        record["max"] = None
        record["jitter"] = None
        record["summary_derived"] = True
        record["p99_unavailable_reason"] = (
            "raw supplementary samples missing; p99 throughput cannot be "
            "reconstructed from a preserved p99 duration"
        )


def regenerate_statistics(newest: Path, old: dict | None) -> dict:
    """Run the canonical statistics generator and retain Rust-native paths.

    The newest campaign is the matched C/Rust corpus and does not contain the
    Rust-native kernels.  Those paths are kept from the previous statistics so
    the thesis tables do not silently lose them; the sidecar names this
    supplementary bundle.
    """
    print(f"regenerating statistics from {newest}")
    subprocess.run(
        [sys.executable, str(HERE / "replication.py"), "--bundle", str(newest)],
        cwd=HERE,
        check=True,
    )
    stats = json.loads(STATS.read_text())
    if old:
        missing = sorted(set(old.get("paths", {})) - set(stats.get("paths", {})))
        keep = [key for key in missing
                if key.split("/", 1)[0] in {"freertos-in-rust", "rivet"}]
        for key in keep:
            stats["paths"][key] = old["paths"][key]
        stats["paths"] = dict(sorted(stats["paths"].items()))
        if keep:
            supplementary = []
            for entry in old.get("supplementary_bundles", []):
                paths = [path for path in entry.get("paths", []) if path in keep]
                if not paths:
                    continue
                source = entry.get("bundle")
                supplementary.append({
                    "bundle": source,
                    "bundle_sha256": entry.get("bundle_sha256"),
                    "paths": paths,
                    "reason": "retained because the selected campaign does not include "
                              "these Rust-native paths",
                    "status": "available" if bundle_path(source).is_dir() else "missing",
                    "preserved_derived": "thesis/generated/data/replication-stats.json",
                })
            if not supplementary:
                supplementary = [{
                    "bundle": old.get("bundle"),
                    "bundle_sha256": old.get("bundle_sha256"),
                    "paths": keep,
                    "reason": "retained from previous statistics because the selected campaign "
                              "does not include these Rust-native paths",
                    "status": "available" if bundle_path(old.get("bundle", "")).is_dir() else "missing",
                    "preserved_derived": "thesis/generated/data/replication-stats.json",
                }]
            stats["supplementary_bundles"] = supplementary
            for entry in supplementary:
                if entry["status"] == "missing":
                    print(f"warning: supplementary evidence missing: {entry['bundle']} "
                          f"(paths: {', '.join(entry['paths'])})")
        transform_native_throughput(stats, old, DATA / "campaign-provenance.json")
        STATS.write_text(json.dumps(stats, indent=2, sort_keys=True) + "\n")
    return stats


def provenance_uses_bundle(stats: dict) -> bool:
    """True when the campaign tables were built from the current statistics."""
    provenance_file = DATA / "campaign-provenance.json"
    if not provenance_file.is_file():
        return False
    try:
        provenance = json.loads(provenance_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    bundle_name = Path(stats["bundle"]).name
    for rtos in MATCHED_RTOS:
        for board in BOARD_ORDER[rtos]:
            for language in ("bench", "rust"):
                key = f"{rtos}/{board}/{language}"
                cases = provenance.get(key, {}).get("cases", {})
                if not cases:
                    return False
                if not any(bundle_name in Path(record.get("run_path", "")).parts
                           for record in cases.values()):
                    return False
    return True


def tables_are_current(stats: dict) -> bool:
    required = [
        TYP / "all-results.typ",
        TYP / "campaign-coverage.typ",
        TYP / "campaign-memory.typ",
        TYP / "campaign-static.typ",
        DATA / "campaign-provenance.json",
    ]
    if not all(path.is_file() for path in required):
        return False
    if any(path.stat().st_mtime < STATS.stat().st_mtime for path in required):
        return False
    return provenance_uses_bundle(stats)


def ensure_current() -> dict:
    """Load current statistics and make the thesis tables match them."""
    newest = selected_bundle()
    old = json.loads(STATS.read_text()) if STATS.is_file() else None
    stats = old
    if (not stats or "bundle" not in stats or "paths" not in stats
            or bundle_path(stats["bundle"]).resolve() != newest):
        stats = regenerate_statistics(newest, old)
    if not tables_are_current(stats):
        print("regenerating campaign tables so they match the statistics")
        subprocess.run([sys.executable, str(HERE / "campaign.py")],
                       cwd=HERE, check=True)
        subprocess.run([sys.executable, str(HERE / "tables.py")],
                       cwd=HERE, check=True)
    if bundle_path(stats["bundle"]).resolve() != newest:
        raise SystemExit("could not bring replication statistics up to date")
    if not tables_are_current(stats):
        raise SystemExit("campaign tables are not derived from the current statistics")
    return stats


def case_groups() -> list[tuple[str, list[int]]]:
    proto_to_thesis = {proto: index + 1 for index, proto in enumerate(THESIS_ORDER)}
    group_by_case: dict[int, str] = {}
    for start, span, name in CASE_GROUPS:
        for index in range(start, start + span):
            thesis_case = proto_to_thesis.get(CASE_ORDER[index])
            if thesis_case is not None:
                group_by_case[thesis_case] = name
    groups = []
    for _start, _span, name in CASE_GROUPS:
        cases = [case for case in ALL_CASES if group_by_case.get(case) == name]
        if cases:
            groups.append((name, cases))
    return groups


def build_cells(stats: dict):
    groups = case_groups()
    rows: list[dict] = []
    group_starts: list[int] = []
    for group, cases in groups:
        group_starts.append(len(rows))
        for position, case in enumerate(cases):
            label = f"{case} {CASE_SHORT[THESIS_ORDER[case - 1]]}"
            if case in HIGHER_IS_BETTER:
                label += " ↑"
            if position == 0:
                label = f"{group} · {label}"
            rows.append({"kind": "case", "label": label,
                         "case": case, "group": group,
                         "group_start": position == 0})
    if len(rows) != len(ALL_CASES):
        raise SystemExit("benchmark category mapping does not cover all 26 cases")

    columns = [(rtos, board) for rtos in MATCHED_RTOS for board in BOARD_ORDER[rtos]]
    matrix = np.full((len(rows), len(columns)), np.nan)
    missing: list[dict] = []
    cells: list[dict] = []
    variants: dict[str, set] = {}

    for col, (rtos, board) in enumerate(columns):
        c_path = stats["paths"].get(f"{rtos}/{board}/bench", {})
        r_path = stats["paths"].get(f"{rtos}/{board}/rust", {})
        c_cases = c_path.get("cases", {})
        r_cases = r_path.get("cases", {})
        for row_index, row in enumerate(rows):
            case = row["case"]
            protocol = THESIS_ORDER[case - 1]
            c_record = c_cases.get(str(protocol))
            r_record = r_cases.get(str(protocol))
            base = {
                "rtos": rtos,
                "board": board,
                "case": case,
                "benchmark": CASE_SHORT[protocol],
                "protocol_case": protocol,
                "higher_is_better": case in HIGHER_IS_BETTER,
                "row": row_index,
                "column": col,
            }
            if not c_record or not r_record:
                missing.append({**base, "reason": "case not advertised by one or both paths"})
                continue
            c_variant = c_record.get("variant")
            r_variant = r_record.get("variant")
            if c_variant != r_variant:
                missing.append({
                    **base,
                    "reason": f"variant mismatch (C={c_variant}, Rust={r_variant})",
                })
                continue
            c_median = c_record.get("median")
            r_median = r_record.get("median")
            if c_median is None or r_median is None:
                missing.append({**base, "reason": "median unavailable"})
                continue
            if c_median <= 0 or r_median <= 0:
                missing.append({
                    **base,
                    "reason": f"non-positive median (C={c_median}, Rust={r_median})",
                })
                continue
            c_unit = c_record.get("unit")
            r_unit = r_record.get("unit")
            if c_unit != r_unit:
                missing.append({
                    **base,
                    "reason": f"unit mismatch (C={c_unit}, Rust={r_unit})",
                })
                continue
            ratio = r_median / c_median
            if not math.isfinite(ratio) or ratio <= 0:
                missing.append({**base, "reason": "ratio not finite and positive"})
                continue
            matrix[row_index, col] = math.log2(ratio)
            variants.setdefault(str(case), set()).add(c_variant)
            cells.append({
                **base,
                "variant": c_variant,
                "unit": c_unit,
                "c_median": c_median,
                "rust_median": r_median,
                "ratio": ratio,
            })

    return rows, columns, matrix, missing, cells, variants, group_starts


def draw(stats: dict, rows: list[dict], columns: list[tuple[str, str]],
         matrix: np.ndarray, missing: list[dict],
         group_starts: list[int]) -> float:
    finite = matrix[np.isfinite(matrix)]
    if not finite.size:
        raise SystemExit("no comparable ratios to plot")
    # A fixed, symmetric display range keeps 0.5 and 2.0 equally far from the
    # neutral centre.  A handful of jitter ratios fall outside 0.25--4; they
    # saturate at the scale limit and their actual values stay in provenance.
    limit = 2.0
    norm = TwoSlopeNorm(vcenter=0.0, vmin=-limit, vmax=limit)

    fig = plt.figure(figsize=(7.2, 7.4), facecolor="white")
    ax = fig.add_axes([0.36, 0.12, 0.50, 0.76])
    cax = fig.add_axes([0.885, 0.12, 0.022, 0.76])

    # Horizontal rules keep the seven benchmark categories visibly separate.
    for start in group_starts[1:]:
        ax.axhline(start - 0.5, color="#888888", lw=0.9, zorder=5)

    x_edges = np.arange(len(columns) + 1) - 0.5
    y_edges = np.arange(len(rows) + 1) - 0.5
    mesh = ax.pcolormesh(
        x_edges,
        y_edges,
        np.ma.masked_invalid(matrix),
        cmap=CMAP,
        norm=norm,
        shading="flat",
        edgecolors="white",
        linewidth=0.6,
        zorder=2,
    )

    # Missing cells are light grey with a cross, never a zero or a colour.
    for cell in missing:
        x, y = cell["column"], cell["row"]
        ax.add_patch(Rectangle(
            (x - 0.5, y - 0.5), 1.0, 1.0,
            facecolor="#ededed", edgecolor="#bdbdbd", linewidth=0.6, zorder=3,
        ))
        ax.text(x, y, "×", ha="center", va="center",
                fontsize=7.0, color="#666666", zorder=4)

    # Separate the three RTOS groups with a white gutter and a dark hairline.
    for boundary in (3.5, 7.5):
        ax.axvline(boundary, color="white", lw=3.0, zorder=5)
        ax.axvline(boundary, color="#555555", lw=0.8, zorder=6)

    ax.add_patch(Rectangle(
        (-0.5, -0.5), len(columns), len(rows),
        fill=False, edgecolor="#777777", linewidth=0.8, zorder=7,
    ))

    ax.set_xlim(-0.5, len(columns) - 0.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xticks(range(len(columns)))
    ax.set_xticklabels(
        [BOARD_LABEL[board] for _rtos, board in columns],
        fontsize=6.6, rotation=45, ha="right", rotation_mode="anchor",
    )
    ax.tick_params(axis="x", length=0, pad=2)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([row["label"] for row in rows], fontsize=6.8)
    for tick, row in zip(ax.get_yticklabels(), rows):
        if row.get("group_start"):
            tick.set_fontweight("bold")
    ax.tick_params(axis="y", length=0, pad=4)
    ax.tick_params(bottom=False, left=False)
    for spine in ax.spines.values():
        spine.set_visible(False)

    # RTOS headings and brackets above the board labels.
    for group_index, rtos in enumerate(MATCHED_RTOS):
        left = group_index * 4 - 0.45
        right = group_index * 4 + 3.45
        ax.plot(
            [left, right], [1.015, 1.015],
            transform=ax.get_xaxis_transform(),
            color="#555555", lw=0.8, clip_on=False,
        )
        ax.text(
            group_index * 4 + 1.5, 1.025,
            RTOS_LABEL[rtos],
            transform=ax.get_xaxis_transform(),
            ha="center", va="bottom",
            fontsize=8.2, fontweight="bold", clip_on=False,
        )

    ticks = [tick for tick in (0.25, 0.33, 0.5, 1.0, 2.0, 3.0, 4.0)
             if abs(math.log2(tick)) <= limit + 1e-9]
    cbar = fig.colorbar(mesh, cax=cax)
    cbar.set_ticks([math.log2(tick) for tick in ticks])
    cbar.set_ticklabels([f"{tick:g}" for tick in ticks])
    cbar.set_label("Rust / C median ratio (log scale)", fontsize=8, labelpad=8)
    cbar.ax.tick_params(labelsize=7, length=2, width=0.6)
    cbar.outline.set_linewidth(0.6)
    cbar.ax.text(0.5, 1.055, "Rust higher", transform=cbar.ax.transAxes,
                 ha="center", va="bottom", fontsize=6.5, color="#444444",
                 clip_on=False)
    cbar.ax.text(0.5, -0.055, "Rust lower", transform=cbar.ax.transAxes,
                 ha="center", va="top", fontsize=6.5, color="#444444",
                 clip_on=False)

    fig.text(
        0.36, 0.025,
        "×  not comparable (missing case, undefined ratio, or variant mismatch)",
        fontsize=6.8, color="#555555", ha="left", va="bottom",
    )

    metadata = {
        "Title": "Median Rust/C ratio for matched C and Rust applications",
        "Description": (
            f"Campaign bundle: {stats.get('bundle')}; "
            f"statistics SHA-256: {hashlib.sha256(STATS.read_bytes()).hexdigest()}; "
            "pooled-sample median Rust/C ratios for all 26 benchmark cases; "
            "throughput and zero-copy are higher-is-better."
        ),
        "Creator": "analysis/benchmark/performance.py",
        "Date": None,
    }
    fig.savefig(
        FIGURE,
        format="svg",
        bbox_inches="tight",
        facecolor="white",
        metadata=metadata,
    )
    plt.close(fig)
    return limit


def write_provenance(stats: dict, rows: list[dict], columns: list[tuple[str, str]],
                     missing: list[dict], cells: list[dict], variants: dict,
                     limit: float) -> None:
    stats_sha = hashlib.sha256(STATS.read_bytes()).hexdigest()
    payload = {
        "figure": str(FIGURE.relative_to(REPO_ROOT)),
        "generated_by": "analysis/benchmark/performance.py",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_statistics": str(STATS.relative_to(REPO_ROOT)),
        "source_statistics_sha256": stats_sha,
        "campaign_bundle": stats.get("bundle"),
        "campaign_bundle_sha256": stats.get("bundle_sha256"),
        "supplementary_bundles": stats.get("supplementary_bundles", []),
        "ratio": "pooled-sample median Rust / pooled-sample median C",
        "scale": {
            "transform": "log2",
            "vcenter": 1.0,
            "vmin": 2.0 ** -limit,
            "vmax": 2.0 ** limit,
        },
        "included_cases": ALL_CASES,
        "excluded_cases": [],
        "higher_is_better_cases": sorted(HIGHER_IS_BETTER),
        "lower_is_better_cases": [case for case in ALL_CASES
                                  if case not in HIGHER_IS_BETTER],
        "clipped_cells": [
            {
                "rtos": cell["rtos"], "board": cell["board"],
                "case": cell["case"], "ratio": cell["ratio"],
            }
            for cell in cells
            if cell["ratio"] < 2.0 ** -limit or cell["ratio"] > 2.0 ** limit
        ],
        "row_order": [row["label"] for row in rows],
        "columns": [
            {"rtos": RTOS_LABEL[rtos], "board": BOARD_LABEL[board]}
            for rtos, board in columns
        ],
        "variants_used": {
            case: sorted(values) for case, values in sorted(variants.items())
        },
        "cells": cells,
        "missing_cells": missing,
    }
    PROVENANCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    svg = FIGURE.read_text()
    comment = (
        "<!-- Generated by analysis/benchmark/performance.py; "
        f"campaign={stats.get('bundle')}; "
        f"statistics_sha256={stats_sha} -->\n"
    )
    if "Generated by analysis/benchmark/performance.py" not in svg:
        svg = svg.replace("<svg", comment + "<svg", 1)
        FIGURE.write_text(svg)


def main() -> int:
    FIGS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    stats = ensure_current()
    rows, columns, matrix, missing, cells, variants, group_starts = build_cells(stats)
    limit = draw(stats, rows, columns, matrix, missing, group_starts)
    write_provenance(stats, rows, columns, missing, cells, variants, limit)
    print(f"wrote {FIGURE.relative_to(REPO_ROOT)}")
    print(f"wrote {PROVENANCE.relative_to(REPO_ROOT)}")
    print(f"campaign={stats.get('bundle')}; "
          f"cells={len(cells)}; missing={len(missing)}; limit=2^{limit:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
