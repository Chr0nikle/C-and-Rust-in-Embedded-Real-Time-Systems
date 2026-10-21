#!/usr/bin/env python3
"""Static flash and RAM differences between matched Rust and C firmware.

The authoritative source is ``analysis/generated/campaign-static.typ``, produced
by ``campaign.py`` from the newest archived run with a static
footprint for each path.  The figure plots, for each of the twelve matched
FreeRTOS/NuttX/Zephyr configurations:

* panel A: Rust static flash minus C static flash, in KiB;
* panel B: Rust static RAM minus C static RAM, in KiB.

Positive values mean Rust uses more memory, negative values mean C does.  The
static footprint is ELF section-based, summed over every image in the run
record, and excludes bootloaders and partition tables.  RAM here is linked
static RAM, not runtime heap high-water or stack high-water.

The Rust-native kernels are deliberately absent: FreeRTOS-in-Rust and Rivet do
not have a matched C application on the same kernel, so no like-for-like
difference can be formed.

Run from the thesis root with the repository's analysis virtual environment::

    <project-root>/.local/venv/bin/python3 analysis/benchmark/memory.py
"""

from __future__ import annotations

import hashlib
import json
import re
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
STATIC = TYP / "campaign-static.typ"
FIGURE = FIGS / "memory-footprint.svg"
PROVENANCE = DATA / "memory-footprint.provenance.json"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from performance import ensure_current  # noqa: E402

POSITIVE_COLOUR = "#D55E00"   # Rust uses more memory
NEGATIVE_COLOUR = "#0072B2"   # C uses more memory

RTOS_ORDER = [("freertos", "FreeRTOS"), ("nuttx", "NuttX"), ("zephyr", "Zephyr")]
BOARD_ORDER = [
    ("stm32f4", "STM32F4"),
    ("stm32h723", "STM32H723"),
    ("esp32c6", "ESP32-C6"),
    ("nrf52840dk", "nRF52840"),
]
BOARD_MAP = {label: board for board, label in BOARD_ORDER}
RTOS_MAP = {label: rtos for rtos, label in RTOS_ORDER}

ROW_RE = re.compile(
    r"^\s*\[(.+?)\],\s*\[([0-9.]+|-)\],\s*\[([0-9.]+|-)\],\s*$"
)
GROUP_RE = re.compile(r"table\.cell\(colspan:\s*3.*?\*([^*]+)\*")

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 8,
    "axes.linewidth": 0.6,
    "svg.fonttype": "path",
    "svg.hashsalt": "uastw-thesis",
})


def parse_table() -> tuple[dict[tuple[str, str, str], tuple[float, float]], int]:
    """Parse the generated static-footprint table without copying values."""
    values: dict[tuple[str, str, str], tuple[float, float]] = {}
    parsed = 0
    current_rtos = None
    for line in STATIC.read_text().splitlines():
        group = GROUP_RE.search(line)
        if group:
            group_label = group.group(1)
            current_rtos = "rust-native" if group_label == "Rust-native" else RTOS_MAP[group_label]
            continue
        match = ROW_RE.match(line)
        if not match:
            continue
        label, flash, ram = match.groups()
        if flash == "-" or ram == "-":
            continue
        if label.startswith("FreeRTOS-in-Rust "):
            rtos = "freertos-in-rust"
            remainder = label[len("FreeRTOS-in-Rust "):]
            board_label, language = (
                remainder.rsplit(" ", 1) if remainder.rsplit(" ", 1)[-1] in ("C", "Rust")
                else (remainder, "Rust")
            )
        elif label.startswith("Rivet RTOS "):
            rtos = "rivet"
            remainder = label[len("Rivet RTOS "):]
            board_label, language = (
                remainder.rsplit(" ", 1) if remainder.rsplit(" ", 1)[-1] in ("C", "Rust")
                else (remainder, "Rust")
            )
        else:
            if current_rtos is None:
                match = re.match(r"^(FreeRTOS|NuttX|Zephyr)\s+(.+)$", label)
                if not match:
                    raise SystemExit("static table row without an RTOS group")
                rtos = RTOS_MAP[match.group(1)]
                remainder = match.group(2)
            else:
                rtos = current_rtos
                remainder = label
            board_label, language = remainder.rsplit(" ", 1)
        board = BOARD_MAP[board_label]
        key = (rtos, board, language)
        if key in values:
            raise SystemExit(f"duplicate static-footprint row for {key}")
        values[key] = (float(flash), float(ram))
        parsed += 1
    return values, parsed


