# `paginate(items, page_size)` drops the last page

`tests/test_paginate.py::test_last_partial_page` fails: a list of 7 items with page_size 3 yields 2 pages instead of 3.

Acceptance criteria:
- [ ] The existing failing test passes without being modified.
- [ ] No other test changes status.
