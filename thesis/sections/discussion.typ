// --- Standalone preview bootstrap ---------------------------------------------

// When this chapter is compiled on its own, the thesis template is applied.
// When included by thesis.typ, the template is already provided.

#import "@preview/uastw-thesis:0.1.0" as uastw

#show: it => context {
  if state("_chapter_embedded", false).get() { it }
  else { uastw.uastw-thesis-page-setup(it) }
}

#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex

// --- End standalone preview bootstrap -----------------------------------------

// Cross-chapter references remain readable in standalone previews.

#let xref(label, text) = context {
  if state("_chapter_embedded", false).get() { ref(label) }
  else { text }
}


= Discussion

The results show that differences between C and Rust depend on the workload, RTOS and hardware platform. Beyond the individual measurements, their implications must be considered when evaluating the suitability of either language for embedded RTOS applications. This chapter discusses the observed trade-offs in performance, memory usage, safety and integration complexity.


== Performance and Resources <sec-performance>

Across the matched configurations, C and Rust achieve similar execution times for most benchmark operations. Since both implementations use the same C-based kernel, much of their scheduling, synchronisation and interrupt behaviour is shared. The reported median operation times are often close for the matched C and Rust configurations. This descriptive comparison does not establish statistical equivalence, and some measurements use a shared C routine.

The communication benchmarks show where this conclusion has limits. Rust achieves lower payload-transfer latency on several FreeRTOS and Zephyr boards and higher IPC throughput on the FreeRTOS Arm configurations examined here; on ESP32-C6 the throughput difference is close to zero. These results are not contradictory. The payload-transfer benchmark measures the latency of individual transfers, whereas the throughput benchmark measures the amount of data transferred over time. Their results depend on different aspects of the implementation, including copying, buffer handling, RTOS behaviour and adapter calls. The results demonstrate workload-specific differences, but do not identify which implementation detail causes them or establish that one language is inherently faster.

Besides comparing C and Rust applications on the same kernel, the evaluation also includes the Rust-native RTOSs FreeRTOS-in-Rust and Rivet. On STM32F4, FreeRTOS-in-Rust records lower preemption latency than conventional FreeRTOS, but higher task-handoff and notification latencies. Rivet performs better in some communication benchmarks, while FreeRTOS-in-Rust performs better in others. Neither Rust-native RTOS is consistently faster. These differences reflect the complete kernel and application implementations rather than the programming language alone.

Memory consumption introduces a clearer trade-off. Although per-task overhead remains identical between matched C and Rust applications, Rust-linked firmware generally occupies more flash and sometimes more static RAM. This means that similar runtime performance does not necessarily come with an equally small memory footprint. On memory-constrained microcontrollers, the additional flash or RAM requirements may limit which dependencies or application features can be included.

The difference between runtime and static memory is particularly relevant. The similar per-task overhead indicates that using Rust does not inherently increase the kernel's bookkeeping requirements when the same kernel is retained. The larger static footprint instead reflects differences in the linked application, integration code and dependencies. It should therefore be evaluated for each build rather than assumed to be a fixed overhead of the Rust language.

Overall, the results show that Rust is a practical option for applications on C-based RTOS kernels when execution time is the main concern. The more visible costs in the evaluated configurations are the additional firmware footprint and workload-specific performance differences, especially in communication. Whether these costs are acceptable depends on the timing requirements and available memory of the target system.


== Safety at the Boundary

The safety analysis reveals an important difference in how C and Rust organise low-level operations. In C, pointer dereferences occur in both application and adapter code, while pointer arithmetic is mainly found in the applications. The analysed Rust applications, by contrast, contain no resolved raw-pointer operations. These operations instead appear in the FFI boundaries. This suggests that Rust can make direct pointer handling easier to locate and review, rather than eliminating the need for it.

However, this separation is not as straightforward as the pointer counts alone suggest. FreeRTOS stands out because almost all recorded Rust application operations occur within `unsafe` contexts, compared with roughly 70% on Zephyr and NuttX. At the same time, its FFI boundary contains considerably fewer resolved raw-pointer dereferences. One possible explanation is that the FreeRTOS application performs more RTOS interactions and global-state accesses directly within `unsafe` contexts, whereas the other implementations organise these operations differently. The inventory supports this distinction. FreeRTOS records both RTOS calls and global-state accesses in its Rust application, while the Zephyr and NuttX application inventories contain no recorded global-state accesses. Nevertheless, the different operation counts and code structures prevent drawing conclusions about their relative safety.

The FFI boundaries introduce further requirements. Their correctness depends on whether pointers remain valid and whether ownership and lifetime assumptions are respected across the language boundary. As discussed in the RustBelt research, a safe interface must be supported by a sound implementation. The Rust firmware also contains C support code, while the underlying kernels and hardware-support components remain outside the compiler's safety checks. Keeping unsafe operations in dedicated modules can therefore make them easier to identify, but does not guarantee that the complete firmware is memory-safe.

The Rust-native results reinforce this limitation. FreeRTOS-in-Rust and Rivet also contain low-level operations in their analysed project code, although the external kernel implementations were not included in the inventories. These results do not establish whether Rust-native kernels are safer than C-based alternatives. Instead, they show that the need to examine low-level interfaces extends beyond C/Rust interoperability.

== Integration Complexity <sec-maintainability>

Besides performance and memory safety, the effort required to integrate and maintain an application is an important consideration when choosing between C and Rust. The results show differences in code size, organisation and integration requirements.

The Rust applications and their boundary modules contain fewer lines of hand-written code than the corresponding C implementations. However, Rust also requires interfaces to communicate with the C-based kernels, which must be updated when the kernel APIs change. A smaller implementation therefore does not necessarily mean less maintenance work.

The implementations also differ in how their logic is organised. The shared C runner handles much of its benchmark logic in a few large functions, while the Rust runner distributes it across more, smaller functions. This results in lower measured per-function complexity for Rust, although it does not necessarily make the code easier to maintain.

Another challenge is keeping the separate C and Rust benchmark runners consistent. Changes to workloads or measurement procedures must be applied to both implementations to ensure a fair comparison. This additional work is not reflected in the complexity measurements.


#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}