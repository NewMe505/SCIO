"""Planning nodes: ``scope`` (T2), parallel ``explore`` (T2), ``localize`` (T1), ``plan``,
``post_plan``.

Explorers are read-only and run in parallel through LangGraph ``Send``; their results
reduce into ``explorer_summaries`` as artifact references of at most ~1.5k tokens each.

T1 plans before it explores, so the planner's ``files_expected`` are guesses. ``localize``
adds the files its explorer found to every open step, so plan drift compares the diff with
files that exist (I-151).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
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
from scio.loop.backends import make_worker

# The drift check's own normalization, so a path added here compares equal there.
from scio.loop.escalation import _norm as drift_norm
from scio.state.models import (
    ArtifactRef,
    ExitReason,
    ExplorerSummary,
    FeatureItem,
    Plan,
    PlannerOutput,
    PlanStep,
    Role,
    SCIOState,
    ScopeOutput,
    Tier,
)

Node = Callable[[SCIOState], dict[str, Any]]
EXPLORER_TOOLS = ("repo_search", "view_file")
SUMMARY_MAX_CHARS = 6000  # ~1.5k tokens
_ELLIPSIS = "..."
# A path with an extension, then ``:`` and a line number or a symbol (``calc.py:134``,
# ``graphcalc/names.py:check_not_constant``); a Windows drive prefix is allowed (I-151).
_CITATION_RE = re.compile(r"((?:[A-Za-z]:[\\/])?[\w./\\-]+\.[A-Za-z0-9]+):(?=:?\w)")
_ENTRY_RE = re.compile(r"(?:[A-Za-z]:[\\/])?[^:\s]+")
EXPLORE_SUMMARY_PROMPT = (
    "Stop exploring. Produce the JSON summary matching the schema: facts with file:line "
    "citations, the relevant files, and open questions. Be concise."
)
GENERIC_QUESTIONS = [
    "How are tests organized and run in this repository (framework, directories, fixtures)?",
    "Which modules and conventions (naming, docstrings, typing) must new code follow?",
]


def _tracked_files(ctx: RunContext) -> list[str]:
    # git_out returns a "(git ... failed: ...)" line on failure, which is no file.
    out = git_out(ctx.sandbox, "ls-files")
    return [f for f in out.splitlines() if f and not f.startswith("(git ")]


def _repo_stats(ctx: RunContext) -> tuple[str, str]:
    files = _tracked_files(ctx)
    src = sum(1 for f in files if ctx.toolchain.is_source(f))
    tree = "\n".join(files[:120]) + ("\n..." if len(files) > 120 else "")
    return f"{len(files)} tracked files, {src} {ctx.toolchain.name} source files", tree


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


def _shorten(text: str, over: int) -> str:
    """``text`` made at least ``over`` characters shorter in its JSON form ('' if it can't be)."""
    keep = len(text) - over - len(_ELLIPSIS)
    return text[:keep] + _ELLIPSIS if keep > 0 else ""


def summary_text(summary: ExplorerSummary, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    """The summary as indented JSON of at most ``max_chars`` characters, always valid (I-152).

    Cutting the serialized text cut ``relevant_files`` and ``open_questions`` off a long
    summary and left invalid JSON. Over the cap, ``findings`` give way first: whole findings
    drop from the end, then the one left is shortened, and a closing entry in ``findings``
    says what was trimmed. The question (an echo of what was asked) is shortened next.
    ``open_questions`` and ``relevant_files`` lose items, from the end, only when they alone
    exceed the cap. Holds for any cap of at least 500 characters.
    """
    data: dict[str, Any] = summary.model_dump()
    text = json.dumps(data, indent=1)
    if len(text) <= max_chars:
        return text
    total = len(summary.findings)
    findings = list(summary.findings)
    notes: dict[str, str] = {}

    def render() -> str:
        dropped = total - len(findings)
        said = [f"{dropped} of {total} findings dropped"] if dropped else []
        note = f"[trimmed to the 1.5k-token cap: {'; '.join(said + list(notes.values()))}]"
        return json.dumps({**data, "findings": [*findings, note]}, indent=1)

    while len(findings) > 1 and len(render()) > max_chars:
        findings.pop()
    if findings and len(render()) > max_chars:
        notes["finding"] = "the last finding kept was shortened"
        findings[-1] = _shorten(findings[-1], len(render()) - max_chars)
        if not findings[-1]:
            findings.pop()
            del notes["finding"]
    if len(render()) > max_chars:
        notes["question"] = "the question was shortened"
        data["question"] = _shorten(str(data["question"]), len(render()) - max_chars)
    for key in ("open_questions", "relevant_files"):
        items, n = list(data[key]), len(data[key])
        while items and len(render()) > max_chars:
            items.pop()
            data[key] = items
            notes[key] = f"{n - len(items)} of {n} {key} dropped"
    return render()


def _explore_once(
    ctx: RunContext, question: str, *, tag: str
) -> tuple[ArtifactRef, ExplorerSummary]:
    """One read-only explorer run: the stored summary's artifact and the summary itself."""
    # Read-only context gathering must stay cheap: every turn resends the transcript (I-033).
    toolbox = ToolBox(ctx.sandbox, EXPLORER_TOOLS, human_gate=False, max_calls=8, role="explorer")
    loop = make_worker(ctx, toolbox, "explorer", max_model_calls=6)
    result = loop.run_typed(
        explore_message(question, repo_root=ctx.sandbox.root),
        ExplorerSummary,
        summary_prompt=EXPLORE_SUMMARY_PROMPT,
        tags={"thread": ctx.thread_id, "explore": tag},
    )
    summary: ExplorerSummary = result.output
    ref = ctx.artifacts.put(
        "summary", summary_text(summary), produced_by=Role.EXPLORER, name=f"explore-{tag}"
    )
    return ref, summary


def _resolve(raw: str, root: str | None, tracked: dict[str, str]) -> str | None:
    """The tracked path ``raw`` names: the same path, or the one tracked path it ends."""
    p = drift_norm(raw.strip("`'\" "), root)
    if not p:
        return None
    if p in tracked:
        return tracked[p]
    hits = {t for n, t in tracked.items() if n.endswith("/" + p)}
    return hits.pop() if len(hits) == 1 else None


def localized_files(
    summary: ExplorerSummary,
    *,
    tracked: Iterable[str],
    is_source: Callable[[str], bool],
    root: str | None = None,
) -> list[str]:
    """The files an explorer localized, as git spells them (I-151).

    Its ``relevant_files`` and the files its ``findings`` cite as ``path:line`` or
    ``path:symbol``; a bare file name counts when exactly one tracked path ends with it.
    Only tracked files the toolchain calls source stay: a planned file that does not exist
    is no evidence, and docs, configs and SCIO's own bookkeeping are not code to change.
    """
    by_norm = {drift_norm(t, None): t for t in tracked if t}
    cited: list[str] = []
    for entry in summary.relevant_files:
        m = _ENTRY_RE.search(entry.strip("`'\" "))
        if m:
            cited.append(m.group(0))
    cited += [m.group(1) for f in summary.findings for m in _CITATION_RE.finditer(f)]
    out: list[str] = []
    for raw in cited:
        path = _resolve(raw, root, by_norm)
        if path is not None and path not in out and is_source(path):
            out.append(path)
    return out


def add_localized_files(
    plan: Plan, files: list[str], *, root: str | None = None
) -> tuple[Plan, dict[str, list[str]]]:
    """``plan`` with ``files`` added to each open step's ``files_expected`` (I-151).

    Planned files stay, even missing ones: a plan may name a file to create. A step that
    names no files keeps none, since an empty ``files_expected`` turns the drift check off.
    Returns the plan (same version) and the files added per step id.
    """
    added: dict[str, list[str]] = {}
    steps: list[PlanStep] = []
    for s in plan.steps:
        if not s.done and s.files_expected:
            have = {drift_norm(f, root) for f in s.files_expected}
            new = [f for f in files if drift_norm(f, root) not in have]
            if new:
                added[s.id] = new
                s = s.model_copy(update={"files_expected": [*s.files_expected, *new]})
        steps.append(s)
    return plan.model_copy(update={"steps": steps}), added


def _localize_plan(ctx: RunContext, plan: Plan, summary: ExplorerSummary) -> tuple[Plan, str]:
    files = localized_files(
        summary,
        tracked=_tracked_files(ctx),
        is_source=ctx.toolchain.is_source,
        root=ctx.sandbox.root,
    )
    new_plan, added = add_localized_files(plan, files, root=ctx.sandbox.root)
    if not files:
        return new_plan, "no files added to the plan (the explorer named no tracked source file)"
    if not added:
        return new_plan, (
            f"no files added to the plan: the explorer localized {', '.join(files)}, but each "
            "open step already lists them or names no files (drift check off)"
        )
    per_step = "; ".join(f"{sid} += {', '.join(fs)}" for sid, fs in added.items())
    return new_plan, f"files the explorer localized added to the plan: {per_step}"


def explore(ctx: RunContext) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Fan-out node: input is the ``Send`` payload, output reduces into ``explorer_summaries``."""

    def node(payload: dict[str, Any]) -> dict[str, Any]:
        question = str(payload["question"])
        ref, _summary = _explore_once(ctx, question, tag=str(payload.get("index", 0)))
        return {"explorer_summaries": [ref]}

    return node


def localize(ctx: RunContext) -> Node:
    """T1: a single explorer answers where/how the first step should be implemented, and
    the files it found join every open step's ``files_expected`` (I-151)."""

    def node(state: SCIOState) -> dict[str, Any]:
        question = localize_question(state.current_step(), state.task)
        try:
            ref, summary = _explore_once(ctx, question, tag="localize")
        except BudgetExceeded as exc:
            entry = fact(f"budget exhausted at localize: {exc.message}", source=Role.SYSTEM)
            return {"exit_reason": ExitReason.BUDGET, "task_ledger": [entry], **ctx.budget_update()}
        entries = [fact("localize: repository context gathered", source=Role.EXPLORER)]
        update: dict[str, Any] = {}
        if state.plan is not None:
            update["plan"], note = _localize_plan(ctx, state.plan, summary)
            entries.append(fact(f"localize: {note}", source=Role.SYSTEM))
        return {
            **update,
            "explorer_summaries": [ref],
            "task_ledger": entries,
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
    # summary_text keeps every stored summary within SUMMARY_MAX_CHARS, so this never cuts.
    return [ctx.artifacts.head(ref, SUMMARY_MAX_CHARS) for ref in state.explorer_summaries]


def _stored_summary(ctx: RunContext, ref: ArtifactRef) -> ExplorerSummary | None:
    """A summary read back from its artifact; None for one the old character cut broke."""
    try:
        return ExplorerSummary.model_validate_json(ctx.artifacts.get(ref))
    except (OSError, ValueError):
        return None


def keep_localized_files(ctx: RunContext, state: SCIOState, plan: Plan) -> tuple[Plan, list[str]]:
    """``plan`` with the T1 localize explorer's files merged back into its open steps
    (I-151). Every replan (the planner's, or the advisor's ``revised_steps``) names its files
    again without exploring, and localize runs once per T1 run (build.py), so the files it
    found would drop out and drift would fire on them again. Returns the notes to record."""
    if state.routing is None or state.routing.tier is not Tier.T1:
        return plan, []
    notes: list[str] = []
    for ref in state.explorer_summaries:
        stored = _stored_summary(ctx, ref)
        if stored is not None:
            plan, note = _localize_plan(ctx, plan, stored)
            notes.append(f"plan v{plan.version}: {note}")
    return plan, notes


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
        new_plan, notes = keep_localized_files(ctx, state, new_plan)
        entries += [fact(n, source=Role.SYSTEM) for n in notes]
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
