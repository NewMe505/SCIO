"""The incident state machine (ADR 0019 section 9). OWNER: wave-1 agent H.

Every transition is one call to :func:`advance`. Each externally visible effect goes
through ``store.step`` with a probe, so running any transition twice (a crash, two ticks)
changes nothing twice. ``run`` calls :func:`on_run_finished` when its child exits, and
``tick`` calls :func:`tick`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from recovery.commands import Runner
from recovery.config import WorkspaceConfig
from recovery.github import GitHub
from recovery.model import Incident, RunRecord
from recovery.store import Store


@dataclass
class Deps:
    """Everything the machine touches, injectable for tests."""

    store: Store
    home: Path
    package_root: Path
    """The directory that holds ``recovery/`` (the supervisor's pinned worktree)."""
    config_for: Callable[[str], WorkspaceConfig]
    git: Runner
    uv: Runner
    claude: Runner
    launcher: Runner
    """Runs the workspace launcher for the health check."""
    github_for: Callable[[str], GitHub]
    scio_running: Callable[[], list[int]]
    start_replay: Callable[[RunRecord, str], int]
    """``(failed_run, incident_id) -> pid``: starts a supervised replay, detached."""
    clock: Callable[[], float]


def default_deps(home: Path, package_root: Path) -> Deps:
    """The real collaborators: ``run_command``, ``GitHub``, ``procs.scio_processes``,
    ``runner.start_replay`` and ``time.time``."""
    raise NotImplementedError


def on_run_finished(run: RunRecord, deps: Deps) -> Incident | None:
    """Read the evidence of a finished (or orphaned) run, classify it, and record it.

    - ``DONE`` or ``HUMAN_STOP``: no incident. A replay that ends this way moves its
      incident to ``VERIFIED``.
    - ``TASK_FAULT``: a new incident, saved straight as ``CLOSED`` for the history. A
      replay that ends this way verifies its incident.
    - A replay that ends with its own incident's signature: that incident escalates
      ("the same failure after the repair").
    - Anything else: attach to the workspace's open incident with the same signature
      (``occurrences += 1``), or open a new one in ``DETECTED``.

    The circuit breaker escalates instead when ``max_incidents_per_day`` is reached, or
    when the signature belongs to an incident that closed after a merged repair (a
    recurrence). Sets ``run.incident_id`` and saves both records.
    """
    raise NotImplementedError


def advance(incident: Incident, deps: Deps) -> Incident:
    """Make at most one transition and return the saved incident.

    An incident that is waiting (CI pending, the merge not done, a backoff running, a
    SCIO process blocking the live-wt move, another repair holding the machine's repair
    lease) comes back unchanged.

    The transitions (ADR 0019 section 9):
    - DETECTED -> SNAPSHOTTED: ``evidence.snapshot`` into ``home/incidents/<id>``.
    - SNAPSHOTTED -> CLASSIFIED: record the class (from ``on_run_finished``) as an event.
    - CLASSIFIED:
      - ``EXTERNAL_TRANSIENT`` -> WAITING_BACKOFF, with ``backoff_until`` from
        ``limits.backoff_s[replays]``;
      - ``ORPHAN`` -> RESUMING, through a replay;
      - anything else -> ISSUE_OPEN, after ``GitHub.ensure_issue`` with the marker and
        labels bug, live-run, recovery (plus needs-human for a class that cannot be
        repaired).
    - ISSUE_OPEN:
      - a class that cannot be repaired -> ESCALATED;
      - else take the ``repair`` lease and go to REPAIRING.
    - REPAIRING:
      - prepare the worktree, render the brief, take fingerprints, run the repair, take
        fingerprints again, and verify the claim;
      - verified -> PR_VERIFIED;
      - ``ci pending`` stays in REPAIRING;
      - ``not_scio`` -> ESCALATED;
      - a failed attempt goes back to ISSUE_OPEN while ``attempts < max_attempts``, else
        ESCALATED;
      - the repair lease is released in every outcome.
    - PR_VERIFIED -> AWAITING_MERGE, with a status comment on the issue.
    - AWAITING_MERGE:
      - merged (``mergedAt``) with green CI on the merge SHA: ``runtime.move_live_wt``,
        then -> RUNTIME_UPDATED;
      - closed without a merge -> ESCALATED;
      - a ``RuntimeRefused`` that cannot be retried -> ESCALATED.
    - RUNTIME_UPDATED:
      - ``lock_changed``, or ``health_check`` problems -> rollback, then ESCALATED;
      - else -> HEALTHY.
    - HEALTHY -> RESUMING: ``deps.start_replay``.
    - WAITING_BACKOFF:
      - when due -> RESUMING (``replays += 1``);
      - over ``max_replays`` -> ESCALATED.
    - RESUMING: waits. ``on_run_finished`` of the replay moves it on.
    - VERIFIED -> CLOSED, with a final status comment.
    - HELD: nothing.

    Every transition adds an event and appends a line to ``home/incidents.jsonl``.
    Every escalation adds needs-human to the issue (when there is one), with the reason.
    """
    raise NotImplementedError


def drive(incident_id: str, deps: Deps, *, max_steps: int = 50) -> Incident:
    """Call :func:`advance` until the incident stops changing or is terminal."""
    raise NotImplementedError


def tick(deps: Deps) -> list[str]:
    """One reconciliation pass, under the machine-wide ``tick`` lease (a second tick
    returns at once).

    1. Each orphaned run (``runner.find_orphans``) goes through :func:`on_run_finished`.
    2. A ``RESUMING`` incident whose replay run is no longer alive, and was never
       finished, is treated the same way.
    3. :func:`drive` runs on every open incident.

    Returns one line per action, for the log.
    """
    raise NotImplementedError


def hold(incident_id: str, deps: Deps) -> Incident:
    """``HELD``, remembering the state to return to."""
    raise NotImplementedError


def release(incident_id: str, deps: Deps) -> Incident:
    """Back to the state it was held from."""
    raise NotImplementedError
