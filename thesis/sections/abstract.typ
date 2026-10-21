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

#heading(outlined: false, bookmarked: true, numbering: none)[Abstract] 

#set par(spacing: 0.9em)

This thesis compares C and Rust applications on embedded real-time operating systems, with the aim of distinguishing the effects of the application language from those of the operating system kernel and hardware. The evaluation covers four hardware platforms and five kernel implementations. FreeRTOS, NuttX and Zephyr are examined with applications written in both languages, while the Rust-native systems FreeRTOS-in-Rust and Rivet are evaluated separately.

A common benchmarking framework was developed with 26 test cases covering scheduling, synchronisation, communication, interrupt handling and other operating system operations. In total, 27 firmware configurations were evaluated on physical hardware, with ten independent measurement runs per configuration. Execution times were examined alongside memory consumption, safety-relevant source operations and integration complexity. The measurements and corresponding build configurations were archived to support reproducibility.

The results show that C and Rust achieve similar execution times for most benchmark operations when the hardware and kernel remain unchanged. Larger differences occur mainly in task communication and depend on the platform and implementation. Per-task memory overhead is identical between matched C and Rust configurations, while Rust-linked firmware generally requires more flash memory and sometimes more static random-access memory.

The safety analysis finds no resolved, directly expressed raw-pointer operations in the analysed Rust application code, whereas such operations occur in the corresponding C applications. However, raw-pointer operations remain present in the interfaces connecting Rust applications to C-based kernels. These findings do not establish complete-firmware memory safety. The Rust implementations also contain fewer lines of hand-written application and boundary code, although their kernel interfaces introduce additional maintenance requirements.

Overall, Rust achieves performance comparable to C for the investigated workloads, but memory requirements, integration boundaries and maintenance implications must be considered for each target system. The archive supports further comparisons; raw data for the retained Rust-native summaries are unavailable.

*Keywords:* real-time operating systems, embedded systems, programming language comparison, Rust, memory consumption, memory safety, performance evaluation, reproducibility
