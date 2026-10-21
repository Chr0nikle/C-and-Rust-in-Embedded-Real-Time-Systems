
// --- Standalone preview bootstrap ---------------------------------------------

// This chapter can be previewed independently or included from thesis.typ.

#import "@preview/uastw-thesis:0.1.0" as uastw
#import "../styles/callouts.typ": takeaway

#show: it => context {
  if state("_chapter_embedded", false).get() { it }
  else { uastw.uastw-thesis-page-setup(it) }
}

#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex

// --- End standalone preview bootstrap -----------------------------------------


= State of the Art

Previous research has examined embedded RTOS performance, compared programming languages on microcontrollers, and investigated Rust's memory-safety guarantees. However, these studies often focus on individual kernels, programming languages, or integration approaches.

This chapter reviews established RTOS benchmarks, empirical comparisons, and research on Rust interoperability. It examines their methods and limitations to identify the research gap addressed by this thesis.


== RTOS Benchmarking Approaches

Comparing RTOS performance requires benchmarks that measure defined operations under reproducible conditions. One early approach is the *Rhealstone benchmark*, introduced by Kar and Porter in 1989. It evaluates six timing measurements intended to characterise the real-time behaviour of a processor and its operating system @Kar1989Rhealstone.

Rhealstone defines six components @Kar1990Rhealstone:

- *Task switching:* The time required to switch between two tasks of equal priority.
- *Preemption:* The time required for a higher-priority task to take over from a lower-priority task.
- *Interrupt latency:* The delay between an interrupt request and the beginning of its service routine.
- *Semaphore shuffling:* The kernel overhead associated with transferring a semaphore between tasks.
- *Deadlock breaking:* The time associated with resolving priority-related resource contention.
- *Inter-task message latency:* The time required to transfer a message between tasks.

These components cover scheduling, interrupt handling, synchronisation, and communication. Rhealstone combines their timings into a single figure of merit, where lower operation times produce a higher score. However, the results depend on the precise measurement boundaries. The benchmark also does not directly describe application-level performance or memory consumption, and an aggregate score can hide differences between individual operations.

While Rhealstone focuses on the latency of individual operations, later approaches consider other aspects of RTOS performance. *Thread-Metric*, for example, evaluates scheduling, communication, synchronisation, and memory allocation through operation rates @ThreadMetric. This provides a complementary perspective by showing how many operations a system can complete within a given period.

Beyond the choice of performance metrics, comparing different RTOSs also requires a consistent benchmark implementation. *RTOSBench* addresses this challenge through an abstraction layer that connects common benchmark workloads to kernel-specific interfaces @RTOSBench. This allows tests to be reused across operating systems, although differences between adapters can still influence the results.

#takeaway[
  Rhealstone provides reference operations, Thread-Metric adds throughput measurements, and RTOSBench addresses cross-kernel comparability. Together, they inform the benchmark design used in this thesis without requiring full conformance to any existing suite.
]

The specific workloads and measurement procedures used in this thesis are described in the methodology chapter.


== Comparisons of Real-Time Operating Systems

Beyond defining how RTOS performance can be measured, previous research has applied similar metrics to compare operating systems and concurrency frameworks. Belleza and de Freitas compare C-based RTOS kernels using synchronisation, message passing, and event-handling operations @Belleza2018Performance. These measurements cover several of the same areas as Rhealstone, but primarily reveal differences between kernel implementations rather than programming languages.

More recent studies also consider Rust-based systems. Vandervelden et al. compare several operating systems and concurrency frameworks @Vandervelden2024Overview. These include Tock, Hubris, RTIC, and Embassy. Their evaluation considers scheduling and interrupt latency, communication, memory use, and isolation.

Wang extends this perspective by comparing the C-based FreeRTOS kernel with the Rust-based RTIC and Embassy frameworks on STM32F3 hardware. The study measures execution time and memory footprint @Wang2026.

