"""The recovery supervisor's store, read by the dashboard (issue #102, ADR 0019).

The supervisor (``recovery/``) keeps its runs, incidents and their timelines in one SQLite
file, ``<home>/state.sqlite``. It is a separate package, deployed apart from SCIO and not in
SCIO's wheel, so SCIO never imports it (``test_scio_never_imports_the_supervisor``). The two
share only the store's schema and the JSON in its ``data`` columns
(``RunRecord.to_dict``, ``Incident.to_dict``). ``tests/unit/test_recovery_contract.py`` writes
a store with the supervisor's own ``Store`` and reads it back here, so a change to that shape
on either side fails there (guardrail 70).

This module only parses; the caller opens the store ``mode=ro`` and closes it after each
read, as it does a checkpoint DB. A row whose JSON does not parse is skipped, never the whole
store, and a store with a newer schema than this reader knows is refused with the reason
(guardrail 72: the two are deployed separately, so neither may assume the other is as new).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

HOME_ENV = "SCIO_RECOVERY_HOME"
STORE_FILE = "state.sqlite"
SCHEMA_VERSION = 1
"""The store schema this reader knows: ``recovery.store.SCHEMA_VERSION``."""

RUNNING, EXITED, KILLED_HANG, ORPHANED, REFUSED = (
    "running",
    "exited",
    "killed_hang",
    "orphaned",
    "refused",
)
"""``recovery.model.RunStatus``'s values. A refused run never started (another live process
held the workspace), so it says nothing about a thread."""
RUN_STATUSES = frozenset({RUNNING, EXITED, KILLED_HANG, ORPHANED, REFUSED})

CLOSED, ESCALATED = "closed", "escalated"
TERMINAL_STATES = frozenset({CLOSED, ESCALATED})
"""``recovery.model.TERMINAL``: every other incident state is open, as ``Store.open_incidents``
reads it. A closed incident is never listed; an escalated one is, while it is recent."""

ESCALATED_WINDOW_S = 14 * 24 * 3600.0
"""An escalated incident is listed ("needs you") while its ``updated_at`` is less than this
old. The supervisor has stopped working on it and handed it to the owner, so it is the row
that most needs reading; after two weeks its GitHub issue carries it alone."""


class StoreError(Exception):
    """The store exists but this reader cannot use it (a newer schema)."""


class StoredRun(BaseModel):
    """The fields of a ``runs`` row's ``data`` (``RunRecord.to_dict``) the dashboard reads."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    workspace: str
    status: str
    started_at: float
    build_thread: str = ""
    pid: int = 0
    pid_created: float = 0.0
    ended_at: float | None = None
    exit_code: int | None = None
    incident_id: str | None = None


class StoredIncident(BaseModel):
    """The fields of an ``incidents`` row's ``data`` (``Incident.to_dict``) the dashboard
    reads."""

    model_config = ConfigDict(extra="ignore")

    incident_id: str
    failure_class: str
    summary: str = ""
    workspace: str
    state: str
    created_at: float
    updated_at: float
    innermost_scio_frame: str = ""
    run_ids: list[str] = Field(default_factory=list)
    attempts: int = 0
    replays: int = 0
    issue_number: int | None = None
    pr_number: int | None = None
    escalation: str = ""


Incidents = list[tuple[StoredIncident, str, list[tuple[float, str]]]]
"""Each incident with the build thread it interrupted and its last timeline entries."""


