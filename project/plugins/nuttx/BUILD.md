# NuttX integration recipe

Status: C/Rust implemented and collected at 1000 samples/case on both boards (STM32F4 and C6, cases 0-4). References checked 2026-09-16; Rust pairing implemented 2026-09-21 (see below).

## ESP32-C6 `isr_to_task` (advertised; console pacing required)

`firmware/c-kernels/nuttx/c/isr.c` implements case 4 for the C6 with the free SYSTIMER
alarm 1 (target 1, IRQ source 58; the system tick owns alarm 0), timestamping
with the C6 performance counter (CSR 0x7e2). An earlier full-suite wedge was a
console stall, not case 4: a 1000-frame flush filled the USB-Serial-JTAG TX ring,
and blocking inside the tty driver wedged it. `main.c` now opens the console
`O_NONBLOCK` and paces `tx_string` (retry `EAGAIN` at 200 us, bounded ~5 s then
drop the frame). With that, both C and Rust complete cases 0-4 at 1000
samples/case. The C6 USB-JTAG quirk is still intermittent; retry or physically
reset if a run loses the port.

## Acquire matched repositories

Use paired NuttX and apps revisions following the [installation layout](https://nuttx.apache.org/docs/latest/quickstart/install.html). The directories must be siblings; install that revision's Kconfig, make, host prerequisites and cross compiler.

```sh
: "${RTOS_NUTTX_WS:?absolute new NuttX workspace}"
: "${RTOS_NUTTX_REV:?full chosen kernel commit}"
: "${RTOS_NUTTX_APPS_REV:?matching apps commit}"
mkdir -p "$RTOS_NUTTX_WS"
git clone https://github.com/apache/nuttx.git "$RTOS_NUTTX_WS/nuttx"
git clone https://github.com/apache/nuttx-apps.git "$RTOS_NUTTX_WS/apps"
git -C "$RTOS_NUTTX_WS/nuttx" checkout --detach "$RTOS_NUTTX_REV"
git -C "$RTOS_NUTTX_WS/apps" checkout --detach "$RTOS_NUTTX_APPS_REV"
```

Use distinct disposable workspace copies for C6/F4 and C/Rust configurations; the native make build modifies the source tree. Never switch experiments in a shared mutable cache checkout.

## ESP32-C6

The [DevKitC board](https://nuttx.apache.org/docs/latest/platforms/risc-v/esp32c6/boards/esp32c6-devkitc/index.html) uses `esp32c6-devkitc:<configuration>`. Confirm the carrier. The [C6 architecture guide](https://nuttx.apache.org/docs/latest/platforms/risc-v/esp32c6/index.html) describes a `riscv-none-elf` compiler and esptool/boot requirements; do not assume IDF's compiler/flash layout is interchangeable. Select and checksum a toolchain compatible with the chosen revision.

```sh
cd "$RTOS_NUTTX_WS/nuttx"
./tools/configure.sh esp32c6-devkitc:nsh
make -j4
# Later, with verified device selection and required boot images:
# make flash ESPTOOL_PORT="$RTOS_PORT"
```

## STM32F4 Discovery

Use a matching Arm GNU toolchain and the [Discovery NSH configuration](https://nuttx.apache.org/docs/latest/platforms/arm/stm32f4/boards/stm32f4discovery/index.html) in a separate workspace:

```sh
cd "$RTOS_NUTTX_WS/nuttx"
./tools/configure.sh stm32f4discovery:nsh
make -j4
```

The documented NSH console uses USART2. Verify that this matches our onboard-debug UART routing; do not assume the probe forwards those pins. The flash adapter must use a verified ST-Link selector, loadable ELF or correct binary address, and a compatible OpenOCD/probe configuration. Capture `.config`, ELF, map, binary and tool versions.

## Integrate our application

NSH is only a bring-up baseline. Register the C session/runner/port under apps using the selected revision's Kconfig/Make.defs/Makefile conventions. Add a saved project configuration enabling that application and a startup path into it. Do not report the NSH prompt as our UART ready record. `firmware/c-kernels/nuttx/c/CMakeLists.txt` is only a blocking reminder; this route normally needs native app registration.

## Rust pairing (implemented 2026-09-21, both boards)

`firmware/c-kernels/nuttx/rust` is a `no_std` staticlib (`librtos_firmware_nuttx.a`) built
for `thumbv7em-none-eabi` (STM32F4) or `riscv32imac-unknown-none-elf` (C6). Both
images are soft-float (STM32F4 `CONFIG_ARCH_FPU` unset; the C6 has no FPU), so
hard-float libraries would not match the C ABI. The crate implements the shared
`rtos_bench::Port` and exports `rust_bench_run_case`; the C session in `main.c`
keeps the UART/JSON protocol and calls it per case when `BENCH_LANGUAGE_RUST`
is defined.

CMake configures this with `-DBENCH_RUST=ON` (the plugin sets it for
`--language rust`; the build_ids become `nuttx-stm32f4-rust-<sha8>` and
`nuttx-c6-rust-<sha8>`). The Rust port reaches the NuttX/POSIX primitives
(mqueue, pthread + semaphores, absolute `clock_nanosleep`, the STM32F4 TIM2 ISR
harness) through thin C shims in `port.c` that reuse the same operations as the
C port; the C6 shims also enable the performance counter because the Rust path
does not call `nuttx_bench_port_init`. The archive is appended to the global
`NUTTX_EXTRA_LIBRARIES` property (the ameba board pattern) so it is scanned
after the app objects; a direct `target_link_libraries` placed it before them
and the linker discarded `rust_bench_run_case`. The official NuttX Rust example
route is still available if a bindgen-based adapter is preferred later.

## nRF52840 DK (added 2026-09-27)

`boards/arm/nrf52/nrf52840-dk` with `firmware/c-kernels/nuttx/c/bench_nrf52840dk_defconfig`
(`rtos nuttx --board nrf52840dk`). The plugin copies the defconfig to
`boards/arm/nrf52/nrf52840-dk/configs/bench` and builds with
`-DBOARD_CONFIG=boards/arm/nrf52/nrf52840-dk/configs/bench`. Console is UART0
(`CONFIG_NRF52_UART0`, `CONFIG_UART0_SERIAL_CONSOLE`) on the J-Link VCOM at 115200.
Flash/debug over the onboard J-Link: probe-rs with `--chip nRF52840_xxAA`
(auto-detect fails on this part) and probe `1366:1061:001050202809`. Timestamps use the
Cortex-M4 DWT cycle counter at 64 MHz (`port.c` reads it directly because nRF52
does not select `ARCH_HAVE_PERF_EVENTS`; `nrf52_dwt_init` only enables it and must
not reset CYCCNT). Case 10 force-pends EGU0 (NuttX IRQ 36); case 4 uses a TIMER1
one-shot (nRF52 TIMER `INTENSET.COMPARE0` is bit 16, `SHORTS.COMPARE0_STOP` bit 8).
Rust: `-DBENCH_RUST=ON`, `thumbv7em-none-eabi`, build_id
`nuttx-nrf52840dk-rust-<sha8>`; the Rust shims enable DWT on first use.

The common queue workload needs bounded nonblocking u32-copy semantics. If using POSIX mqueue, configure capacity/message size/priority explicitly and document differences from other kernels. Timer and interrupt measurements need board-specific validation; generic POSIX calls are not automatically adequate instrumentation.
