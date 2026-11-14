"""The repair session: a Claude Code session started headless on SCIO's own repository, in a
fresh worktree, under SCIO's CLAUDE.md and hooks (no ``--safe-mode``). It runs with
``--permission-mode auto`` and a deny list (ADR 0019 decisions 7 and 12.2).
OWNER: wave-1 agent D."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from recovery.commands import CommandResult, Runner, run_command
from recovery.config import WorkspaceConfig
from recovery.github import GitHub
from recovery.model import VERDICT_SCHEMA, Incident, RepairVerdict

DENIED_TOOLS: tuple[str, ...] = (
    "Bash(gh pr merge*)",
    "Bash(gh api *)",
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git push origin main*)",
    "Bash(git reset --hard*)",
    "Bash(git stash*)",
    "Bash(git checkout main*)",
    "Bash(git worktree *)",
)

STRIPPED_ENV: tuple[str, ...] = ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

SESSION_NAME = "scio-recovery"
UV_SYNC: tuple[str, ...] = ("uv", "sync", "--frozen", "--all-extras", "--group", "dev")
GIT_TIMEOUT_S = 600.0
"""A runaway guard for ``git fetch`` and ``git worktree add``, not a speed target."""
UV_TIMEOUT_S = 1800.0
"""A runaway guard for ``uv sync`` on a cold cache behind the proxy."""

CI_PENDING = "ci pending"
"""The one problem :func:`verify_claim` reports while CI has not finished (or not started)
on the PR's head. The caller waits when it is the only problem and retries later."""

LEDGER_PATH = "docs/ISSUE_LEDGER.md"
_ERROR_DETAIL_CHARS = 500


def branch_name(incident: Incident) -> str:
    """``fix/<issue>-recovery-<signature[:6]>``.

    Raises ``ValueError`` when the incident has no GitHub issue yet: a repair starts only
    after the issue is open (``ISSUE_OPEN``), so a missing one is a caller bug.
    """
    if incident.issue_number is None:
        raise ValueError(f"incident {incident.incident_id} has no GitHub issue to repair under")
    return f"fix/{incident.issue_number}-recovery-{incident.signature[:6]}"


def worktree_path(cfg: WorkspaceConfig, incident: Incident) -> Path:
    """``<scio_repo>/.claude/worktrees/recovery-<incident_id>``. The path is git-ignored and
    skipped by SCIO's repository-wide scans (I-155)."""
    return cfg.scio_repo / ".claude" / "worktrees" / f"recovery-{incident.incident_id}"


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))


