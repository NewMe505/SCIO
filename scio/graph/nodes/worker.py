"""Worker nodes: ``t0_loop`` (Coder) and ``document`` (Documenter + PR body + PR effects)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from scio.aci.toolbox import ToolBox
from scio.aci.tools import GitCheckpointInput, find_commit, git_checkpoint, git_push, head_sha
from scio.context.assembler import coder_message, documenter_message, pr_body_message
from scio.context.projections import review_notes
from scio.graph.effects import effect_key, run_effect
from scio.graph.nodes.common import decision, diff_stat, fact, slug
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.loop.backends import make_worker
from scio.state.models import ExitReason, Role, SCIOState, Verdict

Node = Callable[[SCIOState], dict[str, Any]]

CODER_TOOLS = ("repo_search", "view_file", "edit_file", "run_tests")
DOCUMENTER_TOOLS = ("repo_search", "view_file", "edit_file", "run_tests")


class PRBody(BaseModel):
    """The Documenter's pull request body, rendered into the repository's PR template."""

    summary: str = Field(max_length=1200)
    plan_vs_done: list[str]
    test_evidence: str = Field(max_length=2000)
    risk_notes: str = Field(max_length=1200)


def t0_loop(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        attempt = state.progress.iterations + 1
        last_verify = None
        ref = state.artifacts.get("verify:last")
        if ref is not None:
            last_verify = ctx.artifacts.head(ref, 3000)
        toolbox = ToolBox(
            ctx.sandbox,
            CODER_TOOLS,
            human_gate=False,
            max_calls=ctx.limits.max_tool_calls_per_iter,
            role="coder",
        )
        loop = make_worker(ctx, toolbox, "coder")
        msg = coder_message(
            state.task,
            state.feature_list,
            repo_root=ctx.sandbox.root,
            attempt=attempt,
            last_verify=last_verify,
        )
        try:
            result = loop.run(msg, tags={"thread": ctx.thread_id, "attempt": str(attempt)})
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted during implement: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        transcript = ctx.artifacts.put(
            "log", result.transcript(), produced_by=Role.CODER, name=f"coder-{attempt:02d}"
        )
        progress = state.progress.model_copy(
            update={
                "current_step_id": result.output.step_id,
                "attempts_on_step": state.progress.attempts_on_step + 1,
                "tool_calls": state.progress.tool_calls + result.tool_calls,
            }
        )
        entries = [
            fact(
                f"coder attempt {attempt}: {result.output.summary} "
                f"[{result.tool_calls} tool calls, stop={result.stop}]",
                source=Role.CODER,
                step_id=result.output.step_id,
            )
        ]
        exit_reason = None
        if result.output.needs_design_decision:
            entries.append(
                decision(
                    f"design decision needed: {result.output.design_question}", source=Role.CODER
                )
            )
            exit_reason = ExitReason.ESCALATE_HUMAN
        return {
            "last_worker_output": result.output,
            "progress": progress,
            "artifacts": {**state.artifacts, f"coder:{attempt}": transcript},
            "task_ledger": entries,
            "exit_reason": exit_reason,
            "phase": "verify",
            **ctx.budget_update(),
        }

    return node


def after_implement(state: SCIOState) -> Literal["verify", "halt"]:
    return "halt" if state.exit_reason is not None else "verify"


def document(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        sb = ctx.sandbox
        stat = diff_stat(sb, state.github.base_sha)
        test_ref = state.artifacts.get("verify:last")
        test_summary = ctx.artifacts.head(test_ref, 1500) if test_ref else "(no verify output)"
        toolbox = ToolBox(sb, DOCUMENTER_TOOLS, human_gate=False, max_calls=12, role="documenter")
        loop = make_worker(ctx, toolbox, "documenter")
        docs_check: str | None = None
        logs: dict[str, Any] = {}
        for round_ in (1, 2):
            msg = documenter_message(
                state.task,
                state.feature_list,
                repo_root=sb.root,
                diff_stat=stat,
                test_summary=test_summary,
                docs_check=docs_check,
            )
            try:
                result = loop.run(msg, tags={"thread": ctx.thread_id, "round": str(round_)})
            except BudgetExceeded as exc:
                entry = fact(f"budget exhausted during document: {exc.message}", source=Role.SYSTEM)
                return {
                    "exit_reason": ExitReason.BUDGET,
                    "task_ledger": [entry],
                    **ctx.budget_update(),
                }
            # Kept like the coder's transcripts: the run report reads tool signals from it.
            logs[f"documenter:{round_}"] = ctx.artifacts.put(
                "log",
                result.transcript(),
                produced_by=Role.DOCUMENTER,
                name=f"documenter-{round_:02d}",
            )
            docs_check = run_docs_check(ctx)
            if docs_check is None:
                break

        # PR body (typed), then the side effects: docs commit, push, draft PR.
        pr_req = ctx.harness.build_request(
            "documenter",
            system=ctx.prompts.get("documenter").render(),
            messages=[
                Message.user(
                    pr_body_message(
                        state.task, state.feature_list, diff_stat=stat, test_summary=test_summary
                    )
                )
            ],
            tags={"thread": ctx.thread_id},
        )
        body = ctx.harness.structured(pr_req, PRBody).value
        body_text = render_pr_body(state, body, docs_check)
        body_ref = ctx.artifacts.put(
            "pr_body", body_text, produced_by=Role.DOCUMENTER, name="pr-body"
        )
        adr_path = write_adr(ctx, state)

        marker = f"[scio:{effect_key(ctx.thread_id, 'document', 'commit:docs')}]"
        run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="document",
            intent="commit:docs",
            apply=lambda: git_checkpoint(
                sb, GitCheckpointInput(message=f"docs: update for {state.task.title} {marker}")
            ),
            probe=lambda: find_commit(sb, marker),
        )
        github = state.github
        entries = [fact("documentation updated and committed", source=Role.DOCUMENTER)]
        if adr_path:
            entries.append(fact(f"ADR written: {adr_path}", source=Role.DOCUMENTER))
        if ctx.github is not None and github.branch:
            default = ctx.github.default_branch()
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="document",
                intent=f"push:{github.branch}",
                apply=lambda: git_push(sb, github.branch or "", default_branch=default),
            )
            pr = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="document",
                intent=f"draft_pr:{github.branch}",
                apply=lambda: _ensure_pr(ctx, state, body_text).__dict__,
                probe=lambda: (
                    (p.__dict__ if (p := ctx.github.find_pr(github.branch or "")) else None)
                    if ctx.github
                    else None
                ),
            )
            github = github.model_copy(
                update={"pr_number": pr["number"], "pr_state": "draft", "head_sha": head_sha(sb)}
            )
            entries.append(fact(f"draft PR #{pr['number']} opened", source=Role.DOCUMENTER))
        else:
            github = github.model_copy(update={"head_sha": head_sha(sb)})
            entries.append(
                fact("no GitHub client configured; PR body stored as artifact", source=Role.SYSTEM)
            )
        return {
            "github": github,
            "artifacts": {**state.artifacts, **logs, "pr_body": body_ref},
            "task_ledger": entries,
            "phase": "gate",
            **ctx.budget_update(),
        }

    return node


def after_document(state: SCIOState) -> Literal["gate", "halt"]:
    return "halt" if state.exit_reason is not None else "gate"


def _ensure_pr(ctx: RunContext, state: SCIOState, body: str) -> Any:
    assert ctx.github is not None and state.github.branch
    return ctx.github.ensure_draft_pr(
        head=state.github.branch,
        base=state.task.base_branch,
        title=f"{state.task.title} (#{state.task.issue_number})"
        if state.task.issue_number
        else state.task.title,
        body=body,
    )


def adr_needed(state: SCIOState) -> list[str]:
    """Reasons an ADR must be written: advisor replan / change_approach, or a high-impact change."""
    reasons = [
        f"advisor verdict {e.verdict.value} at step {e.step_id} ({e.trigger.value})"
        for e in state.escalations
        if e.verdict in (Verdict.REPLAN, Verdict.CHANGE_APPROACH)
    ]
    if state.high_impact_flags:
        reasons.append("high-impact change: " + "; ".join(state.high_impact_flags))
    return reasons


def write_adr(ctx: RunContext, state: SCIOState) -> str | None:
    """Write ``docs/adr/NNNN-<slug>.md`` in the target repo when it keeps ADRs and one is due."""
    reasons = adr_needed(state)
    sb = ctx.sandbox
    adr_dir = f"{sb.root}/docs/adr"
    if not reasons or not sb.exists(adr_dir):
        return None
    listing = sb.exec(["ls", adr_dir], timeout_s=30).stdout.split()
    numbers = [int(n[:4]) for n in listing if n[:4].isdigit()]
    number = (max(numbers) + 1) if numbers else 1
    path = f"{adr_dir}/{number:04d}-{slug(state.task.title)}.md"
    if sb.exists(path):
        return path
    plan_lines = (
        [f"- {s.id}: {s.description}" for s in state.plan.steps]
        if state.plan
        else ["- (no plan: T0 run)"]
    )
    guidance = (
        "\n".join(
            f"- {e.id} ({e.trigger.value}): "
            f"{e.verdict.value if e.verdict else 'pending'}, {e.outcome}"
            for e in state.escalations
        )
        or "- none"
    )
    text = "\n".join(
        [
            f"# ADR {number:04d}: {state.task.title}",
            "",
            "- **Status:** Proposed (written by SCIO; a human accepts it on merge)",
            f"- **Run:** {state.thread_id}",
            "",
            "## Context",
            state.task.description.strip()[:1500],
            "",
            "## Why this record exists",
            *[f"- {r}" for r in reasons],
            "",
            "## Decision",
            *plan_lines,
            "",
            "## Advisor consultations",
            guidance,
            "",
            "## Consequences",
            "See the pull request's risk notes and test evidence.",
            "",
        ]
    )
    sb.write_text(path, text)
    return path


def run_docs_check(ctx: RunContext) -> str | None:
    """Run the target repository's own docs check if it has one. None means clean/absent."""
    sb = ctx.sandbox
    script = f"{sb.root}/scripts/check_docs.py"
    if not sb.exists(script):
        return None
    r = sb.exec(ctx.toolchain.script_argv(script), timeout_s=120)
    return None if r.ok else r.output.strip()[:3000]


