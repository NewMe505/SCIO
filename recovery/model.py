"""The supervisor's shared types (ADR 0019).

This module is a frozen contract during a build wave. An agent that needs a change here
reports it to the orchestrator instead of editing it.
"""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Literal


class FailureClass(enum.StrEnum):
    """What ended a supervised run (ADR 0019 decision 5)."""

    DONE = "done"
    HUMAN_STOP = "human_stop"
    TASK_FAULT = "task_fault"
    EXTERNAL_AUTH = "external_auth"
    EXTERNAL_TRANSIENT = "external_transient"
    HARNESS_SUSPECT = "harness_suspect"
    HANG = "hang"
    ORPHAN = "orphan"
    UNKNOWN = "unknown"


REPAIRABLE: frozenset[FailureClass] = frozenset({FailureClass.HARNESS_SUSPECT, FailureClass.HANG})
"""The only classes that start a repair session."""

NOT_AN_INCIDENT: frozenset[FailureClass] = frozenset({FailureClass.DONE, FailureClass.HUMAN_STOP})
"""Outcomes that open no incident. A task fault is recorded as a closed incident."""


class IncidentState(enum.StrEnum):
    """The incident state machine (ADR 0019 section 9)."""

    DETECTED = "detected"
    SNAPSHOTTED = "snapshotted"
    CLASSIFIED = "classified"
    WAITING_BACKOFF = "waiting_backoff"
    ISSUE_OPEN = "issue_open"
    REPAIRING = "repairing"
    PR_VERIFIED = "pr_verified"
    AWAITING_MERGE = "awaiting_merge"
    RUNTIME_UPDATED = "runtime_updated"
    HEALTHY = "healthy"
    RESUMING = "resuming"
    VERIFIED = "verified"
    CLOSED = "closed"
    ESCALATED = "escalated"
    HELD = "held"


TERMINAL: frozenset[IncidentState] = frozenset({IncidentState.CLOSED, IncidentState.ESCALATED})


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    KILLED_HANG = "killed_hang"
    ORPHANED = "orphaned"
    REFUSED = "refused"
    """Not started: another live process holds the workspace's lease."""


@dataclass(frozen=True)
class ProcessKey:
    """A process identity that survives PID reuse: the PID plus its creation time.

    ``created`` is in seconds since the epoch. Two keys match when the PIDs are equal
    and the creation times differ by less than one second.
    """

    pid: int
    created: float

    def matches(self, other: ProcessKey) -> bool:
        return self.pid == other.pid and abs(self.created - other.created) < 1.0


@dataclass
class RunRecord:
    """One supervised launch of the workspace's SCIO launcher."""

    run_id: str
    workspace: str
    launcher: list[str]
    """The workspace's launcher argv, for example ``["sh", "scio.sh"]``."""
    argv: list[str]
    """The SCIO arguments after ``--``, for example ``["build", "--product", "P", "--gh", "R"]``.
    Never includes the ``--outcome-file`` the supervisor appends."""
    env: dict[str, str]
    """The recorded environment subset (``config.recorded_env``): never a secret."""
    started_at: float
    status: RunStatus = RunStatus.RUNNING
    scio_sha: str = ""
    product_dir: str = ""
    build_thread: str = ""
    pid: int = 0
    pid_created: float = 0.0
    supervisor_pid: int = 0
    supervisor_created: float = 0.0
    last_progress_at: float = 0.0
    ended_at: float | None = None
    exit_code: int | None = None
    run_dir: str = ""
    """Directory holding this run's stdout, stderr and outcome files."""
    stdout_path: str = ""
    stderr_path: str = ""
    outcome_path: str = ""
    incident_id: str | None = None
    """The incident this run opened (a failure) or resumes (a replay)."""
    resumed_from: str | None = None
    """The run id this run replays, for a resume."""

    @property
    def process(self) -> ProcessKey:
        return ProcessKey(self.pid, self.pid_created)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values["status"] = RunStatus(values.get("status", RunStatus.RUNNING))
        return cls(**values)


@dataclass(frozen=True)
class Frame:
    """One traceback frame: the path as printed, the line and the function."""

    path: str
    line: int
    function: str


