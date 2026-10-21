// --- Standalone preview bootstrap ---------------------------------------------
// When this chapter is compiled on its own (CLI or editor preview), the block
// below applies the thesis template and bibliography so the chapter renders in
// context. When thesis.typ includes this file it first sets the
// "_chapter_embedded" state, which disables the bootstrap because the thesis
// already supplies the template and bibliography. This is preview scaffolding,
// not part of the assembled thesis layout.
#import "@preview/uastw-thesis:0.1.0" as uastw
#show: it => context {
 if state("_chapter_embedded", false).get() { it }
 else { uastw.uastw-thesis-page-setup(it) }
}
#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex

// The appendix is included by thesis.typ, but not in this standalone preview.
// Use the real cross-reference when assembled and descriptive text otherwise.
#let benchmark-appendix = context {
  if state("_chapter_embedded", false).get() {
    ref(<app-benchmark-specifications>)
  } else {
    [the benchmark specification appendix]
  }
}
// ---[ end standalone preview bootstrap ]---------------------------------------

= Methodology

The experiments use the hardware, RTOS configurations and benchmarking framework described in the preceding chapter. The methodology is designed to distinguish differences between C and Rust from those caused by the hardware or RTOS kernel. For the primary comparison, both languages implement the same benchmark workloads on the same hardware and kernel. This allows the application implementations and their kernel interfaces to be compared under otherwise similar conditions.

The evaluation focuses on individual RTOS operations rather than complete applications. Each benchmark uses defined workloads and measurement boundaries to support consistent comparisons. Rust-native kernels are evaluated separately, as they do not provide equivalent C and Rust application configurations.

== Benchmark Suite

The benchmark suite contains 26 cases covering the main operations and timing characteristics of an RTOS. Several are based on operations considered by Rhealstone, while others examine communication, interrupt behaviour, CPU usage and memory allocation @Kar1990Rhealstone. The suite is not intended to conform to an existing benchmark standard.

@benchmark-overview lists the cases and their measured quantities. The following descriptions explain the timing boundaries and workload parameters where these are important for interpreting the results.


#figure(
  {
    set text(size: 8pt)
    table(
      columns: (0.35fr, 2fr, 2.8fr, 0.65fr),
      inset: (x: 4pt, y: 3pt),
      align: (left, left, left, center),
      table.header(
        [*No.*], [*Benchmark*], [*Measured quantity*], [*Reported unit*],
      ),

      [1], [`timer_overhead`], [Timer and runner overhead], [µs],
      [2], [`queue_roundtrip`], [Queue send and receive], [µs],
      [3], [`semaphore_latency`], [Semaphore give and take], [µs],
      [4], [`mutex_uncontended`], [Mutex lock and unlock], [µs],
      [5], [`alloc_latency`], [Allocation and release of 64 bytes], [µs],

      [6], [`task_handoff`], [Task release to receiver execution], [µs],
      [7], [`yield_roundtrip`], [Voluntary context-switch round trip], [µs],
      [8], [`preempt_latency`], [Higher-priority task wake-up], [µs],
      [9], [`mutex_priority_inheritance`], [Waiting time under priority inversion], [µs],
      [10], [`scheduler_overhead`], [Scheduler call without a task switch], [µs],
      [11], [`notification_latency`], [Task notification to resumption], [µs],

      [12], [`ipc_pingpong`], [Request-and-reply latency], [µs],
      [13], [`ipc_payload_sweep`], [Latency across message sizes], [µs],
      [14], [`ipc_throughput`], [Sustained message transfer rate], [B/s],
      [15], [`zero_copy_ipc`], [Saving from reference-based transfer], [ticks],

      [16], [`isr_to_task`], [Interrupt handler to task resumption], [µs],
      [17], [`trigger_to_isr`], [Interrupt request to handler entry], [µs],
      [18], [`nested_irq_overhead`], [Additional time from interrupt nesting], [µs],
      [19], [`tick_overhead`], [Execution time of one kernel tick], [µs],
      [20], [`max_interrupt_disabled`], [Interrupt delay caused by masking], [µs],

      [21], [`periodic_jitter`], [Variation in periodic wake intervals], [µs],
      [22], [`periodic_jitter_loaded`], [Periodic release error under load], [µs],
      [23], [`sleep_accuracy`], [Error of a requested sleep], [µs],

      [24], [`idle_cpu_overhead`], [Tick processing share while idle], [ppm],
      [25], [`cpu_overhead_under_load`], [Non-idle CPU share under load], [ppm],
      [26], [`heap_fragmentation`], [Fragmentation of available heap memory], [ppm],
    )
  },
  caption: [Overview of the 26 benchmark cases, their measured quantities and reported units.],
) <benchmark-overview>

Timing measurements are recorded as raw counter ticks and converted to microseconds using each configuration's recorded counter frequency. Throughput is derived from the elapsed ticks of the fixed 1 MiB transfer.

