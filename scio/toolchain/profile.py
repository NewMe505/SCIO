"""The data-driven toolchain: one dataclass for every profile, loaded from
``configs/profiles/<name>.yaml``, plus the stack's parsers (ADR 0009, ADR 0014).

A profile is data (commands, exit codes, file rules, the quality bar, the runtime) and a
small set of parsers that read the stack's tool output (``StackParsers``). ``python.py``
and ``node_ts.py`` each provide one parser set; everything else is shared here, so a third
stack is a yaml file and a parser module.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from scio.toolchain.base import Detect, QualityCheck, Runtime, TestOutcome

PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "profiles"


@dataclass(frozen=True)
class StackParsers:
    """The functions that read one stack's tool output."""

    name: str
    failures_only: Callable[[str], str]
    """Trim a test run's output to the failures and the summary."""
    has_summary: Callable[[str], bool]
    """True when the runner printed its end-of-run summary (so a non-zero exit is a test
    failure, not a broken invocation)."""
    error_signature: Callable[[str], str]
    """A short stable hash of a failure block (escalation signal 1)."""
    removed_public_symbol: Callable[[str], str | None]
    """The public symbol a removed diff line defined, if any (impact detector)."""
    syntax_error: Callable[[str, str], str | None]
    """An in-process parse check of ``(source, path)``; None when the stack has none."""


def _no_syntax_check(source: str, path: str) -> str | None:
    return None


