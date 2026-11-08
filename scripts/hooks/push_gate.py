"""Claude Code PreToolUse hook on Bash (issue #61): two guardrails that a rule alone did not hold.

1. ``git push`` waits for the branch's CI (I-104: two red runs went unnoticed behind newer
   pushes). While the latest GitHub Actions run on the branch being pushed failed or is still
   running, the push is denied with the run's link. A branch with no run yet passes. To push
   the fix for a red run (or on top of a running one) on purpose, name the run you have looked
   at: ``SCIO_ACK_CI=<run id> git push ...``.
2. A bare ``git stash`` (``git stash``, ``git stash push``, ``git stash save``, ``git stash -m``)
   is denied: parallel sessions share one stash stack (owner correction).
3. ``git commit`` waits for lint (I-106: a line over the limit reached a commit). In a repository
   whose ``pyproject.toml`` configures ruff, every changed Python file (staged, modified or
   untracked, since ``git add ... && git commit`` stages in the same line) must pass
   ``ruff check`` and ``ruff format --check``.
4. A heredoc body or a ``python -c`` script holding a backslash, or a double backslash
   anywhere, is denied (guardrail 17, issue #88, I-176): the Bash tool halves ``\\\\`` before
   the shell runs, and the shell rewrites escapes in scripts; the Edit and Write tools carry
   text as it is.
5. A check (pytest, mypy, ruff, the gate, the CI watcher, the docs check, the evals) piped
   into another command is denied unless the line sets ``pipefail`` (I-018, I-173): the
   pipe's status is the last command's, so a failed check reads as success.

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
from collections.abc import Callable
from typing import Any

GREEN = {"success", "skipped", "neutral"}
SEPARATORS = re.compile(r"&&|\|\||;|\||\n")
GIT_GLOBAL_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?:\n|$)", re.DOTALL)


PYTHON_C_RE = re.compile(
    r"""\bpython(?:3)?(?:\.exe)?\s+(?:-\w+\s+)*-c\s+(?P<q>['"])(?P<body>.*?)(?<!\\)(?P=q)""",
    re.DOTALL,
)


def backslash_reason(command: str) -> str | None:
    """Guardrail 17 as a gate (issue #88): a heredoc body or a ``python -c`` script holding a
    backslash is refused. The shell layer has rewritten such backslashes (``\\n``, ``\\\\``,
    Windows paths) six times (I-027, I-071, I-072, I-157, I-162, I-167); the Edit and Write
    tools carry text as it is. A plain command's own arguments (a ``grep`` pattern) are left
    alone: they are not a script."""
    if "\\\\" in command:
        return (
            "This command holds a double backslash, and the Bash tool halves it before the "
            "shell runs (`printf %s 'x\\\\y'` printed `x\\y`; I-176, guardrail 17). Put the "
            "pattern or text in a file with the Write tool and pass the file (`grep -f`, "
            "`jq -f`, `python <file>`), or find a spelling without the escape."
        )
    bodies = [m.group(0).split("\n", 1)[-1] for m in HEREDOC_RE.finditer(command)]
    bodies += [m.group("body") for m in PYTHON_C_RE.finditer(command)]
    if any("\\" in body for body in bodies):
        return (
            "This command carries a backslash inside a heredoc or a `python -c` script, and "
            "the shell layer may rewrite it (guardrail 17). Make the edit with the Edit or Write "
            "tool, or write the text or script to a file with Write and pass the file "
            "(`--body-file`, `-F`, `python <file>`)."
        )
    return None


CHECKS = {
    "pytest",
    "mypy",
    "ruff",
    "gate.py",
    "watch_ci.py",
    "check_docs.py",
    "evals.runner",
}
RUNNERS = {"uv", "python", "python3", "pytest", "mypy", "ruff"}
LIST_ENDS = {";", "&&", "||", "&", "(", ")"}


def _is_check(words: list[str]) -> bool:
    """Whether a simple command runs a check: it starts (after ``VAR=x`` prefixes) with a
    Python runner or a check and names a check. ``grep pytest log`` does not run one."""
    i = 0
    while i < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    if i >= len(words) or os.path.basename(words[i]).removesuffix(".exe") not in RUNNERS:
        return False
    return any(os.path.basename(w).removesuffix(".exe") in CHECKS for w in words[i:])


def pipefail_reason(command: str) -> str | None:
    """A check piped into another command without ``pipefail`` is refused (I-018, I-173):
    ``pytest ... | tail -3; echo $?`` prints the status of ``tail``, so a red check reads
    green. A line that sets ``pipefail`` anywhere passes."""
    if "pipefail" in command:
        return None
    for line in strip_heredocs(command).splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            continue
        words: list[str] = []
        for token in tokens:
            if token == "|" and _is_check(words):
                return (
                    f"This line pipes a check (`{' '.join(words)[:80]}`) into another command "
                    "without `set -o pipefail`, so its exit code is lost (I-018, I-173). Start "
                    "the line with `set -o pipefail`, or drop the pipe."
                )
            words = [] if token in LIST_ENDS or token == "|" else [*words, token]
    return None


GIT_BASH_DRIVE_RE = re.compile(r"^/([A-Za-z])(?=/|$)")


def native_dir(path: str, windows: bool = os.name == "nt") -> str:
    """A directory as Windows takes it. Git Bash spells ``C:/Users/x`` as ``/c/Users/x``, and
    ``os.path.join("C:/...", "/c/Users/x")`` keeps the drive: ``C:/c/Users/x``, which does not
    exist. Every git call the gate made there raised ``WinError 267`` and the gate let the
    command through (I-179). Off Windows, and for any other spelling, the path is unchanged."""
    if not windows:
        return path
    m = GIT_BASH_DRIVE_RE.match(path)
    return f"{m.group(1).upper()}:{path[2:] or '/'}" if m else path


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


def changed_python_files(porcelain: str, root: str) -> list[str]:
    """The ``.py`` files ``git status --porcelain`` lists as added, modified or untracked."""
    files: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4 or "D" in line[:2]:
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if path.endswith(".py") and os.path.isfile(os.path.join(root, path)):
            files.append(path)
    return files


def lint_reason(files: list[str], run: Callable[[list[str]], tuple[int, str]]) -> str | None:
    """Why the commit must wait for lint, or None. ``run(argv)`` gives (exit code, output)."""
    if not files:
        return None
    for tool in (["check"], ["format", "--check"]):
        argv = ["uv", "run", "--no-sync", "ruff", *tool, "--force-exclude", "--quiet", *files]
        code, output = run(argv)
        if code != 0:
            return (
                f"`ruff {' '.join(tool)}` fails on the changed Python files; fix them before "
                f"committing (I-106):\n{output.strip()[-800:]}"
            )
    return None


def commit_reason(cwd: str) -> str | None:
    root = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    pyproject = os.path.join(root, "pyproject.toml")
    if not os.path.isfile(pyproject):
        return None
    with open(pyproject, encoding="utf-8") as f:
        if "[tool.ruff" not in f.read():
            return None
    porcelain = _run(["git", "status", "--porcelain", "--untracked-files=all"], root)

    def run(argv: list[str]) -> tuple[int, str]:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=25)
        return r.returncode, r.stdout + r.stderr

    return lint_reason(changed_python_files(porcelain, root), run)


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
            here = os.path.join(here, native_dir(words[1]))
            continue
        call = git_call(words)
        if call is None:
            continue
        sub, args, directory = call
        repo = os.path.join(here, native_dir(directory)) if directory else here
        if sub == "stash" and is_bare_stash(args):
            return (
                "A bare `git stash` pushes onto the stash stack that parallel sessions share. "
                "Copy the files aside or make a temporary commit instead.",
                None,
            )
        if sub == "commit":
            try:
                reason = commit_reason(repo)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
                return None, f"commit gate could not run ruff, commit allowed: {e}"
            if reason:
                return reason, None
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
    reason, warning = backslash_reason(command) or pipefail_reason(command), None
    if reason is None and "git" in command:
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
