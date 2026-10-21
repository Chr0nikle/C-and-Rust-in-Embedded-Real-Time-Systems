from pathlib import Path
import re
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "thesis/generated/typ/safety-comparison.typ"
OUTPUT = ROOT / "thesis/generated/figures"
RTOS = ("FreeRTOS", "Zephyr", "NuttX")
ROLES = ("Rust application", "Rust FFI boundary")

def read_data():
    data = {name: {} for name in RTOS}
    current = None
    for line in SOURCE.read_text(encoding="utf-8").splitlines():
        for name in RTOS:
            if f"[*{name}*]" in line:
                current = name
                break
        if current is None:
            continue
        cells = re.findall(r"\[([^\[\]]+)\]", line)
        if cells and cells[0] == f"*{current}*":
            cells = cells[1:]
        if cells and cells[0] in ROLES:
            if len(cells) < 6:
                raise ValueError(f"Invalid row: {line}")
            num, den = map(int, cells[5].split("/"))
            data[current][cells[0]] = (int(cells[1]), num, den)
    if any(set(data[name]) != set(ROLES) for name in RTOS):
        raise ValueError("Missing Rust source-role rows in safety-comparison.typ")
    return data

def style(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.grid(axis="x", alpha=0.2)
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)

def main():
    data = read_data()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    matplotlib.rcParams["svg.hashsalt"] = "uastw-thesis"
    y = np.arange(len(RTOS))
    app = [data[r]["Rust application"][0] for r in RTOS]
    ffi = [data[r]["Rust FFI boundary"][0] for r in RTOS]

    fig, ax = plt.subplots(figsize=(7.2, 2.8))
    ax.barh(y - 0.20, app, height=0.34, label="Rust application")
    ax.barh(y + 0.20, ffi, height=0.34, label="Rust FFI boundary")
    for i, (a, b) in enumerate(zip(app, ffi)):
        ax.text(max(3, a + 3), i - 0.20, str(a), va="center", fontsize=9)
        ax.text(b + 3, i + 0.20, str(b), va="center", fontsize=9)
    ax.set_yticks(y, RTOS)
    ax.invert_yaxis()
    ax.set_xlim(0, max(ffi) * 1.20)
    ax.set_xlabel("Resolved raw-pointer dereferences")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.23), ncol=2, frameon=False)
    style(ax)
    fig.tight_layout()
    fig.savefig(OUTPUT / "safety-rust-pointer-roles.svg", metadata={"Date": None})
    plt.close(fig)

    percentages = [100 * data[r]["Rust application"][1] / data[r]["Rust application"][2] for r in RTOS]
    fig, ax = plt.subplots(figsize=(7.2, 2.6))
    ax.barh(y, percentages, height=0.54)
    for i, r in enumerate(RTOS):
        _, numerator, denominator = data[r]["Rust application"]
        ax.text(percentages[i] - 2, i, f"{percentages[i]:.1f}% ({numerator}/{denominator})",
                ha="right", va="center", color="white", fontsize=9)
    ax.set_yticks(y, RTOS)
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xlabel("Resolved application records in unsafe contexts (%)")
    style(ax)
    fig.tight_layout()
    fig.savefig(OUTPUT / "safety-rust-unsafe-context.svg", metadata={"Date": None})
    plt.close(fig)
    print("Generated safety-rust-pointer-roles.svg and safety-rust-unsafe-context.svg")

if __name__ == "__main__":
    main()
