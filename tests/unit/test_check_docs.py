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
    assert cd.check_release_cadence() == []


PM_STATUS = (
    "- **Status:** in progress. M6 Foundations done (PR #41); M7 Greenfield bootstrap done; "
    "M8 Decompose + milestone loop done; M9 Dependencies + runtime done; M10 Second stack "
    "done (PR #52); M11 next."
)


def test_milestones_done_but_only_unreleased_are_reported() -> None:
    """I-086: the changelog as it stood before 0.6.0 (M8 to M10 under Unreleased)."""
    cd = _load()
    changelog = (
        "# Changelog\n\n## [Unreleased]\n### Added\n- M10 Second stack\n- M9 runtime\n"
        "- `scio build` (Product Mode M8)\n- carried into M11\n\n"
        "## [0.5.0] - 2026-10-12\n### Added\n- M7 `scio new`; M6 profiles\n"
    )
    assert cd.unreleased_done_milestones(changelog, PM_STATUS) == ["M8", "M9", "M10"]


def test_a_milestone_already_released_is_not_pending_again() -> None:
    cd = _load()
    changelog = (
        "## [Unreleased]\n- M11 release gate; a fix to the M10 install phase\n\n"
        "## [0.6.0] - 2026-10-26\n- M10 Second stack; M9 runtime; M8 loop\n"
    )
    assert cd.unreleased_done_milestones(changelog, PM_STATUS) == []
    done_m11 = PM_STATUS.replace("M11 next", "M11 Product-ready + evals done")
    assert cd.unreleased_done_milestones(changelog, done_m11) == ["M11"]


def test_an_entry_merged_into_a_released_section_is_caught() -> None:
    """I-089: merging main across the 0.6.0 cut put a branch's unreleased Fixed entries
    under [0.6.0], which no longer matched the tag."""
    cd = _load()
    released = "## [0.6.0] - 2026-10-26\n### Fixed\n"
    tagged = f"## [Unreleased]\n\n{released}- old fix\n"
    moved = f"## [Unreleased]\n\n{released}- new fix\n- old fix\n"
    kept = f"## [Unreleased]\n### Fixed\n- new fix\n\n{released}- old fix\n"
    assert cd.released_section_changed(moved, tagged, "0.6.0")
    assert not cd.released_section_changed(kept, tagged, "0.6.0")
    assert not cd.released_section_changed(kept, "## [Unreleased]\n", "0.6.0")  # no tag yet
    assert cd.check_released_section_frozen() == []
