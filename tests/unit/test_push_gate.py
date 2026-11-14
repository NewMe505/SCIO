"""The push-gate hook (issue #61): pushes wait for the branch's CI, bare stashes are refused.

The decision logic runs on recorded ``gh run list --json databaseId,status,conclusion,headSha,url``
output from this repository; ``evaluate`` is exercised with ``latest_run`` stubbed, so no test
reaches GitHub.
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
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


def test_a_git_bash_drive_path_becomes_a_path_windows_can_run_git_in() -> None:
    """I-179: `cd /c/Users/x && git push` made the gate join `C:/c/Users/x`, which does not
    exist. The gate's git call raised WinError 267, and the gate let the command through: 8
    commits unlinted and 3 pushes unchecked in one session. `windows=True` runs on Linux CI."""
    pg = _load()
    assert pg.native_dir("/c/Users/x", windows=True) == "C:/Users/x"
    assert pg.native_dir("/d", windows=True) == "D:/"
    for unchanged in ("C:/Users/x", "sub", "../x", "/cd/x", "/usr/lib", "$M"):
        assert pg.native_dir(unchanged, windows=True) == unchanged, unchanged
    assert pg.native_dir("/c/Users/x", windows=False) == "/c/Users/x"


@pytest.mark.parametrize(
    "line",
    [
        "cd /c/x/SCIO && git -C sub push -q origin feat/91",
        "git -C /c/x/SCIO/sub push -q origin feat/91",
    ],
)
def test_the_gate_runs_git_where_a_git_bash_path_points(
    monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    pg = _load()
    seen: list[str] = []
    monkeypatch.setattr(pg, "native_dir", functools.partial(pg.native_dir, windows=True))

    def fake_latest(branch: str, cwd: str) -> dict[str, Any] | None:
        seen.append(cwd)
        return RED

    monkeypatch.setattr(pg, "latest_run", fake_latest)
    reason, warning = pg.evaluate(line, "C:/start")
    assert reason and "concluded failure" in reason and warning is None
    cwd = seen[0].replace("\\", "/")
    assert "/c/x" not in cwd and cwd.endswith("x/SCIO/sub"), cwd  # was C:/c/x/SCIO/sub
    if os.name == "nt":
        assert cwd.startswith("C:/x/SCIO"), cwd


def test_a_heredoc_that_mentions_git_is_text_not_a_command() -> None:
    pg = _load()
    line = "git log -F - <<'EOF'\nfix: blocks a bare\ngit stash\nand git push\nEOF\ngit log -1"
    assert pg.evaluate(line, ".") == (None, None)


def test_a_backslash_in_a_heredoc_or_a_python_c_script_is_refused() -> None:
    """Guardrail 17 as a gate (issue #88; I-027, I-071, I-072, I-157, I-162, I-167): the shell
    layer rewrites backslashes in a heredoc body or a `python -c` script, so such a command is
    denied and the Edit or Write tool is named. A command's own arguments (a grep pattern) and
    heredocs without a backslash pass."""
    pg = _load()
    refused = [
        "python - <<'EOF'\nt = t.replace('a\\nb', 'c')\nEOF",
        "cat > f.md <<'EOF'\nC:\\Users\\x\\file\nEOF",
        "python -c \"print('a\\tb')\"",
        "git commit -q -F - <<'EOF'\nfix: the \\n escape\nEOF",
    ]
    for line in refused:
        reason = pg.backslash_reason(line)
        assert reason and "Edit or Write" in reason, line
    allowed = [
        'grep -n "a\\|b" file.py',  # a plain command's argument, not a script
        "git commit -q -F - <<'EOF'\nfix: no escapes here\nEOF",
        "python -c \"print('ok')\"",
        "sed -n 1,5p README.md",
    ]
    for line in allowed:
        assert pg.backslash_reason(line) is None, line


def test_a_double_backslash_anywhere_is_refused() -> None:
    """I-176: the Bash tool halves a double backslash before the shell runs: `printf %s
    'x\\\\y'` printed `x\\y`, and a jq filter's `\\\\(` reached jq as `\\(`, its interpolation. The
    hook reads the command as sent, before the halving, so it can refuse it."""
    pg = _load()
    refused = [
        "gh run view 1 --json jobs --jq '.jobs[] | select(.name|test(\"Tests \\\\(\"))'",
        'grep -oE "failed[^\\\\\\\\]{0,80}" log.txt',
        "echo 'x\\\\y'",
    ]
    for line in refused:
        reason = pg.backslash_reason(line)
        assert reason and "I-176" in reason and "Write tool" in reason, line
    assert pg.backslash_reason('grep -n "a\\|b" file.py') is None  # one backslash passes


def test_a_check_piped_without_pipefail_is_refused() -> None:
    """I-173: `watch_ci.py ... 2>&1 | tail -6; echo "EXIT=$?"` printed EXIT=0 over the
    watcher's exit 2 (pre-flight 10, I-018, held only as a rule until then)."""
    pg = _load()
    refused = [
        "uv run python scripts/watch_ci.py --sha 6dba0d2 2>&1 | tail -6; echo EXIT=$?",
        "cd x && UV_SYSTEM_CERTS=1 uv run python -m pytest -n 16 2>&1 | tail -3",
        "uv run python -m mypy | grep error",
        "uv run ruff check . | head",
        "uv run python -m evals.runner --suite regression --provider fake | grep pass",
        "uv run python scripts/gate.py | tee gate.log",
    ]
    for line in refused:
        reason = pg.pipefail_reason(line)
        assert reason and "set -o pipefail" in reason, line
    allowed = [
        "set -o pipefail && uv run python -m pytest -n 16 2>&1 | tail -3",
        "uv run python -m pytest -n 16",
        "uv run python -m pytest -x || echo failed",
        "grep -n pytest CONTRIBUTING.md | head",  # pytest is an argument, not the command
        "git log --oneline | head -3",
        "git commit -q -F - <<'EOF'\nfix: no more pytest | tail\nEOF",
    ]
    for line in allowed:
        assert pg.pipefail_reason(line) is None, line


def _hook(tool_input: dict[str, object]) -> dict[str, object]:
    payload = {"tool_name": "Bash", "tool_input": tool_input, "cwd": str(REPO_ROOT)}
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "hooks" / "push_gate.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(r.stdout) if r.stdout.strip() else {}


def test_a_piped_check_is_rewritten_to_fail_closed_not_refused() -> None:
    """I-188: the refusal cost every session a turn and a retry, about sixteen times in
    issue #95. The hook now prepends `set -o pipefail;`. It keeps every other field of
    the input, since updatedInput replaces the whole object, and it sets no
    permissionDecision, since "allow" would auto-approve the call."""
    tool_input = {
        "command": "uv run python -m pytest -n 16 2>&1 | tail -3",
        "description": "Run the tests",
        "timeout": 600000,
    }
    out = _hook(tool_input)
    spec = out["hookSpecificOutput"]
    assert isinstance(spec, dict)
    assert spec == {
        "hookEventName": "PreToolUse",
        "updatedInput": {**tool_input, "command": "set -o pipefail; " + tool_input["command"]},
    }
    assert "permissionDecision" not in spec
    assert "I-188" in str(out["systemMessage"])


def test_a_line_that_needs_no_rewrite_prints_nothing() -> None:
    for command in ("set -o pipefail; uv run python -m pytest | tail -3", "git log | head -3"):
        assert _hook({"command": command}) == {}, command


def test_a_rewritten_line_still_meets_the_git_gates_and_the_backslash_refusal() -> None:
    stash = _hook({"command": "uv run ruff check . | head && git stash"})
    spec = stash["hookSpecificOutput"]
    assert isinstance(spec, dict) and spec["permissionDecision"] == "deny"
    backslash = _hook({"command": "uv run python -m pytest | grep 'a\\\\b'"})
    spec = backslash["hookSpecificOutput"]
    assert isinstance(spec, dict) and spec["permissionDecision"] == "deny"
    assert "updatedInput" not in spec


def test_the_hook_refuses_a_backslash_heredoc_without_any_git_in_it() -> None:
    """The heredoc check runs before the git checks, which skip a line without `git`."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "python - <<'EOF'\nprint('a\\nb')\nEOF"},
        "cwd": str(REPO_ROOT),
    }
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "hooks" / "push_gate.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "guardrail 17" in out["permissionDecisionReason"]


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
    """I-106: on the whole tree (``git commit -a``, ``git add -A``), staged, modified and
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
    monkeypatch.setattr(pg, "commit_reason", lambda cwd, scope=None: "ruff check fails (I-106)")
    reason, _ = pg.evaluate("git add -A && git commit -q -m 'x'", ".")
    assert reason == "ruff check fails (I-106)"
    monkeypatch.setattr(pg, "commit_reason", lambda cwd, scope=None: None)
    assert pg.evaluate("git add -A && git commit -q -m 'x'", ".") == (None, None)


def _git(repo: Path, *args: str) -> None:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", *identity, *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture(scope="module")
def staged_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """``clean.py`` staged; ``bad.py``, ``my bad.py`` and ``pkg/bad.py`` untracked with a lint
    error; ``a_bad.py`` tracked with an unstaged lint error, so `git status` starts with
    `` M a_bad.py``. The gate never runs the line, so the cases share the repository."""
    repo = tmp_path_factory.mktemp("staged")
    files = {
        "pyproject.toml": "[tool.ruff]\n",
        "a_bad.py": "x = 1\n",
        "clean.py": "x = 1\n",
        "bad.py": "import os\n",
        "my bad.py": "import os\n",
        "pkg/bad.py": "import os\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "pyproject.toml", "a_bad.py")
    _git(repo, "commit", "-q", "--no-verify", "-m", "init")
    (repo / "a_bad.py").write_text("import os\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "clean.py")
    return repo


WHOLE_TREE = ["a_bad.py", "bad.py", "clean.py", "my bad.py", "pkg/bad.py"]


@pytest.mark.parametrize(
    ("line", "linted"),
    [
        # I-186: an unstaged file with a lint error no longer blocks a commit of other files.
        ("git add clean.py && git commit -m x", ["clean.py"]),
        ("git commit -m 'fix (I-186)'", ["clean.py"]),  # nothing added: the staged files
        ("git add bad.py && git commit -m x", ["bad.py", "clean.py"]),
        ("git add a_bad.py && git commit -m x", ["a_bad.py", "clean.py"]),  # status: " M a_bad.py"
        ("git add pkg && git commit -m x", ["clean.py", "pkg/bad.py"]),  # a directory
        ("cd pkg && git add bad.py && git commit -m x", ["clean.py", "pkg/bad.py"]),
        ("git -C pkg add bad.py && git commit -m x", ["clean.py", "pkg/bad.py"]),
        ('git add -- "my bad.py" && git commit -m x', ["clean.py", "my bad.py"]),
        ("git add 'clean.py' 2>&1 && git commit -q -m x > /dev/null", ["clean.py"]),
        (
            "git add clean.py && git commit -m \"$(cat <<'EOF'\nfix: x (I-186)\nEOF\n)\"",
            ["clean.py"],
        ),
        ("git commit -m x -- bad.py", ["bad.py", "clean.py"]),  # commit takes it from the tree
        ("git rm -q gone.py && git commit -m x", ["clean.py"]),  # rm stages no tree content
        ("git commit -a -m x", WHOLE_TREE),
        ("git commit -qam x", WHOLE_TREE),
        ("git commit --all -m x", WHOLE_TREE),
        ("git add -A && git commit -m x", WHOLE_TREE),
        ("git add . && git commit -m x", WHOLE_TREE),
        ("git add -u && git commit -m x", WHOLE_TREE),
        ("git add $FILES && git commit -m x", WHOLE_TREE),  # the shell expands it, not the hook
        ("git ls-files -o | xargs git add && git commit -m x", WHOLE_TREE),
        ("git aa && git commit -m x", WHOLE_TREE),  # an alias may stage anything
        ("git add ../elsewhere.py && git commit -m x", WHOLE_TREE),  # git refuses the pathspec
    ],
)
def test_the_commit_gate_lints_what_the_commit_can_hold(
    staged_repo: Path, monkeypatch: pytest.MonkeyPatch, line: str, linted: list[str]
) -> None:
    """I-186: the hook runs before the line, so it reads what the line stages. It lints the
    staged files and what a ``git add`` (or the commit) names; ``-a``, ``-A``, ``.``, ``-u``
    and any line it cannot read lint the whole tree, the safe direction."""
    pg = _load()
    seen: list[list[str]] = []

    def fake_ruff(files: list[str], run: object) -> str | None:
        seen.append(sorted(files))
        return "ruff check fails (I-106)" if any("bad" in f for f in files) else None

    monkeypatch.setattr(pg, "lint_reason", fake_ruff)
    reason, warning = pg.evaluate(line, str(staged_repo))
    assert seen == [linted] and warning is None
    assert (reason is not None) == any("bad" in f for f in linted)


@pytest.mark.parametrize(
    ("sub", "args", "paths"),
    [
        ("add", ["a.py", "src"], ["a.py", "src"]),
        ("add", ["-v", "--", "-a.py", "b c.py"], ["-a.py", "b c.py"]),
        ("add", ["-A"], None),
        ("add", ["-fu"], None),
        ("add", ["--all", "a.py"], None),
        ("add", ["--pathspec-from-file=list.txt"], None),
        ("add", ["{a,b}.py"], None),
        ("commit", ["-m", "-a", "a.py"], ["a.py"]),  # -a is -m's value
        ("commit", ["-qam", "x"], None),
        ("commit", ["-uall", "-m", "x"], []),  # "all" is -u's value
        ("commit", ["-mx", "--amend", "--no-edit"], []),
        ("commit", ["--author", "A <a@b.c>", "-F", "-", "2>&1"], []),
        ("commit", ["--patch"], None),
    ],
)
def test_work_tree_paths_reads_git_add_and_git_commit(
    sub: str, args: list[str], paths: list[str] | None
) -> None:
    assert _load().work_tree_paths(sub, args) == paths
