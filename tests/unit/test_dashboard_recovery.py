"""The dashboard over the recovery supervisor's store (issue #102, ADR 0019): a supervised
thread whose process is gone reads ``stopped``, the open incidents link the build thread
they interrupted, a missing store is no error, an unreadable one hides nothing and is logged
once per streak, nothing is written, and the page's CSP allows the one pinned Mermaid file.

The stores are written with the supervisor's own ``Store``; the liveness of a supervised
process is a fake keyed by PID, except where the real check is the point."""

from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

pytest.importorskip("fastapi")

# ruff exempts pytest.importorskip from E402: the API tests below need the dashboard extra
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from recovery.model import FailureClass, Incident, IncidentState, RunRecord, RunStatus
from recovery.store import Store
from scio.dashboard import data, liveness, supervisor
from scio.dashboard.app import create_app
from scio.dashboard.contract import ChangeEvent, RecoverySource, RecoveryView
from scio.dashboard.data import CheckpointDataSource, workspace
from scio.graph.checkpointer import serializer
from scio.state.models import Budget, ProductBuildState
from tests.browser_support import requires_browser
from tests.dashboard_support import FixtureDataSource
from tests.unit.test_dashboard_data import _ask, _boom, _finish, _run_tiny

ROOT = "graphcalc-run"
BUILD = "build-demo-m1"
FEATURE = f"{BUILD}-p1-a1"
PID = 4242
CREATED = 1_000.0
REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "scio" / "dashboard" / "static"
BASE = "http://127.0.0.1"
WS_URL = "ws://127.0.0.1/ws"  # absolute: websocket_connect ignores base_url (guardrail 25)
POLL = 0.02
WAIT = 5.0


@pytest.fixture(autouse=True)
def _no_real_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A source that resolves the store from the environment never finds this machine's."""
    monkeypatch.setenv(supervisor.HOME_ENV, str(tmp_path_factory.mktemp("no-recovery-home")))


# ---------- a workspace with a build in progress, and the supervisor's store ----------


def _put(db: Path, thread_id: str, channels: dict[str, Any]) -> None:
    with closing(sqlite3.connect(db, check_same_thread=False)) as conn:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = channels
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        SqliteSaver(conn, serde=serializer()).put(config, checkpoint, {}, {})  # type: ignore[arg-type]
        conn.commit()


def _mid_node(db: Path, thread_id: str) -> None:
    """A task thread in progress: its checkpoint before ``implement``, no stop written yet."""
    _run_tiny(db, thread_id, _ask)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "delete from writes where thread_id = ? and channel = '__interrupt__'", (thread_id,)
        )
        conn.commit()


