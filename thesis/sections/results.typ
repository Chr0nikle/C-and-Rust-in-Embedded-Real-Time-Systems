// --- Standalone preview bootstrap ---------------------------------------------

// This chapter can be compiled independently or included by thesis.typ.

#import "@preview/uastw-thesis:0.1.0" as uastw

#show: it => context {
  if state("_chapter_embedded", false).get() { it }
  else { uastw.uastw-thesis-page-setup(it) }
}

#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex

// --- End standalone preview bootstrap -----------------------------------------

= Results

The results compare C and Rust applications running on the same hardware and RTOS, while Rust-native RTOSs are considered separately. Not every RTOS configuration supports all 26 benchmark cases. The main gaps concern tick accounting, CPU-load measurements and memory allocation. Within each matched board and RTOS, C and Rust support the same cases. The complete coverage by configuration is provided in @app-benchmark-results.

== Benchmark Measurements

The benchmark measurements are presented in five RTOS-specific tables in @app-benchmark-results: @results-freertos, @results-nuttx, @results-zephyr, @results-freertos-in-rust and @results-rivet. @fig-performance-comparison provides an overview of the median Rust/C ratios for the matched configurations. A ratio of 1 indicates identical median values, while values above or below 1 indicate higher or lower measurements for Rust. Since the benchmarks measure different quantities, these differences do not necessarily indicate better or worse performance.

#figure(
  image("../generated/figures/performance-comparison.svg", width: 100%),
  caption: [Median Rust/C benchmark ratios across matched RTOS and hardware configurations.],
) <fig-performance-comparison>

Scheduling, synchronisation and interrupt benchmarks generally show similar median values for C and Rust on the same hardware and RTOS. However, the medians do not always reflect the higher latencies observed in individual measurements. For example, on Zephyr ESP32-C6, preemption latency (8) has a median of 8.75 µs for both languages, while p99 reaches approximately 470 µs in both implementations. The increased tail latency is therefore observed for both languages rather than being specific to either implementation.

Inter-task communication shows some of the clearest differences between C and Rust, although the results depend on the operation being measured. On FreeRTOS STM32F4, Rust achieves lower median payload-transfer latency (13) than C (26.8 versus 53.5 µs) and higher median IPC throughput (14) (3.92 versus 3.47 MB/s). This comparison uses the corrected per-sample conversion of protocol case 19, which times a fixed 1 MiB transfer. The difference includes RTOS, binding, adapter and shared-code effects and cannot be attributed to the programming language alone. The results vary across configurations, with zero-copy savings (15) showing no consistent advantage for either language.

For the payload-sweep benchmark, these values refer to the 256-byte payload, rather than an aggregate across all payload sizes. The median is calculated from 200 samples collected across ten runs for each language.

Loaded periodic jitter (22) requires particular care when comparing RTOSs because its load-generation mechanisms differ across platforms and boards. The reported percentiles describe only retained releases; some variants retain very few valid observations, as shown by their miss counts. Low survivor latencies must not be read as evidence of reliable release timing.

@fig-rust-kernel-comparison compares selected scheduling latencies on STM32F4 between conventional FreeRTOS and the two Rust-native kernels.

#figure(
  image("../generated/figures/rust-kernel-comparison.svg", width: 100%),
  caption: [Selected scheduling latencies across FreeRTOS and Rust-native RTOS configurations on STM32F4. Release mechanisms differ; these values do not isolate kernel-language effects.],
) <fig-rust-kernel-comparison>

Both Rust-native kernels record lower median values for the preemption benchmark (8) than conventional FreeRTOS, but higher task-handoff (6) and notification latencies (11). These are configuration-level observations: FreeRTOS-in-Rust uses an interrupt-driven release, whereas conventional FreeRTOS releases a semaphore waiter. The comparison therefore does not isolate a kernel-language effect. Rivet achieves the lowest preemption latency at 1.71 µs, compared with 3.08 µs for FreeRTOS-in-Rust and approximately 5.1 µs for conventional FreeRTOS. In contrast, the C and Rust applications on conventional FreeRTOS produce similar results across all three measurements.

