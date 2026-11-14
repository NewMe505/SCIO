"""The supervisor's durable store: records, dedupe lookups, the idempotent step log, leases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from recovery.config import recovery_home as home_dir
from recovery.model import (
    FailureClass,
    Incident,
    IncidentState,
    ProcessKey,
    RunRecord,
    RunStatus,
)
from recovery.store import SCHEMA_VERSION, Store


def _run(run_id: str = "run-1", **kw: object) -> RunRecord:
    base: dict[str, object] = {
        "run_id": run_id,
        "workspace": "W",
        "launcher": ["sh", "scio.sh"],
        "argv": ["build", "--product", "P"],
        "env": {"SCIO_PROVIDER_OVERRIDE": "claude_code"},
        "started_at": 100.0,
    }
    base.update(kw)
    return RunRecord(**base)  # type: ignore[arg-type]


def _incident(incident_id: str = "inc-1", **kw: object) -> Incident:
    base: dict[str, object] = {
        "incident_id": incident_id,
        "signature": "abc",
        "failure_class": FailureClass.HARNESS_SUSPECT,
        "summary": "TimeoutExpired in claude_code.run",
        "workspace": "W",
        "state": IncidentState.DETECTED,
        "created_at": 100.0,
        "updated_at": 100.0,
    }
    base.update(kw)
    return Incident(**base)  # type: ignore[arg-type]


def test_the_home_defaults_to_the_isolated_test_directory(recovery_home: Path) -> None:
    assert home_dir(None) == recovery_home


def test_runs_and_incidents_round_trip_and_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    store = Store(path)
    store.save_run(_run(status=RunStatus.EXITED, exit_code=1, incident_id="inc-1"))
    store.save_incident(_incident(run_ids=["run-1"], held_from=IncidentState.ISSUE_OPEN))
    store.close()
    again = Store(path)
    run = again.get_run("run-1")
    inc = again.get_incident("inc-1")
    assert run is not None and run.status is RunStatus.EXITED and run.exit_code == 1
    assert inc is not None and inc.state is IncidentState.DETECTED
    assert inc.held_from is IncidentState.ISSUE_OPEN and inc.run_ids == ["run-1"]
    assert [r.run_id for r in again.runs(status=RunStatus.EXITED)] == ["run-1"]
    again.close()


def test_the_open_incident_for_a_signature_is_found_and_a_closed_one_is_not() -> None:
    store = Store(":memory:")
    store.save_incident(_incident("inc-old", state=IncidentState.CLOSED))
    assert store.open_incident_for("abc", "W") is None
    store.save_incident(_incident("inc-new", state=IncidentState.REPAIRING, created_at=200.0))
    found = store.open_incident_for("abc", "W")
    assert found is not None and found.incident_id == "inc-new"
    assert store.open_incident_for("abc", "other-workspace") is None
    assert store.count_incidents_since("W", 150.0) == 1
    assert [i.incident_id for i in store.open_incidents()] == ["inc-new"]


def test_a_finished_step_never_runs_twice() -> None:
    store = Store(":memory:")
    calls: list[int] = []

    def apply() -> int:
        calls.append(1)
        return 42

    assert store.step("inc-1", "open-issue", apply) == 42
    assert store.step("inc-1", "open-issue", apply) == 42
    assert calls == [1]
    assert store.step_done("inc-1", "open-issue")


def test_a_step_interrupted_inside_apply_is_found_by_its_probe() -> None:
    store = Store(":memory:")

    def crash() -> int:
        raise KeyboardInterrupt  # the process died after the effect, before "done"

    with pytest.raises(KeyboardInterrupt):
        store.step("inc-1", "open-issue", crash)
    applied: list[int] = []

    def apply_again() -> int:
        applied.append(1)
        return 7

    result = store.step("inc-1", "open-issue", apply_again, probe=lambda: 99)
    assert result == 99 and applied == []
    assert store.step("inc-1", "open-issue", lambda: 0) == 99


def test_a_live_lease_refuses_a_second_holder_and_a_dead_one_is_taken_over() -> None:
    store = Store(":memory:")
    a, b = ProcessKey(10, 1.0), ProcessKey(20, 2.0)
    living = {a}
    assert store.acquire_lease("workspace:w", a, lambda k: k in living)
    assert store.acquire_lease("workspace:w", a, lambda k: k in living)  # re-entrant
    assert not store.acquire_lease("workspace:w", b, lambda k: k in living)
    store.release_lease("workspace:w", b)  # never releases another holder's lease
    assert store.lease_holder("workspace:w") == a
    living.clear()  # a died without releasing
    assert store.acquire_lease("workspace:w", b, lambda k: k in living)
    store.release_lease("workspace:w", b)
    assert store.lease_holder("workspace:w") is None


def test_a_reused_pid_does_not_count_as_the_same_holder() -> None:
    assert ProcessKey(10, 1.0).matches(ProcessKey(10, 1.4))
    assert not ProcessKey(10, 1.0).matches(ProcessKey(10, 5.0))


def test_events_keep_their_order() -> None:
    store = Store(":memory:", clock=iter([1.0, 2.0, 3.0]).__next__)
    store.add_event("inc-1", "detected")
    store.add_event("inc-1", "issue #9 opened")
    assert [t for _, t in store.events("inc-1")] == ["detected", "issue #9 opened"]


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    Store(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION + 1,))
    with pytest.raises(RuntimeError, match="newer"):
        Store(path)