def render_pr_body(state: SCIOState, body: PRBody, docs_check: str | None) -> str:
    closes = f"Closes #{state.task.issue_number}" if state.task.issue_number else ""
    plan_rows = "\n".join(f"| {row} | done | |" for row in body.plan_vs_done) or "| | | |"
    features = "\n".join(
        f"- [{'x' if f.status.value == 'passing' else ' '}] {f.id}: {f.description}"
        for f in state.feature_list
    )
    spend = state.budgets
    escalation_rows = (
        "\n".join(
            f"| {e.id} | {e.trigger.value} (step {e.step_id}) | "
            f"{e.verdict.value if e.verdict else 'pending'} | {e.outcome} | ${e.usd:.4f} |"
            for e in state.escalations
        )
        or "| none | | | | |"
    )
    return "\n".join(
        [
            "## Summary",
            body.summary.strip(),
            "",
            closes,
            "",
            "## Plan vs. done",
            "| Plan step | Status | Notes |",
            "|---|---|---|",
            plan_rows,
            "",
            "## Features",
            features,
            "",
            "## Test evidence",
            "```",
            body.test_evidence.strip(),
            "```",
            "",
            "## Docs updated",
            "- [x] Docs updated in this PR where the repository has them"
            + (
                ""
                if docs_check is None
                else f"\n- [ ] Docs check still failing:\n```\n{docs_check}\n```"
            ),
            "",
            "## Risk notes",
            body.risk_notes.strip(),
            "",
            review_notes(state.last_review),
            "",
            "## Agent run log",
            "| Escalation | Trigger | Opus verdict | Resolved? | Cost |",
            "|---|---|---|---|---|",
            escalation_rows,
            "",
            f"Tokens: {spend.tokens_used} · Cost: ${spend.usd_used:.4f} · "
            f"Advisor calls: {spend.advisor_calls_used}/{spend.advisor_calls_max}",
        ]
    )
