"""M8 acceptance on the Fake provider (ADR 0011): a bootstrapped two-milestone product is
built feature by feature through the task graph on the milestone branch, integration checks
and the milestone gate run on the merged branch, a milestone PR stops for the human, and a
crash between features or after the PR resumes with no duplicated issue, commit or PR."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evals.fake_scripts import ScriptedResponder
from scio.graph.build import compile_graph
from scio.graph.checkpointer import open_checkpointer
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.github.client import GitHubClient
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.harness.types import CompletionRequest
from scio.product import resume_product, run_product
from scio.product.build import (
    MILESTONES_DIR,
    build_thread_id,
    integrate,
    integration_checks,
    milestone_gate,
    resume_build,
    run_build,
)
from scio.product.progress import PROGRESS_PATH, ProgressFile
from scio.settings import Settings
from scio.state.models import (
    FeatureRun,
    MilestoneFeatureOutput,
    MilestonePlan,
    ProductBuildState,
    ProductFeature,
    ProductSpec,
    SCIOState,
    Tier,
)
from scio.toolchain import python_toolchain
from tests.unit.test_github_client import FakeGh

PROMPT = "A CLI todo app with tags and JSON export"
PRODUCT = "todo_cli"
Responder = Callable[[CompletionRequest], Any]


class Crash(RuntimeError):
    pass


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider_override="fake",
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )


def _harness(settings: Settings, responder: Responder) -> Harness:
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    registry.register("fake", FakeProvider(responder=responder))
    return Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=2_000_000, usd_max=20.0)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )


def _ctx(
    tmp_path: Path,
    harness: Harness,
    thread: str,
    *,
    github: GitHubClient | None = None,
) -> RunContext:
    return RunContext.create(
        thread_id=thread,
        sandbox_root=tmp_path / "product",
        tier="T1",
        tier_pin="T1",
        settings=harness.settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
        github=github,
    )


def _bootstrap(tmp_path: Path, responder: Responder | None = None) -> Harness:
    """``scio new`` on the Fake: an empty directory becomes the bootstrapped product."""
    harness = _harness(_settings(tmp_path), responder or ScriptedResponder())
    ctx = _ctx(tmp_path, harness, "new-1")
    try:
        run_product(ctx, prompt=PROMPT)
        resume_product(ctx, "answers")
        out = resume_product(ctx, "approve")
        assert out.state.phase == "done" and out.state.bootstrap_commit
    finally:
        ctx.close()
    return harness


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _subjects(repo: Path, ref: str = "HEAD") -> list[str]:
    return _git(repo, "log", "--format=%s", ref).splitlines()


def _progress(repo: Path) -> ProgressFile:
    return ProgressFile.from_json((repo / PROGRESS_PATH).read_text(encoding="utf-8"))


def _feature_state(ctx: RunContext, thread: str) -> SCIOState:
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        values = compile_graph(ctx, saver).get_state({"configurable": {"thread_id": thread}}).values
    return SCIOState.model_validate(values)


def _ci_green(repo: Path) -> None:
    tc = python_toolchain()
    lint = subprocess.run(tc.lint_argv("."), cwd=repo, capture_output=True, text=True)
    assert lint.returncode in (0, 127), lint.stdout
    tests = subprocess.run(
        tc.test_argv(filter=None, path=None), cwd=repo, capture_output=True, text=True
    )
    assert tests.returncode == 0, tests.stdout + tests.stderr


# ---------- the loop ----------


def test_two_milestone_product_builds_through_the_task_graph(tmp_path: Path) -> None:
    harness = _bootstrap(tmp_path)
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        # M1 = P1 (T0) + P2 (T1): both features run, the branch integrates, the PR waits.
        out = run_build(ctx)
        assert out.paused and out.interrupt is not None, out.state.ledger[-3:]
        s = out.state
        assert s.phase == "merge_wait" and s.milestone and s.milestone.id == "M1"
        assert s.milestone.feature_ids() == ["P1", "P2"]
        assert [(r.feature_id, r.tier, r.status) for r in s.feature_runs] == [
            ("P1", Tier.T0, "passed"),
            ("P2", Tier.T1, "passed"),
        ]
        assert all(
            r.commit_sha and r.usd > 0 and r.exit_reason == "success" for r in s.feature_runs
        )
        assert s.integration_ok is True and s.last_gate and s.last_gate.verdict == "pass"
        assert s.pr and s.pr.body_path == f"{MILESTONES_DIR}/M1-pr.md"
        assert out.interrupt["branch"] == "milestone/1" and out.interrupt["cost_usd"] > 0
        assert out.interrupt["features"] == ["P1: passed", "P2: passed"]
        # offline issues: one file per feature, committed once on the milestone branch
        assert sorted(p.name for p in (repo / ".scio/product/issues").iterdir()) == [
            "P1.md",
            "P2.md",
        ]
        m1 = _subjects(repo, "milestone/1")
        assert sum("[scio:issues:M1]" in x for x in m1) == 1
        assert (
            sum(x.startswith("feat:") for x in m1) == 2
            and sum(x.startswith("docs:") for x in m1) == 2
        )
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "milestone/1"
        body = (repo / s.pr.body_path).read_text(encoding="utf-8")
        assert "| P1 " in body and "| P2 " in body and "## Milestone gate" in body

        # typed handoff: the feature thread's own state agrees with the product's record
        p2 = s.latest_run("P2")
        assert p2 is not None and p2.thread_id == f"{ctx.thread_id}-p2-a1"
        inner = _feature_state(ctx, p2.thread_id)
        assert (
            inner.task.base_branch == "milestone/1" and inner.task.title == "P2: Add and list todos"
        )
        assert inner.task.acceptance == ["`todo export --json` prints valid JSON with tags"] or (
            inner.task.acceptance == ["`todo add x` then `todo list` prints x"]
        )
        assert (
            inner.routing
            and inner.routing.tier is Tier.T1
            and inner.routing.classifier_model == "pinned"
        )
        assert inner.github.head_sha == p2.commit_sha and inner.github.branch == p2.branch
        assert (
            inner.github.branch == "agent/p2-add-and-list-todos" and inner.github.pr_number is None
        )
        assert round(inner.budgets.usd_used, 4) == p2.usd and inner.phase == "done"

        # human merges (offline: the orchestrator merges locally), M2 = P3 runs, waits again
        out = resume_build(ctx, "merge")
        assert out.paused and out.state.milestone and out.state.milestone.id == "M2"
        assert out.state.milestones_done == ["M1"]
        assert [r.feature_id for r in out.state.feature_runs] == ["P1", "P2", "P3"]
        progress = _progress(repo)
        assert [m.id for m in progress.milestones] == ["M1"] and progress.milestones[
            0
        ].status == "done"
        assert progress.milestones[0].merged_commit and progress.build_thread == ctx.thread_id
        assert [f.status for f in progress.features] == ["done", "done", "todo"]
        assert progress.feature("P2") and progress.feature("P2").commit == p2.commit_sha

        out = resume_build(ctx, "merged")
        assert not out.paused and out.state.phase == "done" and out.state.exit_reason is None
        assert out.state.milestones_done == ["M1", "M2"]
    finally:
        ctx.close()

    progress = _progress(repo)
    assert [f.status for f in progress.features] == ["done", "done", "done"]
    assert [m.id for m in progress.milestones] == ["M1", "M2"]
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    main = _subjects(repo)
    assert (
        sum("[scio:merge:" in x for x in main) == 2
        and sum("[scio:progress:" in x for x in main) == 2
    )
    assert _git(repo, "status", "--porcelain") == ""
    notes = (repo / "NOTES.md").read_text(encoding="utf-8")
    assert "M1 Milestone 1 merged" in notes and "M2 Milestone 2 merged" in notes
    assert (repo / "todo_cli" / "store.py").is_file() and (
        repo / "tests" / "test_export.py"
    ).is_file()
    _ci_green(repo)  # the product's own CI on main, ground truth


# ---------- crash and resume ----------


def _crash_at_gate_of(feature_title: str) -> ScriptedResponder:
    """A responder that dies once, at the task gate of the named feature."""
    inner = ScriptedResponder()
    armed = {"on": True}

    def responder(req: CompletionRequest) -> Any:
        schema = req.output_schema or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        text = "\n".join(m.text() for m in req.messages)
        if "objective_met" in props and feature_title in text and armed["on"]:
            armed["on"] = False
            raise Crash(f"killed at the gate of {feature_title}")
        return inner(req)

    responder.inner = inner  # type: ignore[attr-defined]
    return responder  # type: ignore[return-value]


def test_crash_between_features_resumes_without_duplicates(tmp_path: Path) -> None:
    harness = _bootstrap(tmp_path, _crash_at_gate_of("P2: Add and list todos"))
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        with pytest.raises(Crash):
            run_build(ctx)
        # P1 finished and was recorded; P2's subgraph died at its gate, after its commits.
        before = _subjects(repo, "milestone/1..agent/p2-add-and-list-todos")
        assert sum(x.startswith("feat:") for x in before) == 1
        assert sum(x.startswith("docs:") for x in before) == 1
        out = resume_build(ctx, None)  # crash-style resume: no answer, same thread
        assert out.paused and out.state.phase == "merge_wait"
        runs = out.state.feature_runs
        assert [(r.feature_id, r.attempt, r.status) for r in runs] == [
            ("P1", 1, "passed"),
            ("P2", 1, "passed"),
        ]
        assert any("resuming thread" in e.text and "P2" in e.text for e in out.state.ledger)
        after = _subjects(repo, "milestone/1")
        assert sum("[scio:issues:M1]" in x for x in after) == 1
        assert sum(x.startswith("feat:") for x in after) == 2
        assert sum(x.startswith("docs:") for x in after) == 2
        assert sorted(p.name for p in (repo / ".scio/product/issues").iterdir()) == [
            "P1.md",
            "P2.md",
        ]
        assert len({r.thread_id for r in runs}) == 2
    finally:
        ctx.close()


def test_github_mode_crash_after_the_pr_duplicates_nothing(tmp_path: Path) -> None:
    """Milestone, issues and the PR go through the GitHub client (a canned ``gh``); the branch
    is pushed to a bare origin; a resume after the PR re-applies no write; the merge is read
    back from GitHub and pulled."""
    harness = _bootstrap(tmp_path)
    repo = tmp_path / "product"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    fake = FakeGh()
    client = GitHubClient("o/r", runner=fake)
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT), github=client)
    try:
        out = run_build(ctx, gh_repo="o/r")
        assert out.paused and out.state.phase == "merge_wait"
        s = out.state
        assert s.pr and s.pr.number and s.pr.url and s.pr.body_path is None
        assert s.issues == {"P1": 102, "P2": 103} or set(s.issues) == {"P1", "P2"}
        assert fake.milestones == {"M1: Milestone 1": min(fake.milestones.values())}
        assert [i["title"] for i in fake.issues.values()] == [
            "P1: Walking skeleton: the app starts, answers --version, and one smoke test passes",
            "P2: Add and list todos",
        ]
        assert not (repo / ".scio/product/issues").exists()  # GitHub holds the issues
        assert _git(origin, "rev-parse", "refs/heads/milestone/1") == _git(
            repo, "rev-parse", "milestone/1"
        )
        pr = next(iter(fake.prs.values()))
        assert pr["isDraft"] is False and "Closes #" in str(pr["body"])
        creates = lambda op: sum(1 for a, _ in fake.calls if a[:2] == op)  # noqa: E731
        assert creates(["pr", "create"]) == 1 and creates(["issue", "create"]) == 2
        milestone_posts = sum(
            1 for a, _ in fake.calls if a[0] == "api" and "-X" in a and "milestones" in " ".join(a)
        )
        assert milestone_posts == 1

        # the process dies after the PR: a resume with no answer asks again, writes nothing
        again = resume_build(ctx, None)
        assert again.paused and again.interrupt == out.interrupt
        assert creates(["pr", "create"]) == 1 and creates(["issue", "create"]) == 2
        assert len(fake.milestones) == 1

        # "merged" before the PR is merged: the orchestrator checks GitHub and asks again
        early = resume_build(ctx, "merged")
        assert early.paused and any("not merged yet" in e.text for e in early.state.ledger)
        # the human merges on GitHub: origin/main moves to the milestone head
        subprocess.run(
            ["git", "update-ref", "refs/heads/main", "refs/heads/milestone/1"],
            cwd=origin,
            check=True,
        )
        fake.merged.add(int(s.pr.number))
        out = resume_build(ctx, "merged")
        assert out.paused and out.state.milestone and out.state.milestone.id == "M2"
        assert out.state.milestones_done == ["M1"]
        progress = _progress(repo)
        assert progress.milestones[0].pr_number == s.pr.number
        assert progress.feature("P1") and progress.feature("P1").issue == s.issues["P1"]
        assert _git(repo, "merge-base", "--is-ancestor", "milestone/1", "main") == ""
    finally:
        ctx.close()


# ---------- orchestrator versus gate ----------


def _state_for_gate(
    ctx: RunContext, *, integration_ok: bool, statuses: list[str]
) -> ProductBuildState:
    spec = ProductSpec(
        name=PRODUCT,
        title="Todo",
        vision="v",
        users=["u"],
        features=[
            ProductFeature(id="P1", description="skeleton", acceptance=["a1"]),
            ProductFeature(id="P2", description="add", acceptance=["a2"]),
        ],
        quality_bar=["q"],
    )
    plan = MilestonePlan(
        number=1,
        title="t",
        branch="milestone/1",
        features=[MilestoneFeatureOutput(id="P1"), MilestoneFeatureOutput(id="P2")],
    )
    runs = [
        FeatureRun(feature_id=f"P{i + 1}", thread_id=f"t{i}", tier=Tier.T1, status=st)  # type: ignore[arg-type]
        for i, st in enumerate(statuses)
    ]
    return ProductBuildState(
        thread_id=ctx.thread_id,
        product_dir=str(ctx.sandbox.root),
        spec=spec,
        milestone=plan,
        feature_runs=runs,
        integration="[tests] failed" if not integration_ok else "[tests] passed",
        integration_ok=integration_ok,
        budgets=ctx.budget,
    )


def test_failing_integration_check_overrides_a_passing_milestone_gate(tmp_path: Path) -> None:
    harness = _harness(_settings(tmp_path), ScriptedResponder(milestone_gate_verdict="pass"))
    ctx = _ctx(tmp_path, harness, "build-gate")
    try:
        node = milestone_gate(ctx)
        patch = node(_state_for_gate(ctx, integration_ok=False, statuses=["passed", "passed"]))
        gate = patch["last_gate"]
        assert gate.verdict == "escalate_human" and gate.objective_met is False
        assert gate.reasoning.startswith("overridden: integration checks failed")
        assert patch["phase"] == "gate"

        patch = node(_state_for_gate(ctx, integration_ok=True, statuses=["passed", "skipped"]))
        assert patch["last_gate"].verdict == "escalate_human"
        assert "features not passed: P2" in patch["last_gate"].reasoning

        patch = node(_state_for_gate(ctx, integration_ok=True, statuses=["passed", "passed"]))
        assert patch["last_gate"].verdict == "pass" and patch["phase"] == "pr"
    finally:
        ctx.close()


def test_integration_checks_are_ground_truth_on_the_milestone_branch(tmp_path: Path) -> None:
    harness = _bootstrap(tmp_path)
    repo = tmp_path / "product"
    _git(repo, "checkout", "-q", "-b", "milestone/1")
    ctx = _ctx(tmp_path, harness, "build-int")
    try:
        text, ok = integration_checks(ctx)
        assert ok and "[lint] clean" in text and "[tests] passed" in text
        # M9: the profile's quality bar runs after tests and lint; an absent tool is a skip
        for name in ("coverage", "build", "audit"):
            line = next(ln for ln in text.splitlines() if ln.startswith(f"[{name}]"))
            assert " passed" in line or " skipped: tool absent" in line, line
        (repo / "tests" / "test_broken.py").write_text(
            "def test_broken():\n    assert 1 == 2\n", encoding="utf-8", newline="\n"
        )
        text, ok = integration_checks(ctx)
        assert not ok and "[tests] failed" in text
        state = _state_for_gate(ctx, integration_ok=True, statuses=["passed", "passed"])
        patch = integrate(ctx)(state)
        assert patch["exit_reason"] == "integration_failed" and patch["integration_ok"] is False
    finally:
        ctx.close()


# ---------- escalations and human decisions across features ----------


def test_escalation_records_cross_from_the_feature_run_to_the_product(tmp_path: Path) -> None:
    """P3's first implementation is wrong (same error twice), the advisor is consulted and
    the guided fix passes: the product records the escalation, the PR body and the report
    show it."""
    harness = _bootstrap(tmp_path, ScriptedResponder(coder_mode="wrong_first", milestone_size=3))
    repo = tmp_path / "product"
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.state.phase == "merge_wait"
        s = out.state
        assert s.milestone and s.milestone.feature_ids() == ["P1", "P2", "P3"]
        p3 = s.latest_run("P3")
        assert p3 is not None and p3.status == "passed"
        assert p3.escalations == 1 and p3.escalations_resolved == 1 and p3.iterations >= 4
        assert s.latest_run("P2") and s.latest_run("P2").escalations == 0
        inner = _feature_state(ctx, p3.thread_id)
        assert inner.escalations[0].outcome == "resolved" and inner.budgets.advisor_calls_used == 1
        assert s.pr and s.pr.body_path
        body = (repo / s.pr.body_path).read_text(encoding="utf-8")
        assert "| P3 Tags and JSON export | T1 | passed |" in body and "| 1/1 |" in body
        from scio.product.report import build_product_report

        report = build_product_report(s, [])
        md = report.to_markdown()
        assert "| P3 | 1 |" in md and "| 1/1 |" in md and report.features_usd > 0
    finally:
        ctx.close()


@pytest.mark.parametrize(
    ("mode", "inner_pause"),
    [("idle", False), ("design_question", True)],
    ids=["stall-exit", "escalate_human-interrupt"],
)
def test_feature_failure_stops_for_one_human_decision(
    tmp_path: Path, mode: str, inner_pause: bool
) -> None:
    """A feature that halts (a stall exit, or the task graph's own human interrupt) stops the
    build once: 'skip' leaves it out, 'retry' starts a new attempt on a new thread, anything
    else halts the build with feature_failed."""
    harness = _bootstrap(tmp_path, ScriptedResponder(coder_mode=mode))
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert out.paused and out.interrupt is not None
        assert out.interrupt["feature"] == "P1" and out.interrupt["thread"].endswith("-p1-a1")
        assert (out.interrupt["inner"] is not None) is inner_pause
        if inner_pause:
            assert out.interrupt["inner"]["reason"] == "escalate_human"
        # leftovers of the failed attempt (I-067): a stray file and a modified tracked file
        repo = tmp_path / "product"
        (repo / "tests" / "test_leftover.py").write_text(
            "def test_x():\n    pass\n", encoding="utf-8"
        )
        readme = repo / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nleftover\n", encoding="utf-8")
        out = resume_build(ctx, "skip")
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2"
        assert out.state.latest_run("P1") and out.state.latest_run("P1").status == "skipped"
        assert not (repo / "tests" / "test_leftover.py").exists()
        assert "leftover" not in _git(repo, "show", "HEAD:README.md")
        assert "leftover" not in readme.read_text(encoding="utf-8")
        out = resume_build(ctx, "retry")
        assert out.paused and out.interrupt and out.interrupt["feature"] == "P2"
        assert out.interrupt["thread"].endswith("-p2-a2")
        out = resume_build(ctx, "stop: fix the spec first")
        assert not out.paused and out.state.exit_reason == "feature_failed"
        runs = out.state.feature_runs
        assert [(r.feature_id, r.attempt, r.status) for r in runs] == [
            ("P1", 1, "skipped"),
            ("P2", 1, "failed"),
            ("P2", 2, "failed"),
        ]
        assert len({r.thread_id for r in runs}) == 3
        assert all(r.exit_reason not in (None, "success") for r in runs)
        assert any("fix the spec first" in n for n in out.state.human_notes)
    finally:
        ctx.close()


def test_milestone_gate_escalation_asks_the_human(tmp_path: Path) -> None:
    harness = _bootstrap(tmp_path, ScriptedResponder(milestone_gate_verdict="escalate_human"))
    ctx = _ctx(tmp_path, harness, build_thread_id(PRODUCT))
    try:
        out = run_build(ctx)
        assert (
            out.paused
            and out.interrupt
            and "Milestone gate verdict escalate_human" in out.interrupt["question"]
        )
        assert out.state.phase == "gate" and out.state.pr is None
        out = resume_build(ctx, "proceed")
        assert out.paused and out.state.phase == "merge_wait" and out.state.pr is not None
        out = resume_build(ctx, "stop")
        assert not out.paused and out.state.exit_reason == "stopped"
    finally:
        ctx.close()


# ---------- the CLI ----------


def test_cli_build_status_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scio.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCIO_PROVIDER_OVERRIDE", "fake")
    monkeypatch.setenv("SCIO_FAKE_SCRIPT", "1")
    monkeypatch.setenv("SCIO_CHECKPOINT_DSN", f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}")
    target = str(tmp_path / "product")
    assert main(["status", "--product", target]) == 2  # nothing bootstrapped yet
    assert main(["new", "--prompt", PROMPT, "--dir", target, "--thread", "new-cli"]) == 0
    assert main(["new", "--thread", "new-cli", "--dir", target, "--decision", "answers"]) == 0
    assert main(["new", "--thread", "new-cli", "--dir", target, "--decision", "approve"]) == 0
    capsys.readouterr()
    assert main(["build", "--product", target]) == 0
    out = capsys.readouterr().out
    assert "PAUSED for a human" in out and "merge" in out and "P1 a1 T0 passed" in out
    assert main(["status", "--product", target]) == 0
    status = capsys.readouterr().out
    assert "milestones: 0 done" in status and "P1 [todo]" in status
    # "merge and stop": the milestone is recorded, the thread ends, nothing more is spent
    assert main(["build", "--product", target, "--decision", "merge and stop"]) == 0
    out = capsys.readouterr().out
    assert "milestones done M1" in out and "phase=done" in out and "current -" in out
    assert main(["status", "--product", target]) == 0
    status = capsys.readouterr().out
    assert "milestones: 1 done" in status and "M1 [done]" in status and "P3 [todo]" in status
    thread = f"build-{PRODUCT}-m1"
    assert main(["report", "--thread", thread, "--out", str(tmp_path / "reports")]) == 0
    report = capsys.readouterr().out
    assert f"# Product build {thread}" in report and "| P2 | 1 |" in report
    data = json.loads((tmp_path / "reports" / f"{thread}.json").read_text(encoding="utf-8"))
    assert data["milestones_done"] == ["M1"] and data["current_milestone"] is None
    # a feature thread inside the build is a task thread, whatever its name (I-066)
    assert main(["report", "--thread", f"{thread}-p2-a1", "--out", str(tmp_path / "reports")]) == 0
    assert f"# Run {thread}-p2-a1: P2: Add and list todos" in capsys.readouterr().out
    # the next scio build starts a new thread for M2 (the m1 thread has ended)
    assert main(["build", "--product", target]) == 0
    out = capsys.readouterr().out
    assert f"build thread build-{PRODUCT}-m2" in out and "current M2" in out
    assert "P3 a1 T1 passed" in out and "PAUSED" in out


# ---------- M9: a dependency and an HTTP server through the whole loop ----------


def test_http_product_with_a_dependency_builds_and_passes_black_box_acceptance(
    tmp_path: Path,
) -> None:
    """M9 done criterion on the Fake: ``todo_api`` approves ``pyyaml`` (present offline through
    the host's packages), the install phase creates the product environment, P1 and P2 run
    through the task graph, and ``integrate`` starts the server, runs the black-box acceptance
    tests against it, and merges the milestone."""
    from evals.product_scripts_http import DEPENDENCY

    harness = _bootstrap(tmp_path, ScriptedResponder(product="todo_api", milestone_size=2))
    repo = tmp_path / "product"
    md = (repo / "PRODUCT.md").read_text(encoding="utf-8")
    assert "- kind: http" in md and f"- {DEPENDENCY}" in md
    assert (repo / "tests" / "acceptance" / "test_acceptance.py").is_file()
    ctx = _ctx(tmp_path, harness, build_thread_id("todo_api"))
    try:
        out = run_build(ctx)
        assert out.paused and out.state.phase == "merge_wait", out.state.ledger[-4:]
        s = out.state
        assert [(r.feature_id, r.status) for r in s.feature_runs] == [
            ("P1", "passed"),
            ("P2", "passed"),
        ]
        assert any(
            "product environment created" in e.text and DEPENDENCY in e.text for e in s.ledger
        )
        # the ledger says which sandbox the build ran in and what a local one does not enforce
        notices = [e.text for e in s.ledger if e.text.startswith("sandbox: local")]
        assert notices and "not enforced" in notices[0]
        assert s.integration and "[acceptance] passed (http http://127.0.0.1:" in s.integration
        assert "[tests] passed" in s.integration and "[lint] clean" in s.integration
        out = resume_build(ctx, "merge and stop")
        assert out.state.phase == "done" and out.state.milestones_done == ["M1"]
    finally:
        ctx.close()
    assert (repo / "todo_api" / "store.py").read_text(encoding="utf-8").startswith('"""YAML')
    assert (repo / ".scio" / "sandbox" / "venv").is_dir()
    assert _git(repo, "status", "--porcelain") == ""  # the black-box run left no junk behind
    _ci_green(repo)