@dataclass
class World:
    """``root`` holds a build in progress (its phase ``feature``) and its feature attempt in
    progress; the supervisor's store is ``store_path``. ``answers`` is the liveness by PID: a
    PID it does not name is gone."""

    root: Path
    db: Path
    store_path: Path
    answers: dict[int, bool | None] = field(default_factory=dict)
    clock: list[float] = field(default_factory=lambda: [500.0])

    def alive(self, pid: int, created: float) -> bool | None:
        return self.answers.get(pid, False)

    def source(
        self,
        alive: Callable[[int, float], bool | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> CheckpointDataSource:
        return CheckpointDataSource(
            [workspace(self.root, name=ROOT)],
            recovery_store=self.store_path,
            alive=alive or self.alive,
            clock=clock,
        )

    def tick(self) -> float:
        self.clock[0] += 1.0
        return self.clock[0]

    def store(self) -> Store:
        return Store(self.store_path, clock=self.tick)

    def run(
        self,
        run_id: str,
        *,
        status: RunStatus = RunStatus.RUNNING,
        thread: str = BUILD,
        pid: int = PID,
        created: float = CREATED,
        started: float = 100.0,
        exit_code: int | None = None,
        ws: str | None = None,
        incident: str | None = None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=run_id,
            workspace=ws if ws is not None else str(self.root.resolve()),
            launcher=["sh", "scio.sh"],
            argv=["build", "--product", "P"],
            env={},
            started_at=started,
            status=status,
            build_thread=thread,
            pid=pid,
            pid_created=created,
            exit_code=exit_code,
            ended_at=None if status is RunStatus.RUNNING else started + 10,
            incident_id=incident,
        )
        with closing(self.store()) as store:
            store.save_run(record)
        return record


@pytest.fixture
def world(tmp_path: Path) -> World:
    root = tmp_path / ROOT
    db = root / ".scio" / "checkpoints.sqlite"
    db.parent.mkdir(parents=True)
    _mid_node(db, FEATURE)
    build = ProductBuildState(
        thread_id=BUILD,
        product_dir=str(tmp_path / "product"),
        phase="feature",
        budgets=Budget(tokens_max=1_000, usd_max=1.0, advisor_calls_max=0),
    )
    _put(db, BUILD, build.model_dump(mode="json"))
    return World(root=root, db=db, store_path=tmp_path / "home" / supervisor.STORE_FILE)


def _statuses(source: CheckpointDataSource) -> dict[str, str]:
    return {s.thread_id: s.status for s in source.list_threads(ROOT)}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- stopped ----------


def test_a_supervised_build_whose_process_is_gone_reads_stopped_and_its_attempt_too(
    world: World,
) -> None:
    world.run("run-1")
    world.answers[PID] = True
    source = world.source()
    assert _statuses(source) == {BUILD: "in_progress", FEATURE: "in_progress"}
    running = source.versions(ROOT)
    [run] = source.recovery().runs
    assert (run.root, run.thread_id, run.status, run.alive, run.stopped) == (
        ROOT,
        BUILD,
        "running",
        True,
        None,
    )

    world.answers[PID] = False  # killed: nothing is written anywhere
    assert _statuses(source) == {BUILD: "stopped", FEATURE: "stopped"}
    rows = {s.thread_id: s for s in source.list_threads(ROOT)}
    detail = source.thread(ROOT, BUILD)
    assert detail.summary == rows[BUILD]
    assert [(f.thread_id, f.status) for f in detail.feature_threads] == [(FEATURE, "stopped")]
    assert source.thread(ROOT, FEATURE).summary == rows[FEATURE]
    [run] = source.recovery().runs
    assert run.alive is False and run.stopped is not None and f"PID {PID}" in run.stopped
    # only the supervisor's view of the threads changed, and the poll sees it
    gone = source.versions(ROOT)
    for tid in (BUILD, FEATURE):
        assert gone[tid] != running[tid]
        assert gone[tid].checkpoint_id == running[tid].checkpoint_id
        assert gone[tid].writes == running[tid].writes


def test_a_reused_pid_reads_gone(world: World) -> None:
    """The real check, on this test's own process: the PID is alive, but a creation time 30 s
    off names another process."""
    me = os.getpid()
    created = liveness.process_created(me)
    assert created is not None
    world.run("run-1", pid=me, created=created)
    source = world.source(alive=liveness.alive)
    assert _statuses(source)[BUILD] == "in_progress"
    world.run("run-2", pid=me, created=created - 30, started=200.0)
    assert _statuses(source)[BUILD] == "stopped"


@pytest.mark.parametrize(
    ("status", "pid", "created", "exit_code", "expected"),
    [
        (RunStatus.ORPHANED, PID, CREATED, None, "stopped"),
        (RunStatus.KILLED_HANG, PID, CREATED, None, "stopped"),
        (RunStatus.EXITED, PID, CREATED, 1, "stopped"),
        (RunStatus.EXITED, PID, CREATED, 0, "in_progress"),  # done or paused: the checkpoint says
        (RunStatus.REFUSED, PID, CREATED, None, "in_progress"),  # never started
        (RunStatus.RUNNING, 0, CREATED, None, "in_progress"),  # saved before the child started
        (RunStatus.RUNNING, PID, 0.0, None, "in_progress"),  # its creation time was not read
        (RunStatus.RUNNING, PID, CREATED, None, "stopped"),  # gone, per the fake
    ],
)
def test_what_the_supervisor_recorded_feeds_the_same_view(
    world: World,
    status: RunStatus,
    pid: int,
    created: float,
    exit_code: int | None,
    expected: str,
) -> None:
    world.run("run-1", status=status, pid=pid, created=created, exit_code=exit_code)
    assert _statuses(world.source()) == {BUILD: expected, FEATURE: expected}


def test_the_latest_run_decides_and_a_refused_one_says_nothing(world: World) -> None:
    world.answers.update({PID: True, PID + 1: False})
    world.run("run-1", status=RunStatus.KILLED_HANG, pid=PID + 1, started=100.0)
    world.run("run-2", pid=PID, started=200.0)  # the replay, alive
    source = world.source()
    assert _statuses(source)[BUILD] == "in_progress"
    assert [r.run_id for r in source.recovery().runs] == ["run-2"]
    world.run("run-3", status=RunStatus.EXITED, exit_code=1, started=300.0)
    world.run("run-4", status=RunStatus.REFUSED, started=400.0)
    assert _statuses(source)[BUILD] == "stopped"
    [run] = source.recovery().runs
    assert run.run_id == "run-3" and run.exit_code == 1 and "code 1" in (run.stopped or "")


def test_stopped_replaces_only_in_progress(world: World) -> None:
    for tid, node in (("run-crash", _boom), ("run-wait", _ask), ("run-done", _finish)):
        _run_tiny(world.db, tid, node)
    _mid_node(world.db, "run-mid")
    for i, tid in enumerate(("run-crash", "run-wait", "run-done", "run-mid")):
        world.run(f"run-{i}", status=RunStatus.ORPHANED, thread=tid)
    statuses = _statuses(world.source())
    assert {t: statuses[t] for t in ("run-crash", "run-wait", "run-done", "run-mid")} == {
        "run-crash": "crashed",
        "run-wait": "waiting",
        "run-done": "done",
        "run-mid": "stopped",
    }


def test_only_runs_of_the_roots_workspace_and_thread_count(world: World) -> None:
    world.answers[PID] = False
    world.run("run-1", ws=str(world.root.parent / "elsewhere"))
    world.run("run-2", thread="build-other-m1")
    source = world.source()
    assert _statuses(source) == {BUILD: "in_progress", FEATURE: "in_progress"}
    # the same directory spelled another way still counts
    world.run("run-3", ws=os.path.join(str(world.root.resolve()), ".", ""), started=300.0)
    assert _statuses(source) == {BUILD: "stopped", FEATURE: "stopped"}


# ---------- the store: missing, unreadable, newer, partly bad, never written ----------


def test_a_missing_store_is_no_error_and_creates_nothing(world: World) -> None:
    source = world.source()
    view = source.recovery()
    assert view == RecoveryView(store=str(world.store_path), present=False)
    assert source.recovery_version() == ""
    assert _statuses(source) == {BUILD: "in_progress", FEATURE: "in_progress"}
    assert not world.store_path.parent.exists()


def test_the_default_store_is_where_the_supervisor_keeps_it_read_on_each_call(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = CheckpointDataSource.from_roots([world.root])
    for home in (tmp_path / "a", tmp_path / "b"):
        monkeypatch.setenv(supervisor.HOME_ENV, str(home))
        assert source.recovery().store == str(home / "state.sqlite")


def test_an_unreadable_store_hides_nothing_and_is_logged_once_per_streak(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="scio.dashboard.data")
    world.store_path.parent.mkdir(parents=True)
    world.store_path.write_bytes(b"this is not a database " * 64)
    source = world.source()
    for _ in range(3):
        assert _statuses(source) == {BUILD: "in_progress", FEATURE: "in_progress"}
        assert set(source.versions(ROOT)) == {BUILD, FEATURE}
        view = source.recovery()
        assert view.present and view.error and "not a database" in view.error
        assert view.incidents == [] and view.runs == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "recovery supervisor's store" in warnings[0].getMessage()
    world.store_path.unlink()
    world.answers[PID] = False
    world.run("run-1")
    assert _statuses(source)[BUILD] == "stopped"
    assert any("reads again" in r.getMessage() for r in caplog.records)


def test_a_newer_store_schema_is_refused_with_the_reason(world: World) -> None:
    world.answers[PID] = False
    world.run("run-1")
    with closing(sqlite3.connect(world.store_path)) as conn:
        conn.execute("update meta set value = '2' where key = 'schema_version'")
        conn.commit()
    source = world.source()
    view = source.recovery()
    assert view.error is not None and "schema 2" in view.error and view.runs == []
    assert _statuses(source)[BUILD] == "in_progress"  # not read, so not trusted


def test_a_row_that_does_not_parse_is_skipped_not_the_store(world: World) -> None:
    world.answers[PID] = False
    world.run("run-1")
    with closing(world.store()) as store:
        store.save_incident(_incident("inc-good", world, run_ids=["run-1"]))
    with closing(sqlite3.connect(world.store_path)) as conn:
        conn.execute(
            "insert into incidents values ('inc-bad', 's', ?, 'detected', 1, 2, '{not json')",
            (str(world.root),),
        )
        conn.execute("insert into runs values ('run-bad', ?, 'running', null, 999, '[]')", ("w",))
        conn.commit()
    source = world.source()
    view = source.recovery()
    assert [i.incident_id for i in view.incidents] == ["inc-good"]
    assert view.error == "1 incident row(s) could not be read"
    assert [r.run_id for r in view.runs] == ["run-1"] and _statuses(source)[BUILD] == "stopped"


def test_the_store_is_never_written(world: World) -> None:
    world.run("run-1")
    with closing(world.store()) as store:
        store.save_incident(_incident("inc-1", world, run_ids=["run-1"]))
        store.add_event("inc-1", "detected")
    before = _sha(world.store_path)
    source = world.source()
    source.list_threads(ROOT)
    source.thread(ROOT, BUILD)
    source.versions(ROOT)
    source.recovery()
    source.recovery_version()
    assert _sha(world.store_path) == before
    with (
        closing(data._connect(world.store_path)) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute("delete from runs")
    assert _sha(world.store_path) == before


def test_the_store_version_changes_with_every_supervisor_write(world: World) -> None:
    source = world.source()
    assert source.recovery_version() == ""
    store = world.store()
    try:
        store.save_incident(_incident("inc-1", world))
        first = source.recovery_version()
        assert first
        store.add_event("inc-1", "snapshotted")
        second = source.recovery_version()
        assert second != first
        assert source.recovery_version() == second  # a read changes nothing
    finally:
        store.close()


# ---------- the incidents ----------


def _incident(incident_id: str, world: World, **fields: Any) -> Incident:
    values: dict[str, Any] = {
        "incident_id": incident_id,
        "signature": "sig-" + incident_id,
        "failure_class": FailureClass.HARNESS_SUSPECT,
        "summary": f"{incident_id}: a traceback in scio",
        "workspace": str(world.root.resolve()),
        "state": IncidentState.ISSUE_OPEN,
        "created_at": 150.0,
        "updated_at": 200.0,
    }
    values.update(fields)
    return Incident(**values)


def test_open_incidents_link_the_build_thread_they_interrupted(world: World) -> None:
    world.answers[PID] = True
    world.run("run-1", incident="inc-live")
    world.run("run-2", thread="build-gone-m1", started=110.0)
    world.run("run-3", ws=str(world.root.parent / "other"), started=120.0)
    with closing(world.store()) as store:
        for inc in (
            _incident(
                "inc-live",
                world,
                run_ids=["run-1"],
                state=IncidentState.REPAIRING,
                failure_class=FailureClass.HANG,
                innermost_scio_frame="scio/product/build.py:run_feature",
                issue_number=71,
                pr_number=72,
                attempts=2,
                replays=1,
                escalation="the circuit breaker opened",
                updated_at=400.0,
            ),
            _incident("inc-untracked", world, run_ids=["run-2"], updated_at=300.0),
            _incident(
                "inc-elsewhere",
                world,
                run_ids=["run-3"],
                workspace=str(world.root.parent / "other"),
            ),
            _incident("inc-norun", world, updated_at=100.0),
            _incident("inc-closed", world, run_ids=["run-1"], state=IncidentState.CLOSED),
            _incident("inc-escalated", world, run_ids=["run-1"], state=IncidentState.ESCALATED),
        ):
            store.save_incident(inc)
        for n in range(7):
            store.add_event("inc-live", f"event {n}")
    view = world.source().recovery()
    assert view.present and view.error is None
    by_id = {i.incident_id: i for i in view.incidents}
    assert [i.incident_id for i in view.incidents] == [
        "inc-live",
        "inc-untracked",
        "inc-elsewhere",
        "inc-norun",
    ]  # open ones only, newest update first
    live = by_id["inc-live"]
    assert (live.root, live.build_thread, live.present) == (ROOT, BUILD, True)
    assert (live.state, live.failure_class, live.frame) == (
        "repairing",
        "hang",
        "scio/product/build.py:run_feature",
    )
    assert (live.issue_number, live.pr_number, live.attempts, live.replays) == (71, 72, 2, 1)
    assert live.escalation == "the circuit breaker opened"
    assert [e.text for e in live.events] == [f"event {n}" for n in range(2, 7)]
    assert live.events == sorted(live.events, key=lambda e: e.at)
    assert live.updated_at.timestamp() == 400.0 and live.created_at.timestamp() == 150.0
    untracked = by_id["inc-untracked"]
    assert (untracked.root, untracked.build_thread, untracked.present) == (
        ROOT,
        "build-gone-m1",
        False,
    )
    elsewhere = by_id["inc-elsewhere"]
    assert (elsewhere.root, elsewhere.present) == (None, False)
    assert by_id["inc-norun"].build_thread == "" and not by_id["inc-norun"].present
    assert view.escalated == []  # inc-escalated was last updated in 1970: out of the window


def test_recently_escalated_incidents_are_listed_as_needs_you_and_closed_ones_never(
    world: World,
) -> None:
    """An escalated incident is the one that most needs the owner: listed apart, with its
    reason, issue, PR and thread link, while its last update is within the window."""
    now = 1_800_000_000.0
    window = supervisor.ESCALATED_WINDOW_S
    world.run("run-1", incident="inc-needs-you")
    with closing(world.store()) as store:
        for inc in (
            _incident(
                "inc-needs-you",
                world,
                run_ids=["run-1"],
                state=IncidentState.ESCALATED,
                escalation="the replays ran out: 3 of 3",
                issue_number=91,
                pr_number=92,
                updated_at=now - 3600,
            ),
            _incident(
                "inc-edge",
                world,
                state=IncidentState.ESCALATED,
                escalation="repair failed twice",
                updated_at=now - window,  # the window's first second still counts
            ),
            _incident(
                "inc-too-old",
                world,
                state=IncidentState.ESCALATED,
                escalation="long ago",
                updated_at=now - window - 1,
            ),
            _incident("inc-closed", world, state=IncidentState.CLOSED, updated_at=now - 60),
            _incident("inc-open", world, state=IncidentState.HELD, updated_at=now - 30),
        ):
            store.save_incident(inc)
        store.add_event("inc-needs-you", "escalated: needs-human label added")
    view = world.source(clock=lambda: now).recovery()
    assert [i.incident_id for i in view.escalated] == ["inc-needs-you", "inc-edge"]
    assert [i.incident_id for i in view.incidents] == ["inc-open"]  # never the closed one
    needs_you = view.escalated[0]
    assert (needs_you.state, needs_you.escalation) == ("escalated", "the replays ran out: 3 of 3")
    assert (needs_you.issue_number, needs_you.pr_number) == (91, 92)
    assert (needs_you.root, needs_you.build_thread, needs_you.present) == (ROOT, BUILD, True)
    assert [e.text for e in needs_you.events] == ["escalated: needs-human label added"]
    # a day later the edge one has aged out
    later = world.source(clock=lambda: now + 86_400).recovery()
    assert [i.incident_id for i in later.escalated] == ["inc-needs-you"]


def test_an_incident_without_its_first_run_takes_its_earliest_run(world: World) -> None:
    world.run("run-1", incident="inc-1", started=100.0)
    world.run("run-2", incident="inc-1", thread="build-later-m1", started=200.0)
    with closing(world.store()) as store:
        store.save_incident(_incident("inc-1", world, run_ids=["run-missing"]))
    [incident] = world.source().recovery().incidents
    assert incident.build_thread == BUILD


# ---------- liveness ----------


def test_liveness_reads_this_process_and_a_process_that_ended() -> None:
    me = os.getpid()
    created = liveness.process_created(me)
    assert created is not None and abs(created - time.time()) < 7 * 24 * 3600
    assert liveness.alive(me, created) is True
    assert liveness.alive(me, created - 30) is False  # a reused PID
    assert liveness.alive(0, created) is None and liveness.alive(me, 0.0) is None
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        born = liveness.process_created(child.pid)
        assert born is not None and liveness.alive(child.pid, born) is True
    finally:
        child.kill()
        child.wait()
    assert liveness.alive(child.pid, born) is False  # the Popen still holds its handle


# ---------- the API ----------


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    executor = ThreadPoolExecutor(max_workers=1)
    yield executor
    executor.shutdown(wait=False, cancel_futures=True)


def receive(ws: WebSocketTestSession, pool: ThreadPoolExecutor) -> ChangeEvent:
    payload: Any = pool.submit(ws.receive_json).result(timeout=WAIT)
    return ChangeEvent.model_validate(payload)


def test_the_api_serves_the_recovery_view(world: World) -> None:
    world.answers[PID] = False
    world.run("run-1", incident="inc-1")
    with closing(world.store()) as store:
        store.save_incident(_incident("inc-1", world, run_ids=["run-1"]))
    source = world.source()
    assert isinstance(source, RecoverySource)
    with TestClient(create_app(source), base_url=BASE) as c:
        resp = c.get("/api/recovery")
    assert resp.status_code == 200
    assert resp.json() == source.recovery().model_dump(mode="json")
    assert resp.json()["incidents"][0]["build_thread"] == BUILD


def test_a_source_that_reads_no_store_serves_an_empty_view_and_says_so_in_the_hello(
    pool: ThreadPoolExecutor,
) -> None:
    fixture = FixtureDataSource()
    assert not isinstance(fixture, RecoverySource)
    with TestClient(create_app(fixture, poll_interval_s=POLL), base_url=BASE) as c:
        assert c.get("/api/recovery").json() == RecoveryView().model_dump(mode="json")
        with c.websocket_connect(WS_URL) as ws:
            assert receive(ws, pool).recovery is False


def test_a_failing_recovery_view_answers_503(world: World) -> None:
    source = world.source()

    def broken() -> RecoveryView:
        raise RuntimeError("a bug")

    source.recovery = broken  # type: ignore[method-assign]
    with TestClient(create_app(source), base_url=BASE) as c:
        resp = c.get("/api/recovery")
    assert resp.status_code == 503 and "a bug" in resp.json()["detail"]


def test_the_hello_and_a_store_write_tell_the_page_to_read_the_view(
    world: World, pool: ThreadPoolExecutor
) -> None:
    source = world.source()
    with (
        TestClient(create_app(source, poll_interval_s=POLL), base_url=BASE) as c,
        c.websocket_connect(WS_URL) as ws,
    ):
        hello = receive(ws, pool)
        assert hello.recovery is True and hello.changed == []
        time.sleep(POLL * 8)  # quiet polls push nothing
        with closing(world.store()) as store:
            store.save_incident(_incident("inc-1", world))
        event = receive(ws, pool)
    assert event.recovery is True and event.changed == [] and event.removed == []


def test_a_supervised_process_dying_pushes_its_threads(
    world: World, pool: ThreadPoolExecutor
) -> None:
    world.answers[PID] = True
    world.run("run-1")
    source = world.source()
    with (
        TestClient(create_app(source, poll_interval_s=POLL), base_url=BASE) as c,
        c.websocket_connect(WS_URL) as ws,
    ):
        receive(ws, pool)
        time.sleep(POLL * 8)
        world.answers[PID] = False
        event = receive(ws, pool)
    assert {(r.root, r.thread_id) for r in event.changed} == {(ROOT, BUILD), (ROOT, FEATURE)}
    assert event.recovery is False  # the store did not change; the process did


def test_a_failing_store_version_never_pushes_and_is_logged_once(
    world: World, pool: ThreadPoolExecutor, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="scio.dashboard.app")
    source = world.source()

    def broken() -> str:
        raise OSError("the disk is gone")

    source.recovery_version = broken  # type: ignore[method-assign]
    with (
        TestClient(create_app(source, poll_interval_s=POLL), base_url=BASE) as c,
        c.websocket_connect(WS_URL) as ws,
    ):
        receive(ws, pool)
        time.sleep(POLL * 10)
        _mid_node(world.db, "run-new")  # a real change still pushes, without recovery
        event = receive(ws, pool)
    assert [r.thread_id for r in event.changed] == ["run-new"] and event.recovery is False
    logged = [r for r in caplog.records if "the disk is gone" in r.getMessage()]
    assert len(logged) == 1, [r.getMessage() for r in logged]


# ---------- the page and the boundary ----------


def test_the_csp_allows_scripts_only_from_the_page_and_the_pinned_mermaid_file() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    [policy] = re.findall(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    directives = {d.split()[0]: d.split()[1:] for d in (p.strip() for p in policy.split(";")) if d}
    [mermaid] = re.findall(r'<script id="mermaid-js" src="([^"]+)"', html)
    assert mermaid.startswith("https://cdnjs.cloudflare.com/ajax/libs/mermaid/")
    assert set(directives["script-src"]) == {"'self'", mermaid}
    assert directives["default-src"] == ["'none'"]


def imports_of(source: str, package: str) -> list[str]:
    """The imports of ``package`` (or a module under it) in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        found += [n for n in names if n == package or n.startswith(package + ".")]
    return found


def test_scio_never_imports_the_supervisor() -> None:
    """The supervisor is deployed apart from SCIO and is not in its wheel: SCIO reads its
    store's shape, never its code."""
    assert imports_of(
        "import recovery\nfrom recovery.store import Store\nimport recoveryx\n", "recovery"
    ) == [
        "recovery",
        "recovery.store",
    ]
    found = {
        str(p.relative_to(REPO)): bad
        for p in sorted((REPO / "scio").rglob("*.py"))
        if (bad := imports_of(p.read_text(encoding="utf-8"), "recovery"))
    }
    assert found == {}


# ---------- the page in a real browser ----------


class _Server:
    """The real app under uvicorn in a thread, with a bounded shutdown (guardrail 28)."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = int(s.getsockname()[1])
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", timeout_graceful_shutdown=3
        )
        self.server = uvicorn.Server(config)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:
            self.error = exc

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), self.error
            time.sleep(0.02)
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(15)
        assert not self.thread.is_alive(), "uvicorn did not stop"
        assert self.error is None, self.error


@requires_browser
def test_the_page_shows_the_incidents_and_a_thread_that_stopped(world: World) -> None:
    from playwright.sync_api import expect, sync_playwright

    world.answers[PID] = True
    world.run("run-1", incident="inc-1")
    with closing(world.store()) as store:
        store.save_incident(
            _incident(
                "inc-1",
                world,
                run_ids=["run-1"],
                issue_number=81,
                pr_number=82,
                escalation="needs the owner",
            )
        )
        store.add_event("inc-1", "issue opened")
        store.save_incident(
            _incident(
                "inc-esc",
                world,
                run_ids=["run-1"],
                state=IncidentState.ESCALATED,
                escalation="repair failed twice",
                issue_number=91,
                pr_number=92,
                updated_at=time.time() - 60,
            )
        )
    source = world.source()
    console: list[str] = []
    errors: list[str] = []
    with _Server(create_app(source, poll_interval_s=0.05)) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": 390, "height": 844})
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
            page.goto(base + "/", wait_until="domcontentloaded")
            incident = page.locator('tr.incident[data-incident="inc-1"]')
            expect(incident).to_contain_text("issue_open")
            expect(incident).to_contain_text("issue #81 · PR #82")
            link = incident.locator("a.incident-thread")
            expect(link).to_have_attribute("href", f"#/thread/{ROOT}/{BUILD}")
            expect(page.locator('tr[data-sub-for-incident="inc-1"] .escalation')).to_contain_text(
                "needs the owner"
            )
            # the escalated incident comes first, as "needs you", with its reason and link
            groups = page.locator(".incidents tbody[data-group]")
            assert groups.evaluate_all("gs => gs.map(g => g.dataset.group)") == [
                "needs-you",
                "open",
            ]
            needs_you = groups.first
            expect(needs_you.locator(".group-label")).to_contain_text("needs you")
            escalated = needs_you.locator('tr.incident[data-incident="inc-esc"]')
            expect(escalated).to_contain_text("issue #91 · PR #92")
            expect(escalated.locator(".badge.i-escalating")).to_have_text("escalated")
            expect(escalated.locator("a.incident-thread")).to_have_attribute(
                "href", f"#/thread/{ROOT}/{BUILD}"
            )
            expect(needs_you.locator(".escalation")).to_contain_text("repair failed twice")
            build_row = page.locator(f'tr.row[data-thread="{ROOT}/{BUILD}"]')
            expect(build_row.locator(".badge")).to_have_text("in progress")
            page.evaluate("window.__kept = true")

            world.answers[PID] = False  # the build is killed: the page follows, no reload
            expect(build_row.locator(".badge.s-stopped")).to_have_text("stopped")
            expect(build_row.locator(".badge")).to_have_attribute(
                "title", re.compile(f"PID {PID}, is gone")
            )
            sub = page.locator(f'tr[data-sub-for="{ROOT}/{FEATURE}"] .stopped-box')
            expect(sub).to_contain_text(f"PID {PID}, is gone")
            assert page.evaluate("window.__kept === true")
            width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
            assert width[0] <= width[1], f"the list scrolls sideways: {width}"

            link.click()
            head = page.locator(f'.head[data-thread="{ROOT}/{BUILD}"]')
            expect(head.locator(".stopped-box")).to_contain_text("supervised run run-1")
            expect(head.locator('.incident-box[data-incident="inc-1"]')).to_contain_text(
                "issue #81"
            )
            # the escalated one first on the thread too
            expect(head.locator(".incident-box").first).to_have_attribute(
                "data-incident", "inc-esc"
            )
            expect(head.locator(".incident-box").first).to_contain_text("repair failed twice")
            assert page.locator("button, form, input, select, textarea").count() == 0
        finally:
            browser.close()
    assert errors == []
    assert all("Failed to load resource" in c for c in console), console  # the aborted CDN


@requires_browser
def test_the_page_says_when_there_is_no_supervisor_store(world: World) -> None:
    from playwright.sync_api import expect, sync_playwright

    with _Server(create_app(world.source(), poll_interval_s=0.05)) as base, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.goto(base + "/", wait_until="domcontentloaded")
            section = page.locator('.recovery-section[data-recovery="absent"]')
            expect(section).to_contain_text("No recovery supervisor store.")
            expect(section).to_contain_text(str(world.store_path))
        finally:
            browser.close()
