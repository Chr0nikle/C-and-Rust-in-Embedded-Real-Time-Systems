# RTOS comparison framework

A Rust host framework and C/Rust firmware scaffold for comparative RTOS
benchmarking on STM32F4 Discovery, ESP32-C6 and nRF52840 DK. The target
architecture is defined in [rules.md](rules.md). The thesis sources live in the
sibling directory `../thesis/`.

**Working now:** host CLI route planning and pinned-source builds; flashing via
probe-rs (STM32F4/nRF52840) and esptool (C6); protocol-v1 UART sessions;
`benchmark` for FreeRTOS, Zephyr and NuttX in C and Rust, plus the Rust-native
kernels FreeRTOS-in-Rust and Rivet on the STM32F4 Discovery; `list` device
discovery; `monitor`, `memory`, `safety`, `gdb`/`debug` and static-footprint
analysis; `scripts/setup/install.sh` and `uninstall.sh`. The suite is
implemented across the paths, with per-path exceptions documented in
[the specification](benchmarks/specifications.md). Cases 21 and 22/23 stay
FreeRTOS/STM32F4-only, and the Rust kernels advertise the subset their
primitives support. The ESP32-C6 ESP-IDF path is implemented under the
`freertos` route, because ESP-IDF's kernel is FreeRTOS; there is no separate
`esp-idf` command. Unfinished operations return errors.

Run commands from this `project/` directory:

```sh
cargo run -p rtos-cli -- --help
cargo run -p rtos-cli -- freertos --board esp32c6 --plan
cargo run -p rtos-cli -- benchmark zephyr --board stm32f4discovery --language rust --plan
bash scripts/maintenance/check-skeleton.sh
```

`rtos benchmark <rtos> --board <board>` builds, flashes, runs and validates the
suite, and writes evidence under the repository-root `results/campaigns/<build_id>/`. `rtos monitor` captures
UART without flashing, and `rtos list` enumerates connected probes and serial
endpoints from the board manifests.

## Layout

- `cli/`, `core/`, `plugins/` — host CLI, shared core and RTOS plugins
- `firmware/` — C/Rust firmware workspaces and kernel integrations
- `benchmarks/` — suite definition and the newest source archive
- `boards/`, `config/` — board manifests and pinned source revisions
- `../results/` — measurement evidence: `campaigns/`, `safety/` and `archive/`
- `scripts/setup/` — install, environment setup and uninstall
- `scripts/maintenance/` — host checks and tests
- `scripts/benchmarks/` — merged analysis, campaign and study tools
- `safety/` — consolidated safety analysis and its artifacts

## Thesis

The thesis sources are in the sibling `../thesis/` directory. Compile them with:

```sh
cd ../thesis
typst compile --root .. thesis.typ thesis.pdf
```

## Benchmark analysis tools

```sh
python3 scripts/benchmarks/analyze.py compare   # comparison.json + Markdown
python3 scripts/benchmarks/analyze.py report    # diagrams (needs matplotlib)
python3 scripts/benchmarks/study.py             # long-duration drift study
python3 scripts/benchmarks/campaign.py run      # full campaign (resumable)
python3 scripts/benchmarks/campaign.py append   # replication passes
python3 scripts/benchmarks/campaign.py status   # live progress
```

Safety analysis (see [safety/README.md](safety/README.md); artifacts under `safety/artifacts/`):

```sh
python3 safety/safety.py all
python3 safety/safety.py test
python3 safety/safety.py --help
```

Read the integration recipes:

- [FreeRTOS: ESP-IDF port on C6; kernel + board support on STM32](plugins/freertos/BUILD.md)
- [Zephyr: west, board targets, optional Rust integration](plugins/zephyr/BUILD.md)
- [NuttX: paired repositories, board configurations and application registration](plugins/nuttx/BUILD.md)

[Benchmark definitions](benchmarks/specifications.md) define the shared
workloads. Source revisions in `config/sources.toml` must be resolved and
verified before acquisition. The firmware workspace is separate from the host
crates: host checks cover the pure runners and adapter crates, while board
build, flash and UART evidence is collected separately under the repository-root `results/campaigns/`.