The benchmarks cover different aspects of RTOS performance. Some measure the cost of individual kernel operations, while others involve task scheduling, communication or interrupt handling. The suite also examines periodic timing, CPU usage and memory allocation.

Most timing measurements include more than the kernel operation itself. They may also include API calls, language bindings, scheduling decisions or timer access. The results therefore describe the complete measured operation rather than the isolated cost of a kernel primitive.

The workloads are designed to be comparable, but their implementation may differ between kernels due to differences in synchronisation mechanisms, interrupt handling and load generation. These differences can influence measured timings independently of the programming language. Therefore, direct C and Rust comparisons are restricted to the same kernel, while comparisons between kernels must also account for differences in their implementations. The exact workloads, timing boundaries, parameters and checks for all 26 cases are documented in #benchmark-appendix.

== Measurement Procedure

The host and target device validate the command and measurement exchange described in Section 4.6. Invalid messages, incomplete runs, and timeouts are treated as failures rather than valid measurements. All received data is retained for later inspection.

The queue round trip, for example, measures a send and receive between two timestamp readings, while interrupt measurements require timestamps inside handlers or resumed tasks. Some hardware-dependent measurements use shared C routines for both language paths. In these cases, parts of the measurement or control procedure are implemented in C and used by both language configurations. The resulting C/Rust comparison therefore includes a shared implementation rather than two fully independent versions of the measured operation. This is distinct from using the same timer interface while implementing the workload separately in each language. The affected benchmark categories are described in Appendix A. Timing measurements are recorded as raw timer ticks and converted to microseconds using the recorded counter frequency. Throughput is reported in bytes per second (B/s) after converting each valid observation, while CPU utilisation is reported in parts per million (ppm).

The measured intervals are reported without subtracting timer overhead, as they represent the complete execution paths, including the instrumentation required to obtain them. Buffering prevents serial communication from affecting the measurements.

Each supported benchmark is configured for ten warm-up iterations followed by 100 recorded observations per run. Ten runs provide 1,000 observations per case before division into parameter variants and exclusion of invalid or sentinel observations.

For benchmarks with several parameter values, the samples are divided between them. For example, a benchmark with five payload sizes records 20 samples per size in each run. Across ten runs, this gives 200 samples per size.

== Statistical Evaluation

The analysis computes the median, the 95th, 99th and 99.9th percentiles (p95, p99 and p99.9), and the highest observed value. Appendix B presents the median and p99; the other summaries remain in the analysis output. Percentiles use linear interpolation between ordered observations. Samples from all ten runs are combined for each case and parameter value. Percentiles describe the upper tail of the measured quantity. For latency and other time measurements, higher percentiles represent longer observed durations, and the difference between p99 and the median describes the spread of slower observations. For throughput, p99 represents higher observed transfer rates and therefore the high-throughput tail; it does not describe unfavourable throughput. Throughput values are reported in B/s after converting each valid observation from its elapsed ticks.
These statistics describe the observed results. They do not establish worst-case execution times. This is particularly important for p99.9, which is close to the highest observed value when only 200 samples are available. For the loaded periodic-jitter benchmark, missed releases are counted separately and excluded from the latency statistics.

== Resource Measurement

Memory usage is evaluated using both the firmware image and measurements from the running system. Static memory usage is determined for each build, while runtime measurements are collected after the benchmarks finish. Runtime memory values are reported in bytes; static footprint values are reported in KiB.

Runtime memory usage is reported by the device in its completion message, outside the timed measurements. The framework records the benchmark task's stack high-water mark (the minimum unused stack space observed during execution), the free heap at the end of the run and, where available, the minimum free heap observed during execution. It also measures allocation overhead by comparing the requested size of a one-byte allocation with the memory consumed by the allocator. If a kernel does not expose the information required for a measurement through its API, the corresponding value is omitted. Failed task-creation and allocation-overhead measurements may instead be reported as zero, making them indistinguishable from valid zero values. No such ambiguous values occurred in the original measurement campaign.

The framework also estimates the memory overhead of creating a task. Where task creation is heap-accounted, it creates a temporary task and measures the resulting heap change. The requested stack size is subtracted, so the reported value includes the task control block and allocator metadata; on NuttX, it also includes additional task-creation bookkeeping. The temporary task is then deleted. Where a minimum free heap value is reported, it is sampled before this test. For kernels with statically allocated task objects, such as Zephyr and Rivet, the reported value is the size of the task structure alone. These values are therefore comparable between C and Rust on the same kernel, but not directly across different kernels.

To determine the static footprint, the host examines the firmware image directly rather than relying on a toolchain size report. It distinguishes code, read-only data, initialised data and zero-initialised data. Read-only data contributes to flash usage, while initialised data contributes to both flash and RAM. Zero-initialised data occupies RAM but does not require storage for its contents in flash.

Finally, the framework records the number of active tasks and the minimum and maximum free stack space among them. These are stored as aggregate values rather than separate records for every task.

