"""``python -m recovery``: the run wrapper's exit code and messages, and the read commands.

The supervised child, the machine and the detached spawn are replaced, so no process
starts. The store is the real one under the test's SCIO_RECOVERY_HOME (conftest)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from recovery import cli, history, machine, procs, runner
from recovery.config import WorkspaceConfig
from recovery.model import (
    FailureClass,
    Incident,
    IncidentState,
    ProcessKey,
    RunRecord,
    RunStatus,
)
from recovery.store import Store


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "recovery.toml").write_text(
        'launcher = ["sh", "scio.sh"]\nscio_repo = "C:/p/SCIO"\nscio_gh_repo = "o/SCIO"\n',
        encoding="utf-8",
        newline="\n",
    )
    return ws


def _record(run_id: str = "run-1", **kw: Any) -> RunRecord:
    values: dict[str, Any] = {
        "run_id": run_id,
        "workspace": "W",
        "launcher": ["sh", "scio.sh"],
        "argv": ["build", "--product", "P"],
        "env": {},
        "started_at": 1_800_000_000.0,
        "status": RunStatus.EXITED,
        "exit_code": 1,
        "run_dir": "/h/runs/run-1",
        "stdout_path": "/h/runs/run-1/stdout.log",
        "stderr_path": "/h/runs/run-1/stderr.log",
    }
    values.update(kw)
    return RunRecord(**values)


def _incident(incident_id: str = "inc-1", **kw: Any) -> Incident:
    values: dict[str, Any] = {
        "incident_id": incident_id,
        "signature": "abc123",
        "failure_class": FailureClass.HARNESS_SUSPECT,
        "summary": "TimeoutExpired in run",
        "workspace": "W",
        "state": IncidentState.REPAIRING,
        "created_at": 1_800_000_000.0,
        "updated_at": 1_800_000_000.0,
    }
    values.update(kw)
    return Incident(**values)


class Supervised:
    """Replaces runner.supervise, machine.on_run_finished and procs.spawn_detached."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.record = _record()
        self.incident: Incident | None = None
        self.supervise_calls: list[dict[str, Any]] = []
        self.spawned: list[dict[str, Any]] = []
        self.finished: list[str] = []
        monkeypatch.setattr(runner, "supervise", self.supervise)
        monkeypatch.setattr(machine, "on_run_finished", self.on_run_finished)
        monkeypatch.setattr(procs, "spawn_detached", self.spawn_detached)

    def supervise(self, cfg: WorkspaceConfig, store: Store, argv: Any, **kw: Any) -> RunRecord:
        self.supervise_calls.append({"cfg": cfg, "argv": list(argv), **kw})
        if self.record.status is RunStatus.REFUSED:
            store.acquire_lease(cfg.lease_name, ProcessKey(4321, 5.0), lambda _k: True)
        store.save_run(self.record)
        return self.record

    def on_run_finished(self, run: RunRecord, deps: Any) -> Incident | None:
        self.finished.append(run.run_id)
        return self.incident

    def spawn_detached(self, argv: Any, *, cwd: Path, env: Any, log_path: Path) -> int:
        self.spawned.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "log": log_path})
        return 999


@pytest.fixture
def sup(monkeypatch: pytest.MonkeyPatch) -> Supervised:
    return Supervised(monkeypatch)


def test_help_lists_the_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(["--help"])
    assert stop.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    for command in ("run", "tick", "status", "incidents", "hold", "release"):
        assert command in out


