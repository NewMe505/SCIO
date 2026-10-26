"""The ``node-ts`` profile (ADR 0014): data from the yaml, TAP parsers, detection. Nothing
here runs Node; the fixtures are TAP output captured from ``node --test`` 22.23."""

from __future__ import annotations

from pathlib import Path

import pytest

from scio.toolchain import (
    Toolchain,
    available_toolchains,
    detect_toolchain,
    get_toolchain,
    node_ts_toolchain,
    python_toolchain,
)
from scio.toolchain.node_ts import error_signature, failures_only, has_summary

TAP_FAIL = """TAP version 13
# Subtest: adds
ok 1 - adds
  ---
  duration_ms: 0.4875
  type: 'test'
  ...
# Subtest: fails
not ok 2 - fails
  ---
  duration_ms: 0.4579
  type: 'test'
  location: 'C:\\\\Users\\\\x\\\\proj\\\\tests\\\\x.test.ts:5:1'
  failureType: 'testCodeFailure'
  error: |-
    Expected values to be strictly equal:

    3 !== 4

  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  expected: 4
  actual: 3
  operator: 'strictEqual'
  stack: |-
    TestContext.<anonymous> (file:///C:/Users/x/proj/tests/x.test.ts:5:30)
    Test.runInAsyncScope (node:async_hooks:214:14)
  ...
# Subtest: skipped
ok 3 - skipped # SKIP APP_URL not set
  ---
  duration_ms: 0.0585
  type: 'test'
  ...
1..3
# tests 3
# suites 0
# pass 1
# fail 1
# cancelled 0
# skipped 1
# todo 0
# duration_ms 85.4743
"""

TAP_EMPTY = """TAP version 13
1..0
# tests 0
# suites 0
# pass 0
# fail 0
# cancelled 0
# skipped 0
# todo 0
# duration_ms 4.0109
"""


def test_node_ts_profile_loads_and_satisfies_the_protocol() -> None:
    tc = node_ts_toolchain()
    assert isinstance(tc, Toolchain)
    assert get_toolchain("node-ts") is tc and available_toolchains() == ("python", "node-ts")
    assert tc.name == "node-ts" and tc.test_runner == "node:test"
    assert tc.is_source("src/a.ts") and tc.is_source("x.mts") and not tc.is_source("a.py")
    assert tc.is_test_path("tests/a.test.ts") and tc.is_test_path("src/x.spec.ts")
    assert tc.is_test_path("pkg/__tests__/y.ts") and not tc.is_test_path("src/main.ts")
    assert "node_modules" in tc.cache_excludes and "package.json" in tc.dependency_files
    assert tc.git_exclude_text().startswith("node_modules/\n")
    assert dict(tc.env) == {"NODE_NO_WARNINGS": "1", "CI": "1"}
    with pytest.raises(KeyError, match="node-ts"):
        get_toolchain("deno")


def test_node_ts_commands() -> None:
    tc = node_ts_toolchain()
    base = ["node", "--test", "--test-reporter=tap"]
    assert tc.test_argv(filter=None, path=None) == [*base, "tests/**/*.test.ts"]
    assert tc.test_argv(filter=None, path="tests/acceptance") == [
        *base,
        "tests/acceptance/**/*.test.ts",
    ]
    assert tc.test_argv(filter=None, path="tests/x.test.ts") == [*base, "tests/x.test.ts"]
    assert tc.test_argv(filter="adds", path=None) == [
        *base,
        "--test-name-pattern",
        "adds",
        "tests/**/*.test.ts",
    ]
    assert tc.is_path_selector("tests/x.test.ts") and not tc.is_path_selector("adds")
    assert tc.lint_argv("src") == [
        "node",
        "node_modules/eslint/bin/eslint.js",
        "--format",
        "unix",
        "src",
    ]
    assert tc.syntax_check_argv("src/a.ts") == ["node", "--check", "src/a.ts"]
    assert tc.syntax_error("const x: number = ;", "a.ts") is None  # no in-process parser
    assert tc.script_argv("scripts/x.js") == ["node", "scripts/x.js"]
    assert tc.skeleton_command("shop") == "node src/main.ts --version"
    assert (
        tc.app_target("shop", None) == "src/main.ts"
        and tc.app_target("shop", "src/s.ts") == "src/s.ts"
    )
    assert tc.command_words == ("node", "npm", "npx") and tc.acceptance_dir == "tests/acceptance"
    rt = tc.runtime()
    assert rt.env_create_argv is None and rt.env_interpreter_posix is None
    assert rt.env_dir == "node_modules" and rt.env_marker_posix == "node_modules/.package-lock.json"
    assert rt.install_argv[:2] == ("npm", "install") and rt.index_allowlist == (
        "registry.npmjs.org",
    )
    assert [c.name for c in tc.quality_checks()] == ["typecheck", "build", "audit"]
    assert (
        tc.quality_checks()[0].outcome(
            1, "Error: Cannot find module 'node_modules/typescript/bin/tsc'"
        )
        == "skipped"
    )
    assert tc.quality_checks()[0].outcome(2, "src/a.ts(3,5): error TS2322") == "failed"
    assert tc.quality_checks()[2].outcome(127, "") == "skipped"


