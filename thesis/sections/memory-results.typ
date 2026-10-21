#set page(margin: (top: 0.5cm, bottom: 0.5cm, left: 0.75cm, right: 0.75cm))

= Complete Memory Results <app-memory-results>

@campaign-memory lists the runtime memory collector fields for all 27 firmware
configurations, and @campaign-static lists their static flash and RAM
footprints. A dot or dash marks a field that the adapter does not report. In the
runtime table, `Heap over` is allocation overhead, `Task RAM` is the
kernel-specific task overhead, `Worst` and `Best` are the minimum and maximum
free stack values across tasks, `Bench stack free` is the benchmark task's
remaining stack headroom, `Heap min` is the minimum observed free heap, and
`Heap free` is free heap at completion. All values are bytes except the task
count. The three Rust-native rows are preserved historical summaries because
their raw campaign bundle is unavailable.

#include "../generated/typ/campaign-memory.typ"

#pagebreak()

#include "../generated/typ/campaign-static.typ"
