"""The ``python`` toolchain profile: pytest, ruff and the pre-M6 parsers, driven by
``configs/profiles/python.yaml`` (ADR 0009)."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from scio.state.signatures import error_signature as _python_error_signature
from scio.toolchain.base import TestOutcome

PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "profiles"

_FAIL_SECTION_RE = re.compile(r"^=+ (FAILURES|ERRORS) =+$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^=+ .*(passed|failed|error|no tests ran).* =+$", re.MULTILINE)
_REMOVED_PUBLIC_RE = re.compile(r"^-\s*(?:async\s+)?(?:def|class)\s+([A-Za-z][\w]*)")


def failures_only(output: str) -> str:
    """Keep the FAILURES/ERRORS sections and the summary line; drop passing noise."""
    m = _FAIL_SECTION_RE.search(output)
    body = output[m.start() :] if m else output
    lines = [ln for ln in body.splitlines() if " PASSED" not in ln and not ln.startswith("PASSED")]
    return "\n".join(lines).strip()


def has_summary(output: str) -> bool:
    return bool(_SUMMARY_RE.search(output))


@dataclass(frozen=True)
class PythonToolchain:
    """Data from the profile file plus the Python-specific parsers."""

    name: str = "python"
    source_suffixes: tuple[str, ...] = (".py",)
    test_dirs: tuple[str, ...] = ("tests/", "test/")
    test_file_prefix: str = "test_"
    cache_excludes: tuple[str, ...] = (
        "__pycache__",
        "*.pyc",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
    )
    dependency_files: frozenset[str] = frozenset()
    env: Mapping[str, str] = field(default_factory=lambda: {"PYTHONUTF8": "1"})
    test_runner: str = "pytest"
    test_base_argv: tuple[str, ...] = ()
    filter_flag: str = "-k"
    exit_passed: frozenset[int] = frozenset({0})
    exit_no_tests: frozenset[int] = frozenset({5})
    exit_broken: frozenset[int] = frozenset({2, 3, 4})
    selector_help: str = "pytest node ids like tests/test_x.py::test_name, or -k keywords"
    filter_help: str = "pytest -k filter"
    lint_base_argv: tuple[str, ...] = ()
    lint_clean_codes: frozenset[int] = frozenset({0, 127})
    lint_findings_codes: frozenset[int] = frozenset({1})
    script_base_argv: tuple[str, ...] = ("python",)

    # --- files ---
    def is_source(self, path: str) -> bool:
        return path.endswith(self.source_suffixes)

    def is_test_path(self, path: str) -> bool:
        p = path.replace("\\", "/").lstrip("./")
        if any(p.startswith(d) for d in self.test_dirs):
            return True
        if any(f"/{d}" in f"/{p}" for d in self.test_dirs):
            return True
        return PurePosixPath(p).name.startswith(self.test_file_prefix)

    def git_exclude_text(self) -> str:
        return "".join(f"{p}\n" if "*" in p else f"{p}/\n" for p in self.cache_excludes)

    def syntax_error(self, source: str, path: str) -> str | None:
        try:
            ast.parse(source, filename=path)
        except SyntaxError as exc:
            return f"syntax error at {path}:{exc.lineno}: {exc.msg}"
        return None

    # --- commands ---
    def test_argv(self, *, filter: str | None, path: str | None) -> list[str]:
        argv = list(self.test_base_argv)
        if filter:
            argv += [self.filter_flag, filter]
        if path:
            argv.append(path)
        return argv

    def test_outcome(self, exit_code: int, stdout: str) -> TestOutcome:
        if exit_code in self.exit_passed:
            return "passed"
        if exit_code in self.exit_no_tests:
            return "no_tests"
        if exit_code in self.exit_broken and not has_summary(stdout):
            return "broken"
        return "failed"

    def failures_only(self, output: str) -> str:
        return failures_only(output)

    def is_path_selector(self, selector: str) -> bool:
        """``path::name`` node ids and ``*.py`` paths are positional targets; else a filter."""
        s = selector.strip()
        return "::" in s or self.is_source(s) or "/" in s or "\\" in s

    def lint_argv(self, target: str) -> list[str]:
        return [*self.lint_base_argv, target]

    def lint_findings(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        if exit_code in self.lint_clean_codes:
            return None
        if exit_code in self.lint_findings_codes:
            return stdout.strip() or stderr.strip() or None
        return None  # a misconfigured linter is not the agent's problem

    def script_argv(self, script: str) -> list[str]:
        return [*self.script_base_argv, script]

    # --- parsers ---
    def error_signature(self, text: str) -> str:
        return _python_error_signature(text)

    def removed_public_symbol(self, diff_line: str) -> str | None:
        m = _REMOVED_PUBLIC_RE.match(diff_line)
        if m and not m.group(1).startswith("_"):
            return m.group(1)
        return None


def load_python_profile(path: Path | None = None) -> PythonToolchain:
    """Build the profile from ``configs/profiles/python.yaml``."""
    data: dict[str, Any] = yaml.safe_load(
        (path or PROFILES_DIR / "python.yaml").read_text(encoding="utf-8")
    )
    test, lint = data["test"], data["lint"]
    return PythonToolchain(
        name=data["name"],
        source_suffixes=tuple(data["source_suffixes"]),
        test_dirs=tuple(data["test_dirs"]),
        test_file_prefix=data["test_file_prefix"],
        cache_excludes=tuple(data["cache_excludes"]),
        dependency_files=frozenset(data["dependency_files"]),
        env=dict(data.get("env", {})),
        test_runner=test["runner"],
        test_base_argv=tuple(test["argv"]),
        filter_flag=test["filter_flag"],
        exit_passed=frozenset(test["exit_codes"]["passed"]),
        exit_no_tests=frozenset(test["exit_codes"]["no_tests"]),
        exit_broken=frozenset(test["exit_codes"]["broken"]),
        selector_help=test["selector_help"],
        filter_help=test["filter_help"],
        lint_base_argv=tuple(lint["argv"]),
        lint_clean_codes=frozenset(lint["clean_exit_codes"]),
        lint_findings_codes=frozenset(lint["findings_exit_codes"]),
        script_base_argv=tuple(data["script_argv"]),
    )
