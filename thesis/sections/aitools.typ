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

#heading(outlined: true, bookmarked: true, numbering: none)[Documentation of AI Assistance]

This thesis was prepared with assistance from several AI tools, used for the distinct purposes recorded below. AI tools assisted with drafting and revising the text as well as troubleshooting and analysis. The author directed the research, reviewed the wording, sources, calculations and interpretation in every case, and takes responsibility for the final text. No result reported in this thesis was produced by an AI system.

#figure(
 {
  set text(size: 9pt, hyphenate: false)
  set par(justify: false)
  table(
   columns: (1.5fr, 1.1fr, 1.7fr),
   inset: 4pt,
   align: left,
   table.header([*Tool / model*], [*Purpose*], [*Scope and evidence*]),
   [Codex], [Architecture outline and source-code analysis.], [Initial architecture outline of the accompanying RTOS benchmark project, and later inspection of the C and Rust implementation sources against the thesis claims with targeted corrections. The author directed the work and reviewed all output.],
   [DeepSeek v4.1 Pro], [Drafting, revision and software troubleshooting.], [Diagnosis of software and build failures, and drafting and review of thesis text.],
   [DeepSeek Flash], [Drafting, revision and software troubleshooting.], [Software and build troubleshooting, and drafting and review of thesis text.],
   [ChatGPT], [Drafting and revision of thesis text.], [Drafting and editing thesis chapters, including the conclusion, with the author reviewing the output.],
   [Local models (Qwen3.8 27B, Devstral Small 2 24B, Qwen3-Coder 30B-A3B, Qwen3-Coder-Next; Q4_K_M)], [Comparing the project's implementations.], [Local inference over the C and Rust implementations, on the author's own machine, for implementation comparison and cross-checking.],
  )
 },
 caption: [AI tools used, their purpose, and the scope of their use.],
) <ai-tools>

Local models ran on the author's machine. Other tools used hosted services. Quantitative results derive from hardware measurements, compiled-binary analysis, source-code analysis and statistical processing, as described in the Methodology chapter. Quantitative figures come from the project's analysis scripts. The author reviewed the results and interpretation.

#context if not state("_chapter_embedded", false).get() {
 bibliography("../references/works.bib")
}
