# C-and-Rust-in-Embedded-Real-Time-Systems

## Masters Thesis: RTOS benchmarking and Rust/C safety analysis

This repository contains the benchmark framework, experiment evidence and
Typst manuscript for the thesis. It is organised into four areas:

- [`project/`](project/) — Rust host framework, firmware, benchmark
  definitions, safety analysis and automation.
- [`thesis/`](thesis/) — Typst manuscript, statistical analysis, figures,
  generated tables and compiled exports.
- [`results/`](results/) — reproducible experimental evidence under
  `campaigns/`, `safety/` and `archive/`.
- [`docs/`](docs/) — architecture, experiment protocol and reproducibility
  guidance.

## Documentation

- [Architecture](docs/architecture.md)
- [Experiments](docs/experiments.md)
- [Reproducibility](docs/reproducibility.md)
- [Scientific evidence audit](docs/evidence-audit.md)
- [Hardware validation plan](docs/hardware-validation.md)
- [Project README](project/README.md)
- [Thesis analysis](thesis/analysis/README.md)
- [Safety analysis](project/safety/README.md)

## Quick start

```sh
make check          # offline checks
make test           # offline unit/integration tests
make thesis         # compile the thesis
```

Hardware work is explicit:

```sh
make check-hardware # discovery and plans only
make benchmark      # build, flash and collect a campaign
make safety         # full safety pipeline
make analyze        # regenerate thesis analysis from the locked campaign
```

See `make help` for all targets and `docs/reproducibility.md` for prerequisites.
There is no CI/CD configuration; all validation is run locally.


## Publication evidence

The thesis results are reproduced from the locked campaign
`results/campaigns/campaign-replication-20261019T214853Z`. The campaign lock is
`thesis/analysis/campaign.lock.json`.

## Reproducing the analysis

```sh
make analyze       # regenerate tables and statistics from the locked campaign
make thesis        # compile the Typst thesis
make check         # offline project checks
make test          # offline tests
```
