"""The supervised run against a real stub child: the record, the outcome flag, echo, hang
kills, the lease, Ctrl+C, orphans and replays (ADR 0019 decisions 2, 3 and 4)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from recovery import procs
from recovery.commands import CommandResult, kill_tree
from recovery.config import Limits, WorkspaceConfig, recorded_env
from recovery.model import ProcessKey, RunRecord, RunStatus
from recovery.runner import (
    OUTCOME_SUBCOMMANDS,
    find_orphans,
    new_run_id,
    progress_time,
    replay_argv,
    start_replay,
    supervise,
)
from recovery.store import Store

STUB = r"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
mode = os.environ.get("STUB_MODE", "ok")
print("ARGV " + json.dumps(sys.argv[1:]), flush=True)
if mode == "ok":
    print("hello from scio \u2713", flush=True)
    print("TOKEN " + os.environ.get("SCIO_STUB_TOKEN", "-"), flush=True)
    print("a warning", file=sys.stderr, flush=True)
elif mode == "pause":
    print("PAUSED for a human: merge PR #7", flush=True)
elif mode == "exit3":
    sys.exit(3)
elif mode == "crash":
    raise RuntimeError("boom")
elif mode == "nap":
    time.sleep(0.6)
    progress = Path(os.environ["STUB_PROGRESS_JSON"])
    progress.write_text(json.dumps({"build_thread": "build-prod-m3"}), encoding="utf-8")
elif mode == "sleep":
    time.sleep(120)
elif mode == "sleep-tree":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    print(f"GRANDCHILD {child.pid}", flush=True)
    time.sleep(120)
elif mode in ("touch-ledger", "print"):
    ledger = Path(os.environ["STUB_LEDGER"])
    ledger.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + float(os.environ["STUB_ACTIVE_S"])
    while time.time() < end:
        if mode == "touch-ledger":
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write("{}\n")
        else:
            print("tick", flush=True)
        time.sleep(0.2)
    time.sleep(120)
"""

NORMAL_EXIT_HANG_S = 60.0
"""Tests that expect a normal exit never come near a hang kill, even on a loaded machine."""


class FakeGit:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = result or CommandResult(0, "abc123\n", "")
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(list(argv))
        return self.result


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    path = tmp_path / "stub_scio.py"
    path.write_text(STUB, encoding="utf-8", newline="\n")
    return path


def _cfg(
    tmp_path: Path, stub: Path, *, hang_after_s: float = NORMAL_EXIT_HANG_S
) -> WorkspaceConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    repo = tmp_path / "SCIO"
    return WorkspaceConfig(
        workspace=ws,
        launcher=(sys.executable, str(stub)),
        scio_repo=repo,
        scio_gh_repo="o/SCIO",
        live_wt=repo / ".scio" / "live-wt",
        limits=Limits(poll_s=0.1, hang_after_s=hang_after_s),
    )


