"""The supervised run: lease, launch, follow, hang detection and the exit record.
OWNER: wave-1 agent A."""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from recovery import procs
from recovery.commands import Runner, kill_tree, run_command
from recovery.config import HOME_ENV, WorkspaceConfig, recorded_env
from recovery.model import ProcessKey, RunRecord, RunStatus
from recovery.store import Store

OUTCOME_SUBCOMMANDS: tuple[str, ...] = ("build", "new", "run", "resume")
"""SCIO subcommands that accept ``--outcome-file``. The supervisor appends it only to these."""

OUTCOME_FLAG = "--outcome-file"


def outcome_flag_supported(live_wt: Path) -> bool:
    """Whether the SCIO that live-wt holds knows ``--outcome-file`` (ADR 0019 decision 11).

    A SCIO older than the flag refuses it with exit 2 before doing anything (I-187). So
    the flag is appended only when ``<live_wt>/scio/cli.py`` names it. Without it the
    supervisor reads the run's text instead, the same fallback it uses when a broken SCIO
    writes no outcome file. This reads a file and starts no process, because the check
    runs before every launch."""
    try:
        return OUTCOME_FLAG in (live_wt / "scio" / "cli.py").read_text(encoding="utf-8")
    except OSError:
        return False


PRODUCT_FLAGS: tuple[str, ...] = ("--product", "--dir")
"""The SCIO flags that name the product directory (``build``/``status`` and ``new``)."""

KILL_WAIT_S = 60.0
"""How long to wait for a killed child before killing it directly."""


def new_run_id(clock: Callable[[], float] = time.time) -> str:
    """``run-<UTC yyyymmddHHMMSS>-<6 hex>``."""
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(clock()))
    return f"run-{stamp}-{secrets.token_hex(3)}"


def progress_time(workspace: Path, run: RunRecord) -> float:
    """The newest modification time among the workspace's ``.scio/ledger/*.jsonl`` and
    ``.scio/checkpoints.sqlite-wal`` and the run's own output files. 0.0 when none exist."""
    scio = workspace / ".scio"
    paths = [*(scio / "ledger").glob("*.jsonl"), scio / "checkpoints.sqlite-wal"]
    paths += [Path(p) for p in (run.stdout_path, run.stderr_path) if p]
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def product_dir(workspace: Path, argv: Sequence[str]) -> Path | None:
    """The product directory named by ``--product X`` or ``--dir X`` (or ``--flag=X``),
    resolved against the workspace, the launcher's working directory."""
    for i, arg in enumerate(argv):
        value: str | None = None
        if arg in PRODUCT_FLAGS and i + 1 < len(argv):
            value = argv[i + 1]
        else:
            flag, eq, rest = arg.partition("=")
            if eq and flag in PRODUCT_FLAGS:
                value = rest
        if value:
            path = Path(value)
            return path if path.is_absolute() else workspace / path
    return None


