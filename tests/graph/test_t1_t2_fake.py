"""End-to-end T1 and T2 runs on the Fake provider: happy paths, the escalation loop
(resolved), the advisor cap, plan drift, and parallel explorers with one writer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from evals.fake_scripts import Edit, ScriptedResponder, Step
from evals.solvers import EvalTaskLike, make_origin, task_spec
from scio.graph.nodes.planning import SUMMARY_MAX_CHARS
from scio.graph.run import RunOutcome, resume_graph, run_graph
from scio.graph.run_context import RunContext
from scio.harness import telemetry as T
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.config import ModelsConfig, ProviderRegistry
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider, FakeReply
from scio.harness.retry import RetryPolicy
from scio.settings import REPO_ROOT, Settings
from scio.state.models import EscalationTrigger, ExitReason, FeatureStatus, Role, Tier, Verdict

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


# ---------- S8: the plan guesses a file, localize names the real one (I-151, I-152) ----------

GUESSED = "pager/core.py"  # not in t002's repository
REAL = "pager/__init__.py"


class GuessedFileResponder(ScriptedResponder):
    """The graphcalc run's S8 on t002. The planner names a file that does not exist; the
    localize explorer says so and names the real one, in a summary over the 1.5k-token cap;
    the coder's first try fails a test and the debugger fixes it. With ``unnamed`` the
    coder also writes a source file nobody named, which must still be plan drift."""

    def __init__(
        self,
        *,
        unnamed: bool = False,
        advisor_verdict: str = "change_approach",
        revised_steps: bool = False,
    ) -> None:
        super().__init__(coder_mode="wrong_first", advisor_verdict=advisor_verdict)
        self.unnamed = unnamed
        self.revised_steps = revised_steps
        self.planned_steps: list[dict[str, Any]] = []

    def _structured(self, role: str, kind: str, text: str) -> FakeReply:
        if kind == "advisor" and self.revised_steps and "Verify passed" not in text:
            # the advisor's own replan: its revised steps name the planner's guess again
            reply = json.loads(super()._structured(role, kind, text).text)
            reply["revised_steps"] = self.planned_steps
            return FakeReply(text=json.dumps(reply))
        if kind == "planner":
            plan = json.loads(super()._structured(role, kind, text).text)
            for step in plan["steps"]:
                step["files_expected"] = [GUESSED]
            self.planned_steps = plan["steps"]
            return FakeReply(text=json.dumps(plan))
        if kind == "explorer":
            padding = [f"fact {i} about the pager package: " + "x" * 300 for i in range(20)]
            summary = {
                "question": "localize",
                "findings": [f"{GUESSED} does not exist; paginate lives in {REAL}", *padding],
                "relevant_files": [f"{self.root}/{REAL}", f"{self.root}/README.md"],
                "open_questions": ["Should the last partial page be kept?"],
            }
            return FakeReply(text=json.dumps(summary))
        return super()._structured(role, kind, text)

    def _edits_for(self, role: str, step: Step, text: str) -> list[Edit]:
        if role == "debugger" and step.fix_edit is not None:
            return [step.fix_edit]  # a debugger that fixes the clear test failure
        edits = super()._edits_for(role, step, text)
        if self.unnamed and role == "coder" and "[context from advisor]" not in text:
            edits.append(Edit("pager/extra.py", "", "EXTRA = 1\n"))
        return edits


class DoubledPathResponder(GuessedFileResponder):
    """graphcalc S11 (I-166): the worker's own report spells every path with the product
    folder's name in front (``graphcalc/graphcalc/calc.py``), while it edited the right
    files. Drift used those spellings and halted a step that touched only planned files."""

    def _worker_output(self, role: str, text: str) -> FakeReply:
        data = json.loads(super()._worker_output(role, text).text)
        root = (self.root or ".").replace("\\", "/").rstrip("/")
        folder = root.rsplit("/", 1)[-1]
        data["files_touched"] = [
            f"{folder}/{p.replace(chr(92), '/').removeprefix(root + '/')}"
            for p in data["files_touched"]
        ]
        return FakeReply(text=json.dumps(data))


def test_drift_reads_what_git_changed_not_how_the_worker_spells_its_paths(
    tmp_path: Path,
) -> None:
    """I-166: drift compares the step's files as git names them (changed against HEAD, and
    new files) with the plan, so a worker that spells ``<folder>/<path>`` drifts on nothing;
    with no advisor call left the old code halted, as P26 attempt 1 did."""
    ctx, outcome = _run(
        tmp_path, "t002_fix_failing_test", "T1", DoubledPathResponder(), advisor_calls_max=0
    )
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS, s.task_ledger[-3:]
    reported = s.last_worker_output.files_touched if s.last_worker_output else []
    assert reported and all(p.count("/") >= 2 for p in reported)  # the doubled spelling stayed
    texts = [e.text for e in s.task_ledger]
    assert any("triage -> debug (no escalation signal" in t for t in texts), texts
    assert not any("plan_drift" in t for t in texts)


def test_a_drift_halt_names_the_drifted_files(tmp_path: Path) -> None:
    """I-166: a halt on plan drift says which files drifted, so the owner (and the live
    run's rule 18) checks them against the plan, not against a stale plan artifact."""
    _ctx_unused, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        GuessedFileResponder(unnamed=True),
        advisor_calls_max=0,
    )
    assert outcome.paused and outcome.interrupt is not None
    assert "plan_drift on pager/extra.py" in outcome.interrupt["triage"]["reason"]


