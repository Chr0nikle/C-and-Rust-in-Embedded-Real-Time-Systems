# Benchmark readiness: full 27-configuration campaign

Status: **software validation complete; physical smoke tests not executed**.
No board was flashed, reset, or power-cycled during this audit. The full
campaign was not run.

Audit revision: `9ba13e02` plus the local corrections described below.
Evidence date: 2026-10-21.

## Readiness classes

- **READY**: build and appropriate physical validation passed for the current
  pipeline.
- **SOFTWARE-READY**: toolchain, build, configuration and routing checks passed;
  the current firmware has not been physically validated.
- **BLOCKED**: a demonstrated issue prevents reliable execution.
- **UNAVAILABLE**: required hardware or dependency is absent.
- **UNSUPPORTED**: the implementation does not support the configuration.

`rtos check` alone is never treated as READY.

## Matrix

All 27 routes were compiled successfully on 2026-10-21. Unless stated otherwise
the console is the board's normal benchmark channel and flashing resets the
target.

| RTOS | Board | Lang | Advertised cases | Timing source | Build / flash | Status |
|---|---|---|---|---|---|---|
| FreeRTOS | STM32F4 Discovery | C | 0-28 | 168 MHz DWT | Arm GCC 14.2.1; probe-rs ST-LINK `52C3BF6D` | **READY** |
| FreeRTOS | STM32F4 Discovery | Rust | 0-28 | 168 MHz DWT | Arm GCC + rustc 1.98.1; probe-rs `52C3BF6D` | SOFTWARE-READY |
| FreeRTOS | STM32H723 | C | 0-28 | 520 MHz DWT | Arm GCC 14.2.1; probe-rs ST-LINK `37C3BF71` | SOFTWARE-READY |
| FreeRTOS | STM32H723 | Rust | 0-28 | 520 MHz DWT | Arm GCC + rustc 1.98.1; probe-rs `37C3BF71` | SOFTWARE-READY |
| FreeRTOS | nRF52840 DK | C | 0-21,24-28 | 64 MHz DWT | Arm GCC 14.2.1; probe-rs J-Link `001050202809` | SOFTWARE-READY |
| FreeRTOS | nRF52840 DK | Rust | 0-21,24-28 | 64 MHz DWT | Arm GCC + rustc 1.98.1; J-Link `001050202809` | SOFTWARE-READY |
| FreeRTOS (ESP-IDF) | ESP32-C6 | C | 0-20,25-28 | 16 MHz SYSTIMER | RISC-V GCC 14.2.0; `idf.py flash` | **BLOCKED** |
| FreeRTOS (ESP-IDF) | ESP32-C6 | Rust | 0-20,25-28 | 16 MHz SYSTIMER | RISC-V GCC + rustc 1.98.1; `idf.py flash` | **BLOCKED** |
| NuttX | STM32F4 Discovery | C | 0-20,24-28 | 168 MHz DWT | Arm GCC 14.2.1; probe-rs `52C3BF6D` | SOFTWARE-READY |
| NuttX | STM32F4 Discovery | Rust | 0-20,24-28 | 168 MHz DWT | Arm GCC + rustc 1.98.1; probe-rs `52C3BF6D` | SOFTWARE-READY |
| NuttX | STM32H723 | C | 0-20,24-28 | 520 MHz DWT | Arm GCC 14.2.1; probe-rs `37C3BF71` | SOFTWARE-READY |
| NuttX | STM32H723 | Rust | 0-20,24-28 | 520 MHz DWT | Arm GCC + rustc 1.98.1; probe-rs `37C3BF71` | SOFTWARE-READY |
| NuttX | nRF52840 DK | C | 0-20,24-28 | 64 MHz DWT | Arm GCC 14.2.1; J-Link `001050202809` | SOFTWARE-READY |
| NuttX | nRF52840 DK | Rust | 0-20,24-28 | 64 MHz DWT | Arm GCC + rustc 1.98.1; J-Link `001050202809` | SOFTWARE-READY |
| NuttX | ESP32-C6 | C | 0-20,24-28 | 160 MHz performance counter | RISC-V GCC 14.2.0; esptool USB-JTAG | **BLOCKED** |
| NuttX | ESP32-C6 | Rust | 0-20,24-28 | 160 MHz performance counter | RISC-V GCC + rustc 1.98.1; esptool | **BLOCKED** |
| Zephyr | STM32F4 Discovery | C | 0-20,24-28 | 168 MHz kernel cycle counter | west 1.5.0/Zephyr 4.4.2; probe-rs `52C3BF6D` | SOFTWARE-READY |
| Zephyr | STM32F4 Discovery | Rust | 0-20,24-28 | 168 MHz kernel cycle counter | west + rustc 1.98.1; probe-rs `52C3BF6D` | SOFTWARE-READY |
| Zephyr | STM32H723 | C | 0-20,24-28 | 520 MHz kernel cycle counter | west 1.5.0/Zephyr 4.4.2; probe-rs `37C3BF71` | SOFTWARE-READY |
| Zephyr | STM32H723 | Rust | 0-20,24-28 | 520 MHz kernel cycle counter | west + rustc 1.98.1; probe-rs `37C3BF71` | SOFTWARE-READY |
| Zephyr | nRF52840 DK | C | 0-20,24-28 | 64 MHz DWT | west 1.5.0/Zephyr 4.4.2; J-Link `001050202809` | SOFTWARE-READY |
| Zephyr | nRF52840 DK | Rust | 0-20,24-28 | 64 MHz DWT | west + rustc 1.98.1; J-Link `001050202809` | SOFTWARE-READY |
| Zephyr | ESP32-C6 | C | 0-20,24-28 | 16 MHz SYSTIMER | west 1.5.0; esptool USB-JTAG | **BLOCKED** |
| Zephyr | ESP32-C6 | Rust | 0-20,24-28 | 16 MHz SYSTIMER | west + rustc 1.98.1; esptool | **BLOCKED** |
| FreeRTOS-in-Rust | STM32F4 Discovery | Rust | 0-28 | 168 MHz DWT | rustc 1.98.1/LLD 22.1.8; probe-rs `52C3BF6D` | SOFTWARE-READY |
| FreeRTOS-in-Rust | STM32H723 | Rust | 0-28 | 520 MHz DWT | rustc 1.98.1/LLD 22.1.8; probe-rs `37C3BF71` | SOFTWARE-READY |
| Rivet | STM32F4 Discovery | Rust | 0-13,15-23,25-28 | 168 MHz DWT | rustc 1.98.1/LLD 22.1.8; probe-rs `52C3BF6D` | SOFTWARE-READY |