@pytest.mark.parametrize("code", [0, 1, 3])
def test_run_returns_the_childs_exit_code_and_starts_no_tick_without_an_incident(
    workspace: Path, sup: Supervised, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    sup.record = _record(exit_code=code)
    assert cli.main(["run", "--workspace", str(workspace), "--", "build", "--product", "P"]) == code
    call = sup.supervise_calls[0]
    assert call["argv"] == ["build", "--product", "P"]  # the leading -- is stripped
    assert call["incident_id"] is None and call["resumed_from"] is None
    assert call["cfg"].workspace == workspace.resolve()
    assert sup.finished == ["run-1"] and sup.spawned == []
    assert capsys.readouterr().err == ""


def test_run_starts_a_background_tick_for_an_open_incident(
    workspace: Path, sup: Supervised, recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sup.incident = _incident(state=IncidentState.DETECTED)
    argv = ["run", "--workspace", str(workspace), "--incident", "inc-0"]
    assert cli.main([*argv, "--resumed-from", "run-0", "--", "build", "--product", "P"]) == 1
    assert sup.supervise_calls[0]["incident_id"] == "inc-0"
    assert sup.supervise_calls[0]["resumed_from"] == "run-0"
    [spawn] = sup.spawned
    assert spawn["argv"] == [sys.executable, "-m", "recovery", "tick", "--incident", "inc-1"]
    assert spawn["cwd"] == cli.PACKAGE_ROOT and (spawn["cwd"] / "recovery").is_dir()
    assert spawn["env"]["SCIO_RECOVERY_HOME"] == str(recovery_home)
    assert spawn["log"].parent == recovery_home / "ticks"
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [
        "recovery: incident inc-1 (harness_suspect) opened; recovery continues in the "
        "background. python -m recovery status"
    ]


def test_run_starts_no_tick_for_a_closed_incident(
    workspace: Path, sup: Supervised, capsys: pytest.CaptureFixture[str]
) -> None:
    sup.incident = _incident(state=IncidentState.CLOSED, failure_class=FailureClass.TASK_FAULT)
    assert cli.main(["run", "--workspace", str(workspace), "--", "build"]) == 1
    assert sup.spawned == [] and capsys.readouterr().err == ""


def test_a_supervisor_failure_after_the_run_keeps_the_childs_exit_code(
    workspace: Path,
    sup: Supervised,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(*_: Any) -> None:
        raise RuntimeError("evidence unreadable")

    monkeypatch.setattr(machine, "on_run_finished", broken)
    sup.record = _record(exit_code=0)
    assert cli.main(["run", "--workspace", str(workspace), "--", "build"]) == 0
    assert "evidence unreadable" in capsys.readouterr().err
    sup.record = _record(status=RunStatus.KILLED_HANG, exit_code=None)
    assert cli.main(["run", "--workspace", str(workspace), "--", "build"]) == 1  # never 0


def test_a_refused_run_returns_2_and_names_the_holder(
    workspace: Path, sup: Supervised, capsys: pytest.CaptureFixture[str]
) -> None:
    sup.record = _record(status=RunStatus.REFUSED, exit_code=None)
    assert cli.main(["run", "--workspace", str(workspace), "--", "build"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "pid 4321" in err[0] and "busy" in err[0]
    assert sup.finished == [] and sup.spawned == []


def test_a_missing_recovery_toml_returns_2_with_one_line(
    tmp_path: Path, sup: Supervised, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "--workspace", str(tmp_path), "--", "build"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "no recovery.toml" in err[0]
    assert sup.supervise_calls == []
    (tmp_path / "recovery.toml").write_text("launcher = [", encoding="utf-8", newline="\n")
    assert cli.main(["run", "--workspace", str(tmp_path), "--", "build"]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


def test_run_without_scio_arguments_returns_2(
    workspace: Path, sup: Supervised, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "--workspace", str(workspace), "--"]) == 2
    assert "after --" in capsys.readouterr().err and sup.supervise_calls == []


def test_status_lists_runs_and_open_incidents_newest_first(
    recovery_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store.at_home(recovery_home)
    store.save_run(_record("run-old", started_at=1.0))
    store.save_run(
        _record(
            "run-new", started_at=2.0, incident_id="inc-1", exit_code=None, status=RunStatus.RUNNING
        )
    )
    store.save_run(_record("run-elsewhere", workspace="Other", started_at=3.0))
    store.save_incident(_incident("inc-1", issue_number=12, pr_number=34))
    store.save_incident(_incident("inc-0", state=IncidentState.CLOSED, created_at=1.0))
    store.save_incident(
        _incident("inc-x", state=IncidentState.ESCALATED, escalation="sign in again")
    )
    store.close()
    assert cli.main(["status", "--workspace", "W"]) == 0
    out = capsys.readouterr().out
    assert "run-elsewhere" not in out
    assert out.index("run-new") < out.index("run-old")
    assert "run-new  running  exit -  build --product P" in out
    assert "stdout: /h/runs/run-1/stdout.log" in out and "dir: /h/runs/run-1" in out
    assert "inc-1  repairing  harness_suspect  issue #12  PR #34" in out
    assert "inc-0" not in out
    assert "escalated" in out and "escalation: sign in again" in out
    assert cli.main(["status"]) == 0
    assert "run-elsewhere" in capsys.readouterr().out


def test_status_with_an_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "runs: none" in out and "open incidents: none" in out


def test_incidents_searches_the_history(
    recovery_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def search(store: Store, **kw: Any) -> list[Incident]:
        seen.update(kw)
        return [_incident(pr_number=34, ledger_id="I-200", innermost_scio_frame="scio/x.py:f")]

    monkeypatch.setattr(history, "search", search)
    assert (
        cli.main(["incidents", "--signature", "abc123", "--grep", "Timeout", "--limit", "5"]) == 0
    )
    assert seen == {"signature": "abc123", "text": "Timeout", "limit": 5}
    out = capsys.readouterr().out
    assert "inc-1" in out and "abc123" in out and "TimeoutExpired in run" in out
    assert "PR #34" in out and "ledger I-200" in out and "frame scio/x.py:f" in out
    monkeypatch.setattr(history, "search", lambda *_, **__: [])
    assert cli.main(["incidents"]) == 0
    assert "no incidents" in capsys.readouterr().out


def test_hold_and_release_print_one_line_and_an_unknown_incident_returns_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def hold(incident_id: str, _deps: Any) -> Incident:
        return _incident(incident_id, state=IncidentState.HELD, held_from=IncidentState.REPAIRING)

    real_hold = machine.hold
    monkeypatch.setattr(machine, "hold", hold)
    monkeypatch.setattr(machine, "release", lambda iid, _d: _incident(iid))
    assert cli.main(["hold", "inc-1"]) == 0
    assert capsys.readouterr().out.strip() == "inc-1: held (from repairing)"
    assert cli.main(["release", "inc-1"]) == 0
    assert capsys.readouterr().out.strip() == "inc-1: released to repairing"
    monkeypatch.setattr(machine, "hold", real_hold)
    assert cli.main(["hold", "inc-missing"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "no incident inc-missing" in err[0]


def test_tick_prints_its_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(machine, "tick", lambda _deps: ["inc-1: detected -> resuming"])
    assert cli.main(["tick"]) == 0
    assert capsys.readouterr().out.strip() == "inc-1: detected -> resuming"
    monkeypatch.setattr(
        machine,
        "drive",
        lambda iid, _deps: _incident(iid, state=IncidentState.ESCALATED, escalation="why"),
    )
    assert cli.main(["tick", "--incident", "inc-2"]) == 0
    assert capsys.readouterr().out.splitlines() == ["inc-2: escalated", "  escalation: why"]
