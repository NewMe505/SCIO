"""The supervisor's durable state: one SQLite file under :func:`config.recovery_home`.

The store holds runs, incidents, their event timelines, an idempotent step log (the
``run_effect`` pattern from ADR 0003, rebuilt with the standard library) and leases.
Every write is its own transaction, and the file is in WAL mode with a busy timeout, so a
``run`` process and a ``tick`` process can share it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from recovery.model import (
    TERMINAL,
    Incident,
    IncidentState,
    ProcessKey,
    RunRecord,
    RunStatus,
)

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS runs ("
    " run_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, status TEXT NOT NULL,"
    " incident_id TEXT, started_at REAL NOT NULL, data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS incidents ("
    " incident_id TEXT PRIMARY KEY, signature TEXT NOT NULL, workspace TEXT NOT NULL,"
    " state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,"
    " data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS events ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id TEXT NOT NULL,"
    " at REAL NOT NULL, text TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS steps ("
    " incident_id TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,"
    " result TEXT, started_at REAL NOT NULL, finished_at REAL,"
    " PRIMARY KEY (incident_id, step))",
    "CREATE TABLE IF NOT EXISTS leases ("
    " name TEXT PRIMARY KEY, pid INTEGER NOT NULL, created REAL NOT NULL,"
    " acquired_at REAL NOT NULL, note TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS incidents_by_signature ON incidents(signature, state)",
    "CREATE INDEX IF NOT EXISTS events_by_incident ON events(incident_id, id)",
)


class Store:
    """``Store(path)`` opens or creates the database. ``Store(":memory:")`` is for tests."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path) if str(path) != ":memory:" else None
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
        with self._lock:
            if self.path is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            for stmt in _SCHEMA:
                self._conn.execute(stmt)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{path} has schema {row[0]}, newer than this supervisor's {SCHEMA_VERSION}"
                )

    @classmethod
    def at_home(cls, home: Path) -> Store:
        return cls(home / "state.sqlite")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(args)).fetchall())

    # Runs

    def save_run(self, run: RunRecord) -> None:
        self._exec(
            "INSERT INTO runs(run_id, workspace, status, incident_id, started_at, data)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET"
            " status=excluded.status, incident_id=excluded.incident_id, data=excluded.data",
            (
                run.run_id,
                run.workspace,
                run.status.value,
                run.incident_id,
                run.started_at,
                json.dumps(run.to_dict(), sort_keys=True),
            ),
        )

    def get_run(self, run_id: str) -> RunRecord | None:
        rows = self._exec("SELECT data FROM runs WHERE run_id=?", (run_id,))
        return RunRecord.from_dict(json.loads(rows[0][0])) if rows else None

    def runs(
        self, *, status: RunStatus | None = None, workspace: str | None = None
    ) -> list[RunRecord]:
        sql, args = "SELECT data FROM runs WHERE 1=1", []
        if status is not None:
            sql += " AND status=?"
            args.append(status.value)
        if workspace is not None:
            sql += " AND workspace=?"
            args.append(workspace)
        rows = self._exec(sql + " ORDER BY started_at", args)
        return [RunRecord.from_dict(json.loads(r[0])) for r in rows]

    # Incidents

    def save_incident(self, incident: Incident) -> None:
        self._exec(
            "INSERT INTO incidents(incident_id, signature, workspace, state, created_at,"
            " updated_at, data) VALUES(?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET"
            " state=excluded.state, updated_at=excluded.updated_at, data=excluded.data",
            (
                incident.incident_id,
                incident.signature,
                incident.workspace,
                incident.state.value,
                incident.created_at,
                incident.updated_at,
                json.dumps(incident.to_dict(), sort_keys=True),
            ),
        )

    def get_incident(self, incident_id: str) -> Incident | None:
        rows = self._exec("SELECT data FROM incidents WHERE incident_id=?", (incident_id,))
        return Incident.from_dict(json.loads(rows[0][0])) if rows else None

    def incidents(
        self,
        *,
        states: Iterable[IncidentState] | None = None,
        workspace: str | None = None,
        signature: str | None = None,
    ) -> list[Incident]:
        sql, args = "SELECT data FROM incidents WHERE 1=1", []
        if states is not None:
            wanted = [s.value for s in states]
            sql += f" AND state IN ({','.join('?' for _ in wanted)})"
            args.extend(wanted)
        if workspace is not None:
            sql += " AND workspace=?"
            args.append(workspace)
        if signature is not None:
            sql += " AND signature=?"
            args.append(signature)
        rows = self._exec(sql + " ORDER BY created_at", args)
        return [Incident.from_dict(json.loads(r[0])) for r in rows]

    def open_incidents(self, *, workspace: str | None = None) -> list[Incident]:
        open_states = [s for s in IncidentState if s not in TERMINAL]
        return self.incidents(states=open_states, workspace=workspace)

    def open_incident_for(self, signature: str, workspace: str) -> Incident | None:
        """The open incident with this signature in this workspace, if any (dedupe)."""
        found = [i for i in self.incidents(signature=signature, workspace=workspace) if i.open]
        return found[-1] if found else None

    def count_incidents_since(self, workspace: str, since: float) -> int:
        rows = self._exec(
            "SELECT COUNT(*) FROM incidents WHERE workspace=? AND created_at>=?",
            (workspace, since),
        )
        return int(rows[0][0])

    # Events (an incident's timeline)

    def add_event(self, incident_id: str, text: str) -> None:
        self._exec(
            "INSERT INTO events(incident_id, at, text) VALUES(?,?,?)",
            (incident_id, self._clock(), text),
        )

    def events(self, incident_id: str) -> list[tuple[float, str]]:
        rows = self._exec(
            "SELECT at, text FROM events WHERE incident_id=? ORDER BY id", (incident_id,)
        )
        return [(float(r[0]), str(r[1])) for r in rows]

    # The step log: each externally visible step runs at most once per incident

    def step[T](
        self,
        incident_id: str,
        name: str,
        apply: Callable[[], T],
        probe: Callable[[], T | None] | None = None,
    ) -> T:
        """Run ``apply`` at most once for ``(incident_id, name)`` and return its result.

        - ``done``: the stored result is returned.
        - ``started`` but not ``done`` (the process died inside ``apply``): ``probe`` runs
          first, and a non-None answer is recorded as the result.
        - The result must be JSON-serializable.
        """
        rows = self._exec(
            "SELECT status, result FROM steps WHERE incident_id=? AND step=?", (incident_id, name)
        )
        if rows and rows[0][0] == "done":
            return json.loads(rows[0][1])  # type: ignore[no-any-return]
        if rows and probe is not None:
            found = probe()
            if found is not None:
                self._finish_step(incident_id, name, found)
                return found
        self._exec(
            "INSERT OR IGNORE INTO steps(incident_id, step, status, started_at)"
            " VALUES(?,?,'started',?)",
            (incident_id, name, self._clock()),
        )
        result = apply()
        self._finish_step(incident_id, name, result)
        return result

    def step_done(self, incident_id: str, name: str) -> bool:
        rows = self._exec(
            "SELECT status FROM steps WHERE incident_id=? AND step=?", (incident_id, name)
        )
        return bool(rows) and rows[0][0] == "done"

    def _finish_step(self, incident_id: str, name: str, result: Any) -> None:
        self._exec(
            "UPDATE steps SET status='done', result=?, finished_at=? WHERE incident_id=? AND"
            " step=?",
            (json.dumps(result, sort_keys=True), self._clock(), incident_id, name),
        )

    # Leases: one holder per name, identified by PID and creation time

    def acquire_lease(
        self,
        name: str,
        holder: ProcessKey,
        alive: Callable[[ProcessKey], bool],
        note: str = "",
    ) -> bool:
        """Take ``name`` for ``holder``. True when it was free, already ``holder``'s, or held
        by a process that is no longer alive. False when a live process holds it."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT pid, created FROM leases WHERE name=?", (name,)
                ).fetchone()
                if row is not None:
                    current = ProcessKey(int(row[0]), float(row[1]))
                    if not current.matches(holder) and alive(current):
                        self._conn.execute("ROLLBACK")
                        return False
                self._conn.execute(
                    "INSERT INTO leases(name, pid, created, acquired_at, note) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, created=excluded.created,"
                    " acquired_at=excluded.acquired_at, note=excluded.note",
                    (name, holder.pid, holder.created, self._clock(), note),
                )
                self._conn.execute("COMMIT")
                return True
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def release_lease(self, name: str, holder: ProcessKey) -> None:
        """Release ``name`` if ``holder`` holds it; never another process's lease."""
        row = self._exec("SELECT pid, created FROM leases WHERE name=?", (name,))
        if row and ProcessKey(int(row[0][0]), float(row[0][1])).matches(holder):
            self._exec("DELETE FROM leases WHERE name=?", (name,))

    def lease_holder(self, name: str) -> ProcessKey | None:
        row = self._exec("SELECT pid, created FROM leases WHERE name=?", (name,))
        return ProcessKey(int(row[0][0]), float(row[0][1])) if row else None
