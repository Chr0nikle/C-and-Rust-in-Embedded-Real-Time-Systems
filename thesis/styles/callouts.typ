
#import "@preview/note-me:0.6.0": admonition

#let takeaway(body) = admonition(
  icon: [],
  title: "Key takeaway",
  color: rgb("#527ca0"),
  background-color: rgb("#F0F5FA"),
)[#body]
