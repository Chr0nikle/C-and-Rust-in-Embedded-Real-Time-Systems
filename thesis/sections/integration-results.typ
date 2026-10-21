#set page(margin: (top: 0.5cm, bottom: 0.5cm, left: 0.75cm, right: 0.75cm))
#show heading.where(level: 1): set text(hyphenate: false)

= Complete Integration and Complexity Results <app-integration-results>

#align(center)[#text(size: 11pt, weight: "bold")[Integration surface]]
#align(center)[#text(size: 8pt, fill: luma(100))[project-owned adapter source and configuration counts]]
#v(0.3em)

#include "../generated/typ/integration-surface.typ"

Integration-surface counts cover project-owned adapter sources and plugin code,
excluding vendored kernels, generated build trees and retained example
applications. They are a heuristic inventory and are not directly comparable to
the module-complexity results, which use a different per-module source scope.
The build-file inventory is not published because the thesis does not define a
verified population for it.

#v(1em)

#align(center)[#text(size: 11pt, weight: "bold")[Runner identifier overlap]]
#align(center)[#text(size: 8pt, fill: luma(100))[Jaccard similarity between hand-maintained runners]]
#v(0.3em)

#include "../generated/typ/duplication.typ"

#v(1em)

#align(center)[#text(size: 11pt, weight: "bold")[Source complexity by module]]
#align(center)[#text(size: 8pt, fill: luma(100))[LOC, function counts, cyclomatic and cognitive complexity, coupling]]
#v(0.3em)

#include "../generated/typ/complexity.typ"
