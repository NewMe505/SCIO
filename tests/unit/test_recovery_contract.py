"""The supervisor (recovery/) and SCIO share no code, only command lines and a file shape.
These tests import both sides and check that what the supervisor sends, SCIO's own
parser accepts (I-184: the health check first called a `scio --version` SCIO never had),
that what the supervisor writes to its store, the dashboard reads back (issue #102), and
that what SCIO reports, the supervisor's classifier reads as meant (I-182)."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from recovery import procs
from recovery.classify import classify
from recovery.commands import CommandResult
from recovery.config import HOME_ENV, WorkspaceConfig, recovery_home
from recovery.model import (
    TERMINAL,
    Evidence,
    FailureClass,
    Incident,
    IncidentState,
    ProcessKey,
    RunRecord,
    RunStatus,
)
from recovery.runner import OUTCOME_SUBCOMMANDS
from recovery.runtime import SMOKE_ARGS, health_check
from recovery.store import SCHEMA_VERSION, Store
from scio.cli import HARNESS_HALT, _parser, harness_stop_line
from scio.dashboard import liveness, supervisor
from scio.dashboard.data import INCIDENT_EVENTS, CheckpointDataSource
from scio.graph.checkpointer import serializer
from scio.harness.failures import (
    BudgetExceeded,
    FatalError,
    HarnessError,
    RetryableError,
    StructuredOutputError,
)
from scio.harness.providers.claude_code import claude_runner
from scio.state.models import Budget, ProductBuildState

LAUNCHER = ("sh", "scio.sh")


def scio_parses(args: Sequence[str]) -> int:
    """The exit code SCIO's argument parser gives ``args``: 0 when it accepts them
    (``--help`` exits 0), 2 when it refuses them."""
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            _parser().parse_args(list(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


class ParsingLauncher:
    """A runner that, instead of running the launcher, hands its SCIO arguments to SCIO's
    real parser."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        args = list(argv[len(LAUNCHER) :])
        self.seen.append(args)
        code = scio_parses(args)
        return CommandResult(code, "", "" if code == 0 else f"scio refused {args}")


def test_the_parser_check_refuses_a_flag_scio_lacks() -> None:
    assert scio_parses(["--version"]) == 2
    assert scio_parses(list(SMOKE_ARGS)) == 0


@pytest.mark.parametrize(
    "args",
    [
        ["build", "--product", "P", "--gh", "o/p"],
        ["new", "--prompt", "an idea", "--dir", "D"],
        ["run", "--spec", "S.md", "--repo", "R"],
        ["resume", "--thread", "T"],
    ],
)
def test_the_outcome_flag_the_supervisor_appends_is_accepted(args: list[str]) -> None:
    assert args[0] in OUTCOME_SUBCOMMANDS
    assert scio_parses([*args, "--outcome-file", "outcome.json"]) == 0


def test_the_outcome_flag_goes_only_to_subcommands_that_take_it() -> None:
    assert set(OUTCOME_SUBCOMMANDS) == {"build", "new", "run", "resume"}
    assert scio_parses(["status", "--product", "P", "--outcome-file", "o.json"]) == 2


def test_every_health_check_command_is_one_scios_cli_accepts(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".scio").mkdir(parents=True)
    # A checkpoint database so step 3 (report on a copy) runs instead of being skipped.
    with sqlite3.connect(workspace / ".scio" / "checkpoints.sqlite") as conn:
        conn.execute("CREATE TABLE t (x)")
    cfg = WorkspaceConfig(
        workspace=workspace,
        launcher=LAUNCHER,
        scio_repo=tmp_path / "SCIO",
        scio_gh_repo="o/SCIO",
        live_wt=tmp_path / "SCIO" / ".scio" / "live-wt",
    )
    run = RunRecord(
        run_id="run-1",
        workspace=str(workspace),
        launcher=list(LAUNCHER),
        argv=["build", "--product", str(tmp_path / "product"), "--gh", "o/p"],
        env={},
        started_at=0.0,
        product_dir=str(tmp_path / "product"),
        build_thread="build-p-m1",
    )
    launcher = ParsingLauncher()
    problems = health_check(cfg, run, tmp_path / "health", runner=launcher)
    assert problems == []
    assert [args[0] for args in launcher.seen] == ["--help", "status", "report"]


# ---------- the supervisor's store, read by the dashboard (guardrail 70, issue #102) ----------

BUILD = "build-demo-m1"


def _build_checkpoint(db: Path, thread_id: str) -> None:
    """A build thread in progress, so the incident's thread is in view."""
    state = ProductBuildState(
        thread_id=thread_id,
        product_dir="product",
        phase="feature",
        budgets=Budget(tokens_max=1_000, usd_max=1.0, advisor_calls_max=0),
    )
    db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db, check_same_thread=False)) as conn:
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = state.model_dump(mode="json")
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        SqliteSaver(conn, serde=serializer()).put(config, checkpoint, {}, {})  # type: ignore[arg-type]
        conn.commit()


