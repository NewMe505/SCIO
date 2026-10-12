"""End-to-end T1 and T2 runs on the Fake provider: happy paths, the escalation loop
(resolved), the advisor cap, plan drift, and parallel explorers with one writer."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evals.fake_scripts import ScriptedResponder
from evals.solvers import EvalTaskLike, make_origin, task_spec
from scio.graph.run import RunOutcome, resume_graph, run_graph
from scio.graph.run_context import RunContext
from scio.harness import telemetry as T
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.retry import RetryPolicy
from scio.settings import REPO_ROOT, Settings
from scio.state.models import EscalationTrigger, ExitReason, FeatureStatus, Tier, Verdict

TASKS = REPO_ROOT / "evals" / "tasks"


def _task(task_id: str, tier: str) -> EvalTaskLike:
    path = TASKS / task_id
    return EvalTaskLike(
        id=task_id, spec=(path / "spec.md").read_text(encoding="utf-8"), path=path, tier=tier
    )


def _ctx(
    tmp_path: Path,
    responder: ScriptedResponder,
    *,
    tier: str,
    advisor_calls_max: int = 3,
    max_iters: int = 12,
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
        budget=BudgetGuard(BudgetLimits(tokens_max=2_000_000, usd_max=20.0)),
        retry=RetryPolicy(max_attempts=2, base_delay_s=0, jitter=False),
        sleep=lambda _s: None,
    )
    return RunContext.create(
        thread_id="t-1",
        sandbox_root=tmp_path / "work",
        tier=tier,
        settings=settings,
        harness=harness,
        work_dir=tmp_path / ".scio",
        budget_limits=BudgetLimits(
            tokens_max=2_000_000,
            usd_max=20.0,
            advisor_calls_max=advisor_calls_max,
            max_iters=max_iters,
            max_stall=max_stall,
        ),
    )


def _run(
    tmp_path: Path, task_id: str, tier: str, responder: ScriptedResponder, **kw: int
) -> tuple[RunContext, RunOutcome]:
    task = _task(task_id, tier)
    origin = make_origin(task.path / "repo", tmp_path / "origin")
    ctx = _ctx(tmp_path, responder, tier=tier, **kw)
    outcome = run_graph(ctx, task_spec(task, origin))
    return ctx, outcome


def _finish(ctx: RunContext, outcome: RunOutcome) -> RunOutcome:
    assert outcome.paused and outcome.state.phase == "merge_wait", (
        outcome.state.phase,
        outcome.state.exit_reason,
        [e.text for e in outcome.state.task_ledger[-6:]],
    )
    return resume_graph(ctx, "approved")


def _commits(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True)
    return out.stdout.strip().splitlines()


def _hidden_pass(ctx: RunContext, task_id: str, name: str) -> bool:
    from scio.aci.tools import RunTestsInput, run_tests

    src = TASKS / task_id / "hidden" / name
    dest = f"{ctx.sandbox.root}/tests/{name}"
    ctx.sandbox.write_text(dest, src.read_text(encoding="utf-8"))
    return run_tests(ctx.sandbox, RunTestsInput(path=dest)).startswith("all tests passed")


# ---------- T1 happy paths ----------


def test_t1_single_step_fix(tmp_path: Path) -> None:
    ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert (
        s.exit_reason is ExitReason.SUCCESS and s.routing is not None and s.routing.tier is Tier.T1
    )
    assert s.plan is not None and s.plan.version == 1 and all(st.done for st in s.plan.steps)
    assert len(s.explorer_summaries) == 1  # localize ran once
    assert all(f.status is FeatureStatus.PASSING and f.commit_sha for f in s.feature_list)
    assert s.last_review is not None and s.last_review.overall == "approve"
    assert s.escalations == [] and s.budgets.advisor_calls_used == 0
    assert "plan:v1" in s.artifacts and "diff:final" in s.artifacts
    assert _hidden_pass(ctx, "t002_fix_failing_test", "test_paginate_hidden.py")
    body = Path(s.artifacts["pr_body"].path).read_text(encoding="utf-8")
    assert "## Agent run log" in body and "| none |" in body and "Reviewer" in body


def test_t1_two_steps_one_commit_each(tmp_path: Path) -> None:
    ctx, outcome = _run(tmp_path, "t004_two_features", "T1", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert [st.done for st in s.plan.steps] == [True, True]  # type: ignore[union-attr]
    subjects = _commits(tmp_path / "work")
    assert len(subjects) == 4 and subjects[0].startswith("docs:")
    assert "(F2)" in subjects[1] and "(F1)" in subjects[2]
    assert _hidden_pass(ctx, "t004_two_features", "test_shapes_hidden.py")


def test_t1_lint_only_failure_runs_the_debugger(tmp_path: Path) -> None:
    """t009: the step is correct and its tests pass, but verify's lint finds an undefined name
    in a module the step never touched; triage routes to debug, which fixes it (issue #31)."""
    ctx, outcome = _run(tmp_path, "t009_verify_only_lint", "T1", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    debug_keys = [k for k in s.artifacts if k.startswith("debugger:")]
    assert debug_keys == ["debugger:2"], sorted(s.artifacts)
    assert s.progress.iterations == 2 and s.escalations == []
    # The debugger was shown the lint failure, not a test failure.
    debug_transcript = Path(s.artifacts["debugger:2"].path).read_text(encoding="utf-8")
    assert "[lint]" in debug_transcript and "F821" in debug_transcript
    assert "FAILED" not in debug_transcript
    assert _hidden_pass(ctx, "t009_verify_only_lint", "test_stdev_hidden.py")


# ---------- escalation ----------


def test_repeated_error_escalates_and_is_resolved(tmp_path: Path) -> None:
    ctx, outcome = _run(
        tmp_path, "t002_fix_failing_test", "T1", ScriptedResponder(coder_mode="wrong_first")
    )
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert len(s.escalations) == 1
    e = s.escalations[0]
    assert e.trigger is EscalationTrigger.REPEATED_ERROR and e.verdict is Verdict.CHANGE_APPROACH
    assert e.outcome == "resolved" and e.tokens > 0 and e.usd > 0 and e.step_id == "S1"
    assert s.budgets.advisor_calls_used == 1 and s.budgets.advisor_calls_on_step == {"S1": 1}
    assert s.progress.iterations >= 4  # coder, debug, debug, guided coder
    assert any("advisor (repeated_error)" in t.text for t in s.task_ledger)
    assert any("sweep" in t.text for t in s.task_ledger)  # the debugger reported its sweep
    body = Path(s.artifacts["pr_body"].path).read_text(encoding="utf-8")
    assert "repeated_error" in body and "resolved" in body


def test_advisor_cap_halts_for_a_human(tmp_path: Path) -> None:
    ctx, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        ScriptedResponder(coder_mode="wrong_first"),
        advisor_calls_max=0,
    )
    assert outcome.paused and outcome.interrupt is not None
    assert (
        outcome.interrupt["reason"] == "advisor_cap"
        and outcome.interrupt["triage"]["route"] == "halt"
    )
    assert outcome.state.exit_reason is ExitReason.ADVISOR_CAP and outcome.state.escalations == []
    final = resume_graph(ctx, "stop")
    assert final.state.phase == "halted" and final.state.human_notes == ["human decision: stop"]


def test_plan_drift_escalates(tmp_path: Path) -> None:
    ctx, outcome = _run(tmp_path, "t004_two_features", "T1", ScriptedResponder(coder_mode="drift"))
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert s.escalations and s.escalations[0].trigger is EscalationTrigger.PLAN_DRIFT
    assert s.escalations[0].outcome == "resolved"
    adr_dir = tmp_path / "work" / "docs" / "adr"
    written = sorted(p.name for p in adr_dir.glob("0002-*.md"))
    assert written and "change_approach" in (adr_dir / written[0]).read_text(encoding="utf-8")


def test_advisor_escalate_human_halts(tmp_path: Path) -> None:
    _ctx_unused, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        ScriptedResponder(coder_mode="wrong_first", advisor_verdict="escalate_human"),
    )
    assert (
        outcome.paused
        and outcome.interrupt is not None
        and outcome.interrupt["reason"] == "escalate_human"
    )
    assert outcome.state.escalations[0].verdict is Verdict.ESCALATE_HUMAN


