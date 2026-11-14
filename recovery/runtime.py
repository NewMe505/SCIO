"""Updating the SCIO runtime (``.scio/live-wt``) after a merged repair, and the health check
before a resume (ADR 0019 decision 8). OWNER: wave-1 agent E.

live-wt is a detached worktree, so its ``.git`` is a file: every git call here goes through
``git -C <live_wt>`` and no ``.git/...`` path is ever built by hand (guardrail 57). A target
is resolved to its full SHA before any comparison (I-172).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from recovery import procs
from recovery.commands import CommandResult, Runner, run_command
from recovery.config import WorkspaceConfig
from recovery.model import RunRecord

GIT_TIMEOUT_S = 120.0
"""A local git command (rev-parse, status, checkout, diff)."""
FETCH_TIMEOUT_S = 300.0
"""``git fetch origin`` goes over the network."""
HEALTH_TIMEOUT_S = 300.0
"""One launcher call of the health check: ``uv run`` plus SCIO's imports are slow here."""

SMOKE_ARGS: tuple[str, ...] = ("--help",)
"""Health step 1. It imports SCIO's CLI and parses arguments; SCIO has no ``--version``
(I-184)."""

CHECKPOINT_DSN_ENV = "SCIO_CHECKPOINT_DSN"
EFFECTS_PATH_ENV = "SCIO_EFFECTS_PATH"
DEFAULT_CHECKPOINT_DSN = "sqlite:///.scio/checkpoints.sqlite"
DEFAULT_EFFECTS_PATH = ".scio/effects.sqlite"
"""SCIO's defaults (``scio/settings.py``), relative to the launcher's working directory."""
SQLITE_DSN_PREFIX = "sqlite:///"

HEALTH_DIR = "health"
HEALTH_NOTES = "health.json"
"""``work_dir/health/health.json``: every step's argv, result or reason for a skip."""

