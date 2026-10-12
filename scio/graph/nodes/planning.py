"""Planning nodes: ``scope`` (T2), parallel ``explore`` (T2), ``localize`` (T1), ``plan``,
``post_plan``.

Explorers are read-only and run in parallel through LangGraph ``Send``; their results
reduce into ``explorer_summaries`` as artifact references of at most ~1.5k tokens each.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from langgraph.types import Send

from scio.aci.toolbox import ToolBox
from scio.context.projections import (
    explore_message,
    localize_question,
    plan_checklist,
    plan_message,
    scope_message,
)
from scio.graph.effects import run_effect
from scio.graph.nodes.common import fact, git_out
from scio.graph.run_context import RunContext
from scio.harness.failures import BudgetExceeded
from scio.harness.types import Message
from scio.loop.agent_loop import AgentLoop
from scio.state.models import (
    ExitReason,
    ExplorerSummary,
    FeatureItem,
    Plan,
    PlannerOutput,
    PlanStep,
    Role,
    SCIOState,
    ScopeOutput,
)

Node = Callable[[SCIOState], dict[str, Any]]
EXPLORER_TOOLS = ("repo_search", "view_file")
SUMMARY_MAX_CHARS = 6000  # ~1.5k tokens
EXPLORE_SUMMARY_PROMPT = (
    "Stop exploring. Produce the JSON summary matching the schema: facts with file:line "
    "citations, the relevant files, and open questions. Be concise."
)
GENERIC_QUESTIONS = [
    "How are tests organized and run in this repository (framework, directories, fixtures)?",
    "Which modules and conventions (naming, docstrings, typing) must new code follow?",
]


def _repo_stats(ctx: RunContext) -> tuple[str, str]:
    files = [f for f in git_out(ctx.sandbox, "ls-files").splitlines() if f]
    py = sum(1 for f in files if f.endswith(".py"))
    tree = "\n".join(files[:120]) + ("\n..." if len(files) > 120 else "")
    return f"{len(files)} tracked files, {py} Python files", tree


def scope(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        stats, tree = _repo_stats(ctx)
        req = ctx.harness.build_request(
            "scoper",
            system=ctx.prompts.get("scoper").render(),
            messages=[Message.user(scope_message(state.task, repo_stats=stats, file_tree=tree))],
            tags={"thread": ctx.thread_id},
        )
        try:
            out: ScopeOutput = ctx.harness.structured(req, ScopeOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at scope: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        cfg = ctx.escalation
        questions = list(out.exploration_questions)
        for q in GENERIC_QUESTIONS:
            if len(questions) >= cfg.explorers_min:
                break
            questions.append(q)
        questions = questions[: cfg.explorers_max]
        features = state.feature_list or [
            FeatureItem(id=f"F{i + 1}", description=f.description)
            for i, f in enumerate(out.draft_features)
        ]
        entry = fact(f"scope: {len(questions)} exploration question(s)", source=Role.SCOPER)
        return {
            "exploration_questions": questions,
            "feature_list": features,
            "task_ledger": [entry],
            "phase": "explore",
            **ctx.budget_update(),
        }

    return node


def fan_out_explore(state: SCIOState) -> list[Send] | str:
    """Conditional edge after ``scope``: one ``explore`` task per question, in parallel."""
    if state.exit_reason is not None:
        return "halt"
    return [
        Send("explore", {"question": q, "index": i})
        for i, q in enumerate(state.exploration_questions)
    ]


def _explore_once(ctx: RunContext, question: str, *, tag: str) -> Any:
    # Read-only context gathering must stay cheap: every turn resends the transcript (I-033).
    toolbox = ToolBox(ctx.sandbox, EXPLORER_TOOLS, human_gate=False, max_calls=8, role="explorer")
    loop = AgentLoop(ctx.harness, toolbox, ctx.prompts, "explorer", max_model_calls=6)
    result = loop.run_typed(
        explore_message(question, repo_root=ctx.sandbox.root),
        ExplorerSummary,
        summary_prompt=EXPLORE_SUMMARY_PROMPT,
        tags={"thread": ctx.thread_id, "explore": tag},
    )
    summary: ExplorerSummary = result.output
    text = json.dumps(summary.model_dump(), indent=1)
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS] + "\n[summary truncated to the 1.5k-token cap]"
    return ctx.artifacts.put("summary", text, produced_by=Role.EXPLORER, name=f"explore-{tag}")


def explore(ctx: RunContext) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Fan-out node: input is the ``Send`` payload, output reduces into ``explorer_summaries``."""

    def node(payload: dict[str, Any]) -> dict[str, Any]:
        question = str(payload["question"])
        ref = _explore_once(ctx, question, tag=str(payload.get("index", 0)))
        return {"explorer_summaries": [ref]}

    return node


