"""Moving live-wt after a merged repair, rolling it back, and the health check before a
resume (ADR 0019 decision 8).

The git tests use real repositories under ``tmp_path``: a bare origin, a clone acting as
the SCIO repository, a detached worktree of it acting as live-wt, and a separate "pusher"
clone. New commits reach the origin only through the pusher, so live-wt sees them only
after its own ``git fetch origin`` (a worktree shares refs with its clone, so pushing from
the SCIO clone would hide a missing fetch).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from recovery.commands import CommandResult, run_command
from recovery.config import WorkspaceConfig
from recovery.model import RunRecord
from recovery.runtime import (
    HEALTH_TIMEOUT_S,
    RuntimeRefused,
    head,
    health_check,
    lock_changed,
    move_live_wt,
    rollback_live_wt,
)

IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@x")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *IDENTITY, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text, encoding="utf-8", newline="\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _not_running() -> list[int]:
    return []


@dataclass
class Repos:
    cfg: WorkspaceConfig
    live_wt: Path
    pusher: Path
    base: str
    """The commit live-wt starts on."""

    def push_main(self, name: str, text: str, message: str) -> str:
        sha = _commit(self.pusher, name, text, message)
        _git(self.pusher, "push", "-q", "origin", "HEAD:main")
        return sha


@pytest.fixture
def repos(tmp_path: Path) -> Repos:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    pusher = tmp_path / "pusher"
    _git(tmp_path, "clone", "-q", str(origin), str(pusher))
    _git(pusher, "checkout", "-q", "-B", "main")
    base = _commit(pusher, "uv.lock", "lock v1\n", "base")
    _git(pusher, "push", "-q", "origin", "main")
    scio = tmp_path / "scio"
    _git(tmp_path, "clone", "-q", str(origin), str(scio))
    live_wt = tmp_path / "live-wt"
    _git(scio, "worktree", "add", "-q", "--detach", str(live_wt), "HEAD")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = WorkspaceConfig(
        workspace=workspace,
        launcher=("sh", "scio.sh"),
        scio_repo=scio,
        scio_gh_repo="o/SCIO",
        live_wt=live_wt,
    )
    return Repos(cfg=cfg, live_wt=live_wt, pusher=pusher, base=base)


@dataclass
class RecordingGit:
    """The real git, with every argv recorded and chosen calls replaced."""

    calls: list[list[str]] = field(default_factory=list)
    fake: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    """A call whose argv after ``git -C <path>`` starts with a key gets that result."""

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
        tail = tuple(argv[3:])
        for prefix, result in self.fake.items():
            if tail[: len(prefix)] == prefix:
                return result
        return run_command(argv, cwd=cwd, stdin=stdin, timeout_s=timeout_s, env=env)

    def ran(self, *prefix: str) -> bool:
        return any(tuple(c[3 : 3 + len(prefix)]) == prefix for c in self.calls)


# --- move_live_wt -------------------------------------------------------------------------


def test_live_wt_moves_to_a_new_commit_on_origin_main_and_returns_the_old_sha(
    repos: Repos,
) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    git = RecordingGit()
    previous = move_live_wt(repos.cfg, target, git=git, scio_running=_not_running)
    assert previous == repos.base
    assert head(repos.live_wt) == target
    assert git.ran("fetch", "origin")
    assert all(c[:3] == ["git", "-C", str(repos.live_wt)] for c in git.calls)


def test_a_short_target_is_resolved_and_the_probe_compares_full_shas(repos: Repos) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    previous = move_live_wt(repos.cfg, target[:8], scio_running=_not_running)
    assert previous == repos.base
    assert head(repos.live_wt) == target
    assert len(head(repos.live_wt)) == 40


def test_the_move_waits_while_scio_runs_and_changes_nothing(repos: Repos) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    with pytest.raises(RuntimeRefused) as caught:
        move_live_wt(repos.cfg, target, scio_running=lambda: [4242])
    assert caught.value.retryable is True
    assert "4242" in caught.value.reason
    assert head(repos.live_wt) == repos.base


def test_an_unreadable_process_list_waits_instead_of_counting_as_none(repos: Repos) -> None:
    """procs.scio_processes raises when it cannot list processes: not knowing whether SCIO
    runs must never move live-wt under a running build."""
    target = repos.push_main("scio.py", "fixed\n", "fix")

    def unreadable() -> list[int]:
        raise RuntimeError("Get-CimInstance failed")

    with pytest.raises(RuntimeRefused) as caught:
        move_live_wt(repos.cfg, target, scio_running=unreadable)
    assert caught.value.retryable is True
    assert head(repos.live_wt) == repos.base


def test_a_dirty_live_wt_is_refused_for_a_human_and_keeps_its_changes(repos: Repos) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    (repos.live_wt / "uv.lock").write_text("edited by hand\n", encoding="utf-8", newline="\n")
    with pytest.raises(RuntimeRefused) as caught:
        move_live_wt(repos.cfg, target, scio_running=_not_running)
    assert caught.value.retryable is False
    assert "local changes" in caught.value.reason
    assert head(repos.live_wt) == repos.base
    assert (repos.live_wt / "uv.lock").read_text(encoding="utf-8") == "edited by hand\n"


def test_a_commit_only_on_a_feature_branch_is_refused(repos: Repos) -> None:
    _git(repos.pusher, "checkout", "-q", "-b", "feature")
    feature = _commit(repos.pusher, "scio.py", "unmerged\n", "wip")
    _git(repos.pusher, "push", "-q", "origin", "feature")
    with pytest.raises(RuntimeRefused) as caught:
        move_live_wt(repos.cfg, feature, scio_running=_not_running)
    assert caught.value.retryable is False
    assert "not on origin/main" in caught.value.reason
    assert head(repos.live_wt) == repos.base


def test_an_unknown_or_malformed_target_is_refused(repos: Repos) -> None:
    for target in ("0123456789abcdef0123456789abcdef01234567", "--orphan", "main"):
        with pytest.raises(RuntimeRefused) as caught:
            move_live_wt(repos.cfg, target, scio_running=_not_running)
        assert caught.value.retryable is False
    assert head(repos.live_wt) == repos.base


def test_already_at_the_target_changes_nothing_even_while_scio_runs(repos: Repos) -> None:
    git = RecordingGit()
    returned = move_live_wt(repos.cfg, repos.base[:10], git=git, scio_running=lambda: [7])
    assert returned == repos.base
    assert head(repos.live_wt) == repos.base
    assert not git.ran("fetch")
    assert not git.ran("checkout")


def test_a_failed_fetch_is_retryable(repos: Repos) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    git = RecordingGit(fake={("fetch",): CommandResult(128, "", "fatal: unable to access")})
    with pytest.raises(RuntimeRefused) as caught:
        move_live_wt(repos.cfg, target, git=git, scio_running=_not_running)
    assert caught.value.retryable is True
    assert head(repos.live_wt) == repos.base


def test_a_failed_checkout_is_refused_and_live_wt_stays_where_it_was(repos: Repos) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    git = RecordingGit(fake={("checkout",): CommandResult(1, "", "error: would be overwritten")})
    with pytest.raises(RuntimeRefused) as caught:
        move_live_wt(repos.cfg, target, git=git, scio_running=_not_running)
    assert caught.value.retryable is False
    assert "is back at" in caught.value.reason
    assert head(repos.live_wt) == repos.base


# --- rollback_live_wt and lock_changed ------------------------------------------------------


def test_rollback_returns_live_wt_to_the_recorded_sha(repos: Repos) -> None:
    target = repos.push_main("scio.py", "fixed\n", "fix")
    previous = move_live_wt(repos.cfg, target, scio_running=_not_running)
    assert head(repos.live_wt) == target
    rollback_live_wt(repos.cfg, previous)
    assert head(repos.live_wt) == repos.base


def test_a_rollback_whose_probe_disagrees_raises(repos: Repos) -> None:
    lying = RecordingGit(fake={("rev-parse", "HEAD"): CommandResult(0, "f" * 40 + "\n", "")})
    with pytest.raises(RuntimeError, match="probe"):
        rollback_live_wt(repos.cfg, repos.base, git=lying)
    with pytest.raises(RuntimeError):
        rollback_live_wt(repos.cfg, "-q")


def test_lock_changed_compares_uv_lock_between_two_shas(repos: Repos) -> None:
    code_only = repos.push_main("scio.py", "fixed\n", "fix")
    new_lock = repos.push_main("uv.lock", "lock v2\n", "deps")
    _git(repos.live_wt, "fetch", "-q", "origin")
    assert lock_changed(repos.cfg, repos.base, code_only) is False
    assert lock_changed(repos.cfg, repos.base, new_lock) is True
    assert lock_changed(repos.cfg, code_only[:8], new_lock[:8]) is True


def test_lock_changed_never_answers_unchanged_when_git_cannot_answer(repos: Repos) -> None:
    with pytest.raises(RuntimeError):
        lock_changed(repos.cfg, repos.base, "0123456789abcdef0123456789abcdef01234567")
    with pytest.raises(RuntimeError):
        lock_changed(repos.cfg, repos.base, "--cached")


# --- health_check ---------------------------------------------------------------------------


@dataclass
class Call:
    argv: list[str]
    cwd: str | Path | None
    env: dict[str, str]
    timeout_s: float | None
    seen_rows: list[str] = field(default_factory=list)
    """For ``report``: the rows of the checkpoint copy its DSN points at, read during the
    call."""


@dataclass
class FakeLauncher:
    results: dict[str, CommandResult] = field(default_factory=dict)
    """Keyed by the first argument after the launcher (``--help``, ``status``,
    ``report``); the default is exit 0."""
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        call = Call(list(argv), cwd, dict(env or {}), timeout_s)
        self.calls.append(call)
        key = argv[2]
        if key == "report":
            dsn = call.env["SCIO_CHECKPOINT_DSN"]
            copy = Path(dsn.removeprefix("sqlite:///"))
            conn = sqlite3.connect(copy)
            try:
                call.seen_rows = [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]
            finally:
                conn.close()
        return self.results.get(key, CommandResult(0, f"{key} ok\n", ""))

    def step(self, key: str) -> Call:
        return next(c for c in self.calls if c.argv[2] == key)


RUN_ENV = {"PATH": "/bin", "SCIO_PROVIDER_OVERRIDE": "claude_code", "UV_SYSTEM_CERTS": "1"}


def _run(product: str = "C:/w/graphcalc", thread: str = "build-graphcalc-m4") -> RunRecord:
    return RunRecord(
        run_id="r1",
        workspace="C:/w",
        launcher=["sh", "scio.sh"],
        argv=["build", "--product", product],
        env=dict(RUN_ENV),
        started_at=0.0,
        product_dir=product,
        build_thread=thread,
    )


def _cfg(workspace: Path) -> WorkspaceConfig:
    return WorkspaceConfig(
        workspace=workspace,
        launcher=("sh", "scio.sh"),
        scio_repo=workspace / "SCIO",
        scio_gh_repo="o/SCIO",
        live_wt=workspace / "SCIO" / ".scio" / "live-wt",
    )


def _fingerprint(path: Path) -> tuple[str, int]:
    """The file's sha256 and mtime; ``("missing", 0)`` once it is gone (a checkpoint on
    close deletes the WAL)."""
    if not path.exists():
        return "missing", 0
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _crashed_wal_db(path: Path, rows: Sequence[str], scratch: Path) -> None:
    """A WAL-mode database as a hard-killed SCIO leaves it: committed rows that are only in
    the ``-wal`` file, and no live connection. It is made by copying the main file and the
    WAL of a database whose writer is still open, with automatic checkpoints off."""
    scratch.mkdir(parents=True, exist_ok=True)
    src = scratch / "writer.sqlite"
    writer = sqlite3.connect(src)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE t (v TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
        writer.commit()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
        shutil.copyfile(src.with_name(src.name + "-wal"), path.with_name(path.name + "-wal"))
    finally:
        writer.close()


def test_the_health_check_runs_each_step_in_the_workspace_with_the_run_env(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    _crashed_wal_db(workspace / ".scio" / "checkpoints.sqlite", ["a"], tmp_path / "w1")
    _crashed_wal_db(workspace / ".scio" / "effects.sqlite", ["e"], tmp_path / "w2")
    work_dir = tmp_path / "incident"
    launcher = FakeLauncher()
    assert health_check(_cfg(workspace), _run(), work_dir, runner=launcher) == []
    health = (work_dir / "health").resolve()
    assert [c.argv for c in launcher.calls] == [
        ["sh", "scio.sh", "--help"],
        ["sh", "scio.sh", "status", "--product", "C:/w/graphcalc"],
        [
            "sh",
            "scio.sh",
            "report",
            "--thread",
            "build-graphcalc-m4",
            "--out",
            (health / "reports").as_posix(),
        ],
    ]
    for call in launcher.calls:
        assert call.cwd == workspace
        assert call.timeout_s == HEALTH_TIMEOUT_S
    assert launcher.step("--help").env == RUN_ENV
    assert launcher.step("status").env == RUN_ENV
    notes = json.loads((work_dir / "health" / "health.json").read_text(encoding="utf-8"))
    assert [n["step"] for n in notes["steps"]] == ["cli", "status", "report"]
    assert notes["problems"] == []


def test_the_report_step_reads_copies_and_never_writes_the_live_databases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    live_ck = workspace / ".scio" / "checkpoints.sqlite"
    live_fx = workspace / ".scio" / "effects.sqlite"
    _crashed_wal_db(live_ck, ["row-in-the-wal", "second"], tmp_path / "w1")
    _crashed_wal_db(live_fx, ["effect"], tmp_path / "w2")
    live_files = [live_ck, live_fx, *(p.with_name(p.name + "-wal") for p in (live_ck, live_fx))]
    before = {p: _fingerprint(p) for p in live_files}
    work_dir = tmp_path / "incident"
    launcher = FakeLauncher()

    assert health_check(_cfg(workspace), _run(), work_dir, runner=launcher) == []

    env = launcher.step("report").env
    copy_ck = (work_dir / "health" / "checkpoints.sqlite").resolve()
    copy_fx = (work_dir / "health" / "effects.sqlite").resolve()
    assert env["SCIO_CHECKPOINT_DSN"] == "sqlite:///" + copy_ck.as_posix()
    assert env["SCIO_EFFECTS_PATH"] == copy_fx.as_posix()
    overrides = ("SCIO_CHECKPOINT_DSN", "SCIO_EFFECTS_PATH")
    assert {k: v for k, v in env.items() if k not in overrides} == RUN_ENV
    assert copy_ck.is_file() and copy_fx.is_file()
    # The copy holds the rows that were only in the live WAL: a backup, not a file copy.
    assert launcher.step("report").seen_rows == ["row-in-the-wal", "second"]
    # The originals, WAL included, are byte for byte and mtime for mtime what they were:
    # a read-write open would have checkpointed the WAL into them on close.
    assert {p: _fingerprint(p) for p in live_files} == before


def test_the_report_step_finds_the_databases_named_in_the_run_env(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _crashed_wal_db(workspace / "elsewhere" / "ck.sqlite", ["custom"], tmp_path / "w1")
    run = _run()
    run.env["SCIO_CHECKPOINT_DSN"] = "sqlite:///elsewhere/ck.sqlite"
    run.env["SCIO_EFFECTS_PATH"] = "elsewhere/fx.sqlite"
    launcher = FakeLauncher()
    assert health_check(_cfg(workspace), run, tmp_path / "incident", runner=launcher) == []
    env = launcher.step("report").env
    assert launcher.step("report").seen_rows == ["custom"]
    assert "elsewhere" not in env["SCIO_CHECKPOINT_DSN"]
    assert env["SCIO_EFFECTS_PATH"].endswith("/health/effects.sqlite")
    assert not (workspace / "elsewhere" / "fx.sqlite").exists()


def test_a_failing_step_and_a_timed_out_step_are_problems(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _crashed_wal_db(workspace / ".scio" / "checkpoints.sqlite", ["a"], tmp_path / "w1")
    failing = FakeLauncher(
        results={"status": CommandResult(2, "", "progress.json not found: run scio new first")}
    )
    problems = health_check(_cfg(workspace), _run(), tmp_path / "i1", runner=failing)
    assert len(problems) == 1
    assert "status" in problems[0] and "exit 2" in problems[0]
    assert "progress.json not found" in problems[0]

    hanging = FakeLauncher(results={"report": CommandResult(-1, "", "", timed_out=True)})
    problems = health_check(_cfg(workspace), _run(), tmp_path / "i2", runner=hanging)
    assert len(problems) == 1
    assert "report" in problems[0] and "timed out" in problems[0]

    version_hangs = FakeLauncher(results={"--help": CommandResult(-1, "", "", timed_out=True)})
    problems = health_check(_cfg(workspace), _run(), tmp_path / "i3", runner=version_hangs)
    assert len(problems) == 1 and "timed out" in problems[0]
    assert [c.argv[2] for c in version_hangs.calls] == ["--help"]


def test_no_product_and_no_thread_skip_steps_two_and_three(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = FakeLauncher()
    work_dir = tmp_path / "incident"
    assert health_check(_cfg(workspace), _run("", ""), work_dir, runner=launcher) == []
    assert [c.argv for c in launcher.calls] == [["sh", "scio.sh", "--help"]]
    notes = json.loads((work_dir / "health" / "health.json").read_text(encoding="utf-8"))
    skipped = {n["step"]: n["skipped"] for n in notes["steps"] if "skipped" in n}
    assert set(skipped) == {"status", "report"}


def test_missing_databases_skip_the_report_step_without_a_problem(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = FakeLauncher()
    work_dir = tmp_path / "incident"
    assert health_check(_cfg(workspace), _run(), work_dir, runner=launcher) == []
    assert [c.argv[2] for c in launcher.calls] == ["--help", "status"]
    notes = json.loads((work_dir / "health" / "health.json").read_text(encoding="utf-8"))
    assert any("no checkpoint database" in n.get("skipped", "") for n in notes["steps"])
    assert not (workspace / ".scio").exists()
