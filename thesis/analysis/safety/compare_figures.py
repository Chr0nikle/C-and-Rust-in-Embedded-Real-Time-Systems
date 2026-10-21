from pathlib import Path
import re
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'thesis/generated/typ/safety-comparison.typ'
OUT = ROOT / 'thesis/generated/figures'
OUT.mkdir(parents=True, exist_ok=True)
rtos = ['FreeRTOS', 'Zephyr', 'NuttX']
roles = ['C application', 'C adapter', 'Rust application', 'Rust FFI boundary']
rows = {r: {} for r in rtos}
current = None
for line in SRC.read_text(encoding='utf-8').splitlines():
    if 'table.cell(rowspan: 4' in line:
        for r in rtos:
            if f'[*{r}*]' in line:
                current = r
                break
    if current is None:
        continue
    cells = re.findall(r'\[([^\[\]]+)\]', line)
    if cells and cells[0] == f'*{current}*':
        cells = cells[1:]
    if cells and cells[0] in roles:
        rows[current][cells[0]] = cells[1]
for r in rtos:
    for role in roles:
        if role not in rows[r]:
            raise ValueError(f'Missing {r}/{role} from {SRC}')

def bounds(value):
    parts = re.split('[–-]', value)
    return int(parts[0]), int(parts[-1])

plt.rcParams.update({'font.size': 10, 'svg.fonttype': 'none', 'axes.spines.top': False, 'axes.spines.right': False})
fig, axs = plt.subplots(1, 2, figsize=(10, 4), sharey=True)
for ax, title, group, limit in zip(axs, ['C source operations', 'Rust raw-pointer operations'], [roles[:2], roles[2:]], [160, 200]):
    for i, role in enumerate(group):
        for j, system in enumerate(rtos):
            low, high = bounds(rows[system][role])
            y = 2-j + (0.17 if i == 0 else -0.17)
            color = '#3977a5' if i == 0 else '#e09348'
            ax.barh(y, high, height=0.27, color=color, alpha=0.8)
            if high > low:
                ax.plot([low, high], [y, y], color='#202020', linewidth=2)
                ax.plot([low, low], [y-.065, y+.065], color='#202020', linewidth=1.3)
            ax.text(high+3, y, rows[system][role], ha='left', va='center', fontsize=9)
    ax.set_title(title)
    ax.set_yticks([2,1,0], rtos)
    ax.set_xlim(0,limit)
    ax.set_xlabel('Resolved dereference occurrences')
    ax.grid(axis='x', alpha=.2)
    ax.set_axisbelow(True)
axs[0].legend(['Application', 'Adapter / FFI boundary'], loc='upper center', bbox_to_anchor=(.5,1.19), frameon=False, ncol=2)
fig.subplots_adjust(top=.78, wspace=.35, bottom=.18)
p1 = OUT / 'safety-c-rust-source-roles.svg'
fig.savefig(p1, bbox_inches='tight')
plt.close(fig)

fig, ax = plt.subplots(figsize=(8.2, 3.1))
names = ['FreeRTOS: Rust application', 'FreeRTOS: Rust FFI', 'FreeRTOS-in-Rust: project code']
values = [(403,404), (241,241), (176,181)]
for y, (name, (num, den)) in enumerate(zip(names, values)):
    pct = num / den * 100
    ax.barh(y, pct, height=.55, color=['#3977a5','#e09348','#6b9a86'][y])
    ax.text(min(pct-2,98), y, f'{pct:.1f}% ({num}/{den})', ha='right', va='center', color='white', fontsize=10, fontweight='bold')
ax.set_yticks(range(3), names)
ax.invert_yaxis()
ax.set_xlim(0, 100)
ax.set_xlabel('Recorded operations in unsafe contexts (%)')
ax.grid(axis='x', alpha=.2)
ax.set_axisbelow(True)
fig.subplots_adjust(left=.36, right=.96, bottom=.23, top=.9)
p2 = OUT / 'safety-freertos-vs-fir-unsafe.svg'
fig.savefig(p2, bbox_inches='tight')
plt.close(fig)
print('Created:',p1,'and',p2,sep='\n')
print('Note: FreeRTOS-in-Rust 176/181 is the Appendix D project-code figure; its external kernel is excluded.')
