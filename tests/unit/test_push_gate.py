"""The push-gate hook (issue #61): pushes wait for the branch's CI, bare stashes are refused.

The decision logic runs on recorded ``gh run list --json databaseId,status,conclusion,headSha,url``
output from this repository; ``evaluate`` is exercised with ``latest_run`` stubbed, so no test
reaches GitHub.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scio.settings import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "hooks" / "push_gate.py"

# Recorded on 2026-10-27 from NewMe505/SCIO (I-104's red run, and the green one after the fix).
RED = {
    "conclusion": "failure",
    "databaseId": 40555091493,
    "headSha": "8e814fe163ecf1e67eb528bb7f45cf92e7b6d04c",
    "status": "completed",
    "url": "https://github.com/NewMe505/SCIO/actions/runs/40555091493",
}
GREEN = {**RED, "conclusion": "success", "databaseId": 40563267533}
RUNNING = {**RED, "conclusion": "", "status": "in_progress", "databaseId": 40568085916}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("push_gate", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_red_or_running_branch_waits_and_a_green_one_passes() -> None:
    pg = _load()
    red = pg.push_reason("feat/58", RED, set())
    assert red and "concluded failure" in red and "SCIO_ACK_CI=40555091493" in red
    running = pg.push_reason("feat/58", RUNNING, set())
    assert running and "still in_progress" in running and "gh run watch 40568085916" in running
    assert pg.push_reason("feat/58", GREEN, set()) is None
    assert pg.push_reason("feat/58", None, set()) is None  # the first push: no run yet


def test_naming_the_red_run_lets_its_fix_through() -> None:
    pg = _load()
    acks = pg.acknowledged("SCIO_ACK_CI=40555091493 git push -q origin feat/58")
    assert acks == {"40555091493"}
    assert pg.push_reason("feat/58", RED, acks) is None
    assert pg.push_reason("feat/58", RED, {"123"}) is not None  # another run's id is no ack


@pytest.mark.parametrize(
    ("args", "branch"),
    [
        ([], None),
        (["-q"], None),
        (["origin"], None),
        (["-u", "origin", "feat/61-guardrails"], "feat/61-guardrails"),
        (["origin", "HEAD:refs/heads/release/0.8.0"], "release/0.8.0"),
        (["origin", "+main"], "main"),
        (["origin", "HEAD"], None),
    ],
)
def test_pushed_branch_reads_the_refspec(args: list[str], branch: str | None) -> None:
    assert _load().pushed_branch(args) == branch


def test_only_stashes_that_push_onto_the_shared_stack_are_refused() -> None:
    pg = _load()
    for args in ([], ["push"], ["save", "wip"], ["-u"], ["-m", "wip"]):
        assert pg.is_bare_stash(args), args
    for args in (["list"], ["show"], ["pop"], ["apply", "stash@{0}"]):
        assert not pg.is_bare_stash(args), args


def test_evaluate_finds_git_inside_a_compound_line(monkeypatch: pytest.MonkeyPatch) -> None:
    pg = _load()
    seen: list[tuple[str, str]] = []

    def fake_latest(branch: str, cwd: str) -> dict[str, Any] | None:
        seen.append((branch, cwd))
        return RED

    monkeypatch.setattr(pg, "latest_run", fake_latest)
    line = 'cd "C:/x/SCIO" && set -o pipefail && git -C sub push -q origin feat/61 2>&1 | tail -2'
    reason, warning = pg.evaluate(line, "C:/start")
    assert reason and "concluded failure" in reason and warning is None
    assert seen and seen[0][0] == "feat/61" and seen[0][1].replace("\\", "/").endswith("x/SCIO/sub")
    assert pg.evaluate("git stash", ".")[0]
    assert pg.evaluate("echo git push", ".") == (None, None)  # git is an argument, not the command
    assert pg.evaluate("git status && git log -1", ".") == (None, None)


def test_a_heredoc_that_mentions_git_is_text_not_a_command() -> None:
    pg = _load()
    line = "git log -F - <<'EOF'\nfix: blocks a bare\ngit stash\nand git push\nEOF\ngit log -1"
    assert pg.evaluate(line, ".") == (None, None)


def test_a_gate_that_cannot_check_warns_and_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    pg = _load()

    def broken(branch: str, cwd: str) -> dict[str, Any] | None:
        raise OSError("gh: not found")

    monkeypatch.setattr(pg, "latest_run", broken)
    reason, warning = pg.evaluate("git push origin feat/61", ".")
    assert reason is None and warning and "could not check CI" in warning


def test_the_hook_speaks_claude_codes_pretooluse_contract() -> None:
    """Stdin is the hook JSON; a deny is ``hookSpecificOutput.permissionDecision``."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "git stash"}, "cwd": str(REPO_ROOT)}
    r = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(payload), capture_output=True, text=True
    )
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    quiet = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(REPO_ROOT)}
    r = subprocess.run(
        [sys.executable, str(SCRIPT)], input=json.dumps(quiet), capture_output=True, text=True
    )
    assert r.returncode == 0 and r.stdout == ""


