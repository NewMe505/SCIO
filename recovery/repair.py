"""The repair session: a Claude Code session started headless on SCIO's own repository, in a
fresh worktree, under SCIO's CLAUDE.md and hooks (no ``--safe-mode``). It runs with
``--permission-mode auto`` and a deny list (ADR 0019 decisions 7 and 12.2).
OWNER: wave-1 agent D."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from recovery.commands import Runner, run_command
from recovery.config import WorkspaceConfig
from recovery.github import GitHub
from recovery.model import Incident, RepairVerdict

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


def branch_name(incident: Incident) -> str:
    """``fix/<issue>-recovery-<signature[:6]>``."""
    raise NotImplementedError


def worktree_path(cfg: WorkspaceConfig, incident: Incident) -> Path:
    """``<scio_repo>/.claude/worktrees/recovery-<incident_id>``. The path is git-ignored and
    skipped by SCIO's repository-wide scans (I-155)."""
    raise NotImplementedError


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
    """
    raise NotImplementedError


def repair_argv(cfg: WorkspaceConfig, *, max_turns: int, max_budget_usd: float) -> list[str]:
    """The flags after the binary:

    ``-p --permission-mode <cfg.permission_mode> --permission-prompts none
    --output-format json --json-schema <VERDICT_SCHEMA as JSON> --max-turns N
    --max-budget-usd X --disallowedTools <DENIED_TOOLS...> --name scio-recovery``

    It never passes ``--safe-mode`` or ``--bare``.
    """
    raise NotImplementedError


def repair_env(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` without ``STRIPPED_ENV``, with ``SCIO_PROVIDER_OVERRIDE=fake`` (offline by
    default) and ``UV_SYSTEM_CERTS=1``."""
    raise NotImplementedError


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
    """
    raise NotImplementedError


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
    retries later.
    """
    raise NotImplementedError