def localize(ctx: RunContext) -> Node:
    """T1: a single explorer answers where/how the first step should be implemented."""

    def node(state: SCIOState) -> dict[str, Any]:
        question = localize_question(state.current_step(), state.task)
        try:
            ref = _explore_once(ctx, question, tag="localize")
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at localize: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        entry = fact("localize: repository context gathered", source=Role.EXPLORER)
        return {
            "explorer_summaries": [ref],
            "task_ledger": [entry],
            "phase": "implement",
            **ctx.budget_update(),
        }

    return node


_NOOP_STEP_RE = re.compile(
    r"\b(explore|exploration|investigate|inspect|read-only|no code changes|no changes|"
    r"verify the current state|locate)\b",
    re.IGNORECASE,
)


def normalize_steps(
    steps: list[PlanStep], feature_ids: list[str]
) -> tuple[list[PlanStep], list[str]]:
    """Make a plan executable (ledger I-039).

    Drops steps that carry no feature or describe exploration / no-op work (verify can never
    pass a step that changes nothing), and appends a final step for every feature the plan
    left uncovered. Returns the steps and human-readable notes of what changed.
    """
    notes: list[str] = []
    kept: list[PlanStep] = []
    for s in steps:
        if not s.feature_ids:
            notes.append(f"plan step {s.id} dropped: no feature ids ({s.description[:60]})")
            continue
        if _NOOP_STEP_RE.search(s.description) and not re.search(
            r"\b(add|implement|write|fix|change|create|update|refactor|remove)\b",
            s.description,
            re.IGNORECASE,
        ):
            notes.append(
                f"plan step {s.id} dropped: exploration is not a plan step ({s.description[:60]})"
            )
            continue
        kept.append(s)
    covered = {fid for s in kept for fid in s.feature_ids}
    missing = [fid for fid in feature_ids if fid not in covered]
    if missing:
        files = sorted({f for s in kept for f in s.files_expected})
        next_id = f"S{len(kept) + 1}"
        while any(s.id == next_id for s in kept):
            next_id = f"S{int(next_id[1:]) + 1}"
        kept.append(
            PlanStep(
                id=next_id,
                description=f"Implement and test the remaining features {', '.join(missing)}",
                feature_ids=missing,
                files_expected=files,
            )
        )
        notes.append(f"plan step {next_id} added for uncovered features {', '.join(missing)}")
    return kept, notes


def summaries_text(ctx: RunContext, state: SCIOState) -> list[str]:
    return [ctx.artifacts.head(ref, SUMMARY_MAX_CHARS) for ref in state.explorer_summaries]