== Safety Analysis

Besides performance and memory consumption, this thesis examines how memory-safety responsibilities differ between C and Rust implementations. The analysis identifies operations that may affect memory safety if used incorrectly. These include pointer dereferences, field and array accesses, pointer arithmetic and conversions, allocation and deallocation, accesses to mutable global state and calls to RTOS APIs. For Rust, the analysis also records whether operations occur within `unsafe` contexts.

To investigate these operations and how individual benchmarks access the RTOS, a dedicated static-analysis pipeline was developed. Existing tools such as Clippy, Clang Static Analyzer, Miri and CodeQL do not directly provide the combination of source-level operation counts and benchmark-specific tracing required for this study.

The analysis begins by parsing the project-written source code into abstract syntax trees (ASTs). These trees represent the structure of the code, allowing operations such as pointer dereferences and function calls to be identified without relying on text searches. C files are parsed using libclang with the compiler definitions and include paths of the corresponding build. Rust files are parsed using the syn library and examined with a custom extractor.

For Rust, an operation is classified as occurring within an `unsafe` context when it appears inside an `unsafe` block or function. The reported percentage is calculated by dividing the number of active, resolved operation records within these contexts by the total number of active, resolved operation records for the relevant source-code role. This measures the location of recorded operations, not the proportion of source code that is unsafe.

For each identified operation, the analysis records its type and location in the source code. For example, the analysis identifies `value = *ptr;` as a pointer dereference and records its file and line number. The location determines which part of the implementation the operation belongs to; if it occurs in the FreeRTOS integration code, it is attributed to that source-code role. This allows safety-relevant operations to be counted and compared separately across different parts of the implementations.

The next step examines how individual benchmarks access the RTOS. For each RTOS and board combination, a profile is generated from the benchmark definitions, source code and build information. A Python script examines the benchmark mappings and function definitions to identify the entry points of individual tests. It combines these with compiler settings and RTOS-specific rules to determine the relevant source files, bridge functions and RTOS interfaces.

The analysis uses these profiles to trace function calls from each benchmark towards the RTOS interface. C and Rust call paths are examined separately. Calls from Rust into C are connected by matching FFI declarations to their corresponding C functions. For example, tracing the FreeRTOS queue benchmark identifies the RTOS functions used to create and delete queues, as well as send and receive messages.

Not all call paths can be identified through direct function calls. Callbacks are included when their registrations can be resolved, while interrupt handlers are traced separately when they can be identified from the source code or platform configuration. Calls that cannot be resolved are marked accordingly in the tracing results. Since the tracing examines source code without executing it, the identified paths do not necessarily represent which functions are called at runtime.

The tracing results record function calls, their arguments and associated checks for each benchmark. They distinguish calls from the shared benchmark runner from those in the RTOS integration code and identify calls targeting RTOS services. These results complement the earlier source-level analysis, where safety-relevant operations are counted by firmware configuration and source-code component rather than by individual benchmark.

The analysis is limited to project-written benchmark and integration code. The internal implementations of third-party RTOS kernels and hardware-abstraction libraries are excluded. Operations that cannot be resolved are recorded separately and excluded from the confirmed operation counts. These counts show where safety-relevant operations occur in the source code, but not whether they are used incorrectly. Therefore, the analysis cannot establish the memory safety of a complete firmware system.

#block(breakable: false)[
  #heading(level: 2)[Complexity and Maintainability]

  To assess the effort involved in implementing and maintaining the C and Rust configurations, the study uses the four source-code metrics introduced in Section 2.6. The analysis covers project-written benchmark code, including shared runners and RTOS adapters. Third-party kernel implementations are excluded.
]

The metrics are calculated for individual modules. For Rust, the application and RTOS boundary modules are analysed separately. For C, the benchmark logic and supporting functions are analysed together because they share a source file. Consequently, the measured modules do not always represent identical boundaries in both languages. The results are therefore interpreted in relation to the code included in each module.

Code size is measured in source lines of code. Cyclomatic complexity measures the number of independent control-flow paths through a function. It is calculated as one plus the number of decision points. Cognitive complexity measures how difficult a function is to follow by assigning points to control-flow structures such as conditionals and loops. Additional points are assigned for nesting, so deeply nested structures receive higher scores. Both complexity metrics are reported using the median, mean and maximum across the functions in each module.

Module coupling is estimated by counting distinct dependencies declared through C `#include` directives and Rust `use` and `mod` statements. Dependencies are separated into internal and external ones. This provides a simple measure of declared dependencies, but does not represent the complete function-call or FFI graph.

Mozilla's rust-code-analysis provides the per-function cyclomatic and cognitive complexity values for both languages @RustCodeAnalysisDocs. A Python script calculates source lines of code and the dependency-based coupling metric. The analysis operates on individual source files, so functions defined in C headers are attributed to those headers rather than to files that include them.

#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}