def test_python_profile_keeps_its_skeleton_and_runtime_shape() -> None:
    tc = python_toolchain()
    assert tc.skeleton_command("shop") == "python -m shop --version"
    assert tc.app_target("shop", None) == "shop" and tc.command_words == ("python", "pytest")
    rt = tc.runtime()
    assert rt.env_create_argv == ("python", "-m", "venv") and rt.env_dir == ".scio/sandbox/venv"
    assert rt.env_interpreter_posix == "bin/python" and rt.env_marker_windows.endswith("python.exe")
    assert tc.syntax_check_argv("a.py") is None and tc.syntax_error("def f(:", "a.py")
    assert tc.detect is not None and "pyproject.toml" in tc.detect.markers


def test_node_ts_outcomes_follow_exit_codes_and_tap_output() -> None:
    tc = node_ts_toolchain()
    assert tc.test_outcome(0, TAP_FAIL.replace("not ok 2", "ok 2")) == "passed"
    assert tc.test_outcome(1, TAP_FAIL) == "failed"
    assert tc.test_outcome(0, TAP_EMPTY) == "no_tests"  # no matching files: exit 0, empty plan
    assert tc.test_outcome(1, "node: bad option --nope") == "failed"
    assert tc.test_outcome(9, "node: bad option --nope") == "broken"
    assert has_summary(TAP_FAIL) and not has_summary("TAP version 13\n")
    assert (
        tc.lint_findings(1, "", "Error: Cannot find module '/w/node_modules/eslint/bin/eslint.js'")
        is None
    )
    assert tc.lint_findings(1, "src/a.ts:3:1: no-unused-vars", "") == "src/a.ts:3:1: no-unused-vars"
    assert tc.lint_findings(0, "", "") is None and tc.lint_findings(127, "", "") is None


def test_failures_only_keeps_failing_blocks_and_the_summary() -> None:
    trimmed = failures_only(TAP_FAIL)
    assert trimmed.startswith("not ok 2 - fails")
    assert "3 !== 4" in trimmed and "code: 'ERR_ASSERTION'" in trimmed
    assert "ok 1 - adds" not in trimmed and "ok 3 - skipped" not in trimmed
    assert "duration_ms: 0.4875" not in trimmed  # the passing test's diagnostics are gone
    assert "# tests 3" in trimmed and "# fail 1" in trimmed
    assert "# Subtest" not in trimmed


def test_error_signature_is_stable_across_line_numbers_and_paths() -> None:
    tc = node_ts_toolchain()
    a = tc.error_signature(TAP_FAIL)
    moved = TAP_FAIL.replace("x.test.ts:5:1", "x.test.ts:9:1").replace(
        "x.test.ts:5:30", "x.test.ts:12:3"
    )
    elsewhere = moved.replace("C:\\\\Users\\\\x\\\\proj", "D:\\\\other").replace(
        "/Users/x/proj", "/srv/b"
    )
    assert a == tc.error_signature(moved) == tc.error_signature(elsewhere)
    other_message = TAP_FAIL.replace("3 !== 4", "3 !== 5")
    assert a != tc.error_signature(other_message)
    other_file = TAP_FAIL.replace("x.test.ts", "y.test.ts")
    assert a != tc.error_signature(other_file)
    uncaught = "TypeError: store.add is not a function\n    at main (file:///w/src/main.ts:12:5)\n"
    assert error_signature(uncaught) == error_signature(uncaught.replace(":12:5", ":40:1"))
    assert error_signature(uncaught) != error_signature(uncaught.replace("add", "list"))


def test_removed_public_symbol_reads_exports() -> None:
    tc = node_ts_toolchain()
    assert tc.removed_public_symbol("-export function add(a: number) {") == "add"
    assert tc.removed_public_symbol("-export async function load() {") == "load"
    assert tc.removed_public_symbol("-export const VERSION = '1'") == "VERSION"
    assert tc.removed_public_symbol("-export interface Bookmark {") == "Bookmark"
    assert tc.removed_public_symbol("-export default class Store {") == "Store"
    assert tc.removed_public_symbol("-function helper() {") is None  # not exported
    assert tc.removed_public_symbol("-  return x;") is None


def test_detection_by_marker_files(tmp_path: Path) -> None:
    assert detect_toolchain(tmp_path) is None
    (tmp_path / "requirements.txt").write_text("rich\n", encoding="utf-8")
    assert detect_toolchain(tmp_path) == "python"
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_toolchain(tmp_path) == "python"  # package.json alone is not node-ts
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert detect_toolchain(tmp_path) == "node-ts"  # both markers win over a stray requirements.txt
    (tmp_path / "requirements.txt").unlink()
    assert detect_toolchain(str(tmp_path)) == "node-ts"