However, these systems differ in their architecture and scheduling behaviour. For example, RTIC uses priority-based preemption, while Embassy primarily relies on cooperative asynchronous task scheduling. Such differences influence response times and scheduling overhead independently of the programming language. Consequently, these studies cannot isolate the performance impact of C versus Rust alone.

Beyond direct comparisons, individual Rust-based operating systems have also been studied in more detail. Tock, for example, combines Rust's type system with hardware protection to isolate applications and manage shared memory @Levy2017. Building on this architecture, Culic et al. investigate how interrupt responsiveness can be improved while maintaining Tock's safety properties @Culic2022LowLatency.

Ariel OS takes a different approach, focusing on an embedded Rust operating system evaluated through microbenchmarks across Arm Cortex-M, RISC-V, and Xtensa platforms @Frank2025ArielOS. These studies demonstrate different design choices and performance characteristics of Rust-based operating systems.

#takeaway[
  Existing RTOS comparisons reveal differences in scheduling, communication, and memory usage. However, the systems also differ in architecture and implementation. Their results therefore cannot isolate the performance impact of C versus Rust.
]


== C and Rust in Embedded Systems

While the previous studies focus on operating systems and their architectures, other research compares programming languages directly. Plauska, Liutkevičius, and Janavičiūtė compare C/C++, MicroPython, Rust, and TinyGo on an ESP32. They use computational workloads such as CRC32, SHA256, FFT, IIR, and FIR with shared inputs and expected outputs @Plauska2023. These workloads reveal differences in computation but do not measure RTOS scheduling or interrupt response.

Embuena et al. compare languages and platforms in terms of performance and power consumption, including STM32F4 Discovery and ESP32/RISC-V hardware @Embuena2026Comparison. Because optimisation settings are not matched, differences in build configuration also need to be considered when interpreting the results.

Thapa et al. study a more controlled C-versus-Rust development scenario. Two teams independently implement equivalent microcontroller functionality and compare performance, memory footprint, and development effort @Thapa2026Ariel. This is particularly relevant to an application-language comparison. However, the study does not evaluate equivalent workloads over the same real-time kernel. The study was also presented at IEEE DCOSS-IoT 2026.

Together, these studies demonstrate that C and Rust can be compared on embedded hardware. However, they do not establish how the two languages perform when the RTOS, board, and workload are held constant.

Performance and resource consumption are only part of the comparison between C and Rust. Another important difference concerns memory safety. Rust prevents many memory-related errors through ownership. However, its guarantees depend on the correctness of underlying abstractions. RustBelt formalises this relationship and shows why a safe interface must also have a sound implementation @Jung2018. Research on unsafe Rust illustrates how difficult maintaining these requirements can be in practice @Holtervennhoff2023Pacemaker.

Even when these requirements are satisfied, memory safety does not guarantee overall software correctness. Santoso, Kwee, and Chua examine which vulnerability classes Rust's memory model prevents and which remain possible @Santoso2023MemorySafetyModel. Zhang et al. support this distinction through an analysis of 790 fixes in Rust programs. Many of the identified defects fall outside memory safety @Zhang2024BeyondMemory.

These limitations become especially relevant when Rust interacts with C code. At this boundary, Rust cannot verify whether external functions respect the assumptions made by its type system. McCormack, Sunshine, and Aldrich demonstrate the consequences by identifying undefined or undesirable behaviour in mixed-language interfaces @McCormack2024FFI. Similar challenges arise when integrating Rust into larger C codebases, as illustrated by Rust for Linux @Li2024RustForLinux. In safety-critical systems, such dependencies also complicate the evidence required to establish system safety @Munch2026SafetyCritical.

These challenges have motivated research into detecting errors that Rust's compiler cannot prevent. SafeFFI investigates sanitisation at language boundaries @Braunsdorf2026SafeFFI. Miri detects certain forms of undefined behaviour, including invalid pointer operations and data races @Jung2026Miri. Other approaches focus on identifying memory-safety defects in unsafe code, including use-after-free and double-free errors @Chen2026Culpa @Li2026Typestate. Although these tools improve error detection, they cannot guarantee that every external interface behaves correctly.

