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

#heading(outlined: false, bookmarked: true, numbering: none)[Acknowledgement]
I would like to thank my supervisor, Dr. Martin Horauer, for his patience, guidance and willingness to help throughout the development of this thesis. I particularly appreciate his understanding and continued support, even during periods when my communication was less frequent than it should have been.

I would also like to thank my friends and family for their encouragement, patience and support throughout my studies. I am especially grateful to those who took the time to proofread this thesis and provide valuable feedback.

// Add any further personal acknowledgements here.