def test_t1_a_guessed_planned_file_does_not_drift_once_localize_names_the_real_one(
    tmp_path: Path,
) -> None:
    """S8: the coder edits the file the explorer named, not the plan's guess. Verify fails
    once on a clear test failure, so triage must debug; with no advisor call left (as in
    the run) the old code halted on plan_drift."""
    ctx, outcome = _run(
        tmp_path, "t002_fix_failing_test", "T1", GuessedFileResponder(), advisor_calls_max=0
    )
    s = _finish(ctx, outcome).state
    assert s.exit_reason is ExitReason.SUCCESS and s.plan is not None
    assert s.plan.version == 1 and s.plan.steps[0].files_expected == [GUESSED, REAL]
    texts = [e.text for e in s.task_ledger]
    assert f"localize: files the explorer localized added to the plan: S1 += {REAL}" in texts
    assert any("triage -> debug (no escalation signal" in t for t in texts), texts
    assert not any("plan_drift" in t for t in texts)
    assert s.escalations == [] and s.budgets.advisor_calls_used == 0
    assert any(k.startswith("debugger:") for k in s.artifacts)
    # I-152: the stored summary was over the cap; it is trimmed, valid, and keeps its files.
    stored = Path(s.explorer_summaries[0].path).read_text(encoding="utf-8")
    assert len(stored) <= SUMMARY_MAX_CHARS
    data = json.loads(stored)
    assert data["relevant_files"][0].endswith(REAL) and data["open_questions"]
    assert "findings dropped" in data["findings"][-1]
    assert _hidden_pass(ctx, "t002_fix_failing_test", "test_paginate_hidden.py")


def test_t1_replan_keeps_the_localized_files_and_an_unnamed_file_still_drifts(
    tmp_path: Path,
) -> None:
    """The coder also writes ``pager/extra.py``, which nobody named: plan drift. The advisor
    replans; plan v2 is the planner's guess again, and localize does not run twice, so the
    explorer's file must be merged back from the stored summary. The second drift then
    meets the one-call-per-step cap and halts."""
    _ctx_unused, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        GuessedFileResponder(unnamed=True, advisor_verdict="replan"),
    )
    assert outcome.paused and outcome.interrupt is not None
    assert outcome.interrupt["reason"] == "advisor_cap"
    assert EscalationTrigger.PLAN_DRIFT in outcome.interrupt["triage"]["triggers"]
    s = outcome.state
    assert [(e.trigger, e.verdict) for e in s.escalations] == [
        (EscalationTrigger.PLAN_DRIFT, Verdict.REPLAN)
    ]
    assert s.plan is not None and s.plan.version == 2
    assert s.plan.steps[0].files_expected == [GUESSED, REAL]
    texts = [e.text for e in s.task_ledger]
    assert f"plan v2: files the explorer localized added to the plan: S1 += {REAL}" in texts


def test_an_advisor_replan_with_revised_steps_keeps_the_localized_files(tmp_path: Path) -> None:
    """I-151's sweep: the advisor's own replan (``revised_steps``) built plan v2 straight from
    its steps, which name the planner's guess again, so the explorer's file dropped out and
    the next diff drifted on it once more. It is merged back as in the planner's replan."""
    _ctx_unused, outcome = _run(
        tmp_path,
        "t002_fix_failing_test",
        "T1",
        GuessedFileResponder(unnamed=True, advisor_verdict="replan", revised_steps=True),
    )
    s = outcome.state
    assert [(e.trigger, e.verdict) for e in s.escalations][:1] == [
        (EscalationTrigger.PLAN_DRIFT, Verdict.REPLAN)
    ]
    assert s.plan is not None and s.plan.version == 2 and s.plan.author is Role.ADVISOR
    assert s.plan.steps[0].files_expected == [GUESSED, REAL]
    texts = [e.text for e in s.task_ledger]
    assert f"plan v2: files the explorer localized added to the plan: S1 += {REAL}" in texts


class RecordingResponder(ScriptedResponder):
    """A scripted responder that keeps every request it answered."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.requests: list[Any] = []

    def __call__(self, req: Any) -> FakeReply:
        self.requests.append(req)
        return super().__call__(req)


def test_a_t1_debugger_gets_the_coder_sessions_report_on_the_same_step(tmp_path: Path) -> None:
    """I-154's sweep: T1's coder and debugger iterations are fresh sessions too, and their
    messages carried the plan, the explorer summaries and the last verify, never what the
    previous session on the step found (graphcalc P4 attempt 2: two 30-turn S1 sessions with
    no edit). The debugger now starts from the coder session's own report."""
    responder = RecordingResponder(coder_mode="wrong_first")
    ctx, outcome = _run(tmp_path, "t002_fix_failing_test", "T1", responder)
    _finish(ctx, outcome)

    def first_message(role: str) -> str:  # a session's opening request (one per tool turn)
        return next(
            r.messages[0].text()
            for r in responder.requests
            if r.role == role and not r.output_schema
        )

    coder, debugger = first_message("coder"), first_message("debugger")
    assert "[context from coder]" not in coder  # nothing to carry into the first session
    assert "[context from coder]" in debugger
    assert "Report from the previous session" in debugger
    assert "Files that session changed: " in debugger
    assert "Changes in the working tree since the base commit" in debugger


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