def test_the_dashboard_reads_back_what_the_supervisor_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor writes a run and an incident with its own ``Store`` where it keeps its
    state; the dashboard, which never imports it, finds the store the same way and reads every
    field it shows back unchanged. A change to the store's tables or JSON on either side
    fails here."""
    home = tmp_path / "home"
    monkeypatch.setenv(HOME_ENV, str(home))
    root = tmp_path / "graphcalc-run"
    _build_checkpoint(root / ".scio" / "checkpoints.sqlite", BUILD)
    me = procs.self_key()
    common = {
        "workspace": str(root.resolve()),
        "launcher": ["sh", "scio.sh"],
        "argv": ["build", "--product", "P", "--gh", "o/p"],
        "env": {"PATH": "/bin"},
        "scio_sha": "abc123",
        "product_dir": str(tmp_path / "product"),
        "supervisor_pid": me.pid,
        "supervisor_created": me.created,
        "incident_id": "inc-1",
    }
    live = RunRecord(
        run_id="run-live",
        started_at=1_700_000_000.5,
        build_thread=BUILD,
        pid=me.pid,  # this test's own process: alive
        pid_created=me.created,
        **common,  # type: ignore[arg-type]
    )
    failed = RunRecord(
        run_id="run-failed",
        started_at=1_700_000_010.25,
        status=RunStatus.EXITED,
        build_thread="build-other-m1",
        pid=me.pid,
        pid_created=me.created,
        ended_at=1_700_000_020.75,
        exit_code=3,
        **common,  # type: ignore[arg-type]
    )
    incident = Incident(
        incident_id="inc-1",
        signature="0123456789abcdef",
        failure_class=FailureClass.HANG,
        summary="no progress for 2700 s in scio/product/build.py",
        workspace=str(root.resolve()),
        state=IncidentState.AWAITING_MERGE,
        created_at=1_700_000_100.25,
        updated_at=1_700_000_200.75,
        innermost_scio_frame="scio/product/build.py:_feature",
        run_ids=["run-live", "run-failed"],
        occurrences=2,
        attempts=2,
        replays=1,
        issue_number=101,
        pr_number=102,
        escalation="the circuit breaker opened",
    )
    store = Store.at_home(recovery_home())
    try:
        store.save_run(live)
        store.save_run(failed)
        store.save_incident(incident)
        for n in range(INCIDENT_EVENTS + 2):
            store.add_event("inc-1", f"step {n}")
        events = store.events("inc-1")
        # escalated a minute ago: listed apart, as "needs you"; closed: never listed
        escalated = dataclasses.replace(
            incident,
            incident_id="inc-escalated",
            state=IncidentState.ESCALATED,
            escalation="the repair failed twice: needs-human",
            updated_at=time.time() - 60,
        )
        store.save_incident(escalated)
        store.save_incident(
            dataclasses.replace(incident, incident_id="inc-closed", state=IncidentState.CLOSED)
        )
    finally:
        store.close()

    source = CheckpointDataSource.from_roots([root])  # the store found as `scio dashboard` finds it
    view = source.recovery()
    assert (view.store, view.present, view.error) == (str(home / "state.sqlite"), True, None)
    [shown] = view.incidents
    [needs_you] = view.escalated
    assert (needs_you.incident_id, needs_you.state, needs_you.escalation) == (
        "inc-escalated",
        "escalated",
        escalated.escalation,
    )
    assert (needs_you.issue_number, needs_you.pr_number, needs_you.build_thread) == (
        101,
        102,
        BUILD,
    )
    assert needs_you.present and needs_you.updated_at.timestamp() == pytest.approx(
        escalated.updated_at, abs=1e-5
    )
    assert shown.model_dump(exclude={"created_at", "updated_at", "events"}) == {
        "incident_id": "inc-1",
        "state": "awaiting_merge",
        "failure_class": "hang",
        "summary": incident.summary,
        "frame": "scio/product/build.py:_feature",
        "issue_number": 101,
        "pr_number": 102,
        "attempts": 2,
        "replays": 1,
        "escalation": "the circuit breaker opened",
        "workspace": str(root.resolve()),
        "build_thread": BUILD,  # the first failed run's thread
        "root": "graphcalc-run",
        "present": True,
    }
    assert shown.created_at.timestamp() == incident.created_at
    assert shown.updated_at.timestamp() == incident.updated_at
    assert [e.text for e in shown.events] == [text for _at, text in events[-INCIDENT_EVENTS:]]
    assert [e.at.timestamp() for e in shown.events] == pytest.approx(
        [at for at, _text in events[-INCIDENT_EVENTS:]], abs=1e-5
    )

    runs = {r.run_id: r for r in view.runs}
    assert set(runs) == {"run-live", "run-failed"}
    for record, alive, stopped in ((live, True, None), (failed, None, "exited with code 3")):
        run = runs[record.run_id]
        assert (run.root, run.thread_id, run.status) == (
            "graphcalc-run",
            record.build_thread,
            record.status.value,
        )
        assert (run.pid, run.exit_code, run.incident_id, run.alive) == (
            record.pid,
            record.exit_code,
            "inc-1",
            alive,
        )
        assert run.started_at is not None and run.started_at.timestamp() == record.started_at
        ended = run.ended_at.timestamp() if run.ended_at else None
        assert ended == record.ended_at
        assert (run.stopped is None) if stopped is None else (stopped in (run.stopped or ""))

    # the process key round-trips: alive reads in progress, a reused PID reads stopped
    assert {s.thread_id: s.status for s in source.list_threads("graphcalc-run")} == {
        BUILD: "in_progress"
    }
    store = Store.at_home(recovery_home())
    try:
        reused = dataclasses.replace(
            live, run_id="run-reused", started_at=1_700_000_300.0, pid_created=me.created - 30
        )
        store.save_run(reused)
    finally:
        store.close()
    assert {s.thread_id: s.status for s in source.list_threads("graphcalc-run")} == {
        BUILD: "stopped"
    }


@pytest.mark.parametrize(
    "env",
    [
        {HOME_ENV: "/srv/recovery-home"},
        {HOME_ENV: "", "LOCALAPPDATA": "/appdata", "XDG_STATE_HOME": "/xdg"},
        {"LOCALAPPDATA": "/appdata"},
        {"XDG_STATE_HOME": "/xdg"},
        {},
    ],
)
def test_the_dashboard_looks_for_the_store_where_the_supervisor_keeps_it(
    env: dict[str, str],
) -> None:
    assert supervisor.store_path(env) == recovery_home(env) / "state.sqlite"


def test_the_two_sides_agree_on_the_schema_the_states_and_the_process_key() -> None:
    assert supervisor.SCHEMA_VERSION == SCHEMA_VERSION
    assert {s.value for s in TERMINAL} == supervisor.TERMINAL_STATES
    assert (IncidentState.CLOSED.value, IncidentState.ESCALATED.value) == (
        supervisor.CLOSED,
        supervisor.ESCALATED,
    )
    assert {s.value for s in RunStatus} == supervisor.RUN_STATUSES
    # every field the dashboard reads is one the supervisor writes
    assert set(supervisor.StoredRun.model_fields) <= {f.name for f in dataclasses.fields(RunRecord)}
    assert set(supervisor.StoredIncident.model_fields) <= {
        f.name for f in dataclasses.fields(Incident)
    }
    # the same creation time from both, or every live run would read stopped
    me = os.getpid()
    theirs = procs.process_key(me)
    ours = liveness.process_created(me)
    assert theirs is not None and ours is not None
    assert theirs.matches(ProcessKey(me, ours))
    assert liveness.alive(me, theirs.created) is True
    assert liveness.MATCH_S == 1.0 and ProcessKey(me, 0.0).matches(ProcessKey(me, 0.99))


def test_scios_harness_stops_land_in_the_classifier_rows_meant_for_them() -> None:
    """I-180, I-182, issue #105: SCIO's stop line and its ``claude`` timeout message, each
    made by SCIO's own code, read as the supervisor means them. A timeout the harness could
    not retry away is transient (replay after a backoff), an expired login is auth, a model
    that never met its schema is a SCIO suspect (repair), a spend cap is the task's."""
    run = claude_runner(sys.executable)
    with pytest.raises(RetryableError) as timed_out:
        run(["-c", "import time; time.sleep(30)"], timeout_s=0.5)
    cases: list[tuple[HarnessError, FailureClass]] = [
        (timed_out.value, FailureClass.EXTERNAL_TRANSIENT),
        (FatalError("claude -p failed: Not logged in (subtype=-)"), FailureClass.EXTERNAL_AUTH),
        (
            StructuredOutputError("milestone_planner: no valid answer", attempts=3, errors=["x"]),
            FailureClass.HARNESS_SUSPECT,
        ),
        (BudgetExceeded("spent", tokens_used=1, usd_used=1.0), FailureClass.TASK_FAULT),
    ]
    for exc, expected in cases:
        line = harness_stop_line("build", exc)
        ev = Evidence(exit_code=1, halt_reason=HARNESS_HALT, last_ledger_line=line)
        assert classify(ev).failure_class is expected, line
