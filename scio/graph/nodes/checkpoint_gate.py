"""``git_checkpoint`` (one commit per passing feature set), ``gate`` (Opus), ``pr_ready``,
``merge_wait`` (human interrupt) and ``halt`` (non-success exits)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.types import interrupt

from scio.aci.tools import GitCheckpointInput, find_commit, git_checkpoint, head_sha
from scio.context.assembler import gate_message
from scio.graph.effects import effect_key, run_effect
from scio.graph.nodes.common import decision, diff_stat, fact
from scio.graph.nodes.verify import lint
from scio.graph.nodes.worker import run_docs_check
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.state.models import ExitReason, GateOutput, Role, SCIOState

Node = Callable[[SCIOState], dict[str, Any]]


def checkpoint(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        sb = ctx.sandbox
        pending = [
            f for f in state.feature_list if f.commit_sha is None and f.status.value == "passing"
        ]
        if not pending:
            return {"phase": "document"}
        ids = "+".join(f.id for f in pending)
        marker = f"[scio:{effect_key(ctx.thread_id, 'git_checkpoint', f'commit:{ids}')}]"
        desc = "; ".join(f.description for f in pending)[:120]
        message = f"feat: {desc} ({ids}) {marker}"

        def apply() -> str:
            git_checkpoint(sb, GitCheckpointInput(message=message))
            return head_sha(sb)

        sha = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="git_checkpoint",
            intent=f"commit:{ids}",
            apply=apply,
            probe=lambda: find_commit(sb, marker),
        )
        committed = {f.id for f in pending}
        features = [
            f.model_copy(update={"commit_sha": sha}) if f.id in committed else f
            for f in state.feature_list
        ]
        entry = fact(f"committed {ids} as {sha[:12]}", source=Role.SYSTEM)
        github = state.github.model_copy(update={"head_sha": sha})
        progress = state.progress
        if state.plan is not None:
            next_open = next((s.id for s in state.plan.steps if not s.done), None)
            progress = progress.model_copy(
                update={
                    "current_step_id": next_open,
                    "attempts_on_step": 0,
                    "fix_attempts_on_error": 0,
                    "last_error_signature": None,
                }
            )
        return {
            "feature_list": features,
            "github": github,
            "progress": progress,
            "task_ledger": [entry],
            "phase": "document",
        }

    return node


def after_checkpoint(state: SCIOState) -> Literal["implement", "review", "document"]:
    """T0 goes straight to docs; T1/T2 continue with the next step or go to review."""
    if state.plan is None:
        return "document"
    if any(not s.done for s in state.plan.steps):
        return "implement"
    return "review"


def gate(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        sb = ctx.sandbox
        test_ref = state.artifacts.get("verify:last")
        test_out = ctx.artifacts.head(test_ref, 2500) if test_ref else "(no verify output)"
        msg = gate_message(
            state.task,
            state.feature_list,
            diff_stat=diff_stat(sb, state.github.base_sha),
            test_output=test_out,
            lint_output=lint(ctx),
            docs_check=run_docs_check(ctx),
        )
        req = ctx.harness.build_request(
            "gate",
            system=ctx.prompts.get("gate").render(),
            messages=[Message.user(msg)],
            tags={"thread": ctx.thread_id},
        )
        try:
            out = ctx.harness.structured(req, GateOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at gate: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        # Ground truth guard: the gate never passes a run whose checks are failing.
        if not state.all_passing() and out.verdict == "pass":
            out = out.model_copy(
                update={
                    "verdict": "escalate_human",
                    "objective_met": False,
                    "reasoning": "overridden: feature checks are not all passing. " + out.reasoning,
                }
            )
        exit_reason = None if out.verdict == "pass" else ExitReason.ESCALATE_HUMAN
        entry = decision(
            f"gate verdict {out.verdict}: {out.reasoning[:300]}"
            + (f" unmet: {out.unmet_acceptance}" if out.unmet_acceptance else ""),
            source=Role.GATE,
        )
        return {
            "last_gate": out,
            "exit_reason": exit_reason,
            "task_ledger": [entry],
            "phase": "pr_ready" if out.verdict == "pass" else "halted",
            **ctx.budget_update(),
        }

    return node


def after_gate(state: SCIOState) -> Literal["pr_ready", "halt"]:
    return (
        "pr_ready" if state.last_gate is not None and state.last_gate.verdict == "pass" else "halt"
    )


def pr_ready(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        github = state.github
        if ctx.github is not None and github.pr_number is not None:
            number = github.pr_number
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="pr_ready",
                intent=f"ready:{number}",
                apply=lambda: ctx.github.mark_ready(number) if ctx.github else False,
            )
            github = github.model_copy(update={"pr_state": "ready"})
            entry = fact(f"PR #{number} marked ready for review", source=Role.SYSTEM)
        else:
            entry = fact("no PR to mark ready (offline run)", source=Role.SYSTEM)
        return {"github": github, "task_ledger": [entry], "phase": "merge_wait"}

    return node


def merge_wait(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        payload = {
            "question": "Merge this pull request?",
            "pr_number": state.github.pr_number,
            "branch": state.github.branch,
            "features": [f"{f.id}: {f.description}" for f in state.feature_list],
            "cost_usd": round(state.budgets.usd_used, 4),
        }
        answer = interrupt(payload)
        return {
            "human_notes": [f"merge decision: {answer}"],
            "exit_reason": ExitReason.SUCCESS,
            "phase": "done",
        }

    return node


def halt(ctx: RunContext) -> Node:
    """Terminal node for every non-success exit. Escalations stop for a human."""

    def node(state: SCIOState) -> dict[str, Any]:
        reason = state.exit_reason or ExitReason.ESCALATE_HUMAN
        notes: list[str] = []
        if reason in (ExitReason.ESCALATE_HUMAN, ExitReason.ADVISOR_CAP):
            answer = interrupt(
                {
                    "question": "The run needs a human decision.",
                    "reason": reason.value,
                    "triage": state.triage.model_dump() if state.triage else None,
                    "last_gate": state.last_gate.model_dump() if state.last_gate else None,
                    "ledger_tail": [e.text for e in state.task_ledger[-5:]],
                }
            )
            notes.append(f"human decision: {answer}")
        return {"exit_reason": reason, "human_notes": notes, "phase": "halted"}

    return node
