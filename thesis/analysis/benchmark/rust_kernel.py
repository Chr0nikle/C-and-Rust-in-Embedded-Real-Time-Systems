#!/usr/bin/env python3
"""Matched-kernel latency comparison for the Rust-native RTOS paths.

The figure compares the C and Rust applications on FreeRTOS with the
Rust-native FreeRTOS-in-Rust and Rivet kernels on STM32F4 for three latency
cases:

* case 6  -- task handoff
* case 8  -- preemption
* case 11 -- task notification

Values are pooled-sample medians from
``analysis/generated/replication-stats.json``.  The newest completed campaign
contains the matched FreeRTOS paths; the Rust-native paths are retained from
the latest campaign that measured them, recorded under
``supplementary_bundles``.  The script verifies that every bar uses the
microsecond unit and the same benchmark variant, and reports unavailable or
non-equivalent measurements instead of substituting values.

Run from the thesis root with the repository's analysis virtual environment::

    <project-root>/.local/venv/bin/python3 analysis/benchmark/rust_kernel.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import MaxNLocator

HERE = Path(__file__).resolve().parent
GENERATED = HERE.parents[1] / "generated"
TYP = GENERATED / "typ"
DATA = GENERATED / "data"
FIGS = GENERATED / "figures"
PROJECT_ROOT = HERE.parents[2] / "project"
REPO_ROOT = PROJECT_ROOT.parent
STATS = DATA / "replication-stats.json"
FIGURE = FIGS / "rust-kernel-comparison.svg"
PROVENANCE = DATA / "rust-kernel-comparison.provenance.json"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from performance import ensure_current  # noqa: E402
from campaign import THESIS_ORDER  # noqa: E402

# Okabe-Ito colours: blue, bluish green and vermillion.
COLOURS = {
    "FreeRTOS (C application)": "#56B4E9",
    "FreeRTOS (Rust application)": "#0072B2",
    "FreeRTOS-in-Rust": "#009E73",
    "Rivet": "#D55E00",
}

CASE_LABELS = {
    6: "Task handoff (6)",
    8: "Preemption (8)",
    11: "Notification latency (11)",
}
CASE_PROTOCOL = {case: THESIS_ORDER[case - 1] for case in CASE_LABELS}

PANELS = [
    {
        "board": "stm32f4",
        "title": "STM32F4",
        "kernels": [
            ("FreeRTOS (C application)", "freertos/stm32f4/bench"),
            ("FreeRTOS (Rust application)", "freertos/stm32f4/rust"),
            ("FreeRTOS-in-Rust", "freertos-in-rust/stm32f4/rust"),
            ("Rivet", "rivet/stm32f4/rust"),
        ],
    },
]

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8,
    "axes.linewidth": 0.6,
    "svg.fonttype": "path",
    "svg.hashsalt": "uastw-thesis",
})


def source_bundle(stats: dict, path: str) -> str:
    for bundle in stats.get("supplementary_bundles", []):
        if path in bundle.get("paths", []):
            return bundle.get("bundle", "unknown")
    return stats.get("bundle", "unknown")


def collect(stats: dict):
    """Return usable bars and an explicit list of excluded measurements."""
    bars: list[dict] = []
    excluded: list[dict] = []

    for panel in PANELS:
        for case, label in CASE_LABELS.items():
            protocol = CASE_PROTOCOL[case]
            candidates: list[dict] = []
            for kernel, path in panel["kernels"]:
                record = stats.get("paths", {}).get(path, {}).get("cases", {}).get(
                    str(protocol)
                )
                base = {
                    "panel": panel["title"],
                    "board": panel["board"],
                    "kernel": kernel,
                    "path": path,
                    "case": case,
                    "benchmark": label,
                    "protocol_case": protocol,
                }
                if not record:
                    excluded.append({
                        **base,
                        "reason": "path does not advertise this case",
                    })
                    continue
                if record.get("unit") != "us":
                    excluded.append({
                        **base,
                        "reason": f"non-microsecond unit: {record.get('unit')}",
                    })
                    continue
                median = record.get("median")
                if median is None or median <= 0:
                    excluded.append({**base, "reason": "missing or non-positive median"})
                    continue
                candidates.append({
                    **base,
                    "median": float(median),
                    "variant": record.get("variant"),
                    "unit": record.get("unit"),
                    "source_bundle": source_bundle(stats, path),
                })

            variants = {bar["variant"] for bar in candidates}
            if len(variants) > 1:
                for bar in candidates:
                    excluded.append({
                        **bar,
                        "reason": f"variant mismatch: {sorted(variants, key=str)}",
                    })
                continue
            bars.extend(candidates)

    # The three cases must also use one variant across the two boards.
    for case in CASE_LABELS:
        case_bars = [bar for bar in bars if bar["case"] == case]
        variants = {bar["variant"] for bar in case_bars}
        if len(variants) > 1:
            for bar in case_bars:
                excluded.append({**bar, "reason": "cross-panel variant mismatch"})
            bars = [bar for bar in bars if bar["case"] != case]
    return bars, excluded


def draw(stats: dict, bars: list[dict]) -> None:
    if not bars:
        raise SystemExit("no equivalent latency measurements to plot")

    positions = {6: 2.0, 8: 1.0, 11: 0.0}
    max_median = max(bar["median"] for bar in bars)
    x_max = max_median * 1.16

    fig, ax = plt.subplots(figsize=(6.6, 3.2))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    panel = PANELS[0]
    step = 0.22
    centre = step * (len(panel["kernels"]) - 1) / 2.0
    offsets = [centre - index * step for index in range(len(panel["kernels"]))]
    kernel_offset = {
        kernel: offset for (kernel, _path), offset in zip(panel["kernels"], offsets)
    }

    for bar in bars:
        y = positions[bar["case"]] + kernel_offset[bar["kernel"]]
        ax.barh(
            y, bar["median"], height=0.18,
            color=COLOURS[bar["kernel"]], edgecolor="white", linewidth=0.6,
            zorder=3,
        )
        ax.text(
            bar["median"] + x_max * 0.012, y, f"{bar['median']:.1f}",
            va="center", ha="left", fontsize=7.0, color="#333333", zorder=4,
        )

    ax.set_title(panel["title"], loc="left", fontsize=9.0,
                 fontweight="bold", pad=6)
    ax.set_ylim(-0.55, 2.55)
    ax.set_xlim(0, x_max)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.xaxis.grid(True, color="#d9d9d9", linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(axis="x", labelsize=7.5, length=2, width=0.6)
    ax.tick_params(axis="y", length=0)
    ax.set_yticks([2.0, 1.0, 0.0])
    ax.set_yticklabels(
        [CASE_LABELS[6], CASE_LABELS[8], CASE_LABELS[11]], fontsize=8.0,
    )
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#999999")

    handles = [
        Patch(facecolor=COLOURS[label], edgecolor="white", linewidth=0.6, label=label)
        for label in ("FreeRTOS (C application)", "FreeRTOS (Rust application)",
                      "FreeRTOS-in-Rust", "Rivet")
    ]
    fig.legend(
        handles=handles, loc="upper center", ncol=4, frameon=False,
        bbox_to_anchor=(0.5, 1.0), fontsize=7.0,
        handlelength=1.0, columnspacing=1.0,
    )
    fig.supxlabel("Median latency (µs)", fontsize=8.0, y=0.075)
    fig.text(
        0.01, 0.015,
        "Rust-native bars: latest retained run; FreeRTOS C/Rust: newest campaign.",
        fontsize=6.2, color="#666666", ha="left", va="bottom",
    )
    fig.subplots_adjust(left=0.245, right=0.98, top=0.78, bottom=0.22)

    metadata = {
        "Title": "Matched-kernel latency comparison for Rust RTOS paths",
        "Description": (
            f"Campaign bundle: {stats.get('bundle')}; "
            f"statistics SHA-256: {hashlib.sha256(STATS.read_bytes()).hexdigest()}; "
            "pooled-sample medians for task handoff (6), preemption (8) and "
            "notification (11)."
        ),
        "Creator": "analysis/benchmark/rust_kernel.py",
        "Date": None,
    }
    fig.savefig(
        FIGURE, format="svg", bbox_inches="tight", facecolor="white",
        metadata=metadata,
    )
    plt.close(fig)


def write_provenance(stats: dict, bars: list[dict], excluded: list[dict]) -> None:
    stats_sha = hashlib.sha256(STATS.read_bytes()).hexdigest()
    payload = {
        "figure": str(FIGURE.relative_to(REPO_ROOT)),
        "generated_by": "analysis/benchmark/rust_kernel.py",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_statistics": str(STATS.relative_to(REPO_ROOT)),
        "source_statistics_sha256": stats_sha,
        "campaign_bundle": stats.get("bundle"),
        "campaign_bundle_sha256": stats.get("bundle_sha256"),
        "supplementary_bundles": stats.get("supplementary_bundles", []),
        "cases": [
            {"thesis_case": case, "protocol_case": CASE_PROTOCOL[case], "label": label}
            for case, label in CASE_LABELS.items()
        ],
        "panels": [
            {"board": panel["board"], "title": panel["title"],
             "kernels": [kernel for kernel, _path in panel["kernels"]]}
            for panel in PANELS
        ],
        "measurement": "pooled-sample median, microseconds",
        "variants": sorted({bar["variant"] for bar in bars}, key=str),
        "units": sorted({bar["unit"] for bar in bars}),
        "bars": bars,
        "unavailable": [],
        "excluded_measurements": excluded,
    }
    PROVENANCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    svg = FIGURE.read_text()
    comment = (
        "<!-- Generated by analysis/benchmark/rust_kernel.py; "
        f"campaign={stats.get('bundle')}; "
        f"statistics_sha256={stats_sha} -->\n"
    )
    if "Generated by analysis/benchmark/rust_kernel.py" not in svg:
        svg = svg.replace("<svg", comment + "<svg", 1)
        FIGURE.write_text(svg)


def main() -> int:
    FIGS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    stats = ensure_current()
    bars, excluded = collect(stats)
    draw(stats, bars)
    write_provenance(stats, bars, excluded)
    print(f"wrote {FIGURE.relative_to(REPO_ROOT)}")
    print(f"wrote {PROVENANCE.relative_to(REPO_ROOT)}")
    print(f"campaign={stats.get('bundle')}; bars={len(bars)}; "
          f"excluded={len(excluded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