def store_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the supervisor keeps its store: ``recovery.config.recovery_home`` plus
    ``state.sqlite``. That is ``$SCIO_RECOVERY_HOME``, else ``%LOCALAPPDATA%/SCIO/recovery``
    on Windows, else ``$XDG_STATE_HOME/scio/recovery`` (``~/.local/state/scio/recovery``).
    An empty variable counts as unset, as there."""
    env = os.environ if env is None else env
    if env.get(HOME_ENV):
        home = Path(env[HOME_ENV])
    elif sys.platform == "win32" and env.get("LOCALAPPDATA"):
        home = Path(env["LOCALAPPDATA"]) / "SCIO" / "recovery"
    else:
        state = env.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        home = Path(state) / "scio" / "recovery"
    return home / STORE_FILE


def store_version(path: Path) -> str:
    """Size and modification time of the store and its WAL, which every write of the
    supervisor changes; "" when there is no store. An empty WAL counts as none: a read-only
    reader of a store no writer has open leaves one behind, and that is no change. Cheap
    enough to read every second, and it opens nothing."""
    parts = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            st = p.stat()
        except OSError:
            parts.append("-")
            continue
        parts.append(f"{st.st_size}:{st.st_mtime_ns}" if st.st_size or p == path else "-")
    return "" if parts[0] == "-" else "|".join(parts)


def same_path(a: str, b: str) -> bool:
    """The supervisor stores a workspace as ``str(Path.resolve())``; the dashboard's root is
    resolved too, but the spelling (case on Windows, separators) may differ."""
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def at(epoch: float | None) -> datetime | None:
    """A store time (seconds since the epoch) in UTC; None for none or a nonsense value."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """No rows for a store whose tables are not created yet."""
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def check_schema(conn: sqlite3.Connection) -> None:
    """Raises :class:`StoreError` for a schema newer than :data:`SCHEMA_VERSION`."""
    rows = _rows(conn, "select value from meta where key = 'schema_version'")
    if not rows:
        return
    try:
        version = int(rows[0][0])
    except (TypeError, ValueError):
        raise StoreError(f"the store's schema version {rows[0][0]!r} is not a number") from None
    if version > SCHEMA_VERSION:
        raise StoreError(
            f"the supervisor's store has schema {version}; this dashboard reads schema "
            f"{SCHEMA_VERSION}, so update SCIO"
        )


def _parse[M: BaseModel](model: type[M], text: object) -> M | None:
    if not isinstance(text, str | bytes):
        return None
    try:
        return model.model_validate_json(text)
    except ValidationError:
        return None


def read_runs(conn: sqlite3.Connection) -> list[StoredRun]:
    """Every run that started (a refused one did not), oldest first; rows that do not parse
    are skipped."""
    rows = _rows(conn, "select data from runs where status != ? order by started_at", (REFUSED,))
    runs = [_parse(StoredRun, data) for (data,) in rows]
    return [r for r in runs if r is not None and r.status != REFUSED]


def latest_runs(runs: list[StoredRun], workspace: str) -> dict[str, StoredRun]:
    """Build thread -> its latest run in ``workspace`` (a replay supersedes the run it
    replays); runs that name no build thread are left out."""
    out: dict[str, StoredRun] = {}
    for run in runs:
        if not run.build_thread or not same_path(run.workspace, workspace):
            continue
        seen = out.get(run.build_thread)
        if seen is None or run.started_at >= seen.started_at:
            out[run.build_thread] = run
    return out


def stop_reason(run: StoredRun, alive: bool | None) -> str | None:
    """Why ``run``'s build no longer runs, or None while it runs (or cannot be told) and
    after a clean exit. ``alive`` is the liveness of a ``running`` run's process."""
    if run.status == RUNNING:
        if alive is False:
            return (
                f"its process, PID {run.pid}, is gone, and the supervisor has not recorded "
                f"an end for run {run.run_id}"
            )
        return None
    if run.status == ORPHANED:
        return f"run {run.run_id} is orphaned: its process and its supervisor are gone"
    if run.status == KILLED_HANG:
        return f"the supervisor killed run {run.run_id} as hung (no progress)"
    if run.status == EXITED and run.exit_code not in (None, 0):
        return f"run {run.run_id} exited with code {run.exit_code}"
    return None


def read_incidents(
    conn: sqlite3.Connection, *, events: int, escalated_since: float
) -> tuple[Incidents, Incidents, int]:
    """The open incidents and the escalated ones updated at or after ``escalated_since``,
    each list newest update first, each incident with the build thread it interrupted and its
    last ``events`` timeline entries, oldest first; and how many incident rows did not parse.
    Closed incidents are never read. The thread is the ``runs`` row of the incident's first
    failed run (``run_ids[0]``), else of its earliest run; "" when neither names one."""
    rows = _rows(
        conn,
        "select data from incidents where state != ? and (state != ? or updated_at >= ?)"
        " order by updated_at desc",
        (CLOSED, ESCALATED, escalated_since),
    )
    open_: Incidents = []
    escalated: Incidents = []
    skipped = 0
    for (data,) in rows:
        incident = _parse(StoredIncident, data)
        if incident is None:
            skipped += 1
            continue
        # the JSON wins over the state and updated_at columns
        if incident.state == ESCALATED:
            if incident.updated_at < escalated_since:
                continue
            into = escalated
        elif incident.state in TERMINAL_STATES:
            continue
        else:
            into = open_
        tail = _rows(
            conn,
            "select at, text from events where incident_id = ? order by id desc limit ?",
            (incident.incident_id, events),
        )
        timeline = [(float(t), str(text)) for t, text in reversed(tail)]
        into.append((incident, _interrupted(conn, incident), timeline))
    return open_, escalated, skipped


def _interrupted(conn: sqlite3.Connection, incident: StoredIncident) -> str:
    """The build thread of the incident's first failed run, else of its earliest run."""
    first = incident.run_ids[0] if incident.run_ids else None
    rows = _rows(conn, "select data from runs where run_id = ?", (first,)) if first else []
    if not rows:
        rows = _rows(
            conn,
            "select data from runs where incident_id = ? order by started_at limit 1",
            (incident.incident_id,),
        )
    run = _parse(StoredRun, rows[0][0]) if rows else None
    return run.build_thread if run is not None else ""