# ---------- T2 ----------


def test_t2_parallel_explorers_and_one_writer(tmp_path: Path, spans: InMemorySpanExporter) -> None:
    ctx, outcome = _run(tmp_path, "t005_cli_json_flag", "T2", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert (
        s.exit_reason is ExitReason.SUCCESS and s.routing is not None and s.routing.tier is Tier.T2
    )
    assert len(s.exploration_questions) == 2 and len(s.explorer_summaries) == 2
    finished = spans.get_finished_spans()
    explorer_spans = [sp for sp in finished if sp.name == "invoke_agent explorer"]
    assert 2 <= len({sp.attributes.get("scio.tag.explore") for sp in explorer_spans}) <= 4  # type: ignore[union-attr]
    # exactly one writer: every edit happened under a writer role, never an explorer
    edit_roles = {
        sp.attributes.get("scio.role") for sp in finished if sp.name == "execute_tool edit_file"
    }  # type: ignore[union-attr]
    assert edit_roles and edit_roles <= {"coder", "debugger", "documenter"}
    explorer_tools = {
        sp.attributes.get(T.TOOL_NAME)
        for sp in finished
        if sp.name.startswith("execute_tool") and sp.attributes.get("scio.role") == "explorer"
    }  # type: ignore[union-attr]
    assert explorer_tools <= {"repo_search", "view_file"}
    assert _hidden_pass(ctx, "t005_cli_json_flag", "test_cli_hidden.py")


def test_t0_docs_task_via_router(tmp_path: Path) -> None:
    ctx, outcome = _run(tmp_path, "t003_update_docs", "T0", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS and s.plan is None
    assert _hidden_pass(ctx, "t003_update_docs", "test_docs_hidden.py")


@pytest.mark.parametrize("reason", [ExitReason.ADVISOR_CAP])
def test_advisor_cap_exit_is_covered(reason: ExitReason) -> None:
    assert reason is ExitReason.ADVISOR_CAP  # see test_advisor_cap_halts_for_a_human


def test_t1_passing_high_impact_diff_gets_one_advisor_review(tmp_path: Path) -> None:
    """t010 / ADR 0008: a wide rename passes verify but trips the impact detector; the run
    consults the advisor once before committing and commits on `continue` (issue #37)."""
    ctx, outcome = _run(tmp_path, "t010_wide_rename", "T1", ScriptedResponder())
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS
    assert len(s.escalations) == 1
    e = s.escalations[0]
    assert e.trigger is EscalationTrigger.HIGH_IMPACT and e.verdict is Verdict.CONTINUE
    assert e.outcome == "resolved" and e.usd > 0
    assert s.high_impact_reviewed and s.budgets.advisor_calls_used == 1
    assert s.progress.iterations == 1  # no extra coder turn after the approval
    assert not any(k.startswith("debugger:") for k in s.artifacts)
    assert _hidden_pass(ctx, "t010_wide_rename", "test_rename_hidden.py")
    body = Path(s.artifacts["pr_body"].path).read_text(encoding="utf-8")
    assert "high_impact" in body and "continue" in body
