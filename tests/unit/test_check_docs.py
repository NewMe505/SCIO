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


LEDGER = (
    "# Ledger\n\n"
    "### I-103 · older\n- Status: resolved\n\n"
    "### I-104 · identity\n- Resolved: 2026-10-27 · Status: resolved\n\n"
    "### I-105 · docker\n- Resolved: 2026-10-27 · Status: resolved\n\n"
    "### I-106 · still open\n- Status: open\n"
)
LESSONS = (
    "# Lessons\n\nintro\n\n## Guardrails (Always / Never)\n\nhow rules work\n\n"
    "1. **Never** commit without SCIO's identity (I-104). Guard: `symbol:SCIO_GIT_IDENTITY`\n"
    "2. **Always** skip on the plain image (I-105). Guard: `test:test_node_in_docker`\n"
    "3. **Always** say the ETA (owner correction). Guard: `manual:` a chat property\n\n"
    "## Standing pre-flight list\n\n1. not a guardrail\n"
)


def _all_exist(kind: str, name: str) -> bool:
    return True


def test_guardrails_pass_when_every_resolved_entry_is_cited_and_every_guard_exists() -> None:
    """Issue #61: the rules are enforced, not just written."""
    cd = _load()
    rules = cd.guardrail_rules(LESSONS)
    assert [r.split(".", 1)[0] for r in rules] == ["1", "2", "3"]  # pre-flight items excluded
    assert cd.resolved_ledger_ids(LEDGER) == ["I-104", "I-105"]  # from I-104, resolved only
    assert cd.guardrail_problems(rules, cd.resolved_ledger_ids(LEDGER), _all_exist) == []


def test_a_resolved_entry_no_rule_cites_fails() -> None:
    cd = _load()
    rules = [r for r in cd.guardrail_rules(LESSONS) if "I-105" not in r]
    problems = cd.guardrail_problems(rules, ["I-104", "I-105"], _all_exist)
    assert problems == ["I-105 is resolved but no guardrail cites it; add an Always/Never rule"]


def test_a_guard_that_does_not_exist_fails() -> None:
    cd = _load()
    rules = cd.guardrail_rules(LESSONS)
    problems = cd.guardrail_problems(rules, [], lambda kind, name: name != "test_node_in_docker")
    assert problems == ["guardrail 2: test:test_node_in_docker does not exist"]


def test_a_rule_needs_always_or_never_a_guard_and_a_manual_reason() -> None:
    cd = _load()
    rules = [
        "1. **Prefer** small commits (I-104). Guard: `symbol:X`",
        "2. **Never** guess (I-104).",
        "3. **Always** check (I-104). Guard: `manual:`",
    ]
    assert cd.guardrail_problems(rules, ["I-104"], _all_exist) == [
        "guardrail 1: must start with **Always** or **Never**",
        "guardrail 2: names no guard (symbol:, test:, check:, hook:, manual:)",
        "guardrail 3: a manual guard needs its reason after it",
    ]


def test_guard_exists_looks_in_the_repository() -> None:
    cd = _load()
    assert cd.guard_exists("symbol", "SCIO_GIT_IDENTITY")  # a constant in scio/aci/tools.py
    assert cd.guard_exists("symbol", "_ci_git_identity")  # a fixture in tests/conftest.py
    assert cd.guard_exists("symbol", "run_effect")  # a PEP 695 generic: def run_effect[T]( (I-111)
    assert cd.guard_exists("symbol", "carrying_product_md")
    assert cd.guard_exists("test", "test_a_guard_that_does_not_exist_fails")
    assert cd.guard_exists("check", "check_guardrails")  # defined and run by main
    assert not cd.guard_exists("check", "guard_exists")  # defined, but main never runs it
    assert not cd.guard_exists("test", "test_that_was_never_written")
    assert not cd.guard_exists("symbol", "NO_SUCH_SYMBOL_ANYWHERE")
    assert not cd.guard_exists("test", "bad name(")  # not an identifier: never a match
