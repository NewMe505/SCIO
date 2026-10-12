import pathlib

from textfmt.cli import DEMO_HEADERS, DEMO_ROWS, main
from textfmt.table import render_table

ROOT = pathlib.Path(__file__).resolve().parent.parent
NEW_DEMO = "item   | qty | price\nwidget |   3 |   2.5\ngear   |  12 |  10.0"


def test_hidden_numbers_right_aligned_text_left():
    assert render_table(DEMO_ROWS, headers=DEMO_HEADERS) == NEW_DEMO
    assert render_table([["a", "1"], ["bbb", "22"]]) == "a   |  1\nbbb | 22"
    assert render_table([["-1.5", "x"], ["10", "yy"]]) == "-1.5 | x\n  10 | yy"


def test_hidden_snapshot_regenerated_not_deleted(capsys):
    golden = ROOT / "tests" / "golden" / "demo.txt"
    assert golden.read_text(encoding="utf-8") == NEW_DEMO + "\n"
    assert (ROOT / "tests" / "test_snapshot.py").exists()
    assert main(["--demo"]) == 0
    assert capsys.readouterr().out == NEW_DEMO + "\n"


def test_hidden_readme_example_updated():
    assert NEW_DEMO in (ROOT / "README.md").read_text(encoding="utf-8")
    assert (ROOT / "tests" / "test_readme.py").exists()
