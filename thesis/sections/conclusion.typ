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
// ---[ end standalone preview bootstrap ]---------------------------------------

= Conclusion

This thesis compared C and Rust applications on embedded real-time operating systems across four hardware platforms and five kernel implementations. The evaluation examined execution time, memory usage, safety-relevant source operations and integration complexity, while keeping the hardware and kernel unchanged for direct C/Rust comparisons.

The performance results show that C and Rust achieve similar execution times for most operations on the same RTOS and hardware. Larger differences occur mainly in communication benchmarks, where the results depend on the kernel and platform. The Rust-native kernels also show different strengths and weaknesses across operations. Neither comparison establishes a general performance advantage for one programming language.

Memory usage presents a clearer trade-off. Per-task memory overhead is identical between matched C and Rust implementations, while Rust-linked firmware generally requires more flash memory and sometimes more static RAM. These additional requirements may be relevant for microcontrollers with limited resources.

The analysis resolves no raw-pointer dereferences, arithmetic or conversions in the matched Rust application code, while resolving such operations in its FFI boundaries. One application raw-pointer record per NuttX configuration remains unresolved. This separates many low-level memory accesses from the application code, but does not eliminate the need to review interfaces to C kernels and hardware-support components. The analysis identifies these boundaries rather than establishing the memory safety of the complete firmware.

The integration results show that Rust requires fewer lines of hand-written application and boundary code than the corresponding C implementations. However, Rust introduces interfaces to the C-based kernels that must be maintained, and keeping separate C and Rust benchmark implementations consistent introduces additional work. The measured complexity differences therefore do not establish that either language is easier to maintain.

Beyond these findings, the thesis provides a documented benchmarking procedure with firmware configurations, build settings and archived measurements. The archived primary campaign supports checking the matched C/Rust results and extending the comparison. The supplementary raw bundle for the Rust-native timing results is unavailable, so those retained summaries cannot currently be reproduced from raw samples.

Overall, Rust is a viable alternative to C for the investigated RTOS applications, with similar execution times in most benchmarks and fewer directly expressed raw-pointer operations in application code. Its additional firmware footprint and integration requirements remain important considerations when choosing a language for a particular embedded system.

== Limitations

The findings are limited to the evaluated hardware platforms, RTOS implementations, build configurations and benchmark workloads. Only one physical device of each board type was tested, and measurements were collected in a fixed order. The results therefore do not capture variation between devices or possible effects of measurement order.

The benchmarks measure individual RTOS operations rather than complete applications. Although C and Rust follow the same benchmark definitions on matched hardware and kernels, their implementation and adapter paths can differ. Some workloads also vary between RTOSs, particularly the loaded-jitter benchmark. Consequently, comparisons across different kernels require additional care. The measured execution times and percentiles do not establish worst-case latency bounds or deadline guarantees.

The safety analysis covers selected source operations and project-written integration code, but not the internal implementations of third-party kernels, SDKs or HALs. Its operation counts cannot establish whether memory-safety defects are present. Similarly, the integration-complexity measurements describe code structure rather than actual development or maintenance effort.

== Future Work

Future work could extend the evaluation to additional hardware platforms, RTOSs and more representative embedded applications. Measuring complete applications with interacting tasks, communication and memory management would help determine whether the differences observed in individual benchmarks also affect larger systems.

The measurement campaign could also include several physical devices of each board type and a randomised or counterbalanced execution order. More detailed profiling of selected benchmarks could help identify the causes of the larger C/Rust performance differences, particularly in communication operations.

Finally, a unified static-analysis tool could apply consistent criteria to both languages, including pointer validity, ownership, lifetimes and FFI-boundary operations. Combining analysis with targeted testing would identify potential memory-safety defects rather than only counting safety-relevant operations.

#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}
