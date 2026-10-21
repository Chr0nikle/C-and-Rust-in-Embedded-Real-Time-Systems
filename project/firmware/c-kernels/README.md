# C-kernel integrations

These directories contain the project's C and Rust applications, board ports,
configuration and RTOS-facing adapters. They are overlays on upstream kernels,
not vendored kernel trees.

- `freertos/c/` — ESP-IDF application (`main/`), STM32F4, STM32H723 and
  nRF52840 board builds, FreeRTOS configuration.
- `freertos/rust/` — Rust application running on the C FreeRTOS kernel.
- `nuttx/c/` and `nuttx/rust/` — NuttX application and port configuration.
- `zephyr/c/` and `zephyr/rust/` — Zephyr applications and board overlays
  (including ESP32-C6).

Upstream sources are staged under `project/.local/`; their revisions are listed
in [../UPSTREAM.md](../UPSTREAM.md). Build directories here are generated and
ignored.
