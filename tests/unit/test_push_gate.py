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
    line = "git commit -q -F - <<'EOF'\nfix: blocks a bare\ngit stash\nand git push\nEOF\ngit log -1"
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
