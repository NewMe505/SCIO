import pathlib

from textfmt.cli import DEMO_HEADERS, DEMO_ROWS
from textfmt.table import render_table

README = pathlib.Path(__file__).parent.parent / "README.md"


def test_readme_shows_the_real_demo_output():
    """The README example must be what the code prints today."""
    expected = render_table(DEMO_ROWS, headers=DEMO_HEADERS)
    assert expected in README.read_text(encoding="utf-8")
