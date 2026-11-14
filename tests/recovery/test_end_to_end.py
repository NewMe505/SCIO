"""ADR 0019 section 13: the whole recovery loop, offline, with real processes and real git.

A stand-in SCIO (a launcher plus a ``scio`` package in a real git origin, clone and
live-wt worktree) runs a three-step build. Step S2 raises the I-180 shape (a
``subprocess.TimeoutExpired`` out of ``scio/stage.py``). Then the supervisor:
1. detects the crash and classifies it;
2. snapshots the workspace and opens an issue (on ``FakeGh``);
3. starts a repair session. Here the session is a stand-in that commits a real fix and
   pushes it to the origin.
4. checks the claim, then waits for the merge. The test merges for real into the
   origin's ``main``, as the owner would.
5. moves live-wt to the merge, passes the health check through the real launcher, and
   replays the original command.

The build then finishes from its checkpoint. Every step's side effect happens exactly
once, and nothing is opened twice.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from recovery import machine, runner
from recovery.commands import CommandResult, run_command
from recovery.config import Limits, WorkspaceConfig
from recovery.github import GitHub
from recovery.machine import Deps
from recovery.model import FailureClass, IncidentState, RunRecord, RunStatus
from recovery.store import Store
from tests.recovery.test_github import REPO, FakeGh

THREAD = "build-p-m1"
STEPS = ("S1", "S2", "S3")

BUGGY_STAGE = (
    "import subprocess\n\n\n"
    "def run(step):\n"
    "    if step == 'S2':\n"
    "        raise subprocess.TimeoutExpired(['claude', '-p'], 600)\n"
)
FIXED_STAGE = "def run(step):\n    return None\n"

LAUNCHER = """\
import json, os, sqlite3, sys, traceback
from pathlib import Path

LIVE_WT = Path({live_wt!r})
WS = Path.cwd()
args = sys.argv[1:]
outcome = None
if "--outcome-file" in args:
    i = args.index("--outcome-file")
    outcome = Path(args[i + 1])
    del args[i : i + 2]


def write_outcome(status, code, exc=None):
    if outcome is None:
        return
    data = dict(version=1, command=args[0] if args else "", status=status, exit_code=code,
                thread="{thread}", exit_reason=None, reason=None, resume=None, exception=exc)
    outcome.write_text(json.dumps(data), encoding="utf-8")


if args == ["--help"]:
    print("usage: scio")
    sys.exit(0)
if args[:1] == ["status"]:
    print("status: ok")
    sys.exit(0)
if args[:1] == ["report"]:
    path = os.environ["SCIO_CHECKPOINT_DSN"].removeprefix("sqlite:///")
    with sqlite3.connect(path) as conn:
        conn.execute("SELECT COUNT(*) FROM done").fetchone()
    sys.exit(0)
if args[:1] != ["build"]:
    sys.exit(2)
sys.path.insert(0, str(LIVE_WT))
import scio.stage as stage

product = Path(args[args.index("--product") + 1])
db = sqlite3.connect(WS / ".scio" / "checkpoints.sqlite")
db.execute("CREATE TABLE IF NOT EXISTS done (step TEXT PRIMARY KEY)")
done = {{row[0] for row in db.execute("SELECT step FROM done")}}
ledger = WS / ".scio" / "ledger" / "{thread}.jsonl"
ledger.parent.mkdir(parents=True, exist_ok=True)
try:
    for step in {steps!r}:
        if step in done:
            continue
        stage.run(step)
        with open(product / "work.log", "a", encoding="utf-8") as fh:
            fh.write(step + "\\n")
        db.execute("INSERT INTO done VALUES (?)", (step,))
        db.commit()
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({{"step": step}}) + "\\n")
except BaseException as exc:
    frames = [dict(path=f.filename, line=f.lineno, function=f.name)
              for f in traceback.extract_tb(exc.__traceback__)]
    kind = type(exc).__module__ + "." + type(exc).__qualname__
    write_outcome("crashed", 1, dict(type=kind, message=str(exc), frames=frames))
    raise
finally:
    db.close()
