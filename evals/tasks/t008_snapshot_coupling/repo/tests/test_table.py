from textfmt.table import render_table


def test_pads_columns_to_widest_cell():
    assert render_table([["a", "bb"], ["ccc", "d"]]) == "a   | bb\nccc | d"


def test_headers_are_the_first_row():
    assert render_table([["x"]], headers=["name"]) == "name\nx"


def test_empty():
    assert render_table([]) == ""
