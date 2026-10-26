"""The ``Toolchain`` protocol: every stack-specific command and parser the graph needs.

ADR 0009. The graph names *what* it wants (run these tests, lint this file, is this a test
path, what is this failure's signature); a profile says *how* for one stack. The ``python``
profile reproduces the pre-M6 behaviour exactly; M10 adds ``node-ts``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

TestOutcome = Literal["passed", "failed", "no_tests", "broken"]
"""``broken``: the runner itself could not run (collection error, bad invocation)."""

CheckOutcome = Literal["passed", "failed", "skipped"]
"""``skipped``: the check's tool is absent; reported, never counted as a pass (M9)."""


@dataclass(frozen=True)
class QualityCheck:
    """One command of the product-ready quality bar (coverage, build, audit, ...)."""

    name: str
    argv: tuple[str, ...]
    pass_exit_codes: frozenset[int] = frozenset({0})
    skip_exit_codes: frozenset[int] = frozenset()
    skip_patterns: tuple[str, ...] = ()
    """Output fragments that mean the tool is absent (``No module named ...``)."""
    timeout_s: int = 600

    def outcome(self, exit_code: int, output: str) -> CheckOutcome:
        if exit_code in self.pass_exit_codes:
            return "passed"
        if exit_code in self.skip_exit_codes or any(p in output for p in self.skip_patterns):
            return "skipped"
        return "failed"


@dataclass(frozen=True)
class Runtime:
    """How a product environment is created, filled and started (M9, generalised in M10).

    A Python product has a virtualenv with its own interpreter; a Node product has
    ``node_modules`` and no interpreter of its own. The fields say which: ``env_create_argv``
    is None when installing creates the environment, and ``env_interpreter_*`` is None when
    commands run with the host runtime as they are.
    """

    env_dir: str
    """The environment's directory, relative to the repository root (git-ignored)."""
    env_marker_posix: str
    env_marker_windows: str
    """A file whose presence means the environment exists (repository-relative)."""
    install_argv: tuple[str, ...]
    """Installs the appended package specs into the environment."""
    index_allowlist: tuple[str, ...]
    """Hosts the install phase may reach (enforced by sandboxes that can)."""
    app_argv: tuple[str, ...]
    """Starts the app: the target (module or entry file) and its arguments are appended."""
    env_create_argv: tuple[str, ...] | None = None
    """Creates the environment at the appended path (``python -m venv ...``); None when the
    first install creates it (``npm install``)."""
    env_interpreter_posix: str | None = None
    env_interpreter_windows: str | None = None
    """The environment's interpreter relative to ``env_dir``; the sandbox rewrites the
    profile's interpreter word to it. None when the host runtime is used as it is."""
    app_default_target: str | None = None
    """What ``app_argv`` starts when the spec names no module (``src/main.ts``)."""
    test_tools: tuple[str, ...] = ()
    """Requirement specs the test and quality commands need (runner, coverage plugin). A
    local sandbox sees them through SCIO's own environment; any other sandbox installs them
    into the product environment when it is created (I-074)."""


@dataclass(frozen=True)
class Detect:
    """Marker files that identify a repository's stack (M10 profile detection)."""

    markers: tuple[str, ...]
    all_of: bool = False
    """True: every marker must exist (``package.json`` and ``tsconfig.json``)."""


@runtime_checkable
class Toolchain(Protocol):
    # Read-only attributes (profiles are frozen dataclasses).
    @property
    def name(self) -> str: ...
    @property
    def source_suffixes(self) -> tuple[str, ...]: ...
    @property
    def test_dirs(self) -> tuple[str, ...]: ...
    @property
    def cache_excludes(self) -> tuple[str, ...]: ...
    @property
    def dependency_files(self) -> frozenset[str]: ...
    @property
    def env(self) -> Mapping[str, str]: ...
    @property
    def test_runner(self) -> str:
        """Human name of the test runner, for messages (``pytest``)."""
        ...

    @property
    def selector_help(self) -> str:
        """How to name a test for ``tests_added`` and ``test_filter`` (prompt text)."""
        ...

    @property
    def filter_help(self) -> str:
        """Short name of a filter expression for prompt text (``pytest -k filter``)."""
        ...

    @property
    def acceptance_dir(self) -> str:
        """Where a product keeps its black-box acceptance tests (M10, stack-neutral)."""
        ...

    @property
    def command_words(self) -> tuple[str, ...]:
        """Commands a walking-skeleton criterion may name (``python``, ``node``); any other
        backticked command is rewritten by the spec normaliser (ADR 0013 decision 8)."""
        ...

    @property
    def detect(self) -> Detect | None:
        """Marker files that identify this stack in an existing repository."""
        ...

    # --- files ---
    def is_source(self, path: str) -> bool: ...
    def is_test_path(self, path: str) -> bool: ...
    def git_exclude_text(self) -> str: ...
    def syntax_error(self, source: str, path: str) -> str | None:
        """An in-process parse check (None when the stack has none; see
        ``syntax_check_argv``)."""
        ...

    def syntax_check_argv(self, path: str) -> list[str] | None:
        """A command that parses one file (``node --check``); None when ``syntax_error``
        covers it in process."""
        ...

    # --- product skeleton ---
    def skeleton_command(self, name: str) -> str:
        """The walking skeleton's version command (``python -m {name} --version``)."""
        ...

    def app_target(self, name: str, module: str | None) -> str:
        """What the app command starts for this product (a module name or an entry file)."""
        ...

    # --- commands ---
    def test_argv(self, *, filter: str | None, path: str | None) -> list[str]: ...
    def test_outcome(self, exit_code: int, stdout: str) -> TestOutcome: ...
    def failures_only(self, output: str) -> str: ...
    def is_path_selector(self, selector: str) -> bool: ...
    def lint_argv(self, target: str) -> list[str]: ...
    def lint_findings(self, exit_code: int, stdout: str, stderr: str) -> str | None: ...
    def script_argv(self, script: str) -> list[str]: ...
    def quality_checks(self) -> tuple[QualityCheck, ...]:
        """The quality bar beyond tests and lint, in run order (M9)."""
        ...

    def runtime(self) -> Runtime:
        """Environment, install and app commands (M9)."""
        ...

    # --- parsers ---
    def error_signature(self, text: str) -> str: ...
    def removed_public_symbol(self, diff_line: str) -> str | None: ...