def build_thread(product: Path | None) -> str:
    """``build_thread`` from ``<product>/.scio/product/progress.json``, or ""."""
    if product is None:
        return ""
    try:
        data = json.loads(
            (product / ".scio" / "product" / "progress.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return ""
    value = data.get("build_thread") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _live_wt_head(cfg: WorkspaceConfig, git: Runner) -> str:
    result = git(["git", "-C", str(cfg.live_wt), "rev-parse", "HEAD"], timeout_s=60)
    return result.stdout.strip() if result.ok else ""


def _wait(popen: subprocess.Popen[bytes]) -> int:
    try:
        return popen.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        popen.kill()
        return popen.wait()


def supervise(
    cfg: WorkspaceConfig,
    store: Store,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    home: Path,
    incident_id: str | None = None,
    resumed_from: str | None = None,
    echo: bool = True,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    spawn: Callable[..., subprocess.Popen[bytes]] = procs.spawn,
    alive: Callable[[ProcessKey], bool] = procs.alive,
    kill: Callable[[int], None] = kill_tree,
    git: Runner = run_command,
) -> RunRecord:
    """Run ``cfg.launcher + argv`` in ``cfg.workspace`` as this process's child and return
    its finished record.

    - **Lease.** Takes the workspace lease (``cfg.lease_name``). If a live process holds
      it, the record is saved as ``REFUSED`` and returned without starting anything.
    - **Run record.** Records the run before starting it: the ``scio_sha`` (live-wt's
      HEAD), ``product_dir`` (from ``--product`` or ``--dir``), the build thread (from
      ``progress.json`` when present), ``run_dir`` under ``home/runs/<run_id>``, and the
      stdout, stderr and outcome paths.
    - **Outcome file.** Appends ``--outcome-file <path>`` when ``argv[0]`` is in
      ``OUTCOME_SUBCOMMANDS``.
    - **While running.** Every ``cfg.limits.poll_s`` seconds it echoes new output to its
      own stdout and stderr (when ``echo``), updates ``last_progress_at`` from
      :func:`progress_time`, and kills the tree (status ``KILLED_HANG``) after
      ``cfg.limits.hang_after_s`` with no progress.
    - **At exit.** Records the exit code, ``ended_at`` and status ``EXITED``, then releases
      the lease.

    The child gets ``env`` whole; the record keeps only ``config.recorded_env(env)``. A
    launcher that cannot start is recorded as exit 127, with the reason in stderr.log. A
    ``KeyboardInterrupt`` kills the tree, records the exit, releases the lease and is
    re-raised. The build thread is read again at exit, when ``progress.json`` names one.
    """
    args = list(argv)
    me = procs.self_key()
    run_id = new_run_id(clock)
    run_dir = home / "runs" / run_id
    product = product_dir(cfg.workspace, args)
    run = RunRecord(
        run_id=run_id,
        workspace=str(cfg.workspace),
        launcher=list(cfg.launcher),
        argv=args,
        env=recorded_env(env),
        started_at=clock(),
        scio_sha=_live_wt_head(cfg, git),
        product_dir=str(product) if product is not None else "",
        build_thread=build_thread(product),
        supervisor_pid=me.pid,
        supervisor_created=me.created,
        run_dir=str(run_dir),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        outcome_path=str(run_dir / "outcome.json"),
        incident_id=incident_id,
        resumed_from=resumed_from,
    )
    if not store.acquire_lease(cfg.lease_name, me, alive, note=run_id):
        run.status = RunStatus.REFUSED
        run.ended_at = clock()
        store.save_run(run)
        return run
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        store.save_run(run)
        return _supervise_child(
            cfg,
            store,
            run,
            product,
            env=env,
            echo=echo,
            clock=clock,
            sleep=sleep,
            spawn=spawn,
            kill=kill,
        )
    finally:
        store.release_lease(cfg.lease_name, me)


def _supervise_child(
    cfg: WorkspaceConfig,
    store: Store,
    run: RunRecord,
    product: Path | None,
    *,
    env: Mapping[str, str],
    echo: bool,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    spawn: Callable[..., subprocess.Popen[bytes]],
    kill: Callable[[int], None],
) -> RunRecord:
    command = list(cfg.launcher) + run.argv
    if run.argv and run.argv[0] in OUTCOME_SUBCOMMANDS and outcome_flag_supported(cfg.live_wt):
        command += ["--outcome-file", run.outcome_path]
    stdout_path, stderr_path = Path(run.stdout_path), Path(run.stderr_path)
    streams = [(stdout_path, sys.stdout), (stderr_path, sys.stderr)]
    offsets = [0, 0]
    limits = cfg.limits
    popen: subprocess.Popen[bytes] | None = None
    try:
        try:
            popen = spawn(
                command,
                cwd=cfg.workspace,
                env=env,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )
        except OSError as exc:
            with stderr_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"recovery: cannot start {command[0]}: {exc}\n")
            if echo:
                procs.follow(streams, offsets)
            run.exit_code = 127
            run.ended_at = clock()
            run.status = RunStatus.EXITED
            store.save_run(run)
            return run
        key = procs.process_key(popen.pid)
        run.pid = popen.pid
        run.pid_created = key.created if key is not None else 0.0
        store.save_run(run)
        while True:
            code = popen.poll()
            if echo:
                offsets = procs.follow(streams, offsets)
            if code is not None:
                break
            progress = progress_time(cfg.workspace, run)
            if progress > run.last_progress_at:
                run.last_progress_at = progress
                store.save_run(run)
            if clock() - max(run.last_progress_at, run.started_at) > limits.hang_after_s:
                kill(popen.pid)
                code = _wait(popen)
                run.status = RunStatus.KILLED_HANG
                break
            sleep(limits.poll_s)
    except KeyboardInterrupt:
        if popen is not None:
            kill(popen.pid)
            run.exit_code = _wait(popen)
        run.ended_at = clock()
        run.status = RunStatus.EXITED
        store.save_run(run)
        raise
    if echo:
        procs.follow(streams, offsets)
    run.exit_code = code
    run.ended_at = clock()
    if run.status is RunStatus.RUNNING:
        run.status = RunStatus.EXITED
    run.last_progress_at = max(run.last_progress_at, progress_time(cfg.workspace, run))
    run.build_thread = build_thread(product) or run.build_thread
    store.save_run(run)
    return run


def find_orphans(
    store: Store, *, alive: Callable[[ProcessKey], bool] = procs.alive
) -> list[RunRecord]:
    """Runs still ``RUNNING`` whose child process and supervisor process are both gone.

    Each one is marked ``ORPHANED``, saved and returned. A run whose supervisor died
    while its child still runs is left alone: the child may finish on its own, and a
    later tick looks again.
    """
    orphans: list[RunRecord] = []
    for run in store.runs(status=RunStatus.RUNNING):
        supervisor = ProcessKey(run.supervisor_pid, run.supervisor_created)
        if alive(run.process) or alive(supervisor):
            continue
        run.status = RunStatus.ORPHANED
        store.save_run(run)
        orphans.append(run)
    return orphans


def replay_argv(run: RunRecord, incident_id: str, *, python: str = sys.executable) -> list[str]:
    """The argv a tick starts to resume ``run`` under a fresh supervisor:

    ``[python, "-m", "recovery", "run", "--workspace", run.workspace,
    "--incident", incident_id, "--resumed-from", run.run_id, "--", *run.argv]``
    """
    return [
        python,
        "-m",
        "recovery",
        "run",
        "--workspace",
        run.workspace,
        "--incident",
        incident_id,
        "--resumed-from",
        run.run_id,
        "--",
        *run.argv,
    ]


def start_replay(
    run: RunRecord,
    incident_id: str,
    *,
    home: Path,
    package_root: Path,
    spawn_detached: Callable[..., int] = procs.spawn_detached,
) -> int:
    """Start :func:`replay_argv` detached, with ``cwd=package_root`` (the directory that
    holds ``recovery/``). The environment is ``run.env`` plus ``SCIO_RECOVERY_HOME=home``,
    and the log goes to ``home/replays/<incident_id>.log``. Returns the PID."""
    env = dict(run.env)
    env[HOME_ENV] = str(home)
    return spawn_detached(
        replay_argv(run, incident_id),
        cwd=package_root,
        env=env,
        log_path=home / "replays" / f"{incident_id}.log",
    )