_REV = re.compile(r"[0-9a-fA-F]{4,64}")
"""A commit SHA, full or abbreviated. Anything else (a ref name, an option) is refused."""
_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class RuntimeRefused(Exception):
    """live-wt cannot move now.

    ``retryable`` is True when waiting may fix it (a SCIO process is running, CI is
    pending) and False when only a human can fix it (a dirty tree, a target that is not
    on ``origin/main``).
    """

    def __init__(self, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


def _git(git: Runner, path: Path, *args: str, timeout_s: float = GIT_TIMEOUT_S) -> CommandResult:
    return git(["git", "-C", str(path), *args], timeout_s=timeout_s)


def _why(result: CommandResult) -> str:
    """A short, human-readable account of a failed command."""
    if result.timed_out:
        return "timed out"
    text = result.stderr.strip() or result.stdout.strip()
    last = text.splitlines()[-1].strip() if text else ""
    return f"exit {result.code}" + (f": {last[:300]}" if last else "")


def head(path: Path, *, git: Runner = run_command) -> str:
    """``git -C <path> rev-parse HEAD``. Raises ``RuntimeError`` on failure."""
    result = _git(git, path, "rev-parse", "HEAD")
    sha = result.stdout.strip()
    if not result.ok or not _FULL_SHA.fullmatch(sha):
        raise RuntimeError(f"git rev-parse HEAD in {path} failed ({_why(result)})")
    return sha


def _resolve(path: Path, rev: str, git: Runner) -> str | None:
    """The full SHA of the commit ``rev`` names in ``path``, or None when git cannot
    resolve it (unknown, ambiguous, or not a commit)."""
    result = _git(git, path, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    sha = result.stdout.strip()
    return sha if result.ok and _FULL_SHA.fullmatch(sha) else None


def _head_or_none(path: Path, git: Runner) -> str | None:
    try:
        return head(path, git=git)
    except RuntimeError:
        return None


def move_live_wt(
    cfg: WorkspaceConfig,
    target_sha: str,
    *,
    git: Runner = run_command,
    scio_running: Callable[[], list[int]] = procs.scio_processes,
) -> str:
    """Move live-wt to ``target_sha`` and return the SHA it was on.

    1. Refuse (retryable) while ``scio_running()`` is non-empty.
    2. Refuse (not retryable) when ``git -C <live_wt> status --porcelain`` is non-empty.
    3. ``git -C <live_wt> fetch origin``.
    4. Refuse (not retryable) unless ``git merge-base --is-ancestor <target> origin/main``.
    5. ``git -C <live_wt> checkout --detach <target>``.
    6. Probe: ``rev-parse HEAD`` equals ``target_sha``.

    If live-wt is already at ``target_sha``, it returns ``target_sha`` and changes nothing.

    Details:
    - Every SHA returned is the full one; a short ``target_sha`` is resolved by git first
      and never compared as text (I-172).
    - "Already at the target" is checked first, before step 1: changing nothing needs no
      precondition, so a retry after the move succeeded never waits on a running SCIO.
    - A failed fetch is retryable (the network may come back). A live-wt that git cannot
      read, a target that is not a SHA or not a commit, and an ancestry check that fails
      are not.
    - A checkout or probe that fails puts live-wt back on the SHA it was on, then refuses
      (not retryable); the reason says whether that restore worked.
    """
    wt = cfg.live_wt
    if not _REV.fullmatch(target_sha):
        raise RuntimeRefused(f"target {target_sha!r} is not a commit SHA", retryable=False)
    try:
        previous = head(wt, git=git)
    except RuntimeError as exc:
        raise RuntimeRefused(f"live-wt {wt} cannot be read: {exc}", retryable=False) from exc
    if _resolve(wt, target_sha, git) == previous:
        return previous

    try:
        running = scio_running()
    except RuntimeError as exc:  # the process list could not be read: not the same as none
        raise RuntimeRefused(
            f"cannot tell whether SCIO is running ({exc}); live-wt stays where it is",
            retryable=True,
        ) from exc
    if running:
        pids = ", ".join(str(pid) for pid in running)
        raise RuntimeRefused(
            f"SCIO is running (pid {pids}); live-wt moves only when no SCIO process runs",
            retryable=True,
        )
    status = _git(git, wt, "status", "--porcelain")
    if not status.ok:
        raise RuntimeRefused(f"git status in live-wt {wt} failed ({_why(status)})", retryable=False)
    if status.stdout.strip():
        changes = "; ".join(status.stdout.strip().splitlines()[:5])
        raise RuntimeRefused(f"live-wt {wt} has local changes: {changes}", retryable=False)
    fetch = _git(git, wt, "fetch", "origin", timeout_s=FETCH_TIMEOUT_S)
    if not fetch.ok:
        raise RuntimeRefused(f"git fetch origin in live-wt failed ({_why(fetch)})", retryable=True)
    target = _resolve(wt, target_sha, git)
    if target is None:
        raise RuntimeRefused(
            f"{target_sha} is not a commit in live-wt after fetching origin", retryable=False
        )
    if target == previous:
        return previous
    ancestor = _git(git, wt, "merge-base", "--is-ancestor", target, "origin/main")
    if ancestor.code == 1 and not ancestor.timed_out:
        raise RuntimeRefused(f"{target} is not on origin/main", retryable=False)
    if not ancestor.ok:
        raise RuntimeRefused(
            f"git merge-base --is-ancestor {target} origin/main failed ({_why(ancestor)})",
            retryable=False,
        )

    checkout = _git(git, wt, "checkout", "--detach", target)
    now = _head_or_none(wt, git)
    if checkout.ok and now == target:
        return previous
    failure = (
        f"git checkout --detach {target} failed ({_why(checkout)})"
        if not checkout.ok
        else f"after git checkout --detach {target}, HEAD is {now or 'unreadable'}"
    )
    restored = now == previous or _restore(wt, previous, git)
    where = "is back at" if restored else "could NOT be put back on"
    raise RuntimeRefused(f"{failure}; live-wt {where} {previous}", retryable=False)


def _restore(wt: Path, sha: str, git: Runner) -> bool:
    """Best effort: ``checkout --detach <sha>``, probed. Never raises."""
    checkout = _git(git, wt, "checkout", "--detach", sha)
    return checkout.ok and _head_or_none(wt, git) == sha


def rollback_live_wt(cfg: WorkspaceConfig, sha: str, *, git: Runner = run_command) -> None:
    """``git -C <live_wt> checkout --detach <sha>``, probed. Raises ``RuntimeError`` if the
    probe fails.

    ``sha`` may be short: the probe compares ``rev-parse HEAD`` with git's full SHA for it.
    It also raises when ``sha`` is not a SHA or the checkout fails.
    """
    wt = cfg.live_wt
    if not _REV.fullmatch(sha):
        raise RuntimeError(f"rollback target {sha!r} is not a commit SHA")
    checkout = _git(git, wt, "checkout", "--detach", sha)
    if not checkout.ok:
        raise RuntimeError(f"git checkout --detach {sha} in {wt} failed ({_why(checkout)})")
    want = _resolve(wt, sha, git)
    now = _head_or_none(wt, git)
    if want is None or now != want:
        raise RuntimeError(
            f"rollback probe failed in {wt}: HEAD is {now or 'unreadable'}, expected {want or sha}"
        )


def lock_changed(
    cfg: WorkspaceConfig, old_sha: str, new_sha: str, *, git: Runner = run_command
) -> bool:
    """True when ``uv.lock`` differs between the two SHAs (``git diff --quiet old new --
    uv.lock`` in live-wt). Decision 12.5: such a resume is refused.

    Raises ``RuntimeError`` when git cannot answer (an unknown SHA, a timeout): an unknown
    lock never counts as unchanged.
    """
    for rev in (old_sha, new_sha):
        if not _REV.fullmatch(rev):
            raise RuntimeError(f"{rev!r} is not a commit SHA")
    result = _git(git, cfg.live_wt, "diff", "--quiet", old_sha, new_sha, "--", "uv.lock")
    if not result.timed_out and result.code == 0:
        return False
    if not result.timed_out and result.code == 1:
        return True
    raise RuntimeError(
        f"git diff --quiet {old_sha} {new_sha} -- uv.lock in {cfg.live_wt} failed ({_why(result)})"
    )


def _env_get(env: Mapping[str, str], name: str) -> str | None:
    """``env[name]`` matched without case, as Windows and SCIO's settings read it."""
    for key, value in env.items():
        if key.upper() == name:
            return value
    return None


def _env_set(env: dict[str, str], name: str, value: str) -> None:
    """Set ``name``, dropping any spelling of it in another case first."""
    for key in [k for k in env if k.upper() == name]:
        del env[key]
    env[name] = value


def _in_workspace(cfg: WorkspaceConfig, path: Path) -> Path:
    return path if path.is_absolute() else cfg.workspace / path


def live_databases(cfg: WorkspaceConfig, env: Mapping[str, str]) -> tuple[Path | None, Path]:
    """The workspace's checkpoint and effect databases as SCIO, started from the workspace
    with ``env``, would open them. The checkpoint path is None when its DSN is not
    ``sqlite:///`` (nothing to copy)."""
    dsn = _env_get(env, CHECKPOINT_DSN_ENV) or DEFAULT_CHECKPOINT_DSN
    effects = _in_workspace(cfg, Path(_env_get(env, EFFECTS_PATH_ENV) or DEFAULT_EFFECTS_PATH))
    if not dsn.startswith(SQLITE_DSN_PREFIX):
        return None, effects
    return _in_workspace(cfg, Path(dsn.removeprefix(SQLITE_DSN_PREFIX))), effects


def _remove_sqlite(path: Path) -> None:
    for suffix in ("", "-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def _backup(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` with ``sqlite3.Connection.backup``.

    The source is opened read-only (``mode=ro``): a read-write connection that closes last
    checkpoints the WAL into the original, which would write it. The backup reads the WAL
    a crashed SCIO left, so the copy holds every committed transaction.
    """
    _remove_sqlite(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def health_check(
    cfg: WorkspaceConfig,
    run: RunRecord,
    work_dir: Path,
    *,
    runner: Runner = run_command,
) -> list[str]:
    """Problems that block a resume. An empty list means healthy.

    It runs the workspace launcher, in the workspace, with ``run.env``:
    1. ``<launcher> --help`` (``SMOKE_ARGS``) exits 0: SCIO's CLI imports and parses.
       SCIO has no ``--version`` (I-184). ``tests/unit/test_recovery_contract.py`` checks
       every step's arguments against SCIO's own parser.
    2. ``<launcher> status --product <run.product_dir>`` exits 0, when there is a product.
    3. ``<launcher> report --thread <run.build_thread>`` exits 0 against copies of the
       workspace's ``checkpoints.sqlite`` and ``effects.sqlite``, made under ``work_dir``
       with ``sqlite3`` backup. The copies are used through ``SCIO_CHECKPOINT_DSN`` and
       ``SCIO_EFFECTS_PATH`` (guardrail 14). The live databases are never opened by the
       new code before the resume.

    Details:
    - Every call has ``cwd=cfg.workspace``, ``env=dict(run.env)`` and a timeout of
      ``HEALTH_TIMEOUT_S``. A call that times out is a problem, and the steps after it are
      skipped so that one hang cannot cost three timeouts.
    - Step 3 adds ``--out <work_dir>/health/reports``, so the report is not written into
      the workspace either. Its databases are found the way SCIO finds them: the DSN and
      path in ``run.env``, else SCIO's defaults, relative to the workspace.
    - Step 3 is skipped (not failed) when the run has no build thread, when the checkpoint
      DSN is not ``sqlite:///``, or when the checkpoint database does not exist. A missing
      effects database alone still runs, against an empty path under ``work_dir``.
    - Each step's argv, result or reason for a skip goes to
      ``work_dir/health/health.json``; its stdout and stderr go to
      ``work_dir/health/<step>.stdout.txt`` and ``.stderr.txt``.
    """
    health = work_dir / HEALTH_DIR
    health.mkdir(parents=True, exist_ok=True)
    base_env = dict(run.env)
    problems: list[str] = []
    notes: list[dict[str, Any]] = []
    hung = False

    def skip(name: str, reason: str) -> None:
        notes.append({"step": name, "skipped": reason})

    def step(name: str, args: list[str], env: dict[str, str]) -> None:
        nonlocal hung
        if hung:
            skip(name, "an earlier step timed out")
            return
        argv = [*cfg.launcher, *args]
        result = runner(argv, cwd=cfg.workspace, env=env, timeout_s=HEALTH_TIMEOUT_S)
        for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            (health / f"{name}.{stream}.txt").write_text(text, encoding="utf-8", newline="\n")
        notes.append(
            {"step": name, "argv": argv, "code": result.code, "timed_out": result.timed_out}
        )
        shown = " ".join(argv)
        if result.timed_out:
            hung = True
            problems.append(
                f"health check {name}: `{shown}` timed out after {HEALTH_TIMEOUT_S:.0f} s "
                f"(output in {health})"
            )
        elif result.code != 0:
            problems.append(
                f"health check {name}: `{shown}` failed ({_why(result)}; output in {health})"
            )

    step("cli", list(SMOKE_ARGS), base_env)

    if run.product_dir:
        step("status", ["status", "--product", run.product_dir], base_env)
    else:
        skip("status", "the run has no product directory")

    if not run.build_thread:
        skip("report", "the run has no build thread")
    else:
        _report_step(cfg, run, health, base_env, step, skip, problems)

    (health / HEALTH_NOTES).write_text(
        json.dumps({"steps": notes, "problems": problems}, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return problems


def _report_step(
    cfg: WorkspaceConfig,
    run: RunRecord,
    health: Path,
    base_env: dict[str, str],
    step: Callable[[str, list[str], dict[str, str]], None],
    skip: Callable[[str, str], None],
    problems: list[str],
) -> None:
    """Step 3: copy the databases under ``health`` and run ``report`` against the copies."""
    checkpoints, effects = live_databases(cfg, base_env)
    if checkpoints is None:
        dsn = _env_get(base_env, CHECKPOINT_DSN_ENV)
        skip("report", f"the checkpoint DSN {dsn!r} is not sqlite:///, so it cannot be copied")
        return
    if not checkpoints.is_file():
        skip("report", f"no checkpoint database at {checkpoints}")
        return
    checkpoints_copy = health / "checkpoints.sqlite"
    effects_copy = health / "effects.sqlite"
    try:
        _backup(checkpoints, checkpoints_copy)
        if effects.is_file():
            _backup(effects, effects_copy)
        else:
            _remove_sqlite(effects_copy)
    except sqlite3.Error as exc:
        problems.append(f"health check report: cannot copy the workspace databases ({exc})")
        return
    env = dict(base_env)
    _env_set(env, CHECKPOINT_DSN_ENV, SQLITE_DSN_PREFIX + checkpoints_copy.resolve().as_posix())
    _env_set(env, EFFECTS_PATH_ENV, effects_copy.resolve().as_posix())
    out = (health / "reports").resolve().as_posix()
    step("report", ["report", "--thread", run.build_thread, "--out", out], env)
