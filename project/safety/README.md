# Safety analysis

One entry point: `safety.py`. Raw analysis evidence goes to `results/safety/`;
the Typst tables consumed by the thesis go to `../../thesis/generated/typ/`.

## Layout

| Path | Purpose |
|---|---|
| `safety.py` | CLI and pipeline orchestration |
| `common/` | Shared IO, repo discovery, layer rules, extractor bridge, artifact paths |
| `checks/` | ABI, array, queue, layout and static-analyser checks on the FreeRTOS sources |
| `inventory/` | Stage 1 FreeRTOS operation inventory |
| `matched/` | Stage 2 matched C/Rust API, FFI and callback analysis |
| `profiles/` | RTOS/board profile generation and per-profile inventories |
| `profiles/data/` | Checked-in profile definitions |
| `tables.py` | Derived analysis tables and thesis Typst tables |
| `specs/` | Hand-maintained inputs: case registry, measurement/counting specs, layer rules and baseline |
| `rust_extractor/` | Rust AST extraction helper built on demand |
| `tests/`, `inventory/tests/`, `matched/tests/`, `profiles/tests/` | Tiered tests; `tests/fixtures/` holds reviewed fixtures, including frozen profile snapshots |
| `results/safety/` | Generated outputs only; excluded from Git and safe to regenerate |

## Pipeline

```sh
python3 safety.py all                  # complete analysis (reuses checked-in profiles)
python3 safety.py all --skip-analysers # skip Clang Analyzer and Clippy
python3 safety.py all --regenerate-profiles  # also rebuild profiles + per-profile inventories
```

Tests are tiered so a clean checkout can still run meaningful checks:

```sh
python3 safety.py test               # unit + integration (no ignored evidence)
python3 safety.py test --tier unit   # reviewed specifications/profiles only
python3 safety.py test --tier repro  # full generated evidence required
python3 safety.py test --tier all    # every tier
```

Individual stages:

```sh
python3 safety.py analyze
python3 safety.py inventory
python3 safety.py matched-api
python3 safety.py profiles
python3 safety.py profile-inventory
python3 safety.py thesis-safety
python3 safety.py test
```

Generated stage outputs live under `results/safety/`: `checks/`, `inventory/`,
`matched/`, `profiles/`, `generated_inventory/` and `generated_outputs/`.
Reviewed specifications live in `specs/`, curated profiles in `profiles/data/`
and fixtures in `tests/fixtures/`. The thesis Typst tables are written to
`thesis/generated/typ/`.

## Inputs vs outputs

Everything in `specs/`, `profiles/data/` and `tests/fixtures/` is reviewed input.
Everything in `results/safety/` is generated and can be rebuilt. The
inventory and matched-API stages read the project sources under
`project/firmware` and `project/config`; profile inventories additionally need
the per-board build `compile_commands.json` files.

## Known reproducibility failure

`test_deterministic_generation` compares a regenerated profile against a stored
profile recorded at Git revision `9fc56aa0`; the current checkout is
`d4cf8483`. The difference is metadata only:

- `generator.source_revision`;
- `source_configs.*.build_id` and `replace_build_id`.

`test_profile_content_is_stable_across_revision_metadata` confirms that all
other profile content is identical. Updating the stored profiles would change
their build IDs away from the locked campaign, so the strict test remains a
known `repro`-tier failure until a new campaign is generated and reviewed.

The former layer-baseline failure was a test-semantics defect, not an inventory
change: `specs/layer-classification-baseline.json` is explicitly a
pre-correction snapshot. The corrected test compares the baseline to the
comparison table's `before` column and the current inventory to `after`; the
historical baseline remains unchanged.