The two Rust-native kernels also differ in communication and interrupt handling. Rivet records lower queue round-trip (2) and IPC ping-pong (12) latencies. On STM32F4, preserved summary-derived medians indicate higher IPC throughput (14) for Rivet than FreeRTOS-in-Rust (6.53 versus 5.05 MiB/s); however, the raw supplementary native bundle is unavailable, so these medians are not independently reproduced sample-level statistics and p99 throughput is not reported. FreeRTOS-in-Rust on STM32H723 has a higher derived median (6.96 MiB/s), but that is a different board and is not a same-board comparison. FreeRTOS-in-Rust records lower trigger-to-interrupt latency (17).




== Memory Usage

Memory is examined at two levels: runtime memory reported by the running benchmark, including heap high-water, stack headroom and per-task overhead, and the static memory contained in the linked ELF image. The complete measurements for all 27 firmware configurations are listed in @app-memory-results: @campaign-memory reports the runtime fields and @campaign-static the section-based static footprint. Runtime and static memory are kept separate here, and the static values are image sizes rather than runtime heap or stack observations.

Within matched C and Rust pairs, per-task RAM overhead is identical on the same kernel and board, and heap allocation overhead and heap fragmentation are close. The benchmark task's stack high-water mark (the minimum unused stack observed) is greater for Rust on every matched pair. This measures remaining benchmark-task headroom, not total memory use, and does not offset the larger static footprint observed on several boards. These runtime differences are small compared with the static footprint.

@fig-memory-footprint shows the static difference between Rust and C, calculated as the footprint of the C program subtracted from the footprint of the Rust version for each of the 12 matched configurations. Positive values mean that the Rust implementation uses more memory, negative ones mean that the C version uses more. Flash and RAM usage are measured and depicted separately.

#figure(
  image("../generated/figures/memory-footprint.svg", width: 100%),
  caption: [Static flash and RAM differences between matched Rust and C firmware configurations.],
) <fig-memory-footprint>

Static RAM differences vary by platform. On FreeRTOS, the C and Rust implementations use nearly identical amounts on the Arm boards, while Rust requires an additional 31.9 KiB on ESP32-C6. NuttX also shows small differences on the Arm boards, but Rust uses 94.5 KiB more RAM on ESP32-C6, an increase of approximately 30%. For Zephyr, memory usage is almost identical on STM32F4, whereas Rust requires an additional 63.5–69.0 KiB on the remaining boards.

The Rust-native kernels show a different memory footprint on STM32F4. FreeRTOS-in-Rust uses 38.0 KiB of flash and 78.3 KiB of static RAM, less than either conventional FreeRTOS configuration. Rivet uses more flash (78.3 KiB) but less static RAM (46.7 KiB) than both. These differences reflect complete firmware configurations rather than the kernel implementation language alone. The full measurements are provided in @app-memory-results.







== Static Safety Analysis

The safety analysis examines how C and Rust implementations handle operations that may affect memory safety. Besides identifying these operations, it distinguishes between application code and the code used to interact with the RTOS. This allows the results to show not only which operations occur, but also where they are located. The complete measurements are provided in @app-safety-results.

A clear difference emerges in how pointer operations are distributed. In the C implementations, pointer dereferences occur in both the benchmark applications and their RTOS adapters. The adapters contain more dereferences across all matched configurations, although the counts vary between boards. For example, the FreeRTOS adapter contains 47 occurrences on both STM32 boards but 142 on nRF52840. Pointer arithmetic follows a different pattern. It occurs mainly in the C applications, while the Zephyr and NuttX adapters contain none. The distribution therefore depends on the type of operation being examined.

The Rust implementations show a different result. Across all 12 matched configurations, their application code contains no resolved raw-pointer dereferences, pointer arithmetic or pointer conversions. Direct raw-pointer dereferences remain present in the FFI boundaries, however. @fig-safety-c-rust-source-roles illustrates this difference in source-code organisation.

#figure(
  image("../generated/figures/safety-c-rust-source-roles.svg", width: 100%),
  caption: [Resolved pointer dereferences by application and integration role. Ranges span boards; C and Rust operation categories are not directly comparable.],
) <fig-safety-c-rust-source-roles>

The Rust FFI boundaries contain 17 resolved raw-pointer dereferences on FreeRTOS, 172 on Zephyr and 169 on NuttX. The latter two values are lower bounds because some boundary dereferences could not be resolved. These findings indicate that the analysed Rust applications avoid direct raw-pointer handling, while such operations remain necessary in their integration code. The Rust firmware also includes C support code, which is analysed separately in @app-safety-results. Because C and Rust use different syntax categories, the operation counts cannot be interpreted as a direct comparison of memory safety.