def plan(ctx: RunContext) -> Node:
    def node(state: SCIOState) -> dict[str, Any]:
        previous = state.plan
        reason = state.advisor_guidance if previous is not None else None
        msg = plan_message(
            state.task,
            state.feature_list,
            summaries=summaries_text(ctx, state),
            previous=previous,
            revision_reason=reason,
        )
        req = ctx.harness.build_request(
            "planner",
            system=ctx.prompts.get("planner").render(),
            messages=[Message.user(msg)],
            tags={
                "thread": ctx.thread_id,
                "plan_version": str((previous.version if previous else 0) + 1),
            },
        )
        try:
            out: PlannerOutput = ctx.harness.structured(req, PlannerOutput).value
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at plan: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}

        # Merge the planner's feature list into ours: keep status/commit, take filters/text.
        existing = {f.id: f for f in state.feature_list}
        features: list[FeatureItem] = []
        for pf in out.feature_list:
            cur = existing.get(pf.id)
            if cur is None:
                features.append(pf)
            else:
                features.append(
                    cur.model_copy(
                        update={
                            "description": pf.description or cur.description,
                            "test_filter": pf.test_filter or cur.test_filter,
                        }
                    )
                )
        for fid, f in existing.items():
            if fid not in {x.id for x in features}:
                features.append(f)
        known = {f.id for f in features}
        steps = []
        done_ids = {s.id for s in previous.steps if s.done} if previous else set()
        for s in out.steps:
            steps.append(
                s.model_copy(
                    update={
                        "feature_ids": [i for i in s.feature_ids if i in known],
                        "done": s.id in done_ids,
                    }
                )
            )
        steps, plan_notes = normalize_steps(steps, [f.id for f in features])
        if reason and reason.startswith("gate replan") and all(s.done for s in steps):
            # A gate replan must produce one more implementation pass; a plan whose steps
            # are all done would hit "implement: no open plan step" and halt (I-043).
            steps.append(
                PlanStep(
                    id=f"S{len(steps) + 1}",
                    description=f"Address the gate's unmet criteria: {reason[:1500]}",
                    feature_ids=[f.id for f in features],
                    files_expected=[],
                )
            )
            plan_notes.append("gate replan: appended a step because every step was done")
        new_plan = Plan(
            version=(previous.version + 1) if previous else 1,
            author=Role.PLANNER,
            steps=steps,
            rationale=out.rationale,
            revision_reason=reason,
        )
        first_open = next((s.id for s in steps if not s.done), None)
        progress = state.progress.model_copy(
            update={
                "current_step_id": first_open,
                "attempts_on_step": 0,
                "fix_attempts_on_error": 0,
                "last_error_signature": None,
            }
        )
        entries = [
            fact(
                f"plan v{new_plan.version}: {len(steps)} step(s); "
                f"risks: {'; '.join(out.risks)[:200]}",
                source=Role.PLANNER,
            )
        ] + [fact(n, source=Role.SYSTEM) for n in plan_notes]
        return {
            "plan": new_plan,
            "feature_list": features,
            "progress": progress,
            "advisor_guidance": None,
            "task_ledger": entries,
            "phase": "post_plan",
            **ctx.budget_update(),
        }

    return node


def after_plan(state: SCIOState) -> str:
    return "halt" if state.exit_reason is not None else "post_plan"


def post_plan(ctx: RunContext) -> Node:
    """Mirror the plan to the GitHub issue as a checklist (idempotent by marker)."""

    def node(state: SCIOState) -> dict[str, Any]:
        assert state.plan is not None
        body = plan_checklist(state.plan, state.feature_list)
        ref = ctx.artifacts.put(
            "plan_comment", body, produced_by=Role.PLANNER, name=f"plan-v{state.plan.version}"
        )
        github = state.github
        if ctx.github is not None and state.task.issue_number:
            issue = state.task.issue_number
            cid = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="post_plan",
                intent=f"plan_comment:v{state.plan.version}",
                apply=lambda: (
                    ctx.github.upsert_issue_comment(issue, body, marker="scio:plan")
                    if ctx.github
                    else 0
                ),
            )
            github = github.model_copy(update={"plan_comment_id": cid})
            entry = fact(f"plan v{state.plan.version} posted to issue #{issue}", source=Role.SYSTEM)
        else:
            entry = fact("plan stored as artifact (no issue to comment on)", source=Role.SYSTEM)
        return {
            "github": github,
            "artifacts": {**state.artifacts, f"plan:v{state.plan.version}": ref},
            "task_ledger": [entry],
            "phase": "implement",
        }

    return node