#takeaway[
  Existing language comparisons provide evidence about performance and resource consumption, but rarely isolate RTOS behaviour. Rust improves memory safety without guaranteeing overall correctness, particularly when external C code is involved.
]

These findings are particularly relevant when Rust applications use C-based RTOSs. In such systems, language bindings connect Rust code to the kernel. Their implementation affects both memory safety and performance.


== Rust Applications on C-Based Kernels

Several projects provide ways to use Rust with existing C-based RTOSs. However, support and integration approaches differ between systems. Sharma et al. identify common interoperability challenges through an analysis of Rust packages, tools, and developer experiences @Sharma2024.

The C-based RTOSs considered in this thesis provide different integration routes. FreeRTOS-rust offers wrappers and a shim for the FreeRTOS API @FreeRTOSRust. Zephyr documents Rust as an optional language module requiring suitable build configuration and target support @ZephyrRust. NuttX documents experimental Rust support, including C interoperability and selected processor architectures @NuttXRust. These resources describe how Rust can be integrated, but do not themselves provide controlled C-versus-Rust performance comparisons.

Another approach to integrating Rust with a C-based RTOS is TECS/Rust. It connects Rust components to an existing RTOS through a component framework. Yoshimura, Oyama, and Azumi evaluate this approach and report low execution-time overhead @Yoshimura2024TECSRust. Later work improves synchronisation through memory-safe locking mechanisms @Yoshimura2025TECSRustOE. The approach has also been extended to multiprocessor systems @Yoshimura2026FMP3.

These studies show that Rust can be integrated into a C-based RTOS with limited overhead in the systems evaluated. However, they focus on one integration approach rather than comparing equivalent C and Rust applications across different kernels.

Performance is also not the only consideration. Motogna, Vescan, and Şerban identify safety, security, and maintainability as important qualities of embedded software @Motogna2023Empirical. This makes the integration effort, language bindings, and safety requirements relevant alongside timing and memory consumption.

The studies reviewed in this chapter therefore leave an important question unanswered. RTOS comparisons mainly examine differences between kernels or frameworks. Language comparisons often use computational workloads without an RTOS. Meanwhile, Rust integration studies focus on specific implementations. As a result, there is limited evidence on how C and Rust compare when the underlying RTOS, hardware, and workload remain unchanged.

The limitations of previous research are summarised in @comparison-families. The first column groups studies by their approach, while the second identifies what each approach leaves unresolved. The final column shows how this thesis addresses these limitations.

#figure(
  {
    set text(hyphenate: false)
    set par(justify: false)
    table(
      columns: (1fr, 1.55fr, 1.15fr),
      stroke: 0.5pt + luma(200),
      align: left,
      inset: 5pt,
      table.header(
        [*Approach*],
        [*What remains unresolved*],
        [*This thesis*],
      ),
      [C RTOS comparisons (Belleza)],
      [Kernel effects are compared, not application-language effects],
      [Compares C and Rust on a fixed kernel],

      [Rust OS comparisons (Vandervelden, Wang)],
      [Kernel or framework also varies],
      [Matches kernel and workload],

      [Individual Rust systems (Levy, Frank, Culic)],
      [No matched C application comparison],
      [Uses paired application implementations],

      [Languages on hardware (Plauska, Embuena)],
      [RTOS operations are not isolated under matched conditions],
      [Benchmarks RTOS operations],

      [Industrial C/Rust comparison (Thapa)],
      [Does not isolate operation-level RTOS costs],
      [Measures defined RTOS operations],

      [Rust/C integration (Yoshimura)],
      [Evidence is specific to one integration approach],
      [Examines several kernels and targets],
    )
  },
  caption: [Comparison approaches and research gap],
) <comparison-families>

The main contribution of this thesis is a controlled comparison of C and Rust applications on the same hardware and RTOS. Workloads and relevant build settings are kept consistent within each experiment. This allows timing, memory consumption, and integration costs to be compared under similar conditions.

The next chapter describes the experimental setup; the Methodology chapter then explains the measurement and analysis procedures.


#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}