@dataclass(frozen=True)
class ProfileToolchain:
    """Data from the profile file plus the stack's parsers. Satisfies ``Toolchain``."""

    parsers: StackParsers
    name: str
    source_suffixes: tuple[str, ...]
    test_dirs: tuple[str, ...]
    cache_excludes: tuple[str, ...]
    dependency_files: frozenset[str]
    test_runner: str
    test_base_argv: tuple[str, ...]
    filter_flag: str
    exit_passed: frozenset[int]
    exit_no_tests: frozenset[int]
    exit_broken: frozenset[int]
    selector_help: str
    filter_help: str
    lint_base_argv: tuple[str, ...]
    lint_clean_codes: frozenset[int]
    lint_findings_codes: frozenset[int]
    script_base_argv: tuple[str, ...]
    runtime_config: Runtime
    env: Mapping[str, str] = field(default_factory=dict)
    test_file_prefix: str = ""
    test_file_suffixes: tuple[str, ...] = ()
    test_default_targets: tuple[str, ...] = ()
    """Appended to the test command when no path is given (``tests/**/*.test.ts``)."""
    test_path_glob: str | None = None
    """How a directory path becomes a target (``{path}/**/*.test.ts``); None: positional."""
    no_tests_patterns: tuple[str, ...] = ()
    """Output fragments that mean no tests ran when the exit code says passed."""
    skipped_only_all: tuple[str, ...] = ()
    skipped_only_none: tuple[str, ...] = ()
    """Regexes over a passing run's output: all of the first and none of the second mean
    every selected test was skipped (I-097)."""
    lint_absent_patterns: tuple[str, ...] = ()
    """Output fragments that mean the linter is not installed (clean, like exit 127)."""
    syntax_check_base_argv: tuple[str, ...] | None = None
    health_probe_base_argv: tuple[str, ...] = ()
    """Exits 0 when ``{url}`` answers below 400; run inside a non-local sandbox."""
    checks: tuple[QualityCheck, ...] = ()
    acceptance_dir: str = "tests/acceptance"
    skeleton_command_template: str = ""
    command_words: tuple[str, ...] = ()
    detect: Detect | None = None
    release_image: str = ""

    def quality_checks(self) -> tuple[QualityCheck, ...]:
        return self.checks

    def runtime(self) -> Runtime:
        return self.runtime_config

    # --- files ---
    def is_source(self, path: str) -> bool:
        return path.endswith(self.source_suffixes)

    def is_test_path(self, path: str) -> bool:
        p = path.replace("\\", "/").lstrip("./")
        if any(p.startswith(d) for d in self.test_dirs):
            return True
        if any(f"/{d}" in f"/{p}" for d in self.test_dirs):
            return True
        name = PurePosixPath(p).name
        if self.test_file_prefix and name.startswith(self.test_file_prefix):
            return True
        return bool(self.test_file_suffixes) and name.endswith(self.test_file_suffixes)

    def git_exclude_text(self) -> str:
        return "".join(f"{p}\n" if "*" in p else f"{p}/\n" for p in self.cache_excludes)

    def syntax_error(self, source: str, path: str) -> str | None:
        return self.parsers.syntax_error(source, path)

    def syntax_check_argv(self, path: str) -> list[str] | None:
        if self.syntax_check_base_argv is None:
            return None
        return [*self.syntax_check_base_argv, path]

    # --- product skeleton ---
    def skeleton_command(self, name: str) -> str:
        return self.skeleton_command_template.format(name=name)

    def app_target(self, name: str, module: str | None) -> str:
        return module or self.runtime_config.app_default_target or name

    # --- commands ---
    def test_argv(self, *, filter: str | None, path: str | None) -> list[str]:
        argv = list(self.test_base_argv)
        if filter:
            argv += [self.filter_flag, filter]
        if path:
            is_file = path.endswith(self.test_file_suffixes) if self.test_file_suffixes else True
            if self.test_path_glob and not is_file:
                argv.append(self.test_path_glob.format(path=path.rstrip("/")))
            else:
                argv.append(path)
        elif self.test_default_targets:
            argv.extend(self.test_default_targets)
        return argv

    def test_outcome(self, exit_code: int, stdout: str) -> TestOutcome:
        if exit_code in self.exit_passed:
            if any(p in stdout for p in self.no_tests_patterns):
                return "no_tests"
            return "passed"
        if exit_code in self.exit_no_tests:
            return "no_tests"
        if exit_code in self.exit_broken and not self.parsers.has_summary(stdout):
            return "broken"
        return "failed"

    def skipped_only(self, stdout: str) -> bool:
        if not self.skipped_only_all:
            return False
        return all(re.search(p, stdout) for p in self.skipped_only_all) and not any(
            re.search(p, stdout) for p in self.skipped_only_none
        )

    def failures_only(self, output: str) -> str:
        return self.parsers.failures_only(output)

    def is_path_selector(self, selector: str) -> bool:
        """``path::name`` node ids and source paths are positional targets; else a filter."""
        s = selector.strip()
        return "::" in s or self.is_source(s) or "/" in s or "\\" in s

    def lint_argv(self, target: str) -> list[str]:
        return [*self.lint_base_argv, target]

    def lint_findings(self, exit_code: int, stdout: str, stderr: str) -> str | None:
        if exit_code in self.lint_clean_codes:
            return None
        if self.lint_absent_patterns and any(
            p in stdout or p in stderr for p in self.lint_absent_patterns
        ):
            return None  # the linter is not installed; not the agent's problem
        if exit_code in self.lint_findings_codes:
            return stdout.strip() or stderr.strip() or None
        return None  # a misconfigured linter is not the agent's problem

    def script_argv(self, script: str) -> list[str]:
        return [*self.script_base_argv, script]

    def health_probe_argv(self, url: str) -> list[str]:
        return [a.replace("{url}", url) for a in self.health_probe_base_argv]

    # --- parsers ---
    def error_signature(self, text: str) -> str:
        return self.parsers.error_signature(text)

    def removed_public_symbol(self, diff_line: str) -> str | None:
        return self.parsers.removed_public_symbol(diff_line)


