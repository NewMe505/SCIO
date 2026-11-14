"""The supervised run: lease, launch, follow, hang detection and the exit record.
OWNER: wave-1 agent A."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from recovery import procs
from recovery.commands import Runner, kill_tree, run_command
from recovery.config import WorkspaceConfig
from recovery.model import ProcessKey, RunRecord
from recovery.store import Store

OUTCOME_SUBCOMMANDS: tuple[str, ...] = ("build", "new", "run", "resume")
"""SCIO subcommands that accept ``--outcome-file``. The supervisor appends it only to these."""


def new_run_id(clock: Callable[[], float] = time.time) -> str:
    """``run-<UTC yyyymmddHHMMSS>-<6 hex>``."""
    raise NotImplementedError


def progress_time(workspace: Path, run: RunRecord) -> float:
    """The newest modification time among the workspace's ``.scio/ledger/*.jsonl`` and
    ``.scio/checkpoints.sqlite-wal`` and the run's own output files. 0.0 when none exist."""
    raise NotImplementedError


def supervise(
    cfg: WorkspaceConfig,
    store: Store,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    home: Path,
    incident_id: str | None = None,
    resumed_from: str | None = None,
    echo: bool = True,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    spawn: Callable[..., object] = procs.spawn,
    alive: Callable[[ProcessKey], bool] = procs.alive,
    kill: Callable[[int], None] = kill_tree,
    git: Runner = run_command,
) -> RunRecord:
    """Run ``cfg.launcher + argv`` in ``cfg.workspace`` as this process's child and return
    its finished record.

    - **Lease.** Takes the workspace lease (``cfg.lease_name``). If a live process holds
      it, the record is saved as ``REFUSED`` and returned without starting anything.
    - **Run record.** Records the run before starting it: the ``scio_sha`` (live-wt's
      HEAD), ``product_dir`` (from ``--product`` or ``--dir``), the build thread (from
      ``progress.json`` when present), ``run_dir`` under ``home/runs/<run_id>``, and the
      stdout, stderr and outcome paths.
    - **Outcome file.** Appends ``--outcome-file <path>`` when ``argv[0]`` is in
      ``OUTCOME_SUBCOMMANDS``.
    - **While running.** Every ``cfg.limits.poll_s`` seconds it echoes new output to its
      own stdout and stderr (when ``echo``), updates ``last_progress_at`` from
      :func:`progress_time`, and kills the tree (status ``KILLED_HANG``) after
      ``cfg.limits.hang_after_s`` with no progress.
    - **At exit.** Records the exit code, ``ended_at`` and status ``EXITED``, then releases
      the lease.
    """
    raise NotImplementedError


def find_orphans(
    store: Store, *, alive: Callable[[ProcessKey], bool] = procs.alive
) -> list[RunRecord]:
    """Runs still ``RUNNING`` whose child process and supervisor process are both gone.

    Each one is marked ``ORPHANED``, saved and returned. A run whose supervisor died
    while its child still runs is left alone: the child may finish on its own, and a
    later tick looks again.
    """
    raise NotImplementedError


def replay_argv(run: RunRecord, incident_id: str, *, python: str = sys.executable) -> list[str]:
    """The argv a tick starts to resume ``run`` under a fresh supervisor:

    ``[python, "-m", "recovery", "run", "--workspace", run.workspace,
    "--incident", incident_id, "--resumed-from", run.run_id, "--", *run.argv]``
    """
    raise NotImplementedError


def start_replay(
    run: RunRecord,
    incident_id: str,
    *,
    home: Path,
    package_root: Path,
    spawn_detached: Callable[..., int] = procs.spawn_detached,
) -> int:
    """Start :func:`replay_argv` detached, with ``cwd=package_root`` (the directory that
    holds ``recovery/``). The environment is ``run.env`` plus ``SCIO_RECOVERY_HOME=home``,
    and the log goes to ``home/replays/<incident_id>.log``. Returns the PID."""
    raise NotImplementedError