def _worktree_branches(repo: Path, git: Runner) -> list[tuple[str, str]]:
    """``(path, branch)`` for each worktree git knows of; the branch is "" when detached."""
    res = _checked(git, ["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    out: list[tuple[str, str]] = []
    path = ""
    branch = ""
    for line in [*res.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
        elif line.startswith("branch "):
            branch = line[len("branch ") :].strip().removeprefix("refs/heads/")
        elif not line.strip() and path:
            out.append((path, branch))
            path, branch = "", ""
    return out


def _describe(argv: Sequence[str]) -> str:
    """The command as a person would type it, without ``-C <repo>``."""
    words = list(argv)
    if len(words) > 2 and words[0] == "git" and words[1] == "-C":
        words = [words[0], *words[3:]]
    return " ".join(words)


def _checked(
    runner: Runner,
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = GIT_TIMEOUT_S,
) -> CommandResult:
    res = runner(argv, cwd=cwd, timeout_s=timeout_s, env=env)
    if res.timed_out:
        raise RuntimeError(f"`{_describe(argv)}` timed out after {timeout_s:.0f} s")
    if res.code != 0:
        detail = (res.stderr.strip() or res.stdout.strip())[-_ERROR_DETAIL_CHARS:]
        raise RuntimeError(f"`{_describe(argv)}` failed (exit {res.code}): {detail}")
    return res


def prepare_worktree(
    cfg: WorkspaceConfig,
    incident: Incident,
    *,
    git: Runner = run_command,
    uv: Runner = run_command,
) -> Path:
    """Create the repair worktree and return its path. If the worktree already exists on
    its branch, it is reused (the probe).

    The steps:
    1. ``git -C <scio_repo> fetch origin``.
    2. ``git -C <scio_repo> worktree add -b <branch> <path> origin/main``.
    3. ``uv sync --frozen --all-extras --group dev`` inside it, with ``UV_SYSTEM_CERTS=1``
       (guardrail 54).

    Raises ``RuntimeError`` naming the failed command.

    Hardening around step 2: ``--no-track``, so the new branch never has ``main`` as its
    upstream; ``git worktree prune`` first, so a deleted directory that git still lists
    does not block the add; and a branch left by an earlier attempt is checked out as it
    is instead of created again. A path that is a worktree on another branch is refused,
    never reused or overwritten. ``uv sync`` runs on reuse too, which covers a crash
    between the add and the sync.
    """
    repo = cfg.scio_repo
    branch = branch_name(incident)
    path = worktree_path(cfg, incident)
    known = [(p, b) for p, b in _worktree_branches(repo, git) if _same_path(p, path)]
    if known and path.is_dir():
        on = known[0][1]
        if on != branch:
            raise RuntimeError(
                f"{path} is already a worktree on {on or 'a detached HEAD'}, not {branch}"
            )
    else:
        _checked(git, ["git", "-C", str(repo), "fetch", "origin"])
        _checked(git, ["git", "-C", str(repo), "worktree", "prune"])
        exists = git(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            timeout_s=GIT_TIMEOUT_S,
        )
        if exists.ok:
            add = ["git", "-C", str(repo), "worktree", "add", path.as_posix(), branch]
        else:
            add = [
                "git",
                "-C",
                str(repo),
                "worktree",
                "add",
                "--no-track",
                "-b",
                branch,
                path.as_posix(),
                "origin/main",
            ]
        path.parent.mkdir(parents=True, exist_ok=True)
        _checked(git, add)
    env = dict(os.environ)
    env["UV_SYSTEM_CERTS"] = "1"
    _checked(uv, list(UV_SYNC), cwd=path, env=env, timeout_s=UV_TIMEOUT_S)
    return path


def repair_argv(cfg: WorkspaceConfig, *, max_turns: int, max_budget_usd: float) -> list[str]:
    """The flags after the binary:

    ``-p --permission-mode <cfg.permission_mode> --permission-prompts none
    --output-format json --json-schema <VERDICT_SCHEMA as JSON> --max-turns N
    --max-budget-usd X --disallowedTools <DENIED_TOOLS...> --name scio-recovery``

    It never passes ``--safe-mode`` or ``--bare``: the session must load SCIO's CLAUDE.md,
    memory and hooks, the opposite of a product session (I-171). There is no
    ``--allowedTools`` (the owner's decision, ADR 0019 12.2). ``--disallowedTools`` is
    variadic, so each rule is its own argument and ``--name`` ends the list.
    """
    return [
        "-p",
        "--permission-mode",
        cfg.permission_mode,
        "--permission-prompts",
        "none",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(VERDICT_SCHEMA),
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        f"{max_budget_usd:.2f}",
        "--disallowedTools",
        *DENIED_TOOLS,
        "--name",
        SESSION_NAME,
    ]


def repair_env(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` without ``STRIPPED_ENV`` and without every ``SCIO_*`` variable, plus
    ``SCIO_PROVIDER_OVERRIDE=fake`` (offline by default) and ``UV_SYSTEM_CERTS=1``.

    A workspace's ``SCIO_WORKER_BACKEND=claude_code``, ``SCIO_PARALLEL_FEATURES`` or
    ``SCIO_CHECKPOINT_DSN`` would otherwise reach the gate the repair session runs on SCIO's
    own tests. The names are compared without case, as Windows does."""
    stripped = {name.upper() for name in STRIPPED_ENV}
    env = {
        k: v
        for k, v in base.items()
        if k.upper() not in stripped and not k.upper().startswith("SCIO_")
    }
    for name in [k for k in env if k.upper() == "UV_SYSTEM_CERTS"]:
        del env[name]
    env["SCIO_PROVIDER_OVERRIDE"] = "fake"
    env["UV_SYSTEM_CERTS"] = "1"
    return env


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return False


def schema_problems(value: Any, schema: Mapping[str, Any], where: str = "answer") -> list[str]:
    """What makes ``value`` fail ``schema``, for the JSON Schema subset ``VERDICT_SCHEMA``
    uses: ``type`` (one or a list), ``enum``, ``properties``, ``required`` and ``items``.
    Extra keys are allowed, as the schema does not forbid them."""
    kinds = schema.get("type")
    if kinds is not None:
        wanted = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_type_ok(value, k) for k in wanted):
            return [f"{where} is {type(value).__name__}, not {' or '.join(wanted)}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where} is {value!r}, not one of {', '.join(map(str, schema['enum']))}"]
    problems: list[str] = []
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{where} lacks the required key {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                problems += schema_problems(value[key], sub, f"{where}.{key}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            problems += schema_problems(item, schema["items"], f"{where}[{i}]")
    return problems


def _cannot(incident: Incident, reason: str) -> RepairVerdict:
    return RepairVerdict(
        verdict="cannot_repair",
        note=f"repair session for incident {incident.incident_id}: {reason}",
    )


def run_repair(
    cfg: WorkspaceConfig,
    incident: Incident,
    worktree: Path,
    brief: str,
    *,
    claude: Runner = run_command,
) -> RepairVerdict:
    """Run ``[cfg.claude_bin, *repair_argv(...)]`` in ``worktree``, with the brief on
    stdin, ``repair_env(os.environ)`` and ``timeout_s=cfg.limits.repair_timeout_s``.

    It parses the result JSON's ``structured_output``. These all become
    ``cannot_repair``, with the reason in ``note``:
    - a timeout;
    - a non-zero exit with no JSON;
    - ``is_error``;
    - a missing ``structured_output``;
    - an answer that fails the schema.

    The result JSON is read the way SCIO reads it (``parse_result`` in
    ``scio/harness/providers/claude_code.py``): JSON on stdout even after a non-zero exit,
    since an ``is_error`` result can come with exit 1 and its ``subtype``
    (``error_max_turns``, ``error_max_budget_usd``) is what sets the caps (I-099).
    """
    limits = cfg.limits
    argv = [
        cfg.claude_bin,
        *repair_argv(
            cfg, max_turns=limits.repair_max_turns, max_budget_usd=limits.repair_max_budget_usd
        ),
    ]
    res = claude(
        argv,
        cwd=worktree,
        stdin=brief,
        timeout_s=limits.repair_timeout_s,
        env=repair_env(os.environ),
    )
    if res.timed_out:
        return _cannot(incident, f"timed out after {limits.repair_timeout_s:.0f} s")
    text = res.stdout.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        detail = (res.stderr.strip() or text)[:_ERROR_DETAIL_CHARS]
        return _cannot(incident, f"claude exited {res.code} with no JSON result: {detail!r}")
    if not isinstance(data, dict):
        return _cannot(incident, f"the result is not a JSON object: {text[:300]!r}")
    return dataclasses.replace(_verdict_from(incident, data), **_metrics(data))


def _verdict_from(incident: Incident, data: Mapping[str, Any]) -> RepairVerdict:
    subtype = str(data.get("subtype") or "")
    if data.get("is_error") or subtype.startswith("error"):
        detail = str(data.get("result") or data.get("error") or data.get("errors") or "").strip()
        return _cannot(
            incident,
            f"is_error (subtype={subtype or '-'}): {detail[:_ERROR_DETAIL_CHARS] or 'no message'}",
        )
    payload = data.get("structured_output")
    if payload is None:
        return _cannot(incident, "the result has no structured_output")
    problems = schema_problems(payload, VERDICT_SCHEMA)
    if problems:
        return _cannot(incident, "the answer fails the verdict schema: " + "; ".join(problems))
    clean = {k: v for k, v in payload.items() if k not in _METRIC_FIELDS}
    return RepairVerdict.from_dict(clean)


_METRIC_FIELDS = frozenset({"num_turns", "cost_usd", "duration_ms", "session_id"})


def _metrics(data: Mapping[str, Any]) -> dict[str, Any]:
    """What the session cost, from Claude Code's own result envelope. The model's answer
    is never trusted for these."""

    def number(key: str) -> float | None:
        value = data.get(key)
        return float(value) if isinstance(value, int | float) else None

    turns, cost, duration = number("num_turns"), number("total_cost_usd"), number("duration_ms")
    return {
        "num_turns": int(turns) if turns is not None else None,
        "cost_usd": cost,
        "duration_ms": int(duration) if duration is not None else None,
        "session_id": str(data.get("session_id") or ""),
    }


_CLOSING_WORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"


def closes_issue(body: str, issue: int) -> bool:
    """True when ``body`` closes ``#issue`` with one of GitHub's closing keywords
    (``Closes #95``); ``#950`` does not count for ``#95``."""
    pattern = rf"\b{_CLOSING_WORDS}:?\s+#{issue}(?![0-9A-Za-z_])"
    return re.search(pattern, body, re.IGNORECASE) is not None


def verify_claim(
    verdict: RepairVerdict,
    incident: Incident,
    cfg: WorkspaceConfig,
    *,
    gh: GitHub,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> list[str]:
    """Problems with a ``repaired`` verdict. An empty list means it holds.

    It checks:
    - the PR exists, is open or merged, its head is ``branch_name(incident)``, and its
      body says ``Closes #<issue>``;
    - CI for the PR's head SHA is ``success``;
    - the PR changes a file under ``tests/`` and ``docs/ISSUE_LEDGER.md``;
    - ``before == after``: the fingerprints of live-wt, the product and the workspace
      databases are unchanged.

    It does not wait for CI: ``pending`` is reported as ``"ci pending"``, and the caller
    retries later. ``none`` (no run registered yet, right after the push) is pending too.

    Each problem is its own string. A verdict that is not ``repaired`` is one problem,
    and the fingerprints are compared for every verdict.
    """
    problems: list[str] = []
    if verdict.verdict != "repaired":
        problems.append(f"the verdict is {verdict.verdict}, not repaired")
    else:
        problems += _pr_problems(verdict, incident, gh)
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old != new:
            problems.append(f"fingerprint {key} changed: {old or '(none)'} -> {new or '(none)'}")
    return problems


def _pr_problems(verdict: RepairVerdict, incident: Incident, gh: GitHub) -> list[str]:
    if incident.issue_number is None:
        return [f"incident {incident.incident_id} has no GitHub issue"]
    expected = branch_name(incident)
    problems: list[str] = []
    if verdict.branch and verdict.branch != expected:
        problems.append(f"the verdict names branch {verdict.branch}, not {expected}")
    if verdict.pr_number is None:
        return [*problems, "no PR: the verdict has no pr_number"]
    number = verdict.pr_number
    pr = gh.pr(number)
    if pr is None:
        return [*problems, f"no PR: #{number} does not exist"]
    if pr.state == "CLOSED":
        problems.append(f"PR #{number} is closed without a merge")
    if pr.head_ref != expected:
        problems.append(f"PR #{number} head branch is {pr.head_ref or '(none)'}, not {expected}")
    if not closes_issue(pr.body, incident.issue_number):
        problems.append(f"PR #{number} body has no `Closes #{incident.issue_number}`")
    if not pr.head_sha:
        problems.append(f"PR #{number} has no head SHA to read CI for")
    else:
        ci = gh.ci_status(pr.head_sha)
        if ci in ("pending", "none"):
            problems.append(CI_PENDING)
        elif ci != "success":
            problems.append(f"ci {ci} on PR #{number} head {pr.head_sha}")
    files = gh.pr_files(number)
    if not any(f.startswith("tests/") for f in files):
        problems.append(f"PR #{number} changes no file under tests/")
    if LEDGER_PATH not in files:
        problems.append(f"PR #{number} does not change {LEDGER_PATH}")
    return problems