print("build thread {thread} phase=done exit=None")
write_outcome("done", 0)
"""


def git(cwd: Path, *args: str) -> str:
    """Git with an identity (the root conftest removes the machine's)."""
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@dataclass
class World:
    tmp: Path
    origin: Path
    repo: Path
    live_wt: Path
    workspace: Path
    product: Path
    cfg: WorkspaceConfig
    gh: FakeGh
    store: Store
    home: Path
    replays: list[tuple[RunRecord, str]] = field(default_factory=list)
    repair_sessions: list[Path] = field(default_factory=list)

    def work_log(self) -> list[str]:
        return (self.product / "work.log").read_text(encoding="utf-8").split()


def build_world(tmp: Path, home: Path) -> World:
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / "scio").mkdir()
    (seed / "scio" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (seed / "scio" / "stage.py").write_text(BUGGY_STAGE, encoding="utf-8", newline="\n")
    (seed / "docs").mkdir()
    (seed / "docs" / "ISSUE_LEDGER.md").write_text("# ledger\n", encoding="utf-8", newline="\n")
    (seed / ".gitignore").write_text("__pycache__/\n", encoding="utf-8", newline="\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main")
    repo = tmp / "SCIOREPO"
    git(tmp, "clone", str(origin), str(repo))
    live_wt = repo / ".scio" / "live-wt"
    git(repo, "worktree", "add", "--detach", str(live_wt), "main")

    workspace = tmp / "ws"
    (workspace / ".scio").mkdir(parents=True)
    product = tmp / "product"
    (product / ".scio" / "product").mkdir(parents=True)
    progress = {"build_thread": THREAD}
    (product / ".scio" / "product" / "progress.json").write_text(
        json.dumps(progress), encoding="utf-8", newline="\n"
    )
    launcher = workspace / "launcher.py"
    text = LAUNCHER.format(live_wt=str(live_wt), thread=THREAD, steps=STEPS)
    launcher.write_text(text, encoding="utf-8", newline="\n")
    cfg = WorkspaceConfig(
        workspace=workspace,
        launcher=(sys.executable, str(launcher)),
        scio_repo=repo,
        scio_gh_repo=REPO,
        live_wt=live_wt,
        limits=Limits(poll_s=0.1, hang_after_s=120.0, backoff_s=(0.0, 0.0, 0.0)),
    )
    store = Store.at_home(home)
    return World(tmp, origin, repo, live_wt, workspace, product, cfg, FakeGh(), store, home)


def fake_claude(world: World) -> Callable[..., CommandResult]:
    """The repair session: in its worktree it fixes ``scio/stage.py``, adds a regression
    test and a ledger entry, commits, pushes the branch to the origin, and opens the PR.
    This is what ``claude -p`` would do under SCIO's CLAUDE.md."""

    def run(
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        assert cwd is not None and stdin is not None
        wt = Path(cwd)
        world.repair_sessions.append(wt)
        assert "--safe-mode" not in argv and "auto" in argv
        branch = git(wt, "branch", "--show-current")
        issue = int(branch.split("/")[1].split("-")[0])
        (wt / "scio" / "stage.py").write_text(FIXED_STAGE, encoding="utf-8", newline="\n")
        (wt / "tests").mkdir(exist_ok=True)
        (wt / "tests" / "test_stage.py").write_text(
            "def test_s2_does_not_raise():\n    pass\n", encoding="utf-8", newline="\n"
        )
        with open(wt / "docs" / "ISSUE_LEDGER.md", "a", encoding="utf-8", newline="\n") as fh:
            fh.write("### I-180 · a hung claude\n")
        git(wt, "add", "-A")
        git(wt, "commit", "-m", f"fix: a timeout is retryable (#{issue})")
        git(wt, "push", "-u", "origin", branch)
        files = ["scio/stage.py", "tests/test_stage.py", "docs/ISSUE_LEDGER.md"]
        pr = world.gh.add_pr(branch, body=f"Fixes the hang.\n\nCloses #{issue}", files=files)
        world.gh.add_run(world.gh.prs[pr]["headRefOid"])
        answer = {
            "verdict": "repaired",
            "pr_number": pr,
            "branch": branch,
            "ledger_id": "I-180",
            "root_cause": "TimeoutExpired was not a HarnessError",
            "regression_tests": ["tests/test_stage.py::test_s2_does_not_raise"],
            "gate": "passed",
            "risks": [],
            "note": "",
        }
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": answer,
            "num_turns": 12,
            "total_cost_usd": 1.5,
            "duration_ms": 90000,
            "session_id": "sess-e2e",
        }
        return CommandResult(0, json.dumps(envelope), "")

    return run


def ok_uv(argv: Sequence[str], **_: object) -> CommandResult:
    return CommandResult(0, "", "")


def make_deps(world: World, claude: Callable[..., CommandResult] | None = None) -> Deps:
    def start_replay(run: RunRecord, incident_id: str) -> int:
        world.replays.append((run, incident_id))
        return 4242

    return Deps(
        store=world.store,
        home=world.home,
        package_root=Path(__file__).resolve().parents[2],
        config_for=lambda _workspace: world.cfg,
        git=run_command,
        uv=ok_uv,
        claude=claude or fake_claude(world),
        launcher=run_command,
        github_for=lambda repo: GitHub(repo, runner=world.gh),
        scio_running=lambda: [],
        start_replay=start_replay,
        clock=time.time,
    )


def supervise(world: World, **kw: object) -> RunRecord:
    argv = ["build", "--product", str(world.product), "--gh", "o/product"]
    return runner.supervise(
        world.cfg, world.store, argv, env=dict(os.environ), home=world.home, echo=False, **kw
    )  # type: ignore[arg-type]


def merge(world: World, pr: int) -> str:
    """The owner's merge: the fix branch goes into the origin's ``main`` for real."""
    branch = world.gh.prs[pr]["headRefName"]
    git(world.repo, "fetch", "origin")
    git(world.repo, "checkout", "-q", "main")
    git(world.repo, "merge", "--no-ff", "-m", f"Merge pull request #{pr}", f"origin/{branch}")
    git(world.repo, "push", "origin", "main")
    sha = git(world.repo, "rev-parse", "HEAD")
    world.gh.prs[pr].update(
        state="MERGED", mergedAt="2026-11-14T12:00:00Z", mergeCommit={"oid": sha}
    )
    world.gh.add_run(sha)
    return sha


@pytest.fixture
def world(tmp_path: Path, recovery_home: Path) -> World:
    return build_world(tmp_path, recovery_home)


def test_a_harness_crash_is_repaired_merged_and_the_build_resumes_without_duplicates(
    world: World,
) -> None:
    deps = make_deps(world)
    first = supervise(world)
    assert first.status is RunStatus.EXITED and first.exit_code == 1
    assert world.work_log() == ["S1"]  # S1 done and checkpointed, S2 crashed

    incident = machine.on_run_finished(first, deps)
    assert incident is not None and incident.open
    assert incident.failure_class is FailureClass.HARNESS_SUSPECT
    assert incident.innermost_scio_frame == "scio/stage.py:run"

    incident = machine.drive(incident.incident_id, deps)
    assert incident.state is IncidentState.AWAITING_MERGE, incident.escalation
    assert incident.issue_number is not None and incident.pr_number is not None
    assert Path(incident.snapshot_dir, "manifest.json").is_file()
    live_before = git(world.live_wt, "rev-parse", "HEAD")

    # The PR is open: nothing moves until the owner merges.
    assert machine.drive(incident.incident_id, deps).state is IncidentState.AWAITING_MERGE
    assert git(world.live_wt, "rev-parse", "HEAD") == live_before

    merged = merge(world, incident.pr_number)
    incident = machine.drive(incident.incident_id, deps)
    assert incident.state is IncidentState.RESUMING, incident.escalation
    assert git(world.live_wt, "rev-parse", "HEAD") == merged
    assert incident.prev_live_wt_sha == live_before
    assert len(world.replays) == 1

    # What the detached replay does: a supervised rerun of the recorded argv.
    failed, incident_id = world.replays[0]
    replay = supervise(world, incident_id=incident_id, resumed_from=failed.run_id)
    assert replay.exit_code == 0
    machine.on_run_finished(replay, deps)
    incident = machine.drive(incident_id, deps)
    assert incident.state is IncidentState.CLOSED, incident.escalation

    # The original task finished from its checkpoint, each step exactly once.
    assert world.work_log() == list(STEPS)
    # One issue, one repair session, one PR, one replay.
    assert len([i for i in world.gh.issues.values()]) == 1
    assert len(world.gh.prs) == 1 and len(world.repair_sessions) == 1
    assert len(world.replays) == 1
    # The second pass over a closed incident changes nothing.
    again = machine.drive(incident_id, deps)
    assert again.state is IncidentState.CLOSED and len(world.gh.issues) == 1
    # The incident's history is searchable.
    lines = (world.home / "incidents.jsonl").read_text(encoding="utf-8").splitlines()
    assert lines and all(
        json.loads(line)["incident"]["incident_id"] == incident_id for line in lines
    )


def test_a_repair_that_fails_twice_escalates_and_leaves_the_checkpoint_alone(
    world: World,
) -> None:
    def failing_claude(argv: Sequence[str], **kw: object) -> CommandResult:
        world.repair_sessions.append(Path(str(kw.get("cwd"))))
        return CommandResult(1, "", "the session crashed")

    deps = make_deps(world, claude=failing_claude)
    first = supervise(world)
    db = world.workspace / ".scio" / "checkpoints.sqlite"
    before = db.read_bytes()
    incident = machine.on_run_finished(first, deps)
    assert incident is not None
    incident = machine.drive(incident.incident_id, deps)
    assert incident.state is IncidentState.ESCALATED
    assert incident.attempts == world.cfg.limits.max_attempts == len(world.repair_sessions)
    assert incident.escalation
    assert db.read_bytes() == before  # the task's checkpoint is preserved, never touched
    assert world.work_log() == ["S1"]
    assert not world.replays
    issue = incident.issue_number
    assert issue is not None and "needs-human" in world.gh.issues[issue]["labels"]


def test_a_killed_build_is_replayed_without_a_repair(world: World) -> None:
    """A hard kill (a reboot, a closed terminal): the run record says running, and both
    the build and its supervisor are gone. The tick replays it with no issue and no repair."""
    deps = make_deps(world)
    now = time.time()
    orphan = RunRecord(
        run_id="run-orphan",
        workspace=str(world.workspace),
        launcher=list(world.cfg.launcher),
        argv=["build", "--product", str(world.product), "--gh", "o/product"],
        env={},
        started_at=now - 60,
        pid=999_999,
        pid_created=1.0,
        supervisor_pid=999_998,
        supervisor_created=1.0,
    )
    world.store.save_run(orphan)
    machine.tick(deps)
    assert len(world.replays) == 1 and world.replays[0][0].run_id == "run-orphan"
    assert not world.gh.issues and not world.repair_sessions
