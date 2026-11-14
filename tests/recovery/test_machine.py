"""The incident state machine (ADR 0019 section 9), driven step by step against fakes.

Every collaborator module is replaced, so these tests pin the machine's own decisions:
which transition happens, which effect runs, and that no effect runs twice.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from recovery import (
    brief,
    classify,
    evidence,
    github,
    history,
    machine,
    repair,
    runner,
    runtime,
    scrub,
)
from recovery.commands import CommandResult
from recovery.config import Limits, WorkspaceConfig
from recovery.github import GitHub, GitHubError
from recovery.machine import Deps
from recovery.model import (
    CiStatus,
    Classification,
    Evidence,
    FailureClass,
    Incident,
    IncidentState,
    ProcessKey,
    PullRequest,
    RepairVerdict,
    RunRecord,
    RunStatus,
)
from recovery.store import Store

S = IncidentState
FC = FailureClass

OLD_SHA = "a" * 40
MERGE_SHA = "b" * 40
HEAD_SHA = "c" * 40
ME = ProcessKey(1, 1.0)
"""The test process's lease key."""

HARNESS = Classification(
    FC.HARNESS_SUSPECT,
    "sig-harness-1",
    "TimeoutExpired in run",
    "scio/harness/providers/claude_code.py:run",
)
TRANSIENT = Classification(FC.EXTERNAL_TRANSIENT, "sig-transient", "rate limit")
ORPHAN = Classification(FC.ORPHAN, "sig-orphan", "the run was orphaned")
DONE = Classification(FC.DONE, "", "done")
TASK = Classification(FC.TASK_FAULT, "sig-task", "integration_failed")


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def _no_command(argv: Any, **_: Any) -> CommandResult:
    raise AssertionError(f"a test ran a real command: {argv}")


class FakeGitHub(GitHub):
    """The GitHub methods the machine calls, in memory."""

    def __init__(self) -> None:
        super().__init__("o/SCIO", runner=_no_command)
        self.issues: dict[int, dict[str, Any]] = {}
        self.ensure_calls = 0
        self.comments: dict[tuple[int, str], str] = {}
        self.prs: dict[int, PullRequest] = {}
        self.ci: dict[str, CiStatus] = {}
        self.crash_after_create = False
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise GitHubError("gh: connection refused")

    def ensure_label(self, name: str, color: str, description: str) -> None:
        self._check()

    def find_issue(self, marker: str) -> int | None:
        self._check()
        return next((n for n, i in self.issues.items() if marker in i["body"]), None)

    def ensure_issue(self, title: str, body: str, labels: Any, marker: str) -> int:
        self._check()
        self.ensure_calls += 1
        found = self.find_issue(marker)
        if found is not None:
            return found
        number = 100 + len(self.issues)
        self.issues[number] = {"title": title, "body": f"{body}\n{marker}", "labels": set(labels)}
        if self.crash_after_create:
            self.crash_after_create = False
            raise KeyboardInterrupt  # the supervisor died after GitHub created the issue
        return number

    def upsert_comment(self, issue: int, marker: str, body: str) -> None:
        self._check()
        self.comments[(issue, marker)] = body

    def add_labels(self, issue: int, labels: Any) -> None:
        self._check()
        self.issues[issue]["labels"].update(labels)

    def pr(self, number: int) -> PullRequest | None:
        self._check()
        return self.prs.get(number)

    def find_pr_by_head(self, branch: str) -> PullRequest | None:
        self._check()
        found = [p for p in self.prs.values() if p.head_ref == branch]
        return max(found, key=lambda p: p.number) if found else None

    def ci_status(self, sha: str) -> CiStatus:
        self._check()
        return self.ci.get(sha, "none")

    def open_pr(self, number: int, branch: str) -> None:
        self.prs[number] = PullRequest(
            number, "OPEN", None, "", branch, HEAD_SHA, "Closes #100", True
        )

    def merge(self, number: int) -> None:
        self.prs[number] = dataclasses.replace(
            self.prs[number],
            state="MERGED",
            merged_at="2026-11-14T10:00:00Z",
            merge_sha=MERGE_SHA,
        )


