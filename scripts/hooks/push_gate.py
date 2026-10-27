"""Claude Code PreToolUse hook on Bash (issue #61): two guardrails that a rule alone did not hold.

1. ``git push`` waits for the branch's CI (I-104: two red runs went unnoticed behind newer
   pushes). While the latest GitHub Actions run on the branch being pushed failed or is still
   running, the push is denied with the run's link. A branch with no run yet passes. To push
   the fix for a red run (or on top of a running one) on purpose, name the run you have looked
   at: ``SCIO_ACK_CI=<run id> git push ...``.
2. A bare ``git stash`` (``git stash``, ``git stash push``, ``git stash save``, ``git stash -m``)
   is denied: parallel sessions share one stash stack (owner correction).

Reads the hook JSON (``tool_input.command``, ``cwd``) from stdin and prints a deny decision as
JSON; anything else is allowed silently. A failure of the gate itself (no ``gh``, offline, not a
GitHub repository) allows the command with a warning: a broken gate must not stop all work.
Standard library only, so it runs on whatever ``python`` the shell finds.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any

GREEN = {"success", "skipped", "neutral"}
SEPARATORS = re.compile(r"&&|\|\||;|\||\n")
GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?:\n|$)", re.DOTALL)


def strip_heredocs(command: str) -> str:
    """The command without heredoc bodies: a commit message that mentions ``git stash`` is
    text, not a command."""
    return HEREDOC_RE.sub("\n", command)


def segments(command: str) -> list[list[str]]:
    """The simple commands in a shell line, as word lists (quotes respected where possible)."""
    out: list[list[str]] = []
    for part in SEPARATORS.split(strip_heredocs(command)):
        try:
            words = shlex.split(part, posix=True)
        except ValueError:
            words = part.split()
        if words:
            out.append(words)
    return out


def git_call(words: list[str]) -> tuple[str, list[str], str | None] | None:
    """``(subcommand, args, -C dir)`` when ``words`` runs git, after any ``VAR=x`` prefixes."""
    i = 0
    while i < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    if i >= len(words) or os.path.basename(words[i]).removesuffix(".exe") != "git":
        return None
    i += 1
    directory: str | None = None
    while i < len(words) and words[i].startswith("-"):
        if words[i] in GIT_GLOBAL_OPTS_WITH_VALUE and i + 1 < len(words):
            if words[i] == "-C":
                directory = words[i + 1]
            i += 2
        else:
            i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1 :], directory


def is_bare_stash(args: list[str]) -> bool:
    """``git stash`` with no subcommand, or one that pushes onto the shared stack."""
    return not args or args[0] in {"push", "save"} or args[0].startswith("-")


def pushed_branch(args: list[str]) -> str | None:
    """The branch a ``git push`` names (``origin feat/x``, ``-u origin HEAD:feat/x``), or None
    for "the current branch"."""
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) < 2:
        return None
    ref = positional[1].lstrip("+")
    ref = ref.split(":", 1)[1] if ":" in ref else ref
    ref = ref.removeprefix("refs/heads/")
    return None if ref in {"", "HEAD"} else ref


def acknowledged(command: str) -> set[str]:
    return set(re.findall(r"\bSCIO_ACK_CI=(\d+)\b", command))


def push_reason(branch: str, run: dict[str, Any] | None, acks: set[str]) -> str | None:
    """Why a push of ``branch`` must wait, given its latest run, or None to let it through."""
    if run is None:
        return None  # the first push of a branch: no run to wait for
    run_id = str(run.get("databaseId", ""))
    if run_id in acks:
        return None
    url = run.get("url", "")
    if run.get("status") != "completed":
        return (
            f"CI on {branch} is still {run.get('status')} ({url}). Wait for it "
            f"(`gh run watch {run_id}`), or push on purpose with SCIO_ACK_CI={run_id} (I-104)."
        )
    if run.get("conclusion") not in GREEN:
        return (
            f"The latest CI run on {branch} concluded {run.get('conclusion')} ({url}). Look at "
            f"it first; to push its fix, prefix the push with SCIO_ACK_CI={run_id} (I-104)."
        )
    return None


def _run(argv: list[str], cwd: str) -> str:
    r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout.strip()


def latest_run(branch: str, cwd: str) -> dict[str, Any] | None:
    out = _run(
        [
            "gh", "run", "list", "--branch", branch, "--limit", "1",
            "--json", "databaseId,status,conclusion,headSha,url",
        ],
        cwd,
    )  # fmt: skip
    runs = json.loads(out or "[]")
    return runs[0] if runs else None


def evaluate(command: str, cwd: str) -> tuple[str | None, str | None]:
    """``(deny reason, warning)`` for one Bash command line."""
    here = cwd
    for words in segments(command):
        if words[0] == "cd" and len(words) > 1:
            here = os.path.join(here, words[1])
            continue
        call = git_call(words)
        if call is None:
            continue
        sub, args, directory = call
        repo = os.path.join(here, directory) if directory else here
        if sub == "stash" and is_bare_stash(args):
            return (
                "A bare `git stash` pushes onto the stash stack that parallel sessions share. "
                "Copy the files aside or make a temporary commit instead.",
                None,
            )
        if sub == "push":
            try:
                branch = pushed_branch(args) or _run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], repo
                )
                reason = push_reason(branch, latest_run(branch, repo), acknowledged(command))
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                return None, f"push gate could not check CI, push allowed: {e}"
            if reason:
                return reason, None
    return None, None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    command = str((data.get("tool_input") or {}).get("command", ""))
    if "git" not in command:
        return 0
    reason, warning = evaluate(command, str(data.get("cwd") or os.getcwd()))
    if reason:
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
        print(json.dumps({"hookSpecificOutput": decision}))
    elif warning:
        print(json.dumps({"systemMessage": warning}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
