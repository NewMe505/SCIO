# Right-align numeric cells in render_table

Tables rendered by `textfmt.table.render_table` left-align every cell, which
makes columns of numbers hard to read. Numbers should line up on the right.

Acceptance criteria:
- [ ] A cell whose text parses as an `int` or `float` is right-aligned to its column width; every other cell, including the header row, keeps left alignment
- [ ] `render_table([["widget", 3, 2.5], ["gear", 12, 10.0]], headers=["item", "qty", "price"])` returns exactly `item   | qty | price\nwidget |   3 |   2.5\ngear   |  12 |  10.0`
- [ ] The existing test suite passes in full, and the project's documented example output is correct for the new behaviour