def test_the_hook_is_wired_in_the_project_settings() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    commands = [
        h["command"]
        for entry in settings["hooks"]["PreToolUse"]
        if entry.get("matcher") == "Bash"
        for h in entry["hooks"]
    ]
    assert any("scripts/hooks/push_gate.py" in c for c in commands), commands
    assert Path(SCRIPT).is_file()


def test_the_commit_gate_lints_every_changed_python_file(tmp_path: Path) -> None:
    """I-106: ``git add ... && git commit`` stages in the same line, so staged, modified and
    untracked files all count; deleted files and non-Python files do not."""
    pg = _load()
    for rel in ("a.py", "b.py", "new.py", "notes.md", "moved.py"):
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")
    porcelain = "M  a.py\n M b.py\n?? new.py\n?? notes.md\n D gone.py\nR  old.py -> moved.py\n"
    assert pg.changed_python_files(porcelain, str(tmp_path)) == [
        "a.py",
        "b.py",
        "new.py",
        "moved.py",
    ]
    calls: list[list[str]] = []

    def clean(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return 0, ""

    assert pg.lint_reason(["a.py"], clean) is None
    assert [c[4] for c in calls] == ["check", "format"] and all(
        "--force-exclude" in c for c in calls
    )
    assert pg.lint_reason([], clean) is None  # nothing changed: nothing to lint

    def too_long(argv: list[str]) -> tuple[int, str]:
        return (1, "E501 Line too long (101 > 100)") if argv[4] == "check" else (0, "")

    reason = pg.lint_reason(["a.py"], too_long)
    assert reason and "ruff check" in reason and "E501" in reason and "I-106" in reason

    def unformatted(argv: list[str]) -> tuple[int, str]:
        return (1, "Would reformat: a.py") if argv[4] == "format" else (0, "")

    reason = pg.lint_reason(["a.py"], unformatted)
    assert reason and "ruff format --check" in reason


def test_the_commit_gate_only_runs_where_ruff_is_configured(tmp_path: Path) -> None:
    pg = _load()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "bad.py").write_text("x=1;y=2\n", encoding="utf-8")
    assert pg.commit_reason(str(tmp_path)) is None  # no pyproject: not our repository's rules
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert pg.commit_reason(str(tmp_path)) is None  # a pyproject without ruff settings


def test_evaluate_holds_a_commit_for_lint(monkeypatch: pytest.MonkeyPatch) -> None:
    pg = _load()
    monkeypatch.setattr(pg, "commit_reason", lambda cwd: "ruff check fails (I-106)")
    reason, _ = pg.evaluate("git add -A && git commit -q -m 'x'", ".")
    assert reason == "ruff check fails (I-106)"
    monkeypatch.setattr(pg, "commit_reason", lambda cwd: None)
    assert pg.evaluate("git add -A && git commit -q -m 'x'", ".") == (None, None)
