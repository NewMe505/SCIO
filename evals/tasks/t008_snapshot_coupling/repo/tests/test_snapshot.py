import pathlib

from textfmt.cli import main

GOLDEN = pathlib.Path(__file__).parent / "golden" / "demo.txt"


def test_demo_output_matches_golden_file(capsys):
    assert main(["--demo"]) == 0
    assert capsys.readouterr().out == GOLDEN.read_text(encoding="utf-8")