def validate_table(values: dict, parsed: int) -> None:
    expected = {
        (rtos, board, language)
        for rtos, _label in RTOS_ORDER
        for board, _board_label in BOARD_ORDER
        for language in ("C", "Rust")
    }
    expected |= {
        ("freertos-in-rust", "stm32f4", "Rust"),
        ("freertos-in-rust", "stm32h723", "Rust"),
        ("rivet", "stm32f4", "Rust"),
    }
    if parsed != 27 or set(values) != expected:
        raise SystemExit(
            f"static table mismatch: parsed={parsed}, "
            f"missing={sorted(expected - set(values))}, "
            f"extra={sorted(set(values) - expected)}"
        )


def build_rows(values: dict[tuple[str, str, str], tuple[float, float]]):
    rows: list[dict] = []
    differences: list[dict] = []
    for rtos, rtos_label in RTOS_ORDER:
        for index, (board, board_label) in enumerate(BOARD_ORDER):
            c_key = (rtos, board, "C")
            rust_key = (rtos, board, "Rust")
            if c_key not in values or rust_key not in values:
                raise SystemExit(f"missing matched footprint for {c_key} or {rust_key}")
            c_flash, c_ram = values[c_key]
            rust_flash, rust_ram = values[rust_key]
            label = f"{rtos_label} · {board_label}" if index == 0 else board_label
            flash_diff = round(rust_flash - c_flash, 3)
            ram_diff = round(rust_ram - c_ram, 3)
            rows.append({
                "label": label,
                "flash_diff": flash_diff,
                "ram_diff": ram_diff,
                "rtos": rtos,
                "board": board,
            })
            differences.append({
                "rtos": rtos,
                "board": board,
                "c_flash_kib": c_flash,
                "rust_flash_kib": rust_flash,
                "flash_difference_kib": flash_diff,
                "c_ram_kib": c_ram,
                "rust_ram_kib": rust_ram,
                "ram_difference_kib": ram_diff,
            })

    excluded = [
        {"rtos": "freertos-in-rust", "board": "stm32f4",
         "reason": "no matched C application on the same kernel"},
        {"rtos": "freertos-in-rust", "board": "stm32h723",
         "reason": "no matched C application on the same kernel"},
        {"rtos": "rivet", "board": "stm32f4",
         "reason": "no matched C application on the same kernel"},
    ]
    return rows, differences, excluded


