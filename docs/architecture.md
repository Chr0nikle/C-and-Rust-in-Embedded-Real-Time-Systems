# Architecture

## Repository split

- `project/` — executable research infrastructure: Rust host workspace
  (`cli/`, `core/`, `plugins/`), firmware (`firmware/`), board manifests,
  pinned configuration, benchmark definitions, safety analysis and scripts.
- `results/` — experimental evidence: `campaigns/`, `analyses/`, `safety/` and
  `archive/`. It is excluded from Git and must be published separately when
  required.
- `thesis/analysis/` — statistical and research analysis.
- `thesis/` — Typst manuscript, figures, generated tables, literature,
  bibliography and exports.
- `docs/` — architecture, experiments and reproducibility.

## Data flow and directory ownership

- `results/campaigns/<id>/benchmarks/` — immutable raw measurements.
- `results/campaigns/<id>/analysis/` — historical campaign-local summaries;
  retained read-only for the completed campaign.
- `results/processing/<id>/<processing-id>/` — deterministic processing
  provenance (inputs, outputs, implementation and environment).
- `results/safety/checks/` — generated safety checks and extracted values.
- `results/safety/inventory/` — FreeRTOS Stage 1 inventory.
- `results/safety/matched/` — FreeRTOS Stage 2 matched-API evidence.
- `results/safety/profiles/` — generated matched/native profiles.
- `results/safety/generated_inventory/` — per-profile extracted inventories.
- `results/safety/generated_outputs/` — per-profile matched reports and tables.
- `thesis/analysis/` — Python implementation of statistical processing.
- `thesis/generated/` — canonical publication tables, figures and data.
- `thesis/sections/` — scientific interpretation and manuscript.

```text
project firmware + RTOS sources
        -> benchmark runs
        -> results/campaigns/<campaign-id>/benchmarks/
        -> thesis analysis using thesis/analysis/campaign.lock.json
        -> thesis/generated/{typ,figures,data}
        -> results/processing/<campaign-id>/<processing-id>/provenance.json
        -> thesis/sections + thesis.typ -> thesis/exports/
```

The thesis consumes explicit locked evidence, never the newest campaign.

## Safety boundary

`project/safety/` contains code, reviewed specs (`specs/`), curated profiles
(`profiles/data/`) and fixtures. Generated safety outputs live under
`results/safety/`. Thesis-facing safety tables are written to
`thesis/generated/typ/`.

## Firmware ownership

`project/firmware/` contains project-owned overlays and Rust-native kernels.
Upstream RTOS sources are staged under ignored `project/.local/`; revisions are
pinned in `project/config/sources.toml` and documented in
`project/firmware/UPSTREAM.md`.
