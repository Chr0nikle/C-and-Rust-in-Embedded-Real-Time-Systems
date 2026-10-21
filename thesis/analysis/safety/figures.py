from pathlib import Path
import re
import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["svg.hashsalt"] = "uastw-thesis"
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "thesis/generated/typ/safety-comparison.typ"
OUT = ROOT / "thesis/generated/figures/safety-pointer-roles.svg"

RTOS = ["FreeRTOS", "Zephyr", "NuttX"]
ROLES = ["C application", "C adapter", "Rust application", "Rust FFI boundary"]

def parse():
    data = {r: {} for r in RTOS}
    current = None
    for line in SRC.read_text(encoding="utf-8").splitlines():
        if "table.cell(rowspan: 4" in line:
            for r in RTOS:
                if f"[*{r}*]" in line:
                    current = r
                    break
        if current is None:
            continue
        cells = re.findall(r"\[([^\[\]]+)\]", line)
        if cells and cells[0] == f"*{current}*":
            cells = cells[1:]
        if cells and cells[0] in ROLES:
            data[current][cells[0]] = cells[1]
    return data

def span(v):
    m = re.fullmatch(r"(\d+)(?:[–-](\d+))?", v.strip())
    if not m:
        raise ValueError(v)
    a = int(m.group(1))
    b = int(m.group(2) or m.group(1))
    return a, b

data = parse()
fig, axes = plt.subplots(1, 2, figsize=(8.2, 3.4), sharey=True)
panels = [("C code", ["C application", "C adapter"]), ("Rust code", ["Rust application", "Rust FFI boundary"])]
colors = {"C application": "#4C78A8", "C adapter": "#F58518", "Rust application": "#4C78A8", "Rust FFI boundary": "#F58518"}
offsets = [0.14, -0.14]
ypos = {"FreeRTOS": 2, "Zephyr": 1, "NuttX": 0}

for ax, (title, roles) in zip(axes, panels):
    for r in RTOS:
        y = ypos[r]
        for role, off in zip(roles, offsets):
            lo, hi = span(data[r][role])
            mid = (lo + hi) / 2
            err = (hi - lo) / 2
            ax.errorbar(mid, y + off, xerr=err if err else None, fmt="o", color=colors[role], capsize=3, markersize=5, linewidth=1.4)
            ax.annotate(data[r][role], (hi, y + off), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8.5)
    ax.set_title(title, fontsize=11)
    ax.set_xlabel("Resolved dereference occurrences", fontsize=9)
    ax.set_xlim(-5, 190)
    ax.set_xticks([0, 50, 100, 150])
    ax.grid(axis="x", color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0)

axes[0].set_yticks([2, 1, 0], RTOS)
axes[1].tick_params(axis="y", labelleft=True)

fig.legend(handles=[Line2D([], [], marker="o", linestyle="none", color="#4C78A8", label="Application"), Line2D([], [], marker="o", linestyle="none", color="#F58518", label="RTOS adapter / FFI boundary")], loc="upper center", bbox_to_anchor=(0.5, 1.02), ncol=2, frameon=False, fontsize=9)
fig.tight_layout(rect=(0, 0, 1, 0.92))
OUT.parent.mkdir(parents=True, exist_ok=True)
fig.savefig(OUT, format="svg", bbox_inches="tight", facecolor="white", metadata={"Date": None})
print(f"wrote {OUT}")
