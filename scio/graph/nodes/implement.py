"""T1/T2 execution nodes: ``implement`` (one step per cycle), ``debug``, ``advise``,
``review``, and the edges around the ``triage`` decision stored by ``verify``."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any, Literal

from scio.aci.toolbox import ToolBox
from scio.context.projections import advisor_brief, debug_message, review_message, step_message
from scio.graph.nodes.common import decision, diff_stat, fact, git_out, untracked_files
from scio.graph.nodes.planning import normalize_steps, summaries_text
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.loop.backends import make_worker
from scio.loop.impact import assess
from scio.state.models import (
    AdvisorOutput,
    EscalationRecord,
    EscalationTrigger,
    ExitReason,
    Plan,
    ReviewerOutput,
    Role,
    SCIOState,
    Verdict,
    WorkerOutput,
)

Node = Callable[[SCIOState], dict[str, Any]]
WRITER_TOOLS = (
    "repo_search",
    "view_file",
    "edit_file",
    "delete_file",
    "run_tests",
    "install_deps",
    "run_app",
)


def _diff_text(ctx: RunContext, state: SCIOState) -> str:
    """The full diff since the base commit, new files included (``git add -N``, I-063)."""
    base = state.github.base_sha
    new = untracked_files(ctx.sandbox)
    if new:
        ctx.sandbox.exec(["git", "add", "-N", "--", *new], timeout_s=60)
    return git_out(ctx.sandbox, "diff", base) if base else git_out(ctx.sandbox, "diff")


def _after_worker(
    ctx: RunContext,
    state: SCIOState,
    output: WorkerOutput,
    *,
    role: Role,
    tool_calls: int,
    stop: str,
    transcript: str,
) -> dict[str, Any]:
    attempt = state.progress.attempts_on_step + 1
    ref = ctx.artifacts.put(
        "log",
        transcript,
        produced_by=role,
        name=f"{role.value}-{state.progress.iterations + 1:02d}",
    )
    progress = state.progress.model_copy(
        update={
            # The plan, not the model, says which step is current: a worker that names a
            # finished step would otherwise move the run back onto it and loop (I-046).
            "current_step_id": state.progress.current_step_id or output.step_id,
            "attempts_on_step": attempt,
            "tool_calls": state.progress.tool_calls + tool_calls,
        }
    )
    flags = assess(
        _diff_text(ctx, state),
        max_files=ctx.escalation.high_impact_files,
        toolchain=ctx.toolchain,
    )
    entries = [
        fact(
            f"{role.value} attempt {attempt} on {progress.current_step_id}: {output.summary} "
            f"[{tool_calls} tool calls, stop={stop}, confidence={output.confidence}]",
            source=role,
            step_id=progress.current_step_id,
        )
    ]
    if output.sweep is not None:
        entries.append(
            fact(
                f"sweep '{output.sweep.pattern}': fixed {output.sweep.files_fixed}, "
                f"ruled out {output.sweep.ruled_out}",
                source=role,
                step_id=progress.current_step_id,
            )
        )
    return {
        "last_worker_output": output,
        "progress": progress,
        "artifacts": {**state.artifacts, f"{role.value}:{progress.iterations + 1}": ref},
        "high_impact_flags": flags,
        "advisor_guidance": None,
        "task_ledger": entries,
        "phase": "verify",
        **ctx.budget_update(),
    }


def implement(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        step = state.current_step()
        if step is None:
            entry = fact("implement: no open plan step", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.ESCALATE_HUMAN, "task_ledger": [entry]}
        last_ref = state.artifacts.get("verify:last")
        last_verify = ctx.artifacts.head(last_ref, 3000) if last_ref else None
        toolbox = ToolBox(
            ctx.sandbox, WRITER_TOOLS, human_gate=False, role="coder", context=ctx.tool_context
        )
        loop = make_worker(ctx, toolbox, "coder")
        msg = step_message(
            state.task,
            step,
            state.step_features(),
            repo_root=ctx.sandbox.root,
            attempt=state.progress.attempts_on_step + 1,
            summaries=summaries_text(ctx, state),
            guidance=state.advisor_guidance,
            last_verify=last_verify,
        )
        try:
            result = loop.run(msg, tags={"thread": ctx.thread_id, "step": step.id})
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted during implement: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        return _after_worker(
            ctx,
            state,
            result.output,
            role=Role.CODER,
            tool_calls=result.tool_calls,
            stop=result.stop,
            transcript=result.transcript(),
        )

    return node


def debug(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        last_ref = state.artifacts.get("verify:last")
        failing = ctx.artifacts.head(last_ref, 4000) if last_ref else "(no verify output)"
        toolbox = ToolBox(
            ctx.sandbox, WRITER_TOOLS, human_gate=False, role="debugger", context=ctx.tool_context
        )
        loop = make_worker(ctx, toolbox, "debugger")
        msg = debug_message(
            state.current_step(),
            repo_root=ctx.sandbox.root,
            failing_output=failing,
            signature=state.progress.last_error_signature,
            attempts=state.progress.fix_attempts_on_error,
            guidance=state.advisor_guidance,
        )
        try:
            result = loop.run(
                msg, tags={"thread": ctx.thread_id, "debug": str(state.progress.iterations)}
            )
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted during debug: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        return _after_worker(
            ctx,
            state,
            result.output,
            role=Role.DEBUGGER,
            tool_calls=result.tool_calls,
            stop=result.stop,
            transcript=result.transcript(),
        )

    return node


def after_worker(state: SCIOState) -> Literal["verify", "halt"]:
    return "halt" if state.exit_reason is not None else "verify"


def triage(state: SCIOState) -> Literal["debug", "advise", "halt"]:
    """Pure edge: reads the decision ``verify`` stored."""
    if state.exit_reason is not None:
        return "halt"
    return state.triage.route if state.triage is not None else "debug"


def advise(ctx: RunContext) -> Node:
    """Consult the Opus advisor with a compact brief; its output is the authoritative revision."""

    def node(state: SCIOState) -> dict[str, Any]:
        triggers = state.triage.triggers if state.triage else [EscalationTrigger.LOW_CONFIDENCE]
        step = state.current_step()
        step_id = step.id if step else (state.progress.current_step_id or "S?")
        last_ref = state.artifacts.get("verify:last")
        last_error = ctx.artifacts.head(last_ref, 1500) if last_ref else "(none)"
        verify_passed = state.progress.last_failure_kind == "none"  # ADR 0008 review
        brief = advisor_brief(
            state.task,
            step,
            triggers=triggers,
            diff_stat=diff_stat(ctx.sandbox, state.github.base_sha),
            last_error=last_error,
            attempts=state.progress.fix_attempts_on_error,
            worker_summary=state.last_worker_output.summary if state.last_worker_output else None,
            impact_flags=state.high_impact_flags,
            verify_passed=verify_passed,
        )
        brief_ref = ctx.artifacts.put(
            "brief", brief, produced_by=Role.SYSTEM, name=f"brief-{len(state.escalations) + 1:02d}"
        )
        before = ctx.harness.ledger.totals()
        req = ctx.harness.build_request(
            "advisor",
            system=ctx.prompts.get("advisor").render(),
            messages=[Message.user(brief)],
            tags={
                "thread": ctx.thread_id,
                "step": step_id,
                "trigger": ",".join(t.value for t in triggers),
            },
        )
        record = EscalationRecord(
            id=f"E{len(state.escalations) + 1}-{uuid.uuid4().hex[:6]}",
            trigger=triggers[0],
            step_id=step_id,
            brief=brief_ref,
        )
        try:
            out: AdvisorOutput = ctx.harness.structured(req, AdvisorOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at advise: {exc.message}", source=Role.SYSTEM)
            return {
                "exit_reason": ExitReason.BUDGET,
                "escalations": [*state.escalations, record],
                "task_ledger": [entry],
                **ctx.budget_update(),
            }
        after = ctx.harness.ledger.totals()
        record = record.model_copy(
            update={
                "verdict": out.verdict,
                "tokens": after.usage.total - before.usage.total,
                "usd": after.usd - before.usd,
                "outcome": "pending",
            }
        )
        budgets = ctx.budget.model_copy(
            update={
                "advisor_calls_used": state.budgets.advisor_calls_used + 1,
                "advisor_calls_on_step": {
                    **state.budgets.advisor_calls_on_step,
                    step_id: state.budgets.advisor_calls_on_step.get(step_id, 0) + 1,
                },
            }
        )
        ctx.budget = budgets
        patch: dict[str, Any] = {
            "escalations": [*state.escalations, record],
            "advisor_guidance": out.guidance,
            "high_impact_reviewed": True,
            "sample_disagreement": False,
            "triage": None,
            "task_ledger": [
                decision(
                    f"advisor ({record.trigger.value}) verdict {out.verdict.value}: "
                    f"{out.guidance[:300]}",
                    source=Role.ADVISOR,
                    step_id=step_id,
                )
            ],
        }
        if out.verdict is Verdict.REPLAN and out.revised_steps and state.plan is not None:
            done_ids = {s.id for s in state.plan.steps if s.done}
            steps = [s.model_copy(update={"done": s.id in done_ids}) for s in out.revised_steps]
            steps, plan_notes = normalize_steps(steps, [f.id for f in state.feature_list])
            patch["task_ledger"] = patch["task_ledger"] + [
                fact(n, source=Role.SYSTEM) for n in plan_notes
            ]
            patch["plan"] = Plan(
                version=state.plan.version + 1,
                author=Role.ADVISOR,
                steps=steps,
                rationale=out.guidance,
                revision_reason=f"advisor replan after {record.trigger.value}",
            )
            first_open = next((s.id for s in steps if not s.done), None)
            patch["progress"] = state.progress.model_copy(
                update={
                    "current_step_id": first_open,
                    "attempts_on_step": 0,
                    "fix_attempts_on_error": 0,
                }
            )
            patch["phase"] = "implement"
        elif out.verdict is Verdict.REPLAN:
            patch["phase"] = "plan"
        elif out.verdict is Verdict.ESCALATE_HUMAN:
            patch["exit_reason"] = ExitReason.ESCALATE_HUMAN
            patch["phase"] = "halted"
        elif out.verdict is Verdict.CONTINUE and verify_passed:
            # ADR 0008: a pre-commit review that approves the diff has nothing to resolve;
            # the run commits without another coder turn.
            record = record.model_copy(update={"outcome": "resolved"})
            patch["escalations"] = [*state.escalations, record]
            patch["phase"] = "checkpoint"
        else:
            patch["phase"] = "implement"
        patch.update(ctx.budget_update())
        patch["budgets"] = patch["budgets"].model_copy(
            update={
                "advisor_calls_used": budgets.advisor_calls_used,
                "advisor_calls_on_step": budgets.advisor_calls_on_step,
            }
        )
        ctx.budget = patch["budgets"]
        return patch

    return node


def after_advise(state: SCIOState) -> Literal["implement", "plan", "checkpoint", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    if state.phase == "checkpoint":  # ADR 0008: approved pre-commit review
        return "checkpoint"
    return "plan" if state.phase == "plan" else "implement"


def review(ctx: RunContext) -> Node:
    """Fresh-context Reviewer: task + diff + test output only. Advisory."""

    def node(state: SCIOState) -> dict[str, Any]:
        diff_text = _diff_text(ctx, state)
        diff_ref = ctx.artifacts.put("diff", diff_text, produced_by=Role.SYSTEM, name="diff-final")
        last_ref = state.artifacts.get("verify:last")
        test_out = ctx.artifacts.head(last_ref, 3000) if last_ref else "(no verify output)"
        req = ctx.harness.build_request(
            "reviewer",
            system=ctx.prompts.get("reviewer").render(),
            messages=[
                Message.user(review_message(state.task, diff_text=diff_text, test_output=test_out))
            ],
            tags={"thread": ctx.thread_id},
        )
        try:
            out: ReviewerOutput = ctx.harness.structured(req, ReviewerOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at review: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        blockers = [f for f in out.findings if f.severity == "blocker"]
        entry = fact(
            f"review {out.overall}: {len(out.findings)} finding(s), {len(blockers)} blocker(s)",
            source=Role.REVIEWER,
        )
        return {
            "last_review": out,
            "artifacts": {**state.artifacts, "diff:final": diff_ref},
            "task_ledger": [entry],
            "phase": "document",
            **ctx.budget_update(),
        }

    return node


def after_review(state: SCIOState) -> Literal["document", "halt"]:
    return "halt" if state.exit_reason is not None else "document"
