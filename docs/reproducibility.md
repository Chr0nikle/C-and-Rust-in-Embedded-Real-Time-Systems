# Reproducibility

## Prerequisites

- Rust 1.98.1 with `rustfmt` and `clippy` (pinned by `project/rust-toolchain.toml`)
  and Cargo dependencies from `project/Cargo.lock`
- Python 3.14.8 for this environment; thesis plotting uses `thesis/.venv`
  with the versions in `thesis/analysis/requirements.txt`
- Typst 0.15.1
- `probe-rs` 0.32.0; `esptool` from the staged ESP-IDF environment
- ARM and RISC-V embedded GCC from `project/.local/toolchains/`
- ESP-IDF v5.5.5; Zephyr v4.4.2; NuttX 20752312
- `clang`/libclang 23.1.1 for the safety C extractor
- `rust-code-analysis-cli` 0.0.25 for complexity analysis
- Zephyr `west` and NuttX sources for those paths

## Setup

```sh
cd project
bash scripts/setup/install.sh
bash scripts/setup/provision-esp-idf.sh esp32c6
```

Toolchains and staged SDKs live under ignored `project/.local/`. The analysis
environment can be recreated with:

```sh
python3 -m venv thesis/.venv
thesis/.venv/bin/pip install -r thesis/analysis/requirements.txt
```

The Rust compiler is pinned independently of `Cargo.lock`; embedded GCC,
ESP-IDF, Zephyr and NuttX versions are documented in
`project/firmware/UPSTREAM.md` and `project/config/sources.toml`.

## Validation

```sh
make check          # offline Rust + project Python checks
make test           # offline unit/integration suites
make test-repro     # full evidence-backed checks
make check-firmware # offline firmware checks
make check-hardware # board discovery and plans only
make thesis         # compile the thesis
```

`make check`, `make test`, `make check-firmware` and `make thesis` never touch
hardware. `make benchmark` is the explicit hardware campaign target.

Individual commands:

```sh
cd project
cargo fmt --all -- --check
cargo check --workspace --offline --locked
cargo clippy --workspace --all-targets --offline --locked -- -D warnings
cargo test --workspace --offline --locked

cd ../thesis/analysis
python3 analyze.py test

cd ../../project/safety
python3 safety.py test
python3 safety.py test --tier repro
```

## Evidence and analysis

The thesis uses the campaign pinned in `thesis/analysis/campaign.lock.json`.
If it is missing, analysis fails with the exact path; it never substitutes
another campaign. Update the lock deliberately after reviewing new evidence.

```sh
make analyze
```

Analysis provenance is written to
`results/processing/<campaign-id>/<analysis-id>/`, including the analyzer commit,
input manifest, command and output hashes. `make thesis` only needs tracked
`thesis/generated/` inputs.

The complete output-to-evidence map is in [evidence-audit.md](evidence-audit.md).

## Evidence archive

`results/` is excluded from Git. To preserve the locked evidence independently:

```sh
# Complete evidence only:
make archive-evidence OUTPUT=results/archive/thesis-evidence.tar.gz

# Known-incomplete evidence (explicitly marked partial):
python3 project/scripts/maintenance/archive_evidence.py --partial \
  --output results/archive/thesis-evidence.partial-v6.tar.gz
make verify-evidence ARCHIVE=results/archive/thesis-evidence.partial-v6.tar.gz
make report-processing
```

The archive contains the locked campaign, selected safety evidence, analysis
provenance, publication inputs and a deterministic SHA-256 manifest. Missing
evidence is named in `EVIDENCE-README.md`; it is never synthesized. The source
evidence is not deleted.

## Known baseline failures

`make test-repro` currently reports one known metadata drift:

- `test_deterministic_generation`: stored profile build IDs belong to the
  locked campaign (`9fc56aa0`); regeneration uses the current Git revision
  (`d4cf8483`). The difference is limited to `generator.source_revision` and
  `source_configs.*.build_id`/`replace_build_id`; the semantic-equivalence test
  passes. Do not update the stored profiles without a reviewed campaign.

The former layer-baseline failure was a test-semantics defect. The baseline is
a pre-correction snapshot and the comparison table records `before` and `after`
separately; the corrected test preserves the baseline unchanged.
