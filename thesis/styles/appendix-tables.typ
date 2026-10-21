#let appendix-table-stroke = (x: none, y: 0.2pt + luma(210))

#let _appendix-styled(stroke) = (
  type(stroke) == dictionary
    and stroke.left == none
    and stroke.right == none
    and stroke.top == (0.2pt + luma(210))
    and stroke.bottom == (0.2pt + luma(210))
)

#let appendix-tables(body) = {
  show table: it => {
    if _appendix-styled(it.stroke) { it } else {
      let count = if type(it.columns) == int { it.columns } else { it.columns.len() }
      let size = if count >= 10 { 7.5pt } else { 8pt }
      text(size: size, table(
        columns: it.columns,
        align: it.align,
        stroke: appendix-table-stroke,
        inset: (x: 2.5pt, y: 1.5pt),
        column-gutter: 2.5pt,
        ..it.children,
      ))
    }
  }
  show figure.where(kind: table): set text(size: 8.5pt)
  set figure(gap: 0.55em)
  body
}
