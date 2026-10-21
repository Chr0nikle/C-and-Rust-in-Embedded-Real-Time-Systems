# Rust-native kernels

These are project-owned Rust implementations, not C-kernel overlays.

- `freertos-in-rust/` — a Rust translation of the FreeRTOS kernel used on
  STM32F4 and STM32H723. The project-specific port and benchmark application
  live here; the translation is maintained in this repository.
- `rivet/` — Rivet-based Rust-native kernel integration for STM32F4. Rivet is
  an upstream dependency pinned as `rivet-rtos` in
  `project/config/sources.toml`.

Both trees build for embedded Rust targets and link the shared
`firmware/common/rust/` runner.
