# FreeRTOS integration recipe

> **Scope note (2026-10-12):** Pico 2 (RP2350) sections below are historical; the
> Pico 2 board was removed from the project. Current FreeRTOS boards are the
> STM32F4 Discovery and ESP32-C6.

Status: host adapter and firmware placeholders only. Commands below are recipes for future implementation; no hardware has been validated. Pin and verify source revisions in `config/sources.toml` first. Upstream references were checked on 2026-09-16; recheck commands against the revision actually selected.

## ESP32-C6

Project decision: use the FreeRTOS port inside pinned ESP-IDF for C6. This `freertos` route is the project's only ESP-IDF path (its kernel is FreeRTOS); there is no separate `esp-idf` adapter, and the C6 firmware lives in `firmware/c-kernels/freertos/c` and `firmware/c-kernels/freertos/rust`. Select our minimal FreeRTOS application sources; do not combine a random upstream RISC-V kernel port with IDF startup. The IDF repository owns its [FreeRTOS component](https://github.com/espressif/esp-idf/tree/master/components/freertos).

`freertos` and `esp-idf` remain separate user-facing adapters but share source acquisition and backend identity through core build helpers, not dependencies between plugin crates. A UART ready record identifies both the requested integration and the actual kernel/backend. Comparing these IDs alone is not comparing different kernels.

## STM32F4 Discovery: acquire kernel and board support

