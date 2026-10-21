# Scientific evidence audit

Audit date: 2026-10-21. Scope: every generated Typst table and figure included
by `thesis/sections/`. Hand-authored diagrams in `thesis/figures/` and the
manuscript text are not treated as generated results.

Selected evidence: `campaign-replication-20261019T214853Z`, pinned in
`thesis/analysis/campaign.lock.json`. One supplementary historical evidence
source is pinned there: `rust-native-timing` from
`campaign-replication-20261014T075134Z`, whose raw bundle is missing.

| Output | Input evidence | Campaign ID | Generator | Reproducible | Issue |
|---|---|---|---|---|---|
| `generated/typ/all-results.typ` | locked campaign + preserved derived statistics for 3 Rust-native paths | `20261019T214853Z` + supplementary `20261014T075134Z` | `thesis/analysis/benchmark/tables.py` | From locked campaign + preserved derived data | Raw supplementary bundle missing |
| `generated/typ/campaign-coverage.typ` | historical published 27-row table | none | `thesis/analysis/benchmark/campaign.py` | Historical only | No raw evidence for 3 native rows |
| `generated/typ/campaign-memory.typ` | historical published 27-row table | none | `thesis/analysis/benchmark/campaign.py` | Historical only | No raw evidence for 3 native rows |
| `generated/typ/campaign-static.typ` | locked campaign matched rows + historical native rows | `20261019T214853Z` + historical | `thesis/analysis/benchmark/campaign.py` | Mixed | Native rows preserved; see `generated/data/static-evidence.json` |
| `generated/typ/complexity.typ` | project source tree | n/a | `thesis/analysis/integration/complexity.py` | Yes | Analyzer revision is dirty in working tree |
| `generated/typ/duplication.typ` | project source tree | n/a | `thesis/analysis/integration/duplication.py` | Yes | Analyzer revision is dirty in working tree |
| `generated/typ/integration-surface.typ` | project source tree | n/a | `thesis/analysis/integration/surface.py` | Yes | Analyzer revision is dirty in working tree |
| `generated/typ/safety-*.typ` (6 files) | `results/safety/generated_inventory` + `generated_outputs` | n/a | `project/safety/tables.py` | With preserved local safety evidence | Safety evidence is excluded from Git; hashes pinned in lock |
| `generated/figures/fig-integration.svg` | project source tree | n/a | `thesis/analysis/integration/surface.py` | Yes | Analyzer revision is dirty |
| `generated/figures/memory-footprint.svg` | historical static table | none | `thesis/analysis/benchmark/memory.py` | Historical only | Figure preserved because static rows are mixed |
| `generated/figures/performance-comparison.svg` | locked campaign | `20261019T214853Z` | `thesis/analysis/benchmark/performance.py` | Yes | None for matched paths |
| `generated/figures/rust-kernel-comparison.svg` | locked campaign + preserved derived stats | `20261019T214853Z` + supplementary `20261014T075134Z` | `thesis/analysis/benchmark/rust_kernel.py` | From locked campaign + preserved data | Raw supplementary bundle missing |

## Evidence classes

- **Fully reproducible from the locked campaign:** the performance comparison
  and the matched-path portions of the appendix tables.
- **Reproducible with preserved derived evidence:** `all-results.typ`,
  `rust-kernel-comparison.svg` and the static/memory/coverage native rows. The
  original `campaign-replication-20261014T075134Z` raw bundle is absent; only
  `thesis/generated/data/replication-stats.json` and the published outputs
  remain.
- **Requires local safety evidence:** the six safety tables. The hashes of
  `results/safety/generated_inventory` and `generated_outputs` are pinned in
  `campaign.lock.json`; a clean checkout intentionally lacks them.
- **Reproducible from source:** complexity, duplication and integration-surface
  outputs. They depend on the selected project source revision, not a campaign.
- **Historical only:** `campaign-coverage.typ`, `campaign-memory.typ`,
  `campaign-static.typ` native rows and `memory-footprint.svg`. These are
  preserved unchanged rather than synthesized.

## Automated checks

`thesis/analysis/tests/test_evidence_traceability.py` enforces:

- every generated `.typ` and `.svg` has an entry in `evidence-map.json`;
- every mapped file and generator exists;
- every generated `#include`/`image()` resolves and is mapped;
- the lock contains benchmark, supplementary and safety evidence;
- safety evidence hashes match when the local evidence is present.

`thesis/analysis/tests/test_static_evidence.py` enforces the mixed-static
regression guard. `thesis/analysis/tests/test_selection.py` enforces explicit
campaign selection and the supplementary evidence declaration.

## Limitations

The missing `campaign-replication-20261014T075134Z` bundle prevents raw
reproduction of the three Rust-native timing paths and the historical static
table. Resolving it requires restoring that bundle or re-running the native
configurations on hardware and updating the lock and evidence map deliberately.

## Safety reproducibility discrepancies

### Profile determinism (metadata drift)

Stored profiles were generated at campaign revision `9fc56aa0`; the current
checkout is `d4cf8483`. Regeneration differs only in:

- `generator.source_revision`;
- `source_configs.c_benchmark.build_id`;
- `source_configs.c_benchmark.replace_build_id`;
- `source_configs.c_support.build_id`.

