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

= Introduction


Systems programming languages such as C and C++ provide direct control over memory and hardware, making them widely used in operating systems, firmware, and performance-critical software. However, they offer limited language-level protection against memory-safety errors, including buffer overflows, use-after-free, and dangling pointers. Such defects are a significant source of software vulnerabilities and remain a longstanding concern in systems programming @Song2019Sanitizing.

The importance of memory safety has also gained recognition at the policy level. In February 2024, the United States Office of the National Cyber Director published Back to the Building Blocks, advocating the adoption of memory-safe programming languages as a strategic measure to reduce software vulnerabilities @WhiteHouse2024MemorySafety.

Memory-safety errors are especially dangerous in embedded systems, where hardware
protection is limited. Many microcontrollers have no memory-management unit.
Protection then depends on whether a memory-protection unit is present and
correctly configured @Salehi2022BareMetal.
Once deployed, such a device becomes hard to patch, and field studies show that
such flaws are widespread in consumer and industrial firmware @Costin2014EmbeddedFirmware.


The stakes are rising as well. The defects that survive in the field are a standing
target, and the tools for finding them keep improving. Machine learning and large
language models are increasingly used for such vulnerability detection @Shaon2025VulnerabilitySurvey.
Once a flaw is found, it can then be turned against the
device. An attacker can take over the device, corrupt or extract its data, or disrupt its
operation. Limited update capabilities can leave vulnerabilities exploitable for years, 
potentially causing operational and financial harm.

Rust aims to address these memory-safety problems while retaining the low-level control required for systems programming.
Rust uses ownership, borrowing, and its type system to prevent broad classes of memory-safety errors and data races at compile time @Vandervelden2024Overview.
It achieves this without requiring a garbage collector @RustOwnership.


However, embedded development often requires operations that Rust's compiler cannot verify. 
These include direct hardware access and interactions with C-based real-time operating system (RTOS) kernels.
Rust allows such operations to be encapsulated in wrappers, limiting the amount of unsafe code. 
Nevertheless, these wrappers do not eliminate the underlying risks or make the surrounding C code safe.
Such interactions introduce additional integration requirements and potential performance overhead, both of which are relevant when comparing C and Rust in embedded RTOS environments.


These challenges complicate comparisons between C and Rust in embedded RTOS environments. Observed differences may stem not only from the programming language but also from the RTOS kernel and the mechanisms used for C/Rust interoperability. Previous research has also identified challenges in tooling and interoperability when combining both languages @Sharma2024.
To account for these factors, this thesis aims to isolate their influence by varying individual components while keeping the remaining experimental conditions constant.

The comparison covers four hardware platforms and five RTOS kernel implementations, including C-based and Rust-based kernels. Equivalent workloads are implemented in C and Rust to measure execution time and memory consumption. Memory safety and integration complexity are examined separately.


The investigation addresses four objectives:

1. *Performance and resource consumption.* The first objective is to examine differences in execution time and memory consumption between equivalent C and Rust implementations. For direct language comparisons, the hardware platform and RTOS kernel are kept constant.

2. *Memory safety.* The second objective is to investigate the memory-safety characteristics of both languages in embedded RTOS environments. Particular attention is given to unsafe operations and interactions between Rust applications and C-based kernels.

3. *Maintainability and integration.* The third objective is to examine the complexity of implementing and maintaining equivalent workloads in both languages. This includes the required adapters, build configurations, and platform-specific code.

4. *Validity and reproducibility.* The final objective is to establish which experimental conditions must be controlled and documented to support meaningful and reproducible comparisons. This includes identifying the influence of the programming language, RTOS kernel, and integration mechanisms.

The evaluation is limited to the selected hardware platforms, RTOS implementations, and benchmark workloads. Power consumption is not investigated, and the measured execution times do not establish worst-case execution-time guarantees. Similarly, the memory-safety analysis does not establish the safety of the complete system.

These objectives do not presume that either language is superior in every category.

#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}