Rivet deliberately does not advertise cases 14 (`alloc_latency`) or 24
(`heap_fragmentation`). Rust-native kernels are not included on boards where
the kernel port is absent. Unsupported cases are absent from `ready.cases`; they
are not recorded as zero measurements.

## Toolchain and source provenance

Validated locally: Arm GNU Toolchain 14.2.1, ESP RISC-V GCC 14.2.0, rustc/cargo
1.98.1 with `thumbv7em-none-eabihf`, `thumbv7em-none-eabi` and
`riscv32imac-unknown-none-elf`, probe-rs 0.32.0, Zephyr 4.4.2/west 1.5.0,
ESP-IDF 5.5.5/esptool 5.4.0, CMake 4.4.4, Ninja 1.13.2 and Clang 23.1.1.

`project/config/sources.toml` declares the upstream revisions, but the staged
NuttX, Zephyr, ESP-IDF and Rivet trees have no `.git` metadata or checksum lock.
The declared revisions can therefore not be independently verified from this
checkout. Campaign provenance records the project revision/diff, compiler
versions, Rust targets, build IDs and artifact hashes, but not an upstream tree
digest. Treat that as a reproducibility limitation, not as evidence of a
different revision.

## Console and reset routing

| Board | Console | Stable selector | Reset / power |
|---|---|---|---|
| STM32F4 Discovery | USART2 via CP2102 | `/dev/serial/by-path/pci-0000:0c:00.0-usb-0:1.4.4.3:1.0-port0` | probe-rs programming/reset |
| STM32H723 | USART1 via CP2102 | `/dev/serial/by-path/pci-0000:0c:00.0-usb-0:1.4.3:1.0-port0` | probe-rs programming/reset |
| nRF52840 DK | UART0 via J-Link VCOM `if00` | `/dev/serial/by-id/usb-SEGGER_J-Link_001050202809-if00` | probe-rs/J-Link reset |
| ESP32-C6 | USB-Serial/JTAG `if00` | unique Espressif by-id node | esptool hard reset + VBUS cycle |

The two CP2102 adapters share serial `0001`; by-path selectors are required.
Both ST-LINK/V2 probes share `0483:3748`; all flash and debug selectors now pin
`52C3BF6D` (Discovery) or `37C3BF71` (H723). The nRF J-Link is pinned to
`001050202809`.

The campaign resolves each serial path from `boards/<id>/board.toml` before it
clears resume markers or touches a board. The ESP32-C6 manifest intentionally
has an empty `port_selector` and `verified=false`: no bidirectional RTOS
handshake has been completed. The runner therefore requires exactly one
Espressif USB-Serial/JTAG by-id node and fails on zero or multiple candidates.

## ESP32-C6 findings

All three RTOS routes use the same USB-Serial/JTAG endpoint:

- FreeRTOS: ESP-IDF `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`.
- NuttX: `CONFIG_ESPRESSIF_USBSERIAL=y`, with the 2 KiB ring-buffer patch.
- Zephyr: `usb_serial` selected for console and shell; `uart0` disabled.

The console therefore re-enumerates after flashing and after a VBUS cycle.
Run IDs and the build ID prevent stale serial data from being accepted, and the
host boot pump only accepts a matching build ID. The campaign now:

- refuses to cycle the hub if `303a:1001` is not present on the configured hub
  port;
