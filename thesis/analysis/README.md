# Thesis processing implementation

`thesis/analysis/` contains Python source for statistical processing, not stored
experimental results. Raw measurements remain under `results/campaigns/`,
deterministic processing records under `results/processing/`, and publication
outputs under `thesis/generated/`.

One entry point: `analyze.py`. It consumes the repository-root `results/` and
writes all generated files under `../generated/`.

Run it with an environment that provides matplotlib:

```sh
../.venv/bin/python analyze.py benchmark
../.venv/bin/python analyze.py integration
../.venv/bin/python analyze.py all
../.venv/bin/python analyze.py test
```

## Layout

| Path | Purpose |
|---|---|
| `analyze.py` | Orchestrates the analysis stages |
| `benchmark/campaign.py` | Builds campaign tables from the newest run |
| `benchmark/replication.py` | Run-level statistics and confidence intervals |
| `benchmark/tables.py` | Per-RTOS appendix tables (`all-results.typ`) |
| `benchmark/performance.py` | Rust/C performance heatmap |
| `benchmark/memory.py` | Static flash/RAM footprint figure |
| `benchmark/rust_kernel.py` | Rust-native kernel comparison figure |
| `integration/complexity.py` | Complexity and maintainability metrics |
| `integration/duplication.py` | C/Rust duplication and drift proxy |
| `integration/surface.py` | Integration-surface table and figure |
| `tests/` | Reproducibility tests |
| `../generated/typ/` | All generated Typst includes (benchmark, integration and safety) |
| `../generated/figures/` | Generated benchmark and integration figures |
| `../generated/data/` | Generated JSON records and provenance sidecars |
| `../figures/` | Hand-authored diagrams |

## Data flow

```
results/campaigns/campaign-replication-*  ->  campaign.py  ->  generated/typ/campaign-*.typ
                                ->  replication.py ->  generated/data/replication-stats.json
                                ->  tables.py     ->  generated/typ/all-results.typ
                                ->  performance.py / memory.py / rust_kernel.py
                                                    ->  generated/figures/*.svg
project/firmware + project/plugins -> complexity.py / duplication.py / surface.py
                                                    ->  generated/typ/*.typ
                                                    ->  generated/figures/fig-integration.svg
```

The selected evidence is pinned in `campaign.lock.json`; analysis never falls
back to the newest campaign. Update the lock deliberately after reviewing a new
campaign. If the locked campaign is missing, analysis exits with the exact
missing path.

`campaign-provenance.json` records the selected run for every reported number.
`performance.py` also owns the resumable statistics/table refresh and retains
Rust-native paths from the previous statistics when the selected campaign omits
them. Each `analyze.py benchmark` / `all` run writes a deterministic provenance
record under `results/processing/<campaign-id>/<processing-id>/`. The identity
is derived from the campaign inputs, lock, implementation sources and stable
output content; repeated equivalent runs reuse the record. Volatile timestamps
and Matplotlib SVG identifiers are excluded from the identity, not from the raw
output hashes. Historical processing records are retained and reported by
`make report-processing`.

When the selected campaign omits Rust-native measurements, the published
coverage, memory and static tables are preserved and recorded in
`generated/data/static-evidence.json`; memory-figure regeneration is skipped
because it depends on those historical rows.

Delete `../generated/` to force a full regeneration.
