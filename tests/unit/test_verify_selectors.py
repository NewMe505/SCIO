"""Test selector normalisation in verify (ledger I-031)."""

from __future__ import annotations

from scio.graph.nodes.verify import (
    is_path_selector,
    join_selectors,
    selector_input,
    split_selectors,
)


def test_path_selectors_become_positional_targets() -> None:
    root = "/work"
    inp = selector_input(root, "tests/test_slugify.py::test_slugify_lowercases")
    assert inp.path == "/work/tests/test_slugify.py::test_slugify_lowercases" and inp.filter is None
    assert selector_input(root, "tests/test_x.py").path == "/work/tests/test_x.py"
    assert selector_input(root, "/work/tests/test_x.py").path == "/work/tests/test_x.py"
    assert (
        selector_input("C:\\repo", "C:\\repo\\tests\\test_x.py").path
        == "C:\\repo\\tests\\test_x.py"
    )


def test_keyword_selectors_become_k_expressions() -> None:
    inp = selector_input("/work", "slugify")
    assert inp.filter == "slugify" and inp.path is None
    assert selector_input("/work", "test_a or test_b").filter == "test_a or test_b"
    assert (
        not is_path_selector("slugify") and is_path_selector("a/b.py") and is_path_selector("x::y")
    )


def test_join_and_split_roundtrip_and_dedupe() -> None:
    joined = join_selectors(["slugify", " tests/test_a.py::t ", "slugify", ""])
    assert split_selectors(joined) == ["slugify", "tests/test_a.py::t"]
    assert join_selectors([]) == "" and split_selectors("") == []