@dataclass
class World:
    deps: Deps
    store: Store
    clock: Clock
    gh: FakeGitHub
    cfg: WorkspaceConfig
    home: Path
    classes: dict[str, Classification] = field(default_factory=dict)
    reading: str = ""
    calls: dict[str, int] = field(default_factory=dict)
    verdicts: list[RepairVerdict] = field(default_factory=list)
    claims: list[list[str]] = field(default_factory=list)
    replays: list[tuple[str, str, list[str]]] = field(default_factory=list)
    jsonl: list[str] = field(default_factory=list)
    live_wt: str = OLD_SHA
    refuse: runtime.RuntimeRefused | None = None
    lock_changed: bool = False
    health: list[str] = field(default_factory=list)
    orphans: list[RunRecord] = field(default_factory=list)
    me: ProcessKey = ME
    living: set[ProcessKey] = field(default_factory=set)
    session_opens_pr: bool = True
    session_crashes: bool = False

    def count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def n(self, name: str) -> int:
        return self.calls.get(name, 0)

    def run(self, run_id: str, cls: Classification, **kw: Any) -> RunRecord:
        values: dict[str, Any] = {
            "run_id": run_id,
            "workspace": "W",
            "launcher": ["sh", "scio.sh"],
            "argv": ["build", "--product", "P"],
            "env": {"PATH": "/bin"},
            "started_at": self.clock.t,
            "status": RunStatus.EXITED,
            "exit_code": 1,
        }
        values.update(kw)
        record = RunRecord(**values)
        self.store.save_run(record)
        self.classes[run_id] = cls
        self.clock.t += 1.0
        return record

    def finish(self, run_id: str, cls: Classification, **kw: Any) -> Incident | None:
        return machine.on_run_finished(self.run(run_id, cls, **kw), self.deps)

    def open(self, run_id: str, cls: Classification, **kw: Any) -> str:
        incident = self.finish(run_id, cls, **kw)
        assert incident is not None
        return incident.incident_id

    def get(self, incident_id: str) -> Incident:
        incident = self.store.get_incident(incident_id)
        assert incident is not None
        return incident

    def step(self, incident_id: str) -> Incident:
        machine.advance(self.get(incident_id), self.deps)
        return self.get(incident_id)

    def drive_to(self, incident_id: str, state: IncidentState) -> Incident:
        for _ in range(30):
            if self.get(incident_id).state is state:
                return self.get(incident_id)
            self.step(incident_id)
        raise AssertionError(f"never reached {state}: {self.get(incident_id).state}")

    def merged_and_green(self) -> None:
        self.gh.merge(7)
        self.gh.ci[MERGE_SHA] = "success"

    def comment(self, incident: Incident) -> str:
        assert incident.issue_number is not None
        marker = f"<!-- scio-recovery-status:{incident.incident_id} -->"
        return self.gh.comments[(incident.issue_number, marker)]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    clock = Clock()
    store = Store(":memory:", clock=clock)
    gh = FakeGitHub()
    cfg = WorkspaceConfig(
        workspace=tmp_path / "ws",
        launcher=("sh", "scio.sh"),
        scio_repo=tmp_path / "SCIO",
        scio_gh_repo="o/SCIO",
        live_wt=tmp_path / "SCIO" / ".scio" / "live-wt",
        limits=Limits(
            max_attempts=2,
            max_incidents_per_day=3,
            max_replays=3,
            backoff_s=(300.0, 900.0, 1800.0),
            ci_wait_s=2700.0,
        ),
    )

    def start_replay(run: RunRecord, incident_id: str) -> int:
        w.count("replay")
        w.replays.append((run.run_id, incident_id, list(run.argv)))
        return 4242

    deps = Deps(
        store=store,
        home=tmp_path / "home",
        package_root=tmp_path,
        config_for=lambda _ws: cfg,
        git=_no_command,
        uv=_no_command,
        claude=_no_command,
        launcher=_no_command,
        github_for=lambda _repo: gh,
        scio_running=list,
        start_replay=start_replay,
        clock=clock,
    )
    w = World(deps=deps, store=store, clock=clock, gh=gh, cfg=cfg, home=tmp_path / "home")
    w.living.add(w.me)

    def read_evidence(run: RunRecord, **_: Any) -> Evidence:
        w.reading = run.run_id  # classify sees only the Evidence: route it by run
        return Evidence(exit_code=run.exit_code)

    def snapshot(run: RunRecord, _cfg: Any, dest: Path, **_: Any) -> dict[str, str]:
        w.count("snapshot")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "stdout.log").write_text("kept", encoding="utf-8", newline="\n")
        return {"stdout": str(dest / "stdout.log")}

    def run_repair(_cfg: Any, incident: Incident, _wt: Path, _brief: str, **_: Any) -> Any:
        w.count("repair")
        if w.session_opens_pr:
            gh.open_pr(7, incident.branch)
        if w.session_crashes:
            raise KeyboardInterrupt  # the supervisor died during the session
        return w.verdicts.pop(0)

    def move(_cfg: Any, target: str, **_: Any) -> str:
        if w.refuse is not None:
            raise w.refuse
        w.count("move")
        prev, w.live_wt = w.live_wt, target
        return prev

    def rollback(_cfg: Any, sha: str, **_: Any) -> None:
        w.count("rollback")
        w.live_wt = sha

    def find_orphans(_store: Store, **_: Any) -> list[RunRecord]:
        w.count("find_orphans")
        out, w.orphans = w.orphans, []
        for run in out:
            run.status = RunStatus.ORPHANED
            store.save_run(run)
        return out

    def append_jsonl(_path: Path, _incident: Incident, event: str) -> None:
        w.jsonl.append(event)

    patches: dict[Any, dict[str, Any]] = {
        evidence: {
            "read_evidence": read_evidence,
            "snapshot": snapshot,
            "fingerprints": lambda *_, **__: {"live_wt": OLD_SHA},
        },
        classify: {"classify": lambda _ev: w.classes[w.reading]},
        history: {"append_jsonl": append_jsonl, "similar": lambda *_, **__: []},
        brief: {"render_brief": lambda *_: "the brief"},
        repair: {
            "branch_name": lambda i: f"fix/{i.issue_number}-recovery-{i.signature[:6]}",
            "worktree_path": lambda _c, i: tmp_path / "wt" / i.incident_id,
            "prepare_worktree": lambda _c, i, **_: tmp_path / "wt" / i.incident_id,
            "run_repair": run_repair,
            "verify_claim": lambda *_, **__: w.claims.pop(0) if w.claims else [],
        },
        runtime: {
            "head": lambda *_, **__: w.live_wt,
            "move_live_wt": move,
            "rollback_live_wt": rollback,
            "lock_changed": lambda *_, **__: w.lock_changed,
            "health_check": lambda *_, **__: list(w.health),
        },
        github: {
            "marker_for": lambda sig: f"<!-- scio-recovery:{sig} -->",
            "status_marker": lambda iid: f"<!-- scio-recovery-status:{iid} -->",
        },
        scrub: {"scrub": lambda text: text},
        runner: {"find_orphans": find_orphans},
        machine: {"lease_holder": lambda: w.me, "lease_alive": lambda k: k in w.living},
    }
    for module, names in patches.items():
        for name, value in names.items():
            monkeypatch.setattr(module, name, value)
    return w


