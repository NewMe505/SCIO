"""Idempotent side effects keyed by ``hash(thread_id, node, intent)``.

A LangGraph checkpoint records state *between* nodes. If the process dies while a
node is running, the node re-runs from its start on resume, so any side effect inside
it (file write, git commit, PR creation) would happen twice. :func:`run_effect` makes
that safe with two layers:

1. A durable :class:`EffectLog` (SQLite) records ``started`` before and ``done`` (with
   the result) after each effect. A resumed node finds ``done`` and returns the stored
   result without re-applying.
2. For the window between applying the effect and recording ``done``, the caller
   supplies a ``probe`` that detects a prior application from the world itself (the
   commit with this key exists, the PR for this branch exists, the file already has the
   target hash). The probe runs whenever the log says ``started`` but not ``done``.

Inside a graph node, use :func:`effect` to derive the key from the running thread.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeVar

from langgraph.config import get_config

T = TypeVar("T")

Status = Literal["started", "done"]


def effect_key(thread_id: str, node: str, intent: str) -> str:
    """Deterministic key for one side effect in one run."""
    blob = f"{thread_id}\x1f{node}\x1f{intent}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class EffectRecord:
    key: str
    node: str
    intent: str
    status: Status
    result: Any
    started_at: str
    finished_at: str | None


class EffectLog:
    """SQLite-backed, process-safe log of side effects. One file per deployment."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS effects ("
                " key TEXT PRIMARY KEY, node TEXT NOT NULL, intent TEXT NOT NULL,"
                " status TEXT NOT NULL, result TEXT, started_at TEXT NOT NULL, finished_at TEXT)"
            )

    def get(self, key: str) -> EffectRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key,node,intent,status,result,started_at,finished_at"
                " FROM effects WHERE key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return EffectRecord(
            key=row[0],
            node=row[1],
            intent=row[2],
            status=row[3],
            result=json.loads(row[4]) if row[4] is not None else None,
            started_at=row[5],
            finished_at=row[6],
        )

    def start(self, key: str, node: str, intent: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO effects(key,node,intent,status,started_at)"
                " VALUES(?,?,?,?,?)",
                (key, node, intent, "started", now),
            )

    def finish(self, key: str, result: Any) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE effects SET status='done', result=?, finished_at=? WHERE key=?",
                (json.dumps(result, sort_keys=True, default=str), now, key),
            )

    def records(self) -> list[EffectRecord]:
        with self._lock:
            keys = [r[0] for r in self._conn.execute("SELECT key FROM effects ORDER BY started_at")]
        return [r for k in keys if (r := self.get(k)) is not None]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def run_effect[T](
    log: EffectLog,
    *,
    thread_id: str,
    node: str,
    intent: str,
    apply: Callable[[], T],
    probe: Callable[[], T | None] | None = None,
    after_apply: Callable[[], None] | None = None,
) -> T:
    """Apply a side effect at most once per ``(thread_id, node, intent)``.

    ``apply`` performs the effect and returns a JSON-serializable result.
    ``probe`` returns the result if the effect already happened in the world, else None.
    ``after_apply`` is a test hook invoked between ``apply`` and recording ``done``.
    """
    key = effect_key(thread_id, node, intent)
    rec = log.get(key)
    if rec is not None and rec.status == "done":
        return rec.result  # type: ignore[no-any-return]
    if rec is not None and probe is not None:
        found = probe()
        if found is not None:
            log.finish(key, found)
            return found
    log.start(key, node, intent)
    result = apply()
    if after_apply is not None:
        after_apply()
    log.finish(key, result)
    return result


def current_thread_id() -> str:
    """The ``thread_id`` of the graph run that is executing the current node."""
    cfg = get_config()
    thread_id = cfg.get("configurable", {}).get("thread_id")
    if not thread_id:
        raise RuntimeError("no thread_id in the running graph config; pass configurable.thread_id")
    return str(thread_id)


def effect[T](
    log: EffectLog,
    *,
    node: str,
    intent: str,
    apply: Callable[[], T],
    probe: Callable[[], T | None] | None = None,
) -> T:
    """:func:`run_effect` for use inside a graph node (thread id taken from the run)."""
    return run_effect(
        log, thread_id=current_thread_id(), node=node, intent=intent, apply=apply, probe=probe
    )
