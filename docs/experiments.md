# Experiments

## Targets

| Board | SoC | Notes |
|---|---|---|
| `stm32f4discovery` | STM32F407 | Primary board, DWT cycle counter |
| `stm32h723` | STM32H723 | High-performance STM32 path |
| `nrf52840dk` | nRF52840 | Cortex-M4F |
| `esp32c6` | ESP32-C6 | RISC-V, ESP-IDF and Zephyr |

Supported paths are FreeRTOS, Zephyr and NuttX in C and Rust, plus the
Rust-native FreeRTOS-in-Rust and Rivet kernels. The ESP32-C6 ESP-IDF path runs
under the `freertos` route because ESP-IDF's kernel is FreeRTOS. Live benchmark
definitions are `project/benchmarks/suite.toml` and
`project/benchmarks/specifications.md`.

## Campaign contract

New campaigns use schema version 2
(`project/benchmarks/campaign-schema.json`):

```text
results/campaigns/<campaign-id>/
├── campaign.json            # schema-versioned record
├── campaign-config.json
├── VALIDATION.json
├── COMPLETE
├── provenance/
│   ├── git-revision.txt
│   ├── git-status.txt
│   ├── worktree.diff
│   ├── toolchains.json
│   ├── devices.txt
│   ├── preflight-checks.json
│   └── input-manifest.sha256
├── benchmarks/<build_id>/
└── analysis/
```

Raw measurements and the input manifest are immutable. Derived analysis is
recorded separately under `results/processing/<campaign-id>/<processing-id>/`
with analyzer revision, input hashes, command, timestamp and output hashes.
The processing identifier is derived from inputs, lock, implementation and
stable output content; repeated equivalent runs reuse the record.
Older campaigns remain readable and are not rewritten. The completed
`campaign-replication-20261019T214853Z` uses the historical `analysis/`
directory for its campaign-local summary; new processing records use
`results/processing/`.

## Retention

Never delete a campaign referenced by `thesis/analysis/campaign.lock.json` or a
released thesis. Retire unreferenced campaigns to `results/archive/` with a
checksum manifest. Never select evidence solely because it is newest.

## Campaign commands

```sh
make benchmark
# or
cd project
python3 scripts/benchmarks/campaign.py run
python3 scripts/benchmarks/campaign.py append
python3 scripts/benchmarks/campaign.py status
```

## Hardware validation

See [hardware-validation.md](hardware-validation.md). Physical campaigns require
explicit authorization; offline validation does not flash boards.