def load_profile(path: Path, parsers: StackParsers) -> ProfileToolchain:
    """Build a profile from its yaml file and the stack's parser set."""
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    test, lint = data["test"], data["lint"]
    syntax = data.get("syntax_check_argv")
    detect = data.get("detect")
    return ProfileToolchain(
        parsers=parsers,
        name=data["name"],
        source_suffixes=tuple(data["source_suffixes"]),
        test_dirs=tuple(data["test_dirs"]),
        test_file_prefix=str(data.get("test_file_prefix", "")),
        test_file_suffixes=tuple(data.get("test_file_suffixes", [])),
        cache_excludes=tuple(data["cache_excludes"]),
        dependency_files=frozenset(data["dependency_files"]),
        env=dict(data.get("env", {})),
        test_runner=test["runner"],
        test_base_argv=tuple(test["argv"]),
        test_default_targets=tuple(test.get("default_targets", [])),
        test_path_glob=test.get("path_glob"),
        filter_flag=test["filter_flag"],
        exit_passed=frozenset(test["exit_codes"]["passed"]),
        exit_no_tests=frozenset(test["exit_codes"]["no_tests"]),
        exit_broken=frozenset(test["exit_codes"]["broken"]),
        no_tests_patterns=tuple(test.get("no_tests_patterns", [])),
        skipped_only_all=tuple(test.get("skipped_only", {}).get("all", [])),
        skipped_only_none=tuple(test.get("skipped_only", {}).get("none", [])),
        selector_help=test["selector_help"],
        filter_help=test["filter_help"],
        lint_base_argv=tuple(lint["argv"]),
        lint_clean_codes=frozenset(lint["clean_exit_codes"]),
        lint_findings_codes=frozenset(lint["findings_exit_codes"]),
        lint_absent_patterns=tuple(lint.get("absent_patterns", [])),
        syntax_check_base_argv=tuple(str(a) for a in syntax) if syntax else None,
        health_probe_base_argv=tuple(str(a) for a in data.get("health_probe_argv", [])),
        script_base_argv=tuple(data["script_argv"]),
        checks=tuple(
            QualityCheck(
                name=str(c["name"]),
                argv=tuple(str(a) for a in c["argv"]),
                pass_exit_codes=frozenset(c.get("pass_exit_codes", [0])),
                skip_exit_codes=frozenset(c.get("skip_exit_codes", [])),
                skip_patterns=tuple(c.get("skip_patterns", [])),
                timeout_s=int(c.get("timeout_s", 600)),
                verify=bool(c.get("verify", False)),
            )
            for c in data.get("checks", [])
        ),
        runtime_config=_runtime(data["runtime"]),
        acceptance_dir=str(data.get("acceptance_dir", "tests/acceptance")),
        skeleton_command_template=str(data.get("skeleton_command", "")),
        command_words=tuple(str(w) for w in data.get("command_words", [])),
        detect=Detect(tuple(detect["markers"]), bool(detect.get("all_of", False)))
        if detect
        else None,
        release_image=str(data.get("release_image", "")),
    )


def _runtime(data: dict[str, Any]) -> Runtime:
    create = data.get("env_create_argv")
    interp = data.get("env_interpreter") or {}
    marker = data.get("env_marker") or {}
    env_dir = str(data["env_dir"])
    # The marker defaults to the interpreter inside the environment.
    interp_posix = str(interp["posix"]) if interp else None
    interp_windows = str(interp["windows"]) if interp else None
    return Runtime(
        env_dir=env_dir,
        env_marker_posix=str(marker["posix"]) if marker else f"{env_dir}/{interp_posix}",
        env_marker_windows=str(marker["windows"]) if marker else f"{env_dir}/{interp_windows}",
        install_argv=tuple(str(a) for a in data["install_argv"]),
        index_allowlist=tuple(str(h) for h in data.get("index_allowlist", [])),
        app_argv=tuple(str(a) for a in data["app_argv"]),
        env_create_argv=tuple(str(a) for a in create) if create else None,
        env_interpreter_posix=interp_posix,
        env_interpreter_windows=interp_windows,
        app_default_target=data.get("app_default_target"),
        test_tools=tuple(str(t) for t in data.get("test_tools", [])),
    )


_WORD_RE = re.compile(r"[A-Za-z_][\w-]*")


def first_word(command: str) -> str | None:
    """The command word of a shell-like string (``python -m x`` -> ``python``)."""
    m = _WORD_RE.match(command.strip())
    return m.group(0) if m else None
