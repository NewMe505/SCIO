"""ADR 0009: the python profile reproduces the pre-M6 commands exactly, and no stack-specific
command is built outside ``scio/toolchain/``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scio.settings import REPO_ROOT
from scio.toolchain import Toolchain, get_toolchain, python_toolchain
from scio.toolchain.python import PythonToolchain, load_python_profile

# The literals the graph used before M6 (scio/aci/tools.py, scio/graph/nodes/verify.py,
# scio/graph/nodes/prepare.py, evals/solvers.py). The profile must reproduce them exactly.
OLD_TEST_ARGV = ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", "-rfE"]
OLD_TEST_ARGV += ["--no-header"]
OLD_LINT_ARGV = ["ruff", "check", "--no-cache", "--select", "E9,F63,F7,F82,F821"]
OLD_LINT_ARGV += ["--output-format", "concise"]
OLD_EXCLUDES = "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n"
OLD_CACHE_PATTERNS = ("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".mypy_cache")
# M9 appended the coverage artifacts the quality bar leaves behind; everything before them
# is still the pre-M6 list byte for byte.
M9_CACHE_PATTERNS = (".coverage*", "htmlcov")
M9_EXCLUDES = ".coverage*\nhtmlcov/\n"


def test_python_profile_loads_from_yaml_and_satisfies_the_protocol() -> None:
    tc = get_toolchain("python")
    assert isinstance(tc, Toolchain) and isinstance(tc, PythonToolchain)
    assert tc is python_toolchain()  # cached
    assert load_python_profile() == tc
    with pytest.raises(KeyError):
        get_toolchain("cobol")


def test_test_command_matches_the_pre_m6_literals() -> None:
    tc = python_toolchain()
    assert tc.test_argv(filter=None, path=None) == OLD_TEST_ARGV
    assert tc.test_argv(filter="slug", path=None) == [*OLD_TEST_ARGV, "-k", "slug"]
    assert tc.test_argv(filter=None, path="/w/tests/t.py") == [*OLD_TEST_ARGV, "/w/tests/t.py"]
    assert tc.test_runner == "pytest"


def test_test_outcomes_follow_pytest_exit_codes() -> None:
    tc = python_toolchain()
    assert tc.test_outcome(0, "1 passed") == "passed"
    assert tc.test_outcome(5, "no tests ran") == "no_tests"
    assert tc.test_outcome(1, "=== 1 failed in 0.1s ===") == "failed"
    assert tc.test_outcome(2, "ImportError while importing") == "broken"
    # A collection error that still printed a summary is a failure, not a broken runner.
    assert tc.test_outcome(2, "=== 1 error in 0.1s ===") == "failed"


def test_lint_command_and_findings_match_the_pre_m6_literals() -> None:
    tc = python_toolchain()
    assert tc.lint_argv(".") == [*OLD_LINT_ARGV, "."]
    assert tc.lint_findings(0, "", "") is None
    assert tc.lint_findings(127, "", "ruff: not found") is None  # linter absent: not our problem
    assert (
        tc.lint_findings(1, "x.py:1:1: F821 undefined name", "") == "x.py:1:1: F821 undefined name"
    )
    assert tc.lint_findings(1, "", "stderr text") == "stderr text"
    assert tc.lint_findings(2, "misconfigured", "") is None


def test_file_rules_match_the_pre_m6_behaviour() -> None:
    tc = python_toolchain()
    assert tc.git_exclude_text() == OLD_EXCLUDES + M9_EXCLUDES
    assert tc.cache_excludes == OLD_CACHE_PATTERNS + M9_CACHE_PATTERNS
    assert tc.is_source("pkg/mod.py") and not tc.is_source("README.md")
    assert tc.is_test_path("tests/test_x.py") and tc.is_test_path("pkg/tests/x.py")
    assert tc.is_test_path("test_x.py") and not tc.is_test_path("pkg/mod.py")
    assert tc.is_path_selector("tests/test_x.py::test_a") and tc.is_path_selector("tests/t.py")
    assert tc.is_path_selector("C:\\w\\tests\\t.py") and not tc.is_path_selector("slug")
    assert tc.syntax_error("def f(:\n", "x.py") is not None
    assert tc.syntax_error("def f():\n    return 1\n", "x.py") is None
    assert tc.removed_public_symbol("-def old_name(x):") == "old_name"
    assert tc.removed_public_symbol("-def _private(x):") is None
    assert tc.removed_public_symbol("-    return x") is None
    assert "pyproject.toml" in tc.dependency_files and "package.json" in tc.dependency_files
    assert tc.script_argv("/w/scripts/check_docs.py") == ["python", "/w/scripts/check_docs.py"]
    assert dict(tc.env) == {"PYTHONUTF8": "1"}


# --- the boundary: no stack-specific command argv outside scio/toolchain/ ---

_ARGV_TOKEN = re.compile(
    r"""["'](?:pytest|ruff|mypy|pip|uv|node|npm|pnpm|npx|vitest|eslint|tsc)["']\s*,"""
)
_LITERALS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "E9,F63")


def _graph_sources() -> list[Path]:
    root = REPO_ROOT / "scio"
    return [
        p
        for p in root.rglob("*.py")
        if "toolchain" not in p.relative_to(root).parts and "__pycache__" not in p.parts
    ]


def test_no_stack_specific_command_outside_the_toolchain_package() -> None:
    offenders: list[str] = []
    for path in _graph_sources():
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if _ARGV_TOKEN.search(line) or any(lit in line for lit in _LITERALS):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()[:80]}")
    assert offenders == [], "\n".join(offenders)


def test_every_git_grep_for_a_marker_is_a_fixed_string() -> None:
    """I-090: SCIO's markers (``[scio:bootstrap]``) are regex character classes to git, so a
    ``--grep=`` without ``--fixed-strings`` matches almost any commit message."""
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{i}"
        for path in (REPO_ROOT / "scio").rglob("*.py")
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "--grep=" in line and "--fixed-strings" not in line
    ]
    assert offenders == [], offenders


def test_python_profile_declares_the_quality_bar_and_classifies_outcomes() -> None:
    """M9: coverage, build and audit are profile data; an absent tool is a skip, not a pass."""
    tc = python_toolchain()
    names = [c.name for c in tc.quality_checks()]
    assert names == ["coverage", "build", "audit"]
    coverage, build, audit = tc.quality_checks()
    assert coverage.outcome(0, "") == "passed"
    assert coverage.outcome(4, "error: unrecognized arguments: --cov") == "skipped"
    assert coverage.outcome(1, "FAIL Required test coverage of 80% not reached") == "failed"
    assert build.outcome(1, "No module named build") == "skipped"
    assert build.outcome(1, "error: invalid pyproject") == "failed"
    assert audit.outcome(1, "/usr/bin/python: No module named pip_audit") == "skipped"
    assert all(str(a).startswith(".scio/sandbox") for a in build.argv if "/" in str(a))