@dataclass
class Evidence:
    """What a finished run left behind, already scrubbed of secrets."""

    exit_code: int | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    outcome: dict[str, Any] | None = None
    """SCIO's ``--outcome-file`` JSON, when SCIO wrote one."""
    hung: bool = False
    orphaned: bool = False
    paused: bool = False
    """stdout (or the outcome file) says the build stopped for a human."""
    halt_reason: str | None = None
    """The build's ``exit_reason`` (``feature_failed``, ``integration_failed`` ...)."""
    last_ledger_line: str = ""
    """The build's last ledger text (the CLI's ``reason:`` line)."""
    exception_type: str = ""
    """The traceback's final exception type as printed, for example
    ``subprocess.TimeoutExpired``."""
    exception_message: str = ""
    frames: list[Frame] = field(default_factory=list)
    """Traceback frames, outermost first."""


@dataclass(frozen=True)
class Classification:
    failure_class: FailureClass
    signature: str
    """``sha256(...)[:16]`` hex; empty for ``DONE`` and ``HUMAN_STOP``."""
    summary: str
    """One line for titles and logs."""
    innermost_scio_frame: str = ""
    """``scio/<path>.py:<function>`` for the innermost frame under ``scio/``, if any."""


@dataclass
class Incident:
    incident_id: str
    signature: str
    failure_class: FailureClass
    summary: str
    workspace: str
    state: IncidentState
    created_at: float
    updated_at: float
    innermost_scio_frame: str = ""
    run_ids: list[str] = field(default_factory=list)
    """The failed runs that hit this incident, oldest first."""
    occurrences: int = 1
    attempts: int = 0
    """Repair sessions started."""
    replays: int = 0
    """Resumes started without a repair (transient or orphan)."""
    issue_number: int | None = None
    worktree: str = ""
    branch: str = ""
    pr_number: int | None = None
    merge_sha: str = ""
    prev_live_wt_sha: str = ""
    snapshot_dir: str = ""
    resume_run_id: str | None = None
    backoff_until: float | None = None
    ledger_id: str = ""
    escalation: str = ""
    """Why it needs a human; set when the state becomes ``ESCALATED``."""
    held_from: IncidentState | None = None
    """The state to return to when a ``HELD`` incident is released."""

    @property
    def open(self) -> bool:
        return self.state not in TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Incident:
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        values["failure_class"] = FailureClass(values["failure_class"])
        values["state"] = IncidentState(values["state"])
        if values.get("held_from") is not None:
            values["held_from"] = IncidentState(values["held_from"])
        return cls(**values)


Verdict = Literal["repaired", "not_scio", "cannot_repair"]
GateResult = Literal["passed", "failed", "not_run"]


@dataclass(frozen=True)
class RepairVerdict:
    """The repair session's final structured answer. The supervisor checks it against
    GitHub and git and never trusts it on its own (guardrail 60)."""

    verdict: Verdict
    pr_number: int | None = None
    branch: str = ""
    ledger_id: str = ""
    root_cause: str = ""
    regression_tests: list[str] = field(default_factory=list)
    gate: GateResult = "not_run"
    risks: list[str] = field(default_factory=list)
    note: str = ""
    """Free text; for ``not_scio`` and ``cannot_repair`` it carries the evidence."""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RepairVerdict:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["repaired", "not_scio", "cannot_repair"]},
        "pr_number": {"type": ["integer", "null"]},
        "branch": {"type": "string"},
        "ledger_id": {"type": "string"},
        "root_cause": {"type": "string"},
        "regression_tests": {"type": "array", "items": {"type": "string"}},
        "gate": {"type": "string", "enum": ["passed", "failed", "not_run"]},
        "risks": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["verdict", "root_cause", "gate", "note"],
}
"""JSON Schema handed to ``claude -p --json-schema`` for the repair's final answer."""


PrState = Literal["OPEN", "CLOSED", "MERGED"]
CiStatus = Literal["success", "failure", "pending", "none"]


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: PrState
    merged_at: str | None
    merge_sha: str
    head_ref: str
    head_sha: str
    body: str
    is_draft: bool