def _repaired(branch: str = "fix/100-recovery-sig-ha") -> RepairVerdict:
    return RepairVerdict(
        "repaired",
        pr_number=7,
        branch=branch,
        ledger_id="I-200",
        gate="passed",
        num_turns=12,
        cost_usd=3.4,
        duration_ms=540_000,
        session_id="s-1",
    )


def test_the_happy_path_goes_from_detection_to_closed(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    opened = w.finish("run-1", HARNESS, argv=["build", "--product", "P", "--decision", "go"])
    assert opened is not None and opened.state is S.DETECTED
    iid = opened.incident_id
    run = w.store.get_run("run-1")
    assert run is not None and run.incident_id == iid

    inc = w.step(iid)
    assert inc.state is S.SNAPSHOTTED and w.n("snapshot") == 1
    assert (Path(inc.snapshot_dir) / "stdout.log").exists()
    assert w.step(iid).state is S.CLASSIFIED
    inc = w.step(iid)
    assert inc.state is S.ISSUE_OPEN and inc.issue_number == 100
    issue = w.gh.issues[100]
    assert issue["labels"] == {"bug", "live-run", "recovery"}
    assert f"scio-recovery:{HARNESS.signature}" in issue["body"]
    assert issue["title"].startswith("recovery: harness_suspect scio/harness/providers/")

    inc = w.step(iid)
    assert inc.state is S.REPAIRING and inc.attempts == 1
    assert w.store.lease_holder(machine.REPAIR_LEASE) == w.me
    inc = w.step(iid)
    assert inc.state is S.PR_VERIFIED and inc.pr_number == 7 and inc.ledger_id == "I-200"
    assert w.store.lease_holder(machine.REPAIR_LEASE) is None
    facts = "12 turns, est. $3.40, 540 s, session s-1"
    assert f"attempt 1 repair session: repaired; {facts}" in [t for _, t in w.store.events(iid)]
    inc = w.step(iid)
    assert inc.state is S.AWAITING_MERGE and "merge PR #7" in w.comment(inc)
    assert f"- Attempt 1 repair session: repaired; {facts}" in w.comment(inc)
    assert w.step(iid).state is S.AWAITING_MERGE  # the PR is still open

    w.merged_and_green()
    inc = w.step(iid)
    assert inc.state is S.RUNTIME_UPDATED and inc.merge_sha == MERGE_SHA
    assert w.live_wt == MERGE_SHA and inc.prev_live_wt_sha == OLD_SHA
    assert w.step(iid).state is S.HEALTHY
    inc = w.step(iid)
    assert inc.state is S.RESUMING
    assert w.replays == [("run-1", iid, ["build", "--product", "P"])]  # no --decision
    assert w.step(iid).state is S.RESUMING  # waits for the replay

    after = w.finish("run-2", DONE, exit_code=0, incident_id=iid, resumed_from="run-1")
    assert after is not None and after.state is S.VERIFIED
    inc = w.step(iid)
    assert inc.state is S.CLOSED and inc.resume_run_id == "run-2"
    assert "got past the failure" in w.comment(inc)
    assert len(w.gh.issues) == 1 and w.n("repair") == 1 and w.n("move") == 1
    assert w.jsonl[0].startswith("detected harness_suspect in run run-1")
    assert len(w.jsonl) == 12  # detected, then one line per transition
    assert w.jsonl[-1].startswith("verified -> closed")


def test_each_transition_run_twice_from_a_stale_copy_repeats_no_effect(world: World) -> None:
    """A crash after an effect but before the incident is saved: the next tick repeats
    the transition from the old record."""
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    for _ in range(20):
        stale = w.get(iid)
        if stale.state is S.RESUMING:
            break
        if stale.state is S.AWAITING_MERGE:
            w.merged_and_green()
        twin = Incident.from_dict(stale.to_dict())
        machine.advance(stale, w.deps)
        machine.advance(twin, w.deps)
    inc = w.get(iid)
    assert inc.state is S.RESUMING and inc.prev_live_wt_sha == OLD_SHA
    assert w.gh.ensure_calls == 1 and len(w.gh.issues) == 1
    assert w.n("snapshot") == 1 and w.n("repair") == 1
    assert w.n("move") == 1 and w.n("replay") == 1


def test_a_crash_inside_each_effect_is_found_by_its_probe(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.CLASSIFIED)
    w.gh.crash_after_create = True
    with pytest.raises(KeyboardInterrupt):
        w.step(iid)
    assert w.get(iid).state is S.CLASSIFIED
    assert w.step(iid).state is S.ISSUE_OPEN
    assert len(w.gh.issues) == 1 and w.gh.ensure_calls == 1

    # the session opened its PR, then the supervisor died: the PR is found, not re-made
    w.session_crashes = True
    assert w.step(iid).state is S.REPAIRING
    with pytest.raises(KeyboardInterrupt):
        w.step(iid)
    w.session_crashes = False
    inc = w.step(iid)
    assert inc.state is S.PR_VERIFIED and inc.pr_number == 7 and w.n("repair") == 1

    # live-wt moved, then the supervisor died
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()
    fake_move = runtime.move_live_wt

    def move_then_die(cfg: Any, target: str, **kw: Any) -> str:
        fake_move(cfg, target, **kw)
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "move_live_wt", move_then_die)
    with pytest.raises(KeyboardInterrupt):
        w.step(iid)
    monkeypatch.setattr(runtime, "move_live_wt", fake_move)
    inc = w.step(iid)
    assert inc.state is S.RUNTIME_UPDATED and w.n("move") == 1
    assert inc.prev_live_wt_sha == OLD_SHA

    # the replay started and recorded its run, then the supervisor died
    w.drive_to(iid, S.HEALTHY)
    fake_start = w.deps.start_replay

    def start_then_die(run: RunRecord, incident_id: str) -> int:
        fake_start(run, incident_id)
        w.run("run-2", DONE, status=RunStatus.RUNNING, incident_id=iid, resumed_from="run-1")
        raise KeyboardInterrupt

    w.deps.start_replay = start_then_die
    with pytest.raises(KeyboardInterrupt):
        w.step(iid)
    w.deps.start_replay = fake_start
    assert w.step(iid).state is S.RESUMING and w.n("replay") == 1