All measurement, case, source, and mapping content is identical; the
semantic-equivalence test passes. Updating the stored profiles would detach
their build IDs from the locked campaign, so they are preserved.

### Frozen layer baseline (test-semantics defect; historical inventory missing)

`freertos-nrf52840-all` has a frozen pre-correction count of 4404 and a current
post-correction count of 2975. The baseline records 4528 raw operation rows; the
current inventory records 3176. The preserved
`layer-classification-comparison.csv` records the before/after layers and
changes, but the historical inventory itself is not preserved, so row-level
equivalence cannot be verified.

The original test incorrectly asserted that the pre-correction baseline total
must equal the current total. It now validates that:

- the baseline equals the comparison table's `before` counts;
- the current inventory equals the `after` counts;
- no current row remains unclassified.

The frozen baseline is unchanged. Resolving the missing 1429 operations would
require restoring the historical inventory (or re-running the affected
configurations under the original counting definition), then reviewing whether
the difference is scope, source, or counting drift before publishing any update.

## Preservation archive

A partial archive of currently available evidence was created and verified:

- `results/archive/campaign-replication-20261019T214853Z-evidence.partial-v6.tar.gz`
  (SHA-256 `c00cc3b2179570eb0f9ab0bc60eeaf70a0bd3d05787659800e70d60fbb36e5a5`)
- 1,312 files, 14.3 MB compressed
- `results/archive/campaign-replication-20261019T214853Z-evidence.partial-v6.inventory.json`
- The earlier `v1`–`v5` snapshots are retained as historical evidence. The
  archive deliberately excludes mutable documentation so provenance is stable.
- `EVIDENCE-STATUS.json` records `"complete": false` and the missing
  Rust-native supplementary campaign.
- Integrity verification: **PASS** for v6, and every archived source hash matches the current working tree.

The archive includes the locked campaign, safety evidence, analysis
provenance, publication inputs, lock and evidence map. It is explicitly marked
partial; it is not a complete reproducibility package.

## Historical recovery search

Searched: repository history and refs, `results/archive`, existing campaign
and analysis directories, local backups, compressed archives, Git worktrees,
trash, and accessible Nextcloud siblings. The missing
`campaign-replication-20261014T075134Z` raw bundle was **not found**. A September
copy under `Master_Thesis_written/uastw-thesis/analysis/inputs/` contains
unrelated older runs, not the missing campaign.

The missing layer inventory was not found; only aggregate baseline counts and
the before/after comparison table remain.

## Reproducibility guarantees

Profile verification has two distinct guarantees:

- **Semantic equivalence** ignores only `generator.source_revision` and
  `source_configs.*.build_id`/`replace_build_id`. Cases, sources, mappings,
  measurements and features are compared unchanged.
- **Byte-for-byte historical reproduction** is strict and remains available in
  `make test-repro`. It currently fails because stored build IDs belong to the
  locked campaign revision `9fc56aa0`, while regeneration records the current
  revision `d4cf8483`.

The strict failure is not skipped or normalized away. The semantic test passes.

Inventory determinism is covered by two consecutive end-to-end inventories. One
previous run reported a `manifest.json` difference; this was not reproducible in
three repeated production inventories or subsequent test runs. The test now
reports the first differing JSON field, and the limitation is documented rather
than suppressed.

The detached review worktree contains only the current
3176-row nRF inventory (hash identical to `results/safety/generated_inventory`),
not the historical 4528-row inventory. The older `Master_Thesis_written` copy
contains September benchmark inputs unrelated to the missing campaign.

## Directory terminology after simplification

- `results/processing/` holds deterministic processing-execution provenance.
  It replaces the former `results/analyses/` name; historical records were
  moved without deletion.
- `results/safety/checks/` holds generated static-check outputs; it replaces the
  former `results/safety/analysis/` name.
- `results/campaigns/campaign-replication-20261019T214853Z/analysis/` remains an
  immutable historical campaign-local summary. It is not renamed or migrated.
- `thesis/analysis/` remains the implementation of statistical processing;
  `thesis/generated/` remains the authoritative publication-output location.
- The three historical campaign tables (`campaign-coverage.typ`,
  `campaign-memory.typ`, `campaign-static.typ`) are preserved together and
  recorded in `static-evidence.json`.

## Processing provenance records

`results/processing/` now contains deterministic processing identities derived
from campaign inputs, lock, implementation sources and stable output content.
Repeated equivalent `make analyze` runs reuse the same record. Volatile JSON
timestamps are excluded from identity only; raw output hashes remain recorded.
New SVG output uses a fixed hash salt and no Matplotlib date metadata, so current
SVG bytes are stable. Historical SVG normalization remains for old records.

Historical timestamped records were moved from `results/analyses/` without
deletion. `make report-processing` currently reports one legacy v1 duplicate
group (two records with identical inputs and outputs). They are retained
read-only.

## Hardware smoke campaign

`campaign-replication-20261021T104952Z` is a schema-v2 hardware smoke test
(FreeRTOS/STM32F4/C, 3 samples per case). It is intentionally not selected by
the thesis lock and is not publication evidence. See
`docs/hardware-validation.md` for the validation record.
