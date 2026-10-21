# Firmware ownership

`project/firmware/` contains project-specific firmware and RTOS integration
code. It is not a copy of the upstream kernels.

| Path | Ownership | Purpose |
|---|---|---|
| `common/c/`, `common/rust/` | Project | Shared runner, protocol and support code |
| `c-kernels/freertos/` | Project overlay | FreeRTOS C/Rust applications and board ports |
| `c-kernels/nuttx/` | Project overlay | NuttX C/Rust applications |
| `c-kernels/zephyr/` | Project overlay | Zephyr C/Rust applications and board overlays |
| `rust-kernels/freertos-in-rust/` | Project | Rust-native FreeRTOS translation |
| `rust-kernels/rivet/` | Project + upstream Rivet | Rivet-based Rust-native kernel integration |

Upstream kernels and SDKs are staged under ignored `project/.local/`:

- `project/.local/source/` — FreeRTOS-Kernel, NuttX, NuttX apps, RIOT, ChibiOS,
  RT-Thread, RTEMS and related sources
- `project/.local/west/` — Zephyr and its west modules
- `project/.local/sdk/esp-idf/` — ESP-IDF

Pinned revisions are in `project/config/sources.toml`. Build outputs
(`build/`, `target/`, per-board build directories) are ignored and must not be
treated as source.

See [UPSTREAM.md](UPSTREAM.md) for the revision table and
[c-kernels/README.md](c-kernels/README.md) and
[rust-kernels/README.md](rust-kernels/README.md) for ownership details.