def test_a_session_interrupted_before_any_pr_is_not_started_again(world: World) -> None:
    w = world
    w.session_opens_pr, w.session_crashes = False, True
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.REPAIRING)
    with pytest.raises(KeyboardInterrupt):
        w.step(iid)
    w.session_crashes = False
    w.verdicts = [_repaired()]
    inc = w.step(iid)
    assert inc.state is S.ISSUE_OPEN and w.n("repair") == 1  # attempt 1 failed
    assert w.store.lease_holder(machine.REPAIR_LEASE) is None


def test_not_scio_escalates_with_needs_human_and_keeps_everything(world: World) -> None:
    w = world
    w.verdicts = [RepairVerdict("not_scio", note="the product's own test is wrong")]
    iid = w.open("run-1", HARNESS)
    inc = w.drive_to(iid, S.ESCALATED)
    assert "outside SCIO" in inc.escalation and "product's own test" in inc.escalation
    assert "needs-human" in w.gh.issues[100]["labels"]
    assert "Escalation:" in w.comment(inc) and "snapshot is kept" in w.comment(inc)
    assert (Path(inc.snapshot_dir) / "stdout.log").exists()
    assert w.n("move") == w.n("rollback") == w.n("replay") == 0
    assert w.store.lease_holder(machine.REPAIR_LEASE) is None