Furthermore, the absence of raw-pointer dereferences does not mean that the Rust applications avoid `unsafe` contexts. The analysis also records whether operations occur within such contexts, as shown in @fig-safety-rust-unsafe-context.

#figure(
  image("../generated/figures/safety-rust-unsafe-context.svg", width: 100%),
  caption: [Share of resolved Rust application operations inside unsafe contexts. This measures the location of recorded operations, not the proportion of unsafe source code.],
) <fig-safety-rust-unsafe-context>

The difference is particularly noticeable on FreeRTOS, where 99.8% of the resolved application operations occur within `unsafe` contexts, compared with approximately 71–72% on Zephyr and NuttX. This does not contradict the pointer results. The inventory includes RTOS calls and accesses to global state, so operations can be recorded inside `unsafe` contexts without involving raw-pointer dereferences. All resolved operation records in the Rust FFI boundaries also occur within `unsafe` contexts. The percentages therefore describe how the code is organised rather than how safe it is.

The Rust-native systems offer another perspective on this distinction. @fig-safety-freertos-vs-fir-unsafe compares conventional FreeRTOS with FreeRTOS-in-Rust, whose analysed project code also contains a high proportion of operations within `unsafe` contexts.

#figure(
  image("../generated/figures/safety-freertos-vs-fir-unsafe.svg", width: 100%),
  caption: [Operations within unsafe contexts for conventional FreeRTOS Rust components and FreeRTOS-in-Rust project code. The analysed source scopes differ.],
) <fig-safety-freertos-vs-fir-unsafe>

In FreeRTOS-in-Rust, 176 of 181 recorded operations (97.2%) occur within `unsafe` contexts. The analysis also identifies one resolved and 21 unresolved raw-pointer dereferences. Rivet similarly contains one resolved raw-pointer dereference, alongside 31 unresolved records. However, these inventories cover only project-written application and board-support code, not the external Rust-native kernel implementations. Their results therefore provide additional observations about low-level code rather than a direct safety comparison between kernels.

Finally, the benchmark-specific source tracing connects these observations to the evaluated workloads. It covers 21–25 comparable cases per matched configuration and identifies 63–75 FFI symbols, depending on the RTOS. This provides information about which interfaces the benchmark code can reach, although the traces do not measure how often those calls execute.


== Integration Complexity <sec-integration-complexity>

The differences between C and Rust extend beyond performance, memory usage and safety. Their implementations also differ in how much code they require and how that code is organised. This section examines these differences through code size, function complexity and dependencies. The complete measurements are provided in @app-integration-results.

@fig-integration compares the amount of hand-written benchmark and integration code across the evaluated configurations.

#figure(
  image("../generated/figures/fig-integration.svg", width: 100%),
  caption: [Hand-written benchmark and kernel-integration code across the evaluated configurations.],
) <fig-integration>

Across the matched RTOSs, the Rust application and FFI modules contain fewer lines of hand-written code than the corresponding C implementations. The C configurations also include substantial kernel and SDK code, which is reported separately in @app-integration-results.

However, the shared benchmark runners show a different pattern. The C runner contains 493 lines of code across four functions, whereas the Rust runner contains 636 lines across 83 functions. The maximum cyclomatic complexity is 250 for C and 113 for Rust, while maximum cognitive complexity is 315 and 61, respectively. The Rust runner therefore has more functions but lower measured maximum complexity.

This difference also appears in the integration modules, although their complexity is generally lower than that of the shared runners. Maximum cyclomatic complexity ranges from 19 to 29 for the C ports and from 6 to 12 for the Rust FFI modules. The C ports also have 4–12 declared dependencies, compared with two for each Rust FFI module.

The two runners also differ in their source structure. They share 104 distinct identifiers. Dividing this intersection by the 279 identifiers in their union gives a Jaccard similarity of 0.37. This describes identifier overlap rather than functional equivalence. Likewise, the dependency counts reflect declared imports and includes, not all function calls or FFI interactions.




#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}

#context if not state("_chapter_embedded", false).get() {
  include "benchmark-results.typ"
}

#context if not state("_chapter_embedded", false).get() {
  include "memory-results.typ"
}

#context if not state("_chapter_embedded", false).get() {
  include "safety-results.typ"
}

#context if not state("_chapter_embedded", false).get() {
  include "integration-results.typ"
}