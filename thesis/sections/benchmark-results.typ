= Complete Benchmark Results <app-benchmark-results>

#text(size: 8.5pt)[
  Each benchmark cell gives the pooled median above and p99 below. A dot marks
  a case the configuration does not advertise; a single value means p99 is
  unavailable. The suffixes k and M denote 10³ and 10⁶. Parameterised rows show
  selected variants: the base peer count for case 7, 256 bytes for case 13, the
  highest recorded load for case 22 and 100 % offered load for case 25. For
  latency, lower is better; for IPC throughput (14), higher is better and p99 is
  the higher-throughput tail. An asterisk marks a configuration with filtered observations, listed in the note table after the five value tables.
]

#include "../generated/typ/campaign-coverage.typ"

#v(0.5em)
#text(size: 8pt)[
  Zephyr nRF52840 throughput uses 980 C and 987 Rust valid historical
  observations; 20 C and 13 Rust samples with zero or wrapped durations are
  excluded and counted in the table. These filtered observations do not support
  lower-tail or worst-case throughput claims. For the Rust-native kernels, only
  a summary-derived median is shown because the supplementary raw bundle is
  unavailable; p99 throughput is not reported.
]

#pagebreak()

#include "../generated/typ/all-results.typ"
