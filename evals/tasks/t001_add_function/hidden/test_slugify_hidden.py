"""Hidden grading tests for t001 (copied into the sandbox after the run; never shown)."""

from textutils import slugify


def test_hidden_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_hidden_collapses_runs_and_strips():
    assert slugify("  --Multiple   spaces__and--dashes!! ") == "multiple-spaces-and-dashes"


def test_hidden_keeps_digits():
    assert slugify("Release 2.0 (beta)") == "release-2-0-beta"


def test_hidden_empty_and_symbols():
    assert slugify("") == ""
    assert slugify("!!!") == ""