def draw(stats: dict, rows: list[dict]) -> None:
    count = len(rows)
    y_positions = [count - 1 - index for index in range(count)]
    labels = [row["label"] for row in rows]

    fig, axes = plt.subplots(
        1, 2, figsize=(7.2, 5.1), sharey=True,
        gridspec_kw={"wspace": 0.08},
    )
    fig.patch.set_facecolor("white")

    panels = [
        (axes[0], "flash_diff", "A  Static flash difference"),
        (axes[1], "ram_diff", "B  Static RAM difference"),
    ]
    for ax, key, title in panels:
        values = [row[key] for row in rows]
        low = min(0.0, min(values))
        high = max(0.0, max(values))
        span = max(high - low, 1.0)
        x_min = low - span * 0.08
        x_max = high + span * 0.10

        colours = [POSITIVE_COLOUR if value >= 0 else NEGATIVE_COLOUR
                   for value in values]
        ax.barh(y_positions, values, height=0.55, color=colours,
                edgecolor="white", linewidth=0.4, zorder=3)
        ax.axvline(0.0, color="#555555", linewidth=0.9, zorder=4)

        # Separators between the three RTOS groups.
        for boundary in (3.5, 7.5):
            ax.axhline(boundary, color="#999999", lw=0.8, zorder=4)

        ax.set_title(title, loc="left", fontsize=9.7, fontweight="bold", pad=8)
        ax.set_xlabel("Rust − C (KiB)", fontsize=8.1)
        ax.set_xlim(x_min, x_max)
        ax.set_ylim(-0.6, count - 0.4)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
        ax.xaxis.grid(True, color="#dddddd", linewidth=0.6, zorder=0)
        ax.set_axisbelow(True)
        ax.tick_params(axis="x", labelsize=7.5, length=2, width=0.6)
        ax.tick_params(axis="y", length=0)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color("#999999")

    axes[0].set_yticks(y_positions)
    axes[0].set_yticklabels(labels, fontsize=7.7, fontweight="normal")
    axes[1].tick_params(axis="y", left=False, labelleft=False)

    handles = [
        Patch(facecolor=POSITIVE_COLOUR, edgecolor="white", linewidth=0.4,
              label="Rust higher"),
        Patch(facecolor=NEGATIVE_COLOUR, edgecolor="white", linewidth=0.4,
              label="C higher"),
    ]
    fig.legend(
        handles=handles, loc="upper center", ncol=2, frameon=False,
        bbox_to_anchor=(0.5, 1.0), fontsize=7.8,
        handlelength=1.0, columnspacing=1.4,
    )
    fig.text(
        0.01, 0.015,
        "ELF section differences (Rust − C); positive = Rust uses more, "
        "negative = C uses more.\n"
        "RAM is linked static RAM, not runtime heap or stack.",
        fontsize=6.7, color="#666666", ha="left", va="bottom", linespacing=1.25,
    )
    fig.subplots_adjust(left=0.28, right=0.985, top=0.83, bottom=0.18)

    metadata = {
        "Title": "Static flash and RAM differences between matched Rust and C firmware",
        "Description": (
            f"Campaign bundle: {stats.get('bundle')}; "
            f"static table SHA-256: {hashlib.sha256(STATIC.read_bytes()).hexdigest()}; "
            "Rust minus C, ELF section-based static footprint in KiB."
        ),
        "Creator": "analysis/benchmark/memory.py",
        "Date": None,
    }
    fig.savefig(FIGURE, format="svg", bbox_inches="tight", facecolor="white",
                metadata=metadata)
    plt.close(fig)


def write_provenance(stats: dict, rows: list[dict], differences: list[dict],
                     excluded: list[dict], parsed: int) -> None:
    table_sha = hashlib.sha256(STATIC.read_bytes()).hexdigest()
    payload = {
        "figure": str(FIGURE.relative_to(REPO_ROOT)),
        "generated_by": "analysis/benchmark/memory.py",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_table": str(STATIC.relative_to(REPO_ROOT)),
        "source_table_sha256": table_sha,
        "campaign_bundle": stats.get("bundle"),
        "campaign_bundle_sha256": stats.get("bundle_sha256"),
        "supplementary_bundles": stats.get("supplementary_bundles", []),
        "definitions": {
            "flash": "ELF section-based static flash, sum of all images in the "
                     "run record, KiB; excludes bootloaders and partition tables",
            "ram": "ELF section-based static RAM, sum of all images in the run "
                   "record, KiB; linked static RAM, not runtime heap or stack",
            "difference": "matched Rust minus matched C for the same RTOS and board",
        },
        "matched_configurations": len(differences),
        "table_rows_parsed": parsed,
        "differences": differences,
        "excluded": excluded,
        "validation": {
            "all_values_from_table": True,
            "matched_pairs": len(differences),
            "plotted_bars": len(rows) * 2,
            "no_unavailable_as_zero": True,
        },
    }
    PROVENANCE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    svg = FIGURE.read_text()
    comment = (
        "<!-- Generated by analysis/benchmark/memory.py; "
        f"campaign={stats.get('bundle')}; table_sha256={table_sha} -->\n"
    )
    if "Generated by analysis/benchmark/memory.py" not in svg:
        svg = svg.replace("<svg", comment + "<svg", 1)
        FIGURE.write_text(svg)


def main() -> int:
    FIGS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    stats = ensure_current()
    values, parsed = parse_table()
    validate_table(values, parsed)
    rows, differences, excluded = build_rows(values)
    draw(stats, rows)
    write_provenance(stats, rows, differences, excluded, parsed)
    print(f"wrote {FIGURE.relative_to(REPO_ROOT)}")
    print(f"wrote {PROVENANCE.relative_to(REPO_ROOT)}")
    print(f"campaign={stats.get('bundle')}; pairs={len(differences)}; "
          f"bars={len(rows) * 2}; excluded={len(excluded)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
