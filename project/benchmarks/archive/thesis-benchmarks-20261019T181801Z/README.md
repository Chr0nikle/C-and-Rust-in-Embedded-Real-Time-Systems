# Thesis benchmark source archive

Snapshot of the benchmark implementations, shared harness, suite specification,
board metadata and source-pin configuration used by the Master's thesis:

*A Traceable Comparison of C and Rust on Embedded Real-Time Operating Systems.*

Included:

- `benchmarks/specifications.md` and `benchmarks/suite.toml`
- `firmware/common` shared C/Rust harness (excluding host test copies and blink experiments)
- `firmware/c-kernels/freertos`, `nuttx`, `zephyr` C and Rust implementations
- `firmware/rust-kernels/freertos-in-rust` and `rivet` sources, including the pinned vendored dependency
- `config/sources.toml`, `config/rtos.toml` and `boards/*/board.toml`
- `rules.md` and the repository `README.md` as scope context

Excluded:

- build products, `target/` trees and flashable binaries
- experimental blink/cube-blink code and host-side test copies
- external NuttX/Zephyr/FreeRTOS checkouts and fetched dependencies outside the archive
- thesis chapters, safety-analysis reports and measurement results

External RTOS revisions are pinned in `config/sources.toml`; those source trees
are not embedded. Hashes for every archived file are in `FILES.sha256` and
`ARCHIVE-MANIFEST.json`.