def _env(mode: str, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["STUB_MODE"] = mode
    env.update(extra)
    return env


def _run(
    cfg: WorkspaceConfig,
    store: Store,
    home: Path,
    argv: Sequence[str],
    mode: str,
    *,
    env: dict[str, str] | None = None,
    **kw: Any,
) -> RunRecord:
    kw.setdefault("git", FakeGit())
    kw.setdefault("echo", False)
    return supervise(cfg, store, argv, env=env or _env(mode), home=home, **kw)


def _stdout(run: RunRecord) -> str:
    return Path(run.stdout_path).read_text(encoding="utf-8")


def _child_argv(run: RunRecord) -> list[str]:
    line = next(ln for ln in _stdout(run).splitlines() if ln.startswith("ARGV "))
    return list(json.loads(line[len("ARGV ") :]))


# The record


@pytest.mark.parametrize(
    ("mode", "code", "marker"),
    [
        ("ok", 0, "hello from scio"),
        ("pause", 0, "PAUSED for a human: merge PR #7"),
        ("exit3", 3, "ARGV"),
        ("crash", 1, "ARGV"),
    ],
)
def test_the_exit_code_and_status_are_recorded(
    tmp_path: Path, stub: Path, mode: str, code: int, marker: str
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    run = _run(cfg, store, home, ["status", "--product", "P"], mode)
    assert run.status is RunStatus.EXITED
    assert run.exit_code == code
    assert run.ended_at is not None and run.ended_at >= run.started_at
    assert run.pid > 0
    assert marker in _stdout(run)
    if mode == "crash":
        stderr = Path(run.stderr_path).read_text(encoding="utf-8")
        assert "Traceback" in stderr and "RuntimeError: boom" in stderr
    saved = store.get_run(run.run_id)
    assert saved is not None and saved.to_dict() == run.to_dict()
    assert store.lease_holder(cfg.lease_name) is None


@pytest.mark.parametrize("subcommand", [*OUTCOME_SUBCOMMANDS, "status", "report"])
def test_the_outcome_flag_is_appended_only_for_build_new_run_and_resume(
    tmp_path: Path, stub: Path, subcommand: str
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    argv = [subcommand, "--thread", "t1"]
    run = _run(cfg, store, home, argv, "ok")
    assert run.argv == argv
    assert run.outcome_path == str(home / "runs" / run.run_id / "outcome.json")
    flag = ["--outcome-file", run.outcome_path]
    expected = argv + flag if subcommand in ("build", "new", "run", "resume") else argv
    assert _child_argv(run) == expected
    saved = store.get_run(run.run_id)
    assert saved is not None and "--outcome-file" not in saved.argv


def test_output_is_echoed_and_lands_in_the_run_files(
    tmp_path: Path, stub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    env = _env("ok", SCIO_STUB_TOKEN="s3cret")
    run = _run(cfg, store, home, ["status"], "ok", env=env, echo=True)
    out, err = capsys.readouterr()
    assert "hello from scio \u2713" in out and "TOKEN s3cret" in out
    assert "\r" not in out
    assert "a warning" in err
    run_dir = home / "runs" / run.run_id
    assert run.run_dir == str(run_dir)
    assert run.stdout_path == str(run_dir / "stdout.log")
    assert run.stderr_path == str(run_dir / "stderr.log")
    assert "hello from scio \u2713" in _stdout(run)
    assert "a warning" in (run_dir / "stderr.log").read_text(encoding="utf-8")
    # the child gets the whole environment; the record keeps only the safe subset
    assert run.env == recorded_env(env)
    assert "SCIO_STUB_TOKEN" not in run.env and "STUB_MODE" not in run.env


def test_the_record_names_the_scio_sha_the_product_and_its_thread(
    tmp_path: Path, stub: Path
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    progress = cfg.workspace / "prod" / ".scio" / "product" / "progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps({"build_thread": "build-prod-m2"}), encoding="utf-8")
    git = FakeGit()
    run = _run(cfg, store, home, ["build", "--product", "prod", "--gh", "o/p"], "ok", git=git)
    assert git.calls == [["git", "-C", str(cfg.live_wt), "rev-parse", "HEAD"]]
    assert run.scio_sha == "abc123"
    assert run.product_dir == str(cfg.workspace / "prod")
    assert run.build_thread == "build-prod-m2"
    me = procs.self_key()
    assert run.supervisor_pid == me.pid and abs(run.supervisor_created - me.created) < 1.0
    assert run.workspace == str(cfg.workspace)
    assert run.launcher == list(cfg.launcher)


def test_a_failed_git_and_no_product_leave_the_fields_empty(tmp_path: Path, stub: Path) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    git = FakeGit(CommandResult(128, "", "not a git repository"))
    run = _run(cfg, store, home, ["report", "--thread", "t"], "ok", git=git)
    assert (run.scio_sha, run.product_dir, run.build_thread) == ("", "", "")
    target = tmp_path / "elsewhere" / "new-product"
    run = _run(cfg, store, home, ["new", f"--dir={target}"], "ok", git=git)
    assert run.product_dir == str(target)


def test_the_record_is_saved_before_the_spawn_then_with_the_pid_and_at_exit(
    tmp_path: Path, stub: Path
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    progress = cfg.workspace / "prod" / ".scio" / "product" / "progress.json"
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps({"build_thread": "build-prod-m2"}), encoding="utf-8")
    before_spawn: list[RunRecord] = []
    while_running: list[RunRecord] = []

    def spawn(*args: Any, **kw: Any) -> subprocess.Popen[bytes]:
        before_spawn.extend(store.runs(status=RunStatus.RUNNING))
        return procs.spawn(*args, **kw)

    def sleep(seconds: float) -> None:
        while_running.extend(store.runs(status=RunStatus.RUNNING))
        time.sleep(seconds)

    env = _env("nap", STUB_PROGRESS_JSON=str(progress))
    run = _run(
        cfg, store, home, ["build", "--product", "prod"], "nap", env=env, spawn=spawn, sleep=sleep
    )
    assert [(r.run_id, r.pid, r.build_thread) for r in before_spawn] == [
        (run.run_id, 0, "build-prod-m2")
    ]
    assert while_running and {(r.pid, r.pid_created) for r in while_running} == {
        (run.pid, run.pid_created)
    }
    assert run.pid_created > 0
    # the thread is read again at exit: the build moved on to m3
    assert run.status is RunStatus.EXITED and run.build_thread == "build-prod-m3"
    assert run.last_progress_at >= run.started_at


def test_a_launcher_that_cannot_start_is_recorded_as_exit_127(
    tmp_path: Path, stub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = WorkspaceConfig(
        workspace=tmp_path,
        launcher=("scio-recovery-no-such-launcher",),
        scio_repo=tmp_path,
        scio_gh_repo="o/SCIO",
        live_wt=tmp_path,
        limits=Limits(poll_s=0.1),
    )
    store, home = Store(tmp_path / "s.sqlite"), tmp_path / "home"
    run = _run(cfg, store, home, ["build", "--product", "P"], "ok", echo=True)
    assert (run.status, run.exit_code) == (RunStatus.EXITED, 127)
    assert "cannot start scio-recovery-no-such-launcher" in capsys.readouterr().err
    assert "cannot start" in Path(run.stderr_path).read_text(encoding="utf-8")
    assert store.lease_holder(cfg.lease_name) is None


# Hangs


def test_a_silent_child_is_killed_as_a_hang_with_its_whole_tree(tmp_path: Path, stub: Path) -> None:
    cfg = _cfg(tmp_path, stub, hang_after_s=1.5)
    store, home = Store(tmp_path / "s.sqlite"), tmp_path / "home"
    killed: list[ProcessKey | None] = []

    def kill(pid: int) -> None:
        log = Path(store.runs()[0].stdout_path)
        pattern = re.compile(r"GRANDCHILD (\d+)")
        deadline = time.monotonic() + 30
        while not pattern.search(log.read_text(encoding="utf-8")) and time.monotonic() < deadline:
            time.sleep(0.1)
        found = pattern.search(log.read_text(encoding="utf-8"))
        assert found is not None, "the stub never started its grandchild"
        killed.append(procs.process_key(int(found.group(1))))
        kill_tree(pid)

    run = _run(cfg, store, home, ["build", "--product", "P"], "sleep-tree", kill=kill)
    assert run.status is RunStatus.KILLED_HANG
    assert run.exit_code is not None
    assert run.ended_at is not None
    assert run.ended_at - max(run.last_progress_at, run.started_at) > 1.5
    assert run.pid_created > 0
    assert not procs.alive(run.process)
    grandchild = killed[0]
    assert grandchild is not None and not procs.alive(grandchild)
    saved = store.get_run(run.run_id)
    assert saved is not None and saved.status is RunStatus.KILLED_HANG
    assert store.lease_holder(cfg.lease_name) is None


@pytest.mark.parametrize("mode", ["touch-ledger", "print"])
def test_a_child_making_progress_is_killed_only_after_hang_after_s_of_silence(
    tmp_path: Path, stub: Path, mode: str
) -> None:
    # hang_s leaves a loaded machine (the gate's 16 workers) time to start the stub's python
    active_s, hang_s = 3.0, 2.5
    cfg = _cfg(tmp_path, stub, hang_after_s=hang_s)
    store, home = Store(tmp_path / "s.sqlite"), tmp_path / "home"
    ledger = cfg.workspace / ".scio" / "ledger" / "build-p-m1-p1-a1.jsonl"
    env = _env(mode, STUB_LEDGER=str(ledger), STUB_ACTIVE_S=str(active_s))
    run = _run(cfg, store, home, ["build", "--product", "P"], mode, env=env)
    assert run.status is RunStatus.KILLED_HANG
    assert run.ended_at is not None
    # active for 3 s (writes every 0.2 s), then hang_s of silence before the kill
    assert run.ended_at - run.started_at >= active_s - 0.2 + hang_s
    assert run.last_progress_at - run.started_at >= active_s - 0.5
    assert not procs.alive(run.process)


# Ctrl+C


def test_ctrl_c_kills_the_child_records_the_exit_and_releases_the_lease(
    tmp_path: Path, stub: Path
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run(cfg, store, home, ["build", "--product", "P"], "sleep", sleep=interrupt)
    [run] = store.runs()
    assert run.status is RunStatus.EXITED
    assert run.exit_code is not None and run.ended_at is not None
    assert run.pid > 0 and not procs.alive(run.process)
    assert store.lease_holder(cfg.lease_name) is None


# The lease


def _holder() -> tuple[subprocess.Popen[bytes], ProcessKey]:
    popen = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key = procs.process_key(popen.pid)
    assert key is not None
    return popen, key


def test_a_second_supervise_is_refused_while_a_live_process_holds_the_lease(
    tmp_path: Path, stub: Path
) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    holder, key = _holder()
    spawned: list[object] = []

    def spawn(*args: Any, **kw: Any) -> subprocess.Popen[bytes]:
        spawned.append(args)
        raise AssertionError("a refused run must not start anything")

    try:
        assert store.acquire_lease(cfg.lease_name, key, procs.alive)
        run = _run(cfg, store, home, ["build", "--product", "P"], "ok", spawn=spawn)
    finally:
        holder.kill()
        holder.wait()
    assert run.status is RunStatus.REFUSED
    assert spawned == [] and run.pid == 0 and run.exit_code is None
    saved = store.get_run(run.run_id)
    assert saved is not None and saved.status is RunStatus.REFUSED
    assert store.lease_holder(cfg.lease_name) == key


def test_a_dead_holders_lease_is_taken_over(tmp_path: Path, stub: Path) -> None:
    cfg, store, home = _cfg(tmp_path, stub), Store(tmp_path / "s.sqlite"), tmp_path / "home"
    holder, key = _holder()
    assert store.acquire_lease(cfg.lease_name, key, procs.alive)
    holder.kill()
    holder.wait()
    run = _run(cfg, store, home, ["build", "--product", "P"], "ok")
    assert (run.status, run.exit_code) == (RunStatus.EXITED, 0)
    assert store.lease_holder(cfg.lease_name) is None


# Orphans and replays


def _record(run_id: str, pid: int, supervisor_pid: int, **kw: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run_id": run_id,
        "workspace": "W",
        "launcher": ["sh", "scio.sh"],
        "argv": ["build", "--product", "P"],
        "env": {"PATH": "/bin"},
        "started_at": 100.0,
        "pid": pid,
        "pid_created": 10.0,
        "supervisor_pid": supervisor_pid,
        "supervisor_created": 20.0,
    }
    base.update(kw)
    return RunRecord(**base)


def test_a_run_is_orphaned_only_when_both_its_child_and_its_supervisor_are_gone(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "s.sqlite")
    living = {ProcessKey(11, 10.0), ProcessKey(22, 20.0)}
    store.save_run(_record("both-gone", pid=1, supervisor_pid=2))
    store.save_run(_record("child-alive", pid=11, supervisor_pid=2))
    store.save_run(_record("supervisor-alive", pid=1, supervisor_pid=22))
    store.save_run(_record("finished", pid=1, supervisor_pid=2, status=RunStatus.EXITED))
    # a reused child PID with another creation time is not the run's child
    store.save_run(_record("reused-pid", pid=11, pid_created=99.0, supervisor_pid=2))

    def alive(key: ProcessKey) -> bool:
        return key in living

    orphans = find_orphans(store, alive=alive)
    assert sorted(r.run_id for r in orphans) == ["both-gone", "reused-pid"]
    assert all(r.status is RunStatus.ORPHANED for r in orphans)
    status = {r.run_id: r.status for r in store.runs()}
    assert status == {
        "both-gone": RunStatus.ORPHANED,
        "child-alive": RunStatus.RUNNING,
        "supervisor-alive": RunStatus.RUNNING,
        "finished": RunStatus.EXITED,
        "reused-pid": RunStatus.ORPHANED,
    }
    assert find_orphans(store, alive=alive) == []


def test_the_replay_argv_has_its_exact_shape() -> None:
    run = _record("run-1", pid=1, supervisor_pid=2, workspace="C:/ws")
    assert replay_argv(run, "inc-9", python="py") == [
        "py",
        "-m",
        "recovery",
        "run",
        "--workspace",
        "C:/ws",
        "--incident",
        "inc-9",
        "--resumed-from",
        "run-1",
        "--",
        "build",
        "--product",
        "P",
    ]
    assert replay_argv(run, "inc-9")[0] == sys.executable


def test_start_replay_runs_detached_from_the_package_root_with_the_recorded_env(
    tmp_path: Path,
) -> None:
    run = _record("run-1", pid=1, supervisor_pid=2, env={"PATH": "/bin", "SCIO_X": "1"})
    calls: list[dict[str, Any]] = []

    def spawn_detached(argv: Sequence[str], **kw: Any) -> int:
        calls.append({"argv": list(argv), **kw})
        return 4321

    home, root = tmp_path / "home", tmp_path / "pinned"
    pid = start_replay(run, "inc-9", home=home, package_root=root, spawn_detached=spawn_detached)
    assert pid == 4321
    assert calls == [
        {
            "argv": replay_argv(run, "inc-9"),
            "cwd": root,
            "env": {"PATH": "/bin", "SCIO_X": "1", "SCIO_RECOVERY_HOME": str(home)},
            "log_path": home / "replays" / "inc-9.log",
        }
    ]
    assert run.env == {"PATH": "/bin", "SCIO_X": "1"}


# Ids and progress


def test_a_run_id_carries_the_utc_time_and_six_hex_digits() -> None:
    assert re.fullmatch(r"run-19700101000001-[0-9a-f]{6}", new_run_id(lambda: 1.0))
    assert new_run_id() != new_run_id()


def test_progress_is_the_newest_ledger_wal_or_output_mtime(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    run = _record("r", pid=1, supervisor_pid=2)
    assert progress_time(ws, run) == 0.0
    scio = ws / ".scio"
    (scio / "ledger").mkdir(parents=True)
    files: dict[str, float] = {
        "ledger/a.jsonl": 1000.0,
        "ledger/b.jsonl": 2000.0,
        "checkpoints.sqlite-wal": 1500.0,
        "ledger/ignored.txt": 9000.0,
        "effects.sqlite": 9000.0,
    }
    for name, mtime in files.items():
        (scio / name).write_text("x", encoding="utf-8")
        os.utime(scio / name, (mtime, mtime))
    assert progress_time(ws, run) == 2000.0
    out = tmp_path / "stdout.log"
    out.write_text("x", encoding="utf-8")
    os.utime(out, (3000.0, 3000.0))
    run.stdout_path = str(out)
    run.stderr_path = str(tmp_path / "missing.log")
    assert progress_time(ws, run) == 3000.0