Obtain the [kernel](https://github.com/FreeRTOS/FreeRTOS-Kernel) and [STM32CubeF4](https://github.com/STMicroelectronics/STM32CubeF4) separately. Cube supplies MCU/board support; our build must select exactly one kernel implementation.

```sh
: "${RTOS_SOURCE_DIR:?absolute source cache path}"
: "${RTOS_FREERTOS_REV:?full chosen kernel commit}"
: "${RTOS_CUBE_REV:?full chosen STM32CubeF4 commit}"
git clone https://github.com/FreeRTOS/FreeRTOS-Kernel.git "$RTOS_SOURCE_DIR/freertos-kernel"
git -C "$RTOS_SOURCE_DIR/freertos-kernel" checkout --detach "$RTOS_FREERTOS_REV"
git -C "$RTOS_SOURCE_DIR/freertos-kernel" submodule update --init --recursive
git clone https://github.com/STMicroelectronics/STM32CubeF4.git "$RTOS_SOURCE_DIR/stm32cubef4"
git -C "$RTOS_SOURCE_DIR/stm32cubef4" checkout --detach "$RTOS_CUBE_REV"
git -C "$RTOS_SOURCE_DIR/stm32cubef4" submodule update --init --recursive
```

Use the [GCC ARM_CM4F port](https://github.com/FreeRTOS/FreeRTOS-Kernel/tree/main/portable/GCC/ARM_CM4F) with the verified MCU and compatible Arm GNU toolchain. The [Discovery project tree](https://github.com/STMicroelectronics/STM32CubeF4/tree/master/Projects/STM32F4-Discovery) is a starting point for startup/linker/clock configuration, not a ready benchmark image.

Generation tasks in `firmware/c-kernels/freertos/c/`:

1. Add an `arm-none-eabi` CMake toolchain, confirmed MCU startup/vector table and linker script, CMSIS/HAL board initialization, and explicit UART routing matching the onboard debug interface.
2. Supply `FreeRTOSConfig.h`, interrupt priorities, scheduler tick, stack sizes and a chosen heap policy. Include tasks/queue/list plus the ARM_CM4F port and exactly one heap implementation; optional kernel features must match the configuration.
3. Use a consistent ABI throughout: proposed Cortex-M4F flags are `-mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard`. Validate FPU/context handling and clock settings rather than assuming them.
4. Create the smoke/benchmark task, then start the scheduler. Implement `bench_port` with bounded native queue operations and calibrated timer reads. Link the shared runner.

After those files exist, the intended invocation is:

```sh
: "${RTOS_PROJECT:?absolute project root}"
: "${RTOS_BUILD_DIR:?isolated build directory}"
cmake -S "$RTOS_PROJECT/firmware/c-kernels/freertos/c" -B "$RTOS_BUILD_DIR" -G Ninja \
  -DCMAKE_TOOLCHAIN_FILE="$RTOS_PROJECT/firmware/c-kernels/freertos/c/cmake/arm-none-eabi.cmake" \
  -DFREERTOS_KERNEL_PATH="$RTOS_SOURCE_DIR/freertos-kernel" \
  -DSTM32CUBE_F4_PATH="$RTOS_SOURCE_DIR/stm32cubef4"
cmake --build "$RTOS_BUILD_DIR"
```

The toolchain file is a generation task, not present yet. Export ELF, map and loadable image paths in the artifact manifest. Implement ST-Link flashing through a pinned OpenOCD/probe backend with an explicit probe selector and verified load address. Never guess device selection or infer UART from successful SWD access.

## Rust pairing (implemented 2026-09-21; Pico 2 added 2026-10-02)

`firmware/c-kernels/freertos/rust` is a `no_std` staticlib that implements the shared
`rtos_bench::Port` and exports `rust_bench_run_case`. The same crate serves every
target; the C session keeps the UART/JSON protocol and calls it per case when
`BENCH_LANGUAGE_RUST` is defined. Builds are selected with `-DBENCH_RUST=ON`
(the plugin sets it for `--language rust`), giving build_ids
`freertos-stm32f4-rust-<sha8>`, `freertos-c6-rust-<sha8>` and
`freertos-pico2-rust-<sha8>`.

- **STM32F4**: `thumbv7em-none-eabihf` (hard-float matches the C objects), linked
  through the Makefile's `RUST=1` path; the Rust adapter calls the FreeRTOS
  symbols directly and reads DWT for time.
- **C6 (ESP-IDF)**: `riscv32imac-unknown-none-elf`, linked with
  `target_link_libraries(${COMPONENT_LIB} INTERFACE ...)` from
  `firmware/c-kernels/freertos/c/main/CMakeLists.txt`; `bench_rust_*` accessors and
  `bench_isr_*` wrappers live in `main/bench.c`. Three ESP-IDF differences from
  upstream FreeRTOS required target-specific handling in the crate:
  1. IDF declares `xTaskCreate` as a `static inline` over
     `xTaskCreatePinnedToCore`, so the crate calls the pinned variant on
     RISC-V (`task_create`).
  2. IDF sizes the `xTaskCreate` stack argument in **bytes**, not words, so the
     peer-task stack is 4096 bytes on RISC-V (256 would overflow immediately).
  3. The periodic-jitter period must derive from the port's tick base
     (`ticks_per_second / 1000`), not a hardcoded 168 MHz DWT constant.
- **Pico 2 (RP2350)**: `thumbv8m.main-none-eabi` (soft-float ABI matching the
  pico-sdk `-mfloat-abi=softfp` C objects), linked from
  `firmware/c-kernels/freertos/c/pico2/CMakeLists.txt`; the plugin builds the staticlib
  with `cargo` for that target before configuring CMake.

Neither route is a native Rust kernel; the RTOS stays C and is called through the
same primitives as the C path.

## Pico 2 (RP2350): pico-sdk + RP2350_ARM_NTZ port

FreeRTOS on the Pico 2 uses the upstream kernel with the Community-Supported
`portable/ThirdParty/Community-Supported-Ports/GCC/RP2350_ARM_NTZ` port and
heap_4, built by the pico-sdk CMake project in `firmware/c-kernels/freertos/c/pico2`:

```sh
cmake -S firmware/c-kernels/freertos/c/pico2 -B firmware/c-kernels/freertos/c/pico2/build -G Ninja \
  -DPICO_BOARD=pico2 \
  -DPICO_SDK_PATH="$PWD/.local/source/pico-sdk" \
  -DFREERTOS_KERNEL_PATH="$PWD/.local/source/FreeRTOS-Kernel" \
  -DBENCH_BUILD_ID=freertos-pico2-bench-<sha> [-DBENCH_RUST=ON]
cmake --build firmware/c-kernels/freertos/c/pico2/build
```

The plugin runs exactly this with the project-local `arm-none-eabi` toolchain and
`.local/toolchains/pico` on PATH. `FREERTOS_CONFIG_FILE_DIRECTORY` must point at
`firmware/c-kernels/freertos/c/pico2` before the port import, so the pico-sdk sources that
include `pico/config.h` can see `FreeRTOSConfig.h`. `configMAX_SYSCALL_INTERRUPT_PRIORITY`
is the port's tested value `16`, and `configSUPPORT_PICO_SYNC_INTEROP` is `0`
(single core, no pico-sync interop).

- **Timer**: Cortex-M33 DWT CYCCNT at clk_sys 150 MHz (`ticks_per_second=150000000`).
  Cases 4/10/11 use TIMER0 ALARM0 (IRQ 0), a force-pended TIMER1_IRQ_2 (IRQ 6) and
  TIMER0 ALARM1 / TIMER1 ALARM0 in the clk_sys `TIMERAWL` domain (mirrors the NuttX
  RP2350 port).
- **Console**: pico-sdk stdio USB CDC; an app `bootsel` line calls
  `reset_usb_boot(0, 0)` so the host can reflash without the physical button.
- **Flash**: UF2 mass-storage via `flash_pico2` (volume label `RP2350`), then wait
  for the CDC console to re-enumerate.
- **Rust**: `thumbv8m.main-none-eabi` staticlib (soft-float ABI matching the
  pico-sdk `-mfloat-abi=softfp` C objects), linked with `-DBENCH_RUST=ON`.

Build ids: `freertos-pico2-bench-<sha>` and `freertos-pico2-rust-<sha>`.
Benchmark-verified 2026-10-02 (cases 0-11, 1000 samples, three runs each).

## Installed ESP-IDF environment resolution

The C6 adapter asks the selected SDK's `tools/idf_tools.py export --format
key-value` for installed tool paths, parses the result without shell evaluation,
and uses the resolved virtual-environment Python explicitly for build and flash.
It overrides inherited SDK/Python selection with project-local paths. The SDK
selects compatible tool versions; no compiler-version directory is hardcoded.
Missing tools/export failures stop before building; this never installs tools.
A pristine `rtos --board esp32c6 build freertos` passed on 2026-09-19
(journal `20260919T014415Z-820cc0ed1f5e`); host checks passed
`20260919T014424Z-b6c5859b3cce`. This change did not reflash FreeRTOS.
