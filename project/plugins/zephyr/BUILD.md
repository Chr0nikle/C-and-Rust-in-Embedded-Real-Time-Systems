# Zephyr integration recipe

> **Scope note (2026-10-12):** Pico 2 (RP2350) references below are historical;
> the Pico 2 and STM32H723 boards were removed from the project. Current Zephyr
> boards are the STM32F4 Discovery, ESP32-C6 and nRF52840 DK.

Status: C6 C native development benchmark verified on 2026-09-19, and the C6 C
build/flash route is wired into the public CLI (`rtos --board esp32c6 build
zephyr`, `rtos --board esp32c6 --port <port> zephyr`). Rust routes remain
unverified. Upstream references checked 2026-09-16.

## Acquire workspace

Use a dedicated Python environment and the native west workspace workflow from [Zephyr getting started](https://docs.zephyrproject.org/latest/develop/getting_started/index.html). Pin west, SDK and Python dependencies with the selected Zephyr revision.

```sh
: "${RTOS_ZEPHYR_WS:?absolute new west workspace}"
: "${RTOS_ZEPHYR_REV:?chosen immutable Zephyr revision}"
west init -m https://github.com/zephyrproject-rtos/zephyr --mr "$RTOS_ZEPHYR_REV" "$RTOS_ZEPHYR_WS"
cd "$RTOS_ZEPHYR_WS"
west update
west zephyr-export
# On revisions providing these commands:
west packages pip --install
west sdk install
west manifest --freeze > "$RTOS_ZEPHYR_WS/frozen-manifest.yml"
```

For older selected releases use that release's Python requirements/SDK installation instructions. Freeze all module revisions after any optional modules are enabled; copying the top-level SHA alone is insufficient.

## Board-specific C bring-up

The upstream [ESP32-C6 DevKitC board](https://docs.zephyrproject.org/latest/boards/espressif/esp32c6_devkitc/doc/index.html) defines target `esp32c6_devkitc/esp32c6/hpcore`; confirm our carrier before using it. The documented Espressif module flow includes `west blobs fetch hal_espressif`. Track fetched blob versions/licenses and use the selected release's bootloader mode.

```sh
cd "$RTOS_ZEPHYR_WS"
west blobs fetch hal_espressif
west build -p always -d build-c6 -b esp32c6_devkitc/esp32c6/hpcore zephyr/samples/hello_world
```

The upstream [Discovery board](https://docs.zephyrproject.org/latest/boards/st/stm32f4_disco/doc/index.html) uses `stm32f4_disco`:

```sh
cd "$RTOS_ZEPHYR_WS"
west build -p always -d build-f4 -b stm32f4_disco zephyr/samples/hello_world
```

These compile upstream hello applications, not our benchmarks. To integrate ours, implement `firmware/c-kernels/zephyr/c/CMakeLists.txt`, prj.conf, board console overlays, native main and port. Replace the sample source argument with the absolute application directory and use a configuration-specific build directory under project target/. Preserve `.config`, devicetree and ELF/map in provenance.

Use `west flash -d <build-directory>` with the pinned runner's explicit device selection options. Reconcile console port and reset behavior with the core UART service. Do not launch `west espressif monitor` or another serial terminal from the final workflow.

## Rust pairing

Implemented for **all four boards** (Pico 2 added 2026-09-26, nRF52840 DK
2026-09-27). `firmware/c-kernels/zephyr/rust` is a `no_std` staticlib
(`librtos_firmware_zephyr.a`) built for `thumbv7em-none-eabi` (STM32F4 and
nRF52840), `riscv32imac-unknown-none-elf` (C6) or `thumbv8m.main-none-eabi`
(RP2350 Cortex-M33). All four C images are soft-float (STM32F4, nRF52840 and the
Pico 2 have `CONFIG_FPU` unset; the C6 has no FPU), so hard-float libraries would
not match the C ABI. The corresponding rustup targets must be installed
(`rustup target add thumbv7em-none-eabi riscv32imac-unknown-none-elf
thumbv8m.main-none-eabi`); they are not provisioned by a setup script. The Rust
crate implements the shared `rtos_bench::Port` and exports
`rust_bench_run_case`; the C session in each board's `main.c` keeps the
UART/JSON protocol and calls it per case when `BENCH_LANGUAGE_RUST` is defined.

Each board's `CMakeLists.txt` builds and links the staticlib when configured
with `-DBENCH_RUST=ON`, which the plugin sets for `--language rust`; the
build_ids become `zephyr-stm32f4-rust-<sha8>`, `zephyr-c6-rust-<sha8>`,
`zephyr-pico2-rust-<sha8>` and `zephyr-nrf52840dk-rust-<sha8>` (C stays
`...-bench-...`). Zephyr's `k_*` surface is
macro/inline and passes `k_timeout_t` structs by value, so the Rust port reaches
the primitives through thin C shims in each `port.c` that reuse the same
operations as the C port; this is the closest ABI-stable equivalent. Timing,
queue size/payload, thread priority and the ISR timer are identical to the C
path.

The official `zephyr-lang-rust` module route described in earlier drafts is not
used; it remains available if a bindgen-based adapter is preferred later.

## Verified C6 development build environment

The 2026-09-19 native route uses `ZEPHYR_BASE` pointing at `.local/west/zephyr`,
`ZEPHYR_TOOLCHAIN_VARIANT=cross-compile`, and absolute `CROSS_COMPILE` ending in
`riscv32-esp-elf-` from `esp-14.2.0_20260121`. Prepend `.local/venv/zephyr/bin`
to PATH for esptool 5.4.0. The pristine build log in
`results/campaigns/zephyr-c6-20260919T0126Z/logs/build.log` retains all ten explicit CMake
tool paths. Source is `firmware/c-kernels/zephyr/c/esp32c6`; target is
`esp32c6_devkitc/esp32c6/hpcore`. Generated runners.yaml specifies the single
image at 0x0, dio, 80 MHz, 8 MB. Native tool use was explicitly authorized
because the public CLI rejects this board. This does not promote CLI support.

Console input capacity is 1,025 bytes for protocol frames up to 1,024 bytes.
Wait for each hello reply before another request: console echo can interleave
with application output for pipelined requests. The original verifier only warns
on count/frequency differences; the archived wrapper adds strict identity,
challenge, sample order/count, frequency, run ID and completion checks.
