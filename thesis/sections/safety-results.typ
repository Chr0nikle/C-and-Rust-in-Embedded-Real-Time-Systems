#set page(margin: (top: 0.5cm, bottom: 0.5cm, left: 0.75cm, right: 0.75cm))

= Complete Safety Analysis Results <app-safety-results>

The tables report resolved source operations separately from unresolved records.
Unresolved records are excluded from the resolved-operation denominators and are
not evidence that an operation is absent. A resolved record identifies a
source-level operation; it does not establish that the complete firmware is
memory-safe. Trace coverage and profile labels identify the scope of each table.

#include "../generated/typ/safety-comparison.typ"

#v(0.6em)

#include "../generated/typ/safety-c-categories.typ"

#v(0.6em)

#include "../generated/typ/safety-role-counts.typ"

#include "../generated/typ/safety-rust-categories.typ"

#v(0.6em)

#include "../generated/typ/safety-trace-coverage.typ"

#v(0.6em)

#include "../generated/typ/safety-native-results.typ"