def test_a_failed_claim_is_retried_up_to_max_attempts_then_escalates(world: World) -> None:
    w = world
    w.verdicts = [_repaired(), _repaired()]
    w.claims = [["no file under tests/ changed"], ["CI failure on the PR head"]]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.REPAIRING)
    inc = w.step(iid)
    assert inc.state is S.ISSUE_OPEN and inc.attempts == 1
    assert w.step(iid).state is S.REPAIRING
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and inc.attempts == 2 and w.n("repair") == 2
    assert "2 of 2" in inc.escalation and "CI failure" in inc.escalation


def test_cannot_repair_counts_as_a_failed_attempt(world: World) -> None:
    w = world
    w.verdicts = [RepairVerdict("cannot_repair", note="no reproduction")] * 2
    inc = w.drive_to(w.open("run-1", HARNESS), S.ESCALATED)
    assert inc.attempts == 2 and "no reproduction" in inc.escalation


def test_ci_pending_stays_in_repairing_and_gives_up_after_the_ci_wait(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    w.claims = [["ci pending"], ["ci pending"], []]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.REPAIRING)
    assert w.step(iid).state is S.REPAIRING
    w.clock.t += 60
    assert w.step(iid).state is S.REPAIRING
    assert w.step(iid).state is S.PR_VERIFIED
    assert w.n("repair") == 1  # the session ran once; only the check repeated

    w.verdicts = [_repaired()]
    w.claims = [["ci pending"], ["ci pending"]]
    other = w.open("run-9", dataclasses.replace(HARNESS, signature="sig-other"))
    w.drive_to(other, S.REPAIRING)
    assert w.step(other).state is S.REPAIRING
    w.clock.t += w.cfg.limits.ci_wait_s + 1
    inc = w.step(other)
    assert inc.state is S.ISSUE_OPEN
    assert "did not finish" in w.store.events(other)[-1][1]


