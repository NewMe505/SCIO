"""Regression tests for scripts/check_docs.py (ledger I-011)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from scio.settings import REPO_ROOT


def _load() -> ModuleType:
    path = REPO_ROOT / "scripts" / "check_docs.py"
    spec = importlib.util.spec_from_file_location("check_docs", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_links_in_code_are_ignored(tmp_path: Path) -> None:
    cd = _load()
    (tmp_path / "real.md").write_text("x", encoding="utf-8", newline="\n")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "ok [a](real.md)\n"
        "inline `def f[T: BaseModel](...)` is code\n"
        "```python\nprint([1](...))\n```\n"
        "broken [b](missing.md)\n",
        encoding="utf-8",
        newline="\n",
    )
    errors = cd.links_in(doc)
    assert errors == [f"{doc.name}: broken link -> missing.md"]


def test_dotdotdot_is_never_an_existing_target(tmp_path: Path) -> None:
    cd = _load()
    doc = tmp_path / "doc.md"
    doc.write_text("[x](...)\n", encoding="utf-8", newline="\n")
    assert cd.links_in(doc)  # Windows resolves '...' to a parent dir; the checker must not


def test_repo_docs_pass() -> None:
    cd = _load()
    assert cd.check_links() == []
    assert cd.check_model_ids() == []
    assert cd.check_changelog() == []
