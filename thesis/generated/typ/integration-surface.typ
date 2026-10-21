#figure(
  text(size: 7.5pt, table(
    columns: (auto, 1fr, 1fr, 1fr, 1fr),
    stroke: (x: none, y: 0.25pt + luma(210)),
    inset: (x: 3pt, y: 1.5pt),
    column-gutter: 2pt,
    align: (left, right, right, right, right),
    table.header(
      table.cell(fill: luma(235))[*System*],
      table.cell(fill: luma(235))[*Adapter C*],
      table.cell(fill: luma(235))[*Adapter Rust*],
      table.cell(fill: luma(235))[*cfg branches*],
      table.cell(fill: luma(235))[*Patch keyword matches*],
    ),
    table.hline(stroke: 0.6pt + luma(150)),
  [freertos], [14109], [1893], [181], [0],
  [freertos-in-rust], [0], [1771], [28], [0],
  [nuttx], [5920], [2050], [96], [5],
  [rivet], [0], [1763], [2], [0],
  [zephyr], [12138], [2082], [78], [2],
  )),
  caption: [Integration surface by system. Adapter lines are project-owned code; patch counts are keyword matches, not applied patches.],
) <integration-surface>