def test_a_pr_closed_without_a_merge_escalates(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.gh.prs[7] = dataclasses.replace(w.gh.prs[7], state="CLOSED")
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and "closed without a merge" in inc.escalation
    assert "needs-human" in w.gh.issues[100]["labels"]


def test_a_red_main_after_the_merge_escalates_and_live_wt_stays(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.gh.merge(7)
    assert w.step(iid).state is S.AWAITING_MERGE  # no CI run on main yet
    w.gh.ci[MERGE_SHA] = "failure"
    assert w.step(iid).state is S.ESCALATED and w.live_wt == OLD_SHA


def test_runtime_refusals_wait_when_retryable_and_escalate_otherwise(world: World) -> None:
    w = world
    w.verdicts = [_repaired(), _repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()
    w.refuse = runtime.RuntimeRefused("a scio process runs from live-wt", retryable=True)
    assert w.step(iid).state is S.AWAITING_MERGE
    assert w.step(iid).state is S.AWAITING_MERGE
    w.refuse = None
    assert w.step(iid).state is S.RUNTIME_UPDATED and w.n("move") == 1

    other = w.open("run-2", dataclasses.replace(HARNESS, signature="sig-2"))
    w.gh.prs.clear()
    w.drive_to(other, S.AWAITING_MERGE)
    w.gh.merge(7)
    w.refuse = runtime.RuntimeRefused("live-wt has local changes", retryable=False)
    inc = w.step(other)
    assert inc.state is S.ESCALATED and "local changes" in inc.escalation


@pytest.mark.parametrize("cause", ["lock", "health"])
def test_a_changed_lock_or_a_failed_health_check_rolls_back_then_escalates(
    world: World, cause: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    monkeypatch.setattr(scrub, "scrub", lambda text: text.replace("hunter2", "[redacted]"))
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()
    if cause == "lock":
        w.lock_changed = True
    else:
        w.health = ["scio report --thread T exited 1: TOKEN=hunter2"]
    assert w.step(iid).state is S.RUNTIME_UPDATED and w.live_wt == MERGE_SHA
    inc = w.step(iid)
    assert inc.state is S.ESCALATED
    assert w.live_wt == OLD_SHA and w.n("rollback") == 1 and w.n("replay") == 0
    assert ("uv.lock" if cause == "lock" else "exited 1") in inc.escalation
    assert "back on" in inc.escalation
    assert "hunter2" not in inc.escalation and "hunter2" not in w.comment(inc)
    # a repeat (a crash before the save) reads the recorded verdict: the rolled-back old
    # code would pass a fresh check, and the build would resume on it
    w.lock_changed, w.health = False, []
    stale = dataclasses.replace(inc, state=S.RUNTIME_UPDATED, escalation="")
    again = machine.advance(stale, w.deps)
    assert again.state is S.ESCALATED and w.n("replay") == 0


def test_a_runtime_that_cannot_be_read_escalates_instead_of_crashing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()

    def broken(*_: Any, **__: Any) -> str:
        raise RuntimeError("git -C live-wt rev-parse HEAD exited 128")

    monkeypatch.setattr(runtime, "head", broken)
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and "exited 128" in inc.escalation and w.n("move") == 0


def test_a_live_wt_already_on_the_target_rolls_back_to_the_runs_sha(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS, scio_sha="d" * 40)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()
    w.live_wt = MERGE_SHA  # someone moved it by hand
    inc = w.step(iid)
    assert inc.state is S.RUNTIME_UPDATED and inc.prev_live_wt_sha == "d" * 40


def test_a_transient_failure_backs_off_replays_and_escalates_past_max_replays(
    world: World,
) -> None:
    w = world
    iid = w.open("run-1", TRANSIENT)
    inc = w.drive_to(iid, S.WAITING_BACKOFF)
    assert inc.backoff_until == pytest.approx(w.clock.t + 300.0)
    assert w.step(iid).state is S.WAITING_BACKOFF  # not due yet
    for n, wait in enumerate((300.0, 900.0, 1800.0), start=1):
        w.clock.t += wait
        inc = w.step(iid)
        assert inc.state is S.RESUMING and inc.replays == n and w.n("replay") == n
        last = w.replays[-1][0]
        after = w.finish(f"replay-{n}", TRANSIENT, incident_id=iid, resumed_from=last)
        assert after is not None and after.incident_id == iid
        assert after.state is S.WAITING_BACKOFF
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and "max_replays" in inc.escalation
    # The escalation reaches the owner on GitHub even though the incident never had an
    # issue: one issue with needs-human, and still no repair.
    [issue] = w.gh.issues.values()
    assert "needs-human" in issue["labels"] and inc.issue_number is not None
    assert w.n("repair") == 0
    assert [r[0] for r in w.replays] == ["run-1", "replay-1", "replay-2"]


def test_a_later_incident_with_the_same_signature_gets_its_own_issue(world: World) -> None:
    """One issue per incident: reusing an earlier incident's issue would put the new
    repair on the same fix/<issue>-... branch the earlier repair left behind."""
    w = world
    first = w.open("run-1", HARNESS)
    a = w.drive_to(first, S.ISSUE_OPEN)
    w.store.save_incident(dataclasses.replace(a, state=S.ESCALATED, escalation="by hand"))
    second = w.open("run-2", HARNESS)
    assert second != first
    b = w.drive_to(second, S.ISSUE_OPEN)
    assert a.issue_number is not None and b.issue_number is not None
    assert a.issue_number != b.issue_number and len(w.gh.issues) == 2


@pytest.mark.parametrize("cls", [FC.EXTERNAL_AUTH, FC.UNKNOWN])
def test_auth_and_unknown_get_an_issue_then_escalate_without_a_repair(
    world: World, cls: FailureClass
) -> None:
    w = world
    iid = w.open("run-1", Classification(cls, f"sig-{cls.value}", "not logged in"))
    inc = w.drive_to(iid, S.ESCALATED)
    assert w.gh.issues[100]["labels"] == {"bug", "live-run", "recovery", "needs-human"}
    assert w.n("repair") == 0 and w.n("replay") == 0
    assert ("Sign in again" if cls is FC.EXTERNAL_AUTH else "no known class") in inc.escalation


def test_a_task_fault_is_a_closed_record_with_no_issue(world: World) -> None:
    w = world
    inc = w.finish("run-1", TASK)
    assert inc is not None and inc.state is S.CLOSED and inc.failure_class is FC.TASK_FAULT
    assert machine.advance(inc, w.deps).state is S.CLOSED
    assert w.gh.issues == {} and w.n("snapshot") == 0
    assert w.finish("run-2", DONE, exit_code=0) is None


def test_a_second_failure_with_the_same_signature_joins_the_open_incident(world: World) -> None:
    w = world
    first = w.finish("run-1", HARNESS)
    second = w.finish("run-2", HARNESS)
    assert first is not None and second is not None
    assert second.incident_id == first.incident_id
    assert second.occurrences == 2 and second.run_ids == ["run-1", "run-2"]
    assert len(w.store.incidents()) == 1
    run = w.store.get_run("run-2")
    assert run is not None
    again = machine.on_run_finished(run, w.deps)
    assert again is not None and again.occurrences == 2  # a run is handled once


def test_the_circuit_breaker_trips_on_the_daily_limit(world: World) -> None:
    w = world
    for n in range(3):
        w.finish(f"run-{n}", dataclasses.replace(HARNESS, signature=f"sig-{n}"))
    w.finish("run-t", TASK)  # a task fault is not counted
    fourth = w.finish("run-4", dataclasses.replace(HARNESS, signature="sig-4"))
    assert fourth is not None and fourth.state is S.DETECTED
    assert "circuit breaker" in fourth.escalation
    inc = w.drive_to(fourth.incident_id, S.ESCALATED)
    assert w.n("snapshot") == 1  # the snapshot still comes first
    assert inc.issue_number is not None
    assert "needs-human" in w.gh.issues[inc.issue_number]["labels"] and w.n("repair") == 0
    w.clock.t += machine.DAY_S + 1
    fresh = w.finish("run-5", dataclasses.replace(HARNESS, signature="sig-5"))
    assert fresh is not None and fresh.escalation == ""


def test_a_recurrence_after_a_merged_repair_escalates(world: World) -> None:
    w = world
    old = Incident("inc-old", HARNESS.signature, FC.HARNESS_SUSPECT, "old", "W", S.CLOSED, 1.0, 1.0)
    w.store.save_incident(dataclasses.replace(old, pr_number=5, merge_sha=MERGE_SHA))
    inc = w.finish("run-1", HARNESS)
    assert inc is not None and inc.escalation.startswith("recurrence")
    assert "inc-old" in inc.escalation and "PR #5" in inc.escalation
    assert w.drive_to(inc.incident_id, S.ESCALATED).attempts == 0
    # an earlier incident that never merged a repair is no recurrence
    w.store.save_incident(dataclasses.replace(old, incident_id="inc-esc", signature="sig-e"))
    fresh = w.finish("run-2", dataclasses.replace(HARNESS, signature="sig-e"))
    assert fresh is not None and fresh.escalation == ""


def test_a_replay_ending_with_the_same_signature_escalates(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()
    w.drive_to(iid, S.RESUMING)
    after = w.finish("run-2", HARNESS, incident_id=iid, resumed_from="run-1")
    assert after is not None and after.state is S.RESUMING and after.escalation
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and "same failure came back" in inc.escalation
    assert inc.occurrences == 2 and "needs-human" in w.gh.issues[100]["labels"]


def test_a_replay_ending_with_another_failure_verifies_and_opens_its_own(world: World) -> None:
    w = world
    iid = w.open("run-1", ORPHAN, status=RunStatus.ORPHANED)
    w.drive_to(iid, S.RESUMING)
    other = w.finish("run-2", HARNESS, incident_id=iid, resumed_from="run-1")
    assert other is not None and other.incident_id != iid and other.state is S.DETECTED
    assert w.get(iid).state is S.VERIFIED
    run = w.store.get_run("run-2")
    assert run is not None and run.incident_id == iid  # the replay stays linked


def test_an_orphan_is_replayed_with_no_issue(world: World) -> None:
    w = world
    iid = w.open("run-1", ORPHAN, status=RunStatus.ORPHANED, exit_code=None)
    inc = w.drive_to(iid, S.RESUMING)
    assert inc.replays == 1 and w.replays == [("run-1", iid, ["build", "--product", "P"])]
    assert w.gh.issues == {} and w.n("repair") == 0
    # orphaned again: the same signature, so it escalates instead of looping
    w.finish("run-2", ORPHAN, status=RunStatus.ORPHANED, incident_id=iid, resumed_from="run-1")
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and "orphaned again" in inc.escalation
    assert w.n("replay") == 1 and inc.occurrences == 2


def test_a_repaired_incident_whose_replay_is_orphaned_is_replayed_again(world: World) -> None:
    w = world
    w.verdicts = [_repaired()]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.AWAITING_MERGE)
    w.merged_and_green()
    w.drive_to(iid, S.RESUMING)
    after = w.finish(
        "run-2", ORPHAN, status=RunStatus.ORPHANED, incident_id=iid, resumed_from="run-1"
    )
    assert after is not None and after.state is S.WAITING_BACKOFF and not after.escalation
    inc = w.step(iid)
    assert inc.state is S.RESUMING and inc.replays == 1 and w.n("replay") == 2
    assert w.replays[-1][0] == "run-2" and len(w.store.incidents()) == 1


def test_hold_stops_the_automation_and_release_resumes_it(world: World) -> None:
    w = world
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.ISSUE_OPEN)
    held = machine.hold(iid, w.deps)
    assert held.state is S.HELD and held.held_from is S.ISSUE_OPEN
    assert machine.hold(iid, w.deps).state is S.HELD
    assert machine.drive(iid, w.deps).state is S.HELD
    assert w.step(iid).state is S.HELD and w.n("repair") == 0
    assert "release" in w.comment(held)
    back = machine.release(iid, w.deps)
    assert back.state is S.ISSUE_OPEN and back.held_from is None
    with pytest.raises(ValueError, match="not held"):
        machine.release(iid, w.deps)
    closed = w.finish("run-t", TASK)
    assert closed is not None
    with pytest.raises(ValueError, match="only an open one"):
        machine.hold(closed.incident_id, w.deps)


def test_a_hold_during_a_long_repair_survives_its_result(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.REPAIRING)

    def held_meanwhile(*_: Any, **__: Any) -> RepairVerdict:
        machine.hold(iid, w.deps)
        return _repaired()

    monkeypatch.setattr(repair, "run_repair", held_meanwhile)
    inc = w.step(iid)
    assert inc.state is S.HELD and inc.held_from is S.PR_VERIFIED
    assert machine.release(iid, w.deps).state is S.PR_VERIFIED


def test_tick_handles_orphans_and_drives_every_open_incident(world: World) -> None:
    w = world
    w.orphans = [w.run("run-1", ORPHAN, status=RunStatus.RUNNING, exit_code=None)]
    lines = machine.tick(w.deps)
    assert any(line.startswith("orphan run-1: incident") for line in lines)
    incidents = w.store.incidents()
    assert len(incidents) == 1 and incidents[0].state is S.RESUMING and w.n("replay") == 1
    assert w.store.lease_holder(machine.TICK_LEASE) is None
    assert machine.tick(w.deps) == []  # nothing new, and nothing done twice
    assert w.n("replay") == 1


def test_a_second_tick_returns_at_once_while_one_runs(world: World) -> None:
    w = world
    other = ProcessKey(99, 9.0)
    w.living.add(other)
    assert w.store.acquire_lease(machine.TICK_LEASE, other, lambda k: k in w.living)
    assert machine.tick(w.deps) == ["another tick is running (pid 99)"]
    assert w.n("find_orphans") == 0
    w.living.discard(other)  # it died without releasing
    machine.tick(w.deps)
    assert w.n("find_orphans") == 1


def test_a_driver_skips_an_incident_another_live_process_drives(world: World) -> None:
    w = world
    iid = w.open("run-1", HARNESS)
    other = ProcessKey(77, 7.0)
    w.living.add(other)
    assert w.store.acquire_lease(f"incident:{iid}", other, lambda k: k in w.living)
    assert machine.drive(iid, w.deps).state is S.DETECTED and w.n("snapshot") == 0
    w.living.discard(other)  # it died: the next driver takes over
    w.verdicts = [RepairVerdict("not_scio", note="elsewhere")]
    assert machine.drive(iid, w.deps).state is S.ESCALATED and w.n("snapshot") == 1


def test_tick_picks_up_a_finished_replay_and_escalates_a_lost_one(world: World) -> None:
    w = world
    a = w.open("run-1", ORPHAN, status=RunStatus.ORPHANED)
    w.drive_to(a, S.RESUMING)
    # the replay's supervisor recorded the exit, then died before handling it
    w.run("run-2", DONE, exit_code=0, incident_id=a, resumed_from="run-1")
    machine.tick(w.deps)
    assert w.get(a).state is S.CLOSED

    b = w.open("run-3", dataclasses.replace(ORPHAN, signature="sig-o2"), status=RunStatus.ORPHANED)
    w.drive_to(b, S.RESUMING)
    machine.tick(w.deps)
    assert w.get(b).state is S.RESUMING  # too early to call it lost
    w.clock.t += machine.LOST_REPLAY_AFTER_S + 1
    lines = machine.tick(w.deps)
    inc = w.get(b)
    assert inc.state is S.ESCALATED and "recorded no run" in inc.escalation
    assert f"{b}: the replay never ran; escalated" in lines


def test_a_refused_replay_waits_and_replays_again(world: World) -> None:
    w = world
    iid = w.open("run-1", ORPHAN, status=RunStatus.ORPHANED)
    w.drive_to(iid, S.RESUMING)
    refused = w.run(
        "run-2",
        DONE,
        status=RunStatus.REFUSED,
        exit_code=None,
        incident_id=iid,
        resumed_from="run-1",
    )
    assert machine.on_run_finished(refused, w.deps) is None
    machine.tick(w.deps)
    assert w.get(iid).state is S.WAITING_BACKOFF and w.n("replay") == 1
    w.clock.t += 2000
    machine.tick(w.deps)
    assert w.get(iid).state is S.RESUMING and w.n("replay") == 2


def test_a_github_outage_leaves_the_incident_for_the_next_tick(world: World) -> None:
    w = world
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.CLASSIFIED)
    w.gh.down = True
    assert w.step(iid).state is S.CLASSIFIED
    assert "GitHub failed" in w.store.events(iid)[-1][1]
    w.gh.down = False
    assert w.step(iid).state is S.ISSUE_OPEN and len(w.gh.issues) == 1


def test_an_escalation_is_saved_even_when_github_is_down_and_posted_later(world: World) -> None:
    w = world
    w.verdicts = [RepairVerdict("not_scio", note="elsewhere")]
    iid = w.open("run-1", HARNESS)
    w.drive_to(iid, S.REPAIRING)
    w.gh.down = True
    inc = w.step(iid)
    assert inc.state is S.ESCALATED and "needs-human" not in w.gh.issues[100]["labels"]
    w.gh.down = False
    assert machine.tick(w.deps) == [f"{iid}: needs-human posted"]
    assert "needs-human" in w.gh.issues[100]["labels"]
    assert machine.tick(w.deps) == []
