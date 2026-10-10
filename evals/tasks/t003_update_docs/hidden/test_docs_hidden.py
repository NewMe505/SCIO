import pathlib

from cache import Cache


def test_hidden_docstring_mentions_args_and_ttl():
    doc = Cache.get_or_set.__doc__ or ""
    assert "ttl" in doc.lower() and "factory" in doc.lower() and "return" in doc.lower()


def test_hidden_readme_and_changelog():
    root = pathlib.Path(__file__).resolve().parent.parent
    assert "get_or_set" in (root / "README.md").read_text(encoding="utf-8")
    assert "get_or_set" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
