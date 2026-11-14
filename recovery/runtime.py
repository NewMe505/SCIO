"""Updating the SCIO runtime (``.scio/live-wt``) after a merged repair, and the health check
before a resume (ADR 0019 decision 8). OWNER: wave-1 agent E."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from recovery import procs
from recovery.commands import Runner, run_command
from recovery.config import WorkspaceConfig
from recovery.model import RunRecord


class RuntimeRefused(Exception):
    """live-wt cannot move now.

    ``retryable`` is True when waiting may fix it (a SCIO process is running, CI is
    pending) and False when only a human can fix it (a dirty tree, a target that is not
    on ``origin/main``).
    """

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


def head(path: Path, *, git: Runner = run_command) -> str:
    """``git -C <path> rev-parse HEAD``. Raises ``RuntimeError`` on failure."""
    raise NotImplementedError


def move_live_wt(
    cfg: WorkspaceConfig,
    target_sha: str,
    *,
    git: Runner = run_command,
    scio_running: Callable[[], list[int]] = procs.scio_processes,
) -> str:
    """Move live-wt to ``target_sha`` and return the SHA it was on.

    1. Refuse (retryable) while ``scio_running()`` is non-empty.
    2. Refuse (not retryable) when ``git -C <live_wt> status --porcelain`` is non-empty.
    3. ``git -C <live_wt> fetch origin``.
    4. Refuse (not retryable) unless ``git merge-base --is-ancestor <target> origin/main``.
    5. ``git -C <live_wt> checkout --detach <target>``.
    6. Probe: ``rev-parse HEAD`` equals ``target_sha``.

    If live-wt is already at ``target_sha``, it returns ``target_sha`` and changes nothing.
    """
    raise NotImplementedError


def rollback_live_wt(cfg: WorkspaceConfig, sha: str, *, git: Runner = run_command) -> None:
    """``git -C <live_wt> checkout --detach <sha>``, probed. Raises ``RuntimeError`` if the
    probe fails."""
    raise NotImplementedError


def lock_changed(
    cfg: WorkspaceConfig, old_sha: str, new_sha: str, *, git: Runner = run_command
) -> bool:
    """True when ``uv.lock`` differs between the two SHAs (``git diff --quiet old new --
    uv.lock`` in live-wt). Decision 12.5: such a resume is refused."""
    raise NotImplementedError


def health_check(
    cfg: WorkspaceConfig,
    run: RunRecord,
    work_dir: Path,
    *,
    runner: Runner = run_command,
) -> list[str]:
    """Problems that block a resume. An empty list means healthy.

    It runs the workspace launcher, in the workspace, with ``run.env``:
    1. ``<launcher> --version`` exits 0.
    2. ``<launcher> status --product <run.product_dir>`` exits 0, when there is a product.
    3. ``<launcher> report --thread <run.build_thread>`` exits 0 against copies of the
       workspace's ``checkpoints.sqlite`` and ``effects.sqlite``, made under ``work_dir``
       with ``sqlite3`` backup. The copies are used through ``SCIO_CHECKPOINT_DSN`` and
       ``SCIO_EFFECTS_PATH`` (guardrail 14). The live databases are never opened by the
       new code before the resume.
    """
    raise NotImplementedError