- waits for the exact console path, not merely any Espressif device;
- aborts the unit if the VBUS cycle fails, instead of running a wedged board.

`verified=false` remains correct until a FreeRTOS/C smoke test proves boot,
handshake, 25-case completion and console recovery across at least one
power-cycle.

## Measurement checks

- Protocol case IDs and C/Rust workload order are shared by the common runner.
- A case is requested only when the device advertises it. Missing cases are not
  zero-filled.
- `requested repetitions` is the total per case per run; variant cases cycle
  variants across that budget. Ten runs of 100 samples give 1,000 samples per
  case (200 per variant when five variants exist).
- Warm-up is 10 iterations per run; the documented full protocol is ten
  restarted runs, hence 100 warm-up iterations total per case.
- Case 11 and case 27 `UINT64_MAX` values are excluded from latency statistics
  and counted as deadline misses or no-benefit zero-copy samples.
- Each sample carries its own `ticks_per_second`; the processing code prefers
  `ticks_per_second_by_case`. C6 paths use 16 MHz (FreeRTOS/Zephyr) or 160 MHz
  (NuttX), while ARM paths use DWT clocks, so cross-board plots must use each
  case's rate.
- Pooled percentiles are descriptive. The ten restarted runs are retained as
  per-run medians, but no confidence interval or between-run standard error is
  computed. Run-level uncertainty is therefore not yet quantified.

### Corrected throughput reporting

Case 19 (`ipc_throughput`) stores **elapsed timer ticks for a fixed 1 MiB
transfer**. The thesis processing pipeline now converts every valid observation
to B/s before aggregation; the published table, heatmap and prose use the
corrected values. Historical case-19 zero and wrapped durations remain in the
raw evidence and are counted and excluded from the descriptive statistics.

Case 27 remains a latency difference expressed in raw ticks in the thesis
table. The primary analyzer may report microseconds for its own diagnostics;
the thesis convention is the raw tick saving. This distinction is documented in
the thesis Appendix B.

## Representative smoke tests

Each command is non-destructive until executed; all flash, reset, power-cycle,
and measurement actions require separate explicit authorization.

```sh
cd project
cargo build -p rtos-cli --offline --locked
```

| Risk covered | Command suffix | Expected |
|---|---|---|
| Rust integration | `--combination freertos/stm32f4discovery/rust` | 29 cases; 87 samples |
| NuttX | `--combination nuttx/stm32f4discovery/c` | 26 cases; 78 samples |
| Zephyr | `--combination zephyr/stm32f4discovery/c` | 26 cases; 78 samples |
| Rust-native kernel | `--combination freertos-in-rust/stm32f4discovery/rust` | 29 cases; 87 samples |
| Second Rust-native kernel | `--combination rivet/stm32f4discovery/rust` | 27 cases; 81 samples |
| STM32H723 board | `--combination freertos/stm32h723/c` | 29 cases; 87 samples |
| nRF52840 board | `--combination freertos/nrf52840dk/c` | 27 cases; 81 samples |
| ESP32-C6 console/reset | `--combination freertos/esp32c6/c` | 25 cases; 75 samples |

Use this suffix for every row:

```sh
python3 scripts/benchmarks/campaign.py run \
  --combination <RTOS/BOARD/LANGUAGE> \
  --fresh --runs 1 --repetitions 3 --warmup 1
```

Acceptance for each smoke: new unique campaign, schema 2, `COMPLETE`, one unit,
preflight `collected`, manifest valid, exact advertised-case count, three
samples per case, no stale run ID, and the thesis lock unchanged. The ESP32-C6
test additionally requires a verified VBUS cycle and console re-enumeration.

FreeRTOS/C on STM32F4 was already validated in
`campaign-replication-20261021T112527Z`; the later source changes affect build
routing and campaign bookkeeping, not the firmware protocol.

## Full campaign

The protocol is 27 combinations x 10 independent runs x 100 samples per case
per run (1,000 samples per case), with 10 warm-ups per run and 100 total. A
single global 26-case filter cannot be used across all configurations: the
device rejects any requested case it does not advertise, and Rivet/nRF paths
support different subsets. The safe full-campaign command therefore runs each
configuration's advertised suite; thesis publication processing filters the
26 selected tasks and never renders the three omitted protocol IDs.

```sh
cd project
cargo build -p rtos-cli --offline --locked
python3 scripts/benchmarks/campaign.py run \
  --fresh --runs 10 --repetitions 1000 --warmup 100
```

Expected: 270 units, one new schema-v2 campaign, `COMPLETE` only if all units
validate. The thesis lock is not updated automatically. Raw data must be
backed up separately. The six ESP32-C6 configurations remain **BLOCKED** until
the USB-Serial/JTAG reset and console-recovery checks are completed; a full
campaign cannot be publication-ready while those routes are unresolved.

Do not start the campaign while the worktree is dirty with execution-relevant
changes. Commit the corrected firmware and campaign runner first, or run from a
clean worktree at a recorded revision.
