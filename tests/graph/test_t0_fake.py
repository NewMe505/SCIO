"""End-to-end T0 runs on the Fake provider: the happy path and every exit reason."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evals.fake_scripts import ScriptedResponder
from evals.solvers import make_origin
from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import RunOutcome, resume_t0, run_t0
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.settings import REPO_ROOT, Settings
from scio.state.models import ExitReason, FeatureStatus, Role, TaskSpec, Tier

FIXTURE = REPO_ROOT / "evals" / "tasks" / "t001_add_function"


def _spec(origin: Path) -> TaskSpec:
    text = (FIXTURE / "spec.md").read_text(encoding="utf-8")
    return TaskSpec(
        title="Add slugify",
        description=text,
        acceptance=acceptance_from_issue_body(text),
        repo_url=str(origin),
        base_branch="main",
    )


def _ctx(
    tmp_path: Path,
    responder: ScriptedResponder,
    *,
    usd_max: float = 5.0,
    max_iters: int = 10,
    max_stall: int = 2,
) -> RunContext:
    settings = Settings(
        provider_override="fake",
        checkpoint_dsn=f"sqlite:///{(tmp_path / 'ckpt.sqlite').as_posix()}",
    )
    config = ModelsConfig.load(settings.models_path)
    registry = ProviderRegistry(config, settings)
    registry.register("fake", FakeProvider(responder=responder))
    harness = Harness(
        config,
        registry,
        settings=settings,
        budget=BudgetGuard(BudgetLimits(tokens_max=500_000, usd_max=usd_max)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )
    return RunContext.create(
        thread_id="t-1",
        sandbox_root=tmp_path / "work",
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
        budget_limits=BudgetLimits(
            tokens_max=500_000,
            usd_max=usd_max,
            advisor_calls_max=0,
            max_iters=max_iters,
            max_stall=max_stall,
        ),
    )


def _commits(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True)
    return out.stdout.strip().splitlines()


def test_happy_path_end_to_end(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder())
    outcome = run_t0(ctx, _spec(origin))
    assert outcome.paused and outcome.state.phase == "merge_wait"
    assert outcome.interrupt is not None and outcome.interrupt["question"].startswith("Merge")
    assert outcome.state.exit_reason is None

    final: RunOutcome = resume_t0(ctx, "approved")
    s = final.state
    assert s.exit_reason is ExitReason.SUCCESS and s.phase == "done" and not final.paused
    assert s.human_notes == ["merge decision: approved"]
    assert s.routing is not None and s.routing.tier is Tier.T0
    assert all(f.status is FeatureStatus.PASSING and f.commit_sha for f in s.feature_list)
    assert s.progress.iterations == 1 and 0 < s.progress.tool_calls <= 10
    assert (
        s.github.branch == "agent/add-slugify" and s.github.base_sha and s.github.pr_number is None
    )
    assert s.budgets.tokens_used > 0 and s.budgets.usd_used > 0
    assert "pr_body" in s.artifacts and "verify:last" in s.artifacts
    assert s.artifacts["documenter:1"].produced_by is Role.DOCUMENTER  # report tool signals
    work = tmp_path / "work"
    assert "def slugify" in (work / "textutils" / "__init__.py").read_text()
    assert "slugify" in (work / "CHANGELOG.md").read_text()
    subjects = _commits(work)
    assert (
        len(subjects) == 3
        and subjects[0].startswith("docs:")
        and subjects[1].startswith("feat:")
        and "[scio:" in subjects[1]
    )
    tracked = subprocess.run(["git", "ls-files"], cwd=work, capture_output=True, text=True).stdout
    assert "__pycache__" not in tracked and ".pyc" not in tracked  # repo-local excludes (I-028)
    assert s.last_gate is not None and s.last_gate.verdict == "pass"
    assert any("routed to T0" in e.text for e in s.task_ledger)
    assert Path(s.artifacts["pr_body"].path).read_text(encoding="utf-8").startswith("## Summary")


def test_resume_after_crash_does_not_duplicate_commits(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder())
    run_t0(ctx, _spec(origin))  # pauses at merge_wait
    # "crash": a fresh context on the same checkpoint and effect log, then resume
    ctx2 = _ctx(tmp_path, ScriptedResponder())
    final = resume_t0(ctx2, "approved")
    assert final.state.exit_reason is ExitReason.SUCCESS
    assert len(_commits(tmp_path / "work")) == 3


def test_tool_cap_and_max_iters_exit(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder(coder_mode="busy"), max_iters=1)
    outcome = run_t0(ctx, _spec(origin))
    s = outcome.state
    assert s.exit_reason is ExitReason.MAX_ITERS and s.phase == "halted" and not outcome.paused
    assert s.progress.tool_calls == 11  # 10 executed + 1 refused by the cap
    assert all(f.status is FeatureStatus.FAILING for f in s.feature_list)
    assert "no changes" in Path(s.artifacts["verify:last"].path).read_text(encoding="utf-8")


def test_stall_exit(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder(coder_mode="idle"), max_iters=20, max_stall=1)
    s = run_t0(ctx, _spec(origin)).state
    assert (
        s.exit_reason is ExitReason.STALL
        and s.progress.stall_count == 2
        and s.progress.iterations == 3
    )


def test_budget_exit(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder(), usd_max=0.0005)
    outcome = run_t0(ctx, _spec(origin))
    assert outcome.state.exit_reason is ExitReason.BUDGET and not outcome.paused
    assert any("budget exhausted" in e.text for e in outcome.state.task_ledger)


def test_gate_rejection_escalates_to_human(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder(gate_verdict="replan"))
    outcome = run_t0(ctx, _spec(origin))
    assert (
        outcome.paused
        and outcome.interrupt is not None
        and outcome.interrupt["reason"] == "escalate_human"
    )
    final = resume_t0(ctx, "stop here")
    assert final.state.exit_reason is ExitReason.ESCALATE_HUMAN and final.state.phase == "halted"
    assert final.state.human_notes == ["human decision: stop here"]


def test_design_question_escalates_before_verify(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder(coder_mode="design_question"))
    outcome = run_t0(ctx, _spec(origin))
    assert outcome.paused and outcome.state.exit_reason is ExitReason.ESCALATE_HUMAN
    assert any("design decision needed" in e.text for e in outcome.state.task_ledger)


def test_non_t0_tier_halts_in_m3(tmp_path: Path) -> None:
    origin = make_origin(FIXTURE / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, ScriptedResponder(router_tier="T1"))
    outcome = run_t0(ctx, _spec(origin))
    assert (
        outcome.paused
        and outcome.state.routing is not None
        and outcome.state.routing.tier is Tier.T1
    )
    assert outcome.state.github.branch is None  # never prepared


@pytest.mark.parametrize("reason", list(ExitReason))
def test_every_exit_reason_is_covered(reason: ExitReason) -> None:
    covered = {
        ExitReason.SUCCESS: test_happy_path_end_to_end,
        ExitReason.MAX_ITERS: test_tool_cap_and_max_iters_exit,
        ExitReason.STALL: test_stall_exit,
        ExitReason.BUDGET: test_budget_exit,
        ExitReason.ESCALATE_HUMAN: test_gate_rejection_escalates_to_human,
        ExitReason.ADVISOR_CAP: None,  # M4: the advisor does not exist in T0
    }
    assert reason in covered
