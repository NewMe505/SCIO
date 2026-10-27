"""``intake`` (spec or issue → TaskSpec + failing feature list) and ``route`` (tier)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from scio.context.assembler import router_message
from scio.graph.nodes.common import acceptance_from_issue_body, fact, features_from_acceptance
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.state.models import ExitReason, Role, RouterOutput, Routing, SCIOState, Tier

Node = Callable[[SCIOState], dict[str, Any]]

_PATTERN: dict[Tier, Literal["single_loop", "plan_execute_gate", "explore_plan_execute_gate"]] = {
    Tier.T0: "single_loop",
    Tier.T1: "plan_execute_gate",
    Tier.T2: "explore_plan_execute_gate",
}
_EFFORT: dict[Tier, Literal["low", "medium", "high", "xhigh"]] = {
    Tier.T0: "medium",
    Tier.T1: "high",
    Tier.T2: "xhigh",
}


def intake(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        task = state.task
        entries = []
        if task.issue_number and ctx.github is not None:
            issue = ctx.github.get_issue(task.issue_number)
            acceptance = acceptance_from_issue_body(issue.body) or task.acceptance
            task = task.model_copy(
                update={
                    "title": issue.title or task.title,
                    "description": issue.body or task.description,
                    "acceptance": acceptance,
                }
            )
            entries.append(fact(f"task loaded from issue #{issue.number}", source=Role.SYSTEM))
        features = features_from_acceptance(task.acceptance)
        entries.append(fact(f"{len(features)} feature(s), all failing", source=Role.SYSTEM))
        return {"task": task, "feature_list": features, "task_ledger": entries, "phase": "route"}

    return node


def route(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        if ctx.tier_pin is not None:
            # Eval tasks built to exercise one tier's path pin it: the classifier prefers
            # the cheaper tier when in doubt and rated t008 T0, which skipped plan, verify
            # and debug, the very nodes the task exists to test (ledger I-050).
            pinned = Tier(ctx.tier_pin)
            routing = Routing(
                tier=pinned,
                pattern=_PATTERN[pinned],
                effort_tier=_EFFORT[pinned],
                rationale="tier pinned by the eval task (tier_pin); classifier not consulted",
                classifier_model="pinned",
                confidence=1.0,
            )
            entry = fact(f"routed to {pinned.value} (pinned by the task)", source=Role.ROUTER)
            return {"routing": routing, "task_ledger": [entry], "phase": "prepare"}
        stats = _repo_stats(ctx)
        system = ctx.prompts.get("router").render()
        req = ctx.harness.build_request(
            "router",
            system=system,
            messages=[Message.user(router_message(state.task, repo_stats=stats))],
            tags={"thread": ctx.thread_id},
        )
        try:
            result = ctx.harness.structured(req, RouterOutput)
        except BudgetExceeded as exc:  # every model call in a node is guarded (I-098)
            entry = fact(f"budget exhausted at routing: {exc.message}", source=Role.SYSTEM)
            return {
                "exit_reason": ExitReason.BUDGET,
                "task_ledger": [entry],
                **ctx.budget_update(),
            }
        out = result.value
        routing = Routing(
            tier=out.tier,
            pattern=_PATTERN[out.tier],
            effort_tier=_EFFORT[out.tier],
            rationale=out.rationale,
            classifier_model=req.model,
            confidence=out.confidence,
        )
        entries = [
            fact(
                f"routed to {out.tier.value} (confidence {out.confidence:.2f}): {out.rationale}",
                source=Role.ROUTER,
            )
        ]
        if ctx.budget_from_tier and out.tier.value != ctx.tier:
            # I-053: a budget sized from tier defaults follows the tier the classifier
            # picked; explicit limits (evals) are left alone.
            ctx.apply_tier_budgets(out.tier.value)
            entries.append(
                fact(
                    f"budget resized for {out.tier.value}: {ctx.budget.tokens_max} tokens, "
                    f"${ctx.budget.usd_max:.2f}, {ctx.budget.max_iters} iterations",
                    source=Role.SYSTEM,
                )
            )
        return {
            "routing": routing,
            "task_ledger": entries,
            "phase": "prepare",
            **ctx.budget_update(),
        }

    return node


def after_route(state: SCIOState) -> Literal["prepare", "halt"]:
    """M3 runs T0 only; other tiers halt for a human until M4 adds them."""
    if state.routing is not None and state.routing.tier is Tier.T0:
        return "prepare"
    return "halt"


def _repo_stats(ctx: RunContext) -> str:
    r = ctx.sandbox.exec(["git", "ls-files"], timeout_s=30)
    if not r.ok:
        return "(repository not prepared yet; stats unavailable)"
    files = [f for f in r.stdout.splitlines() if f]
    src = sum(1 for f in files if ctx.toolchain.is_source(f))
    return f"{len(files)} tracked files, {src} {ctx.toolchain.name} source files"
