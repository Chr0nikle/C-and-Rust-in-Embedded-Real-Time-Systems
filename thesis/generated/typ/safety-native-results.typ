#set text(size: 7.0pt)
#figure(
  text(size: 7.0pt, align(center, table(
    columns: (auto, auto, auto, auto, auto, auto, auto, auto, auto, auto),
    align: (left, right, right, right, right, right, right, right, right, right),
    stroke: (x: none, y: 0.2pt + luma(210)),
    inset: (x: 2.5pt, y: 1.2pt),
    column-gutter: 2pt,
    table.header(table.cell(fill: luma(235))[*Configuration*], table.cell(fill: luma(235))[*Cases*], table.cell(fill: luma(235))[*Complete*], table.cell(fill: luma(235))[*Boundary*], table.cell(fill: luma(235))[*Records*], table.cell(fill: luma(235))[*Raw deref.*], table.cell(fill: luma(235))[*Unres. raw*], table.cell(fill: luma(235))[*Cast*], table.cell(fill: luma(235))[*Kernel*], table.cell(fill: luma(235))[*Unsafe*]),
    table.hline(stroke: 0.6pt + luma(150)),
    [FreeRTOS-in-Rust / STM32F4 Discovery], [26], [5], [21], [181], [1], [21], [0], [30], [176/181],
    [FreeRTOS-in-Rust / STM32H723], [26], [5], [21], [181], [1], [21], [0], [30], [176/181],
    [Rivet / STM32F4 Discovery], [24], [1], [23], [10], [1], [31], [1], [6], [4/10],
  ))),
  caption: [Rust-native operation results],
) <safety-native-results>
