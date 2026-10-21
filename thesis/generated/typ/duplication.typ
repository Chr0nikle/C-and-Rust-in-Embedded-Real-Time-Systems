#figure(
  text(size: 7.5pt, table(
    columns: (1fr, 1fr, 1fr, 1fr, 1fr),
    stroke: (x: none, y: 0.25pt + luma(210)),
    inset: (x: 3pt, y: 1.5pt),
    column-gutter: 2pt,
    align: (left, right, right, right, right),
    table.header(
      table.cell(fill: luma(235))[*Pair*],
      table.cell(fill: luma(235))[*C idents*],
      table.cell(fill: luma(235))[*Rust idents*],
      table.cell(fill: luma(235))[*Shared*],
      table.cell(fill: luma(235))[*Similarity*],
    ),
    table.hline(stroke: 0.6pt + luma(150)),
  [runner.c ↔ lib.rs], [194], [189], [104], [0.37],
  )),
  caption: [Runner identifier overlap (Jaccard similarity)],
) <duplication>
