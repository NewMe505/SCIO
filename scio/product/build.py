"""The milestone loop behind ``scio build`` (ADR 0011).

    load -> plan_milestone -> issues -> feature (x N: the task graph as a subgraph, one
    checkpoint thread per feature attempt) -> integrate -> milestone_gate -> [gate_decision]
    -> milestone_pr -> merge_wait -> update_progress -> plan_milestone | release_check
    -> [release_decision] -> release -> [release_merge_wait, GitHub only] -> release_publish
    -> done                                            (the release gate, M11; issue #58)

The orchestrator, not a model, picks the next feature (the first open one in ``PRODUCT.md``
order); the milestone planner only proposes how many to group and at what tier. Every
human stop is an ``interrupt`` resumed with ``scio build --product DIR --decision "<text>"``;
every side effect is an idempotent ``run_effect`` with a world probe, and each feature's
subgraph has its own thread, so a crash between features or after the PR resumes without a
duplicated issue, commit or pull request (ADR 0003).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from scio.aci.runtime import ToolContext, ensure_product_env
from scio.aci.sandbox.base import Sandbox
from scio.aci.tools import SCIO_GIT_IDENTITY, find_commit, git_push, head_sha
from scio.context.artifacts import ArtifactStore
from scio.graph.checkpointer import DURABILITY, open_checkpointer
from scio.graph.effects import run_effect
from scio.graph.nodes.common import decision, git_out
from scio.graph.nodes.common import fact as ledger_fact
from scio.graph.run import resume_graph, run_graph, thread_started
from scio.graph.run_context import T0_TOOL_CAP, RunContext, product_sandbox_backend
from scio.harness.budget import BudgetGuard, BudgetLimits, BudgetsConfig
from scio.harness.failures import BudgetExceeded, HarnessError
from scio.harness.types import Message
from scio.loop.exits import LoopLimits
from scio.product.acceptance import run_acceptance
from scio.product.progress import (
    PRODUCT_MD,
    PROGRESS_PATH,
    FeatureRecord,
    MilestoneRecord,
    ProgressFile,
    normalise_milestone,
    parse_product_md,
)
from scio.product.release import (
    build_artifacts,
    cut_changelog,
    read_version,
    release_notes,
    tag_exists,
)
from scio.product.release_checks import release_report
from scio.state.models import (
    Budget,
    ExitReason,
    FeatureRun,
    GateOutput,
    LedgerEntry,
    MilestoneFeatureOutput,
    MilestonePlan,
    MilestonePlanOutput,
    MilestonePR,
    ProductBuildState,
    ProductFeature,
    ProductSpec,
    ReleaseRecord,
    Role,
    SCIOState,
    TaskSpec,
)
from scio.toolchain import get_toolchain

Node = Callable[[ProductBuildState], dict[str, Any]]
MILESTONE_GATE_HEADER = "# Milestone gate"
ISSUES_DIR = ".scio/product/issues"
MILESTONES_DIR = ".scio/product/milestones"
MERGE_ANSWERS = ("merge", "merged", "approve", "approved", "yes", "ok")
STOP_ANSWERS = ("stop", "abort", "no", "halt")


@dataclass
class BuildOutcome:
    state: ProductBuildState
    interrupt: dict[str, Any] | None

    @property
    def paused(self) -> bool:
        return self.interrupt is not None


def build_thread_id(product: str, milestone: int = 1) -> str:
    """The build thread that starts at milestone ``milestone``: ``scio build`` resumes it
    while it is paused and starts the next one (from ``progress.json``) once it has ended."""
    return f"build-{product}-m{milestone}"


# ---------- helpers ----------


def fact(text: str, *, source: Role = Role.SYSTEM) -> LedgerEntry:
    """A ledger fact; the orchestrator is the default source."""
    return ledger_fact(text, source=source)


def _fact(text: str, source: Role = Role.SYSTEM) -> dict[str, Any]:
    return {"ledger": [fact(text, source=source)]}


def _write(sb: Sandbox, rel: str, text: str) -> str:
    """Write ``rel`` under the sandbox root and return ``rel`` (effect result)."""
    sb.write_text(f"{sb.root}/{rel}", text)
    return rel


def _exists(sb: Sandbox, rel: str) -> str | None:
    """``rel`` when it exists under the sandbox root (effect probe)."""
    return rel if sb.exists(f"{sb.root}/{rel}") else None


def _halt(ctx: RunContext, reason: str, text: str, source: Role = Role.SYSTEM) -> dict[str, Any]:
    return {
        "exit_reason": reason,
        "phase": "halted",
        "ledger": [fact(text, source=source)],
        **ctx.budget_update(),
    }


def _read_progress(ctx: RunContext) -> ProgressFile:
    return ProgressFile.from_json(ctx.sandbox.read_text(f"{ctx.sandbox.root}/{PROGRESS_PATH}"))


def _write_progress(ctx: RunContext, progress: ProgressFile) -> None:
    ctx.sandbox.write_text(f"{ctx.sandbox.root}/{PROGRESS_PATH}", progress.to_json())


def _open_features(spec: ProductSpec, progress: ProgressFile) -> list[ProductFeature]:
    open_ids = set(progress.open_feature_ids())
    return [f for f in spec.features if f.id in open_ids]


def _feature(spec: ProductSpec, fid: str) -> ProductFeature:
    f = next((x for x in spec.features if x.id == fid), None)
    if f is None:
        raise KeyError(f"feature {fid} is not in PRODUCT.md")
    return f


def _branch_exists(ctx: RunContext, name: str) -> bool:
    return ctx.sandbox.exec(["git", "rev-parse", "--verify", "-q", name], timeout_s=30).ok


def _git(ctx: RunContext, *args: str) -> str:
    r = ctx.sandbox.exec(["git", *SCIO_GIT_IDENTITY, *args], timeout_s=120)  # I-104
    if not r.ok:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def total_usd(state: ProductBuildState) -> float:
    """Orchestrator spend plus the latest attempt of every feature run."""
    return round(state.budgets.usd_used + sum(r.usd for r in state.feature_runs), 4)


def product_env(ctx: RunContext, spec: ProductSpec) -> str:
    """Make sure this process runs the product's environment (M9): idempotent, so every node
    that executes commands calls it; a resumed build has a fresh context without it."""
    summary = ensure_product_env(ctx.sandbox, ctx.toolchain, tuple(spec.dependencies))
    ctx.tool_context = ToolContext(toolchain=ctx.toolchain, approved=tuple(spec.dependencies))
    return summary


def _commit_changed(
    ctx: RunContext,
    plan: MilestonePlan,
    node: str,
    kind: str,
    subject: str,
    wanted: Callable[[str], bool],
) -> str | None:
    """Commit the changed files ``wanted`` selects on the checked-out milestone branch, once
    per milestone and node (probe: the marker). Returns the commit, or None if none changed."""
    sb = ctx.sandbox
    marker = f"[scio:{kind}:{plan.id}:{node}]"

    def apply() -> str | None:
        status = git_out(sb, "status", "--porcelain", "--untracked-files=all")
        paths = [ln.split(maxsplit=1)[1] for ln in status.splitlines() if ln.strip()]
        files = [p for p in paths if wanted(p)]
        if not files:
            return None
        _git(ctx, "add", "--", *files)
        _git(ctx, "commit", "-q", "-m", f"chore: {subject} {marker}")
        return head_sha(sb)

    return run_effect(
        ctx.effects,
        thread_id=ctx.thread_id,
        node=node,
        intent=f"{kind}-commit:{plan.id}",
        apply=apply,
        probe=lambda: find_commit(sb, marker),
    )


def commit_install(ctx: RunContext, plan: MilestonePlan, node: str) -> str | None:
    """Commit what the install phase wrote into the product tree (I-085). ``npm install
    --save`` rewrites ``package.json`` and writes the lockfile outside any feature commit;
    left alone they ride every checkout, the pre-attempt reset deletes the lockfile, and the
    merge lands them on ``main`` unreviewed. Only the profile's dependency files are
    committed; pip writes none."""
    deps = ctx.toolchain.dependency_files
    return _commit_changed(
        ctx, plan, node, "install", "install approved dependencies", lambda p: Path(p).name in deps
    )


def commit_progress_sync(ctx: RunContext, plan: MilestonePlan, node: str) -> str | None:
    """Commit ``load``'s sync of ``progress.json`` with ``PRODUCT.md`` (features added or
    removed since the last run). Uncommitted, the first feature's reset reverted it and
    ``update_progress`` wrote the stale records back (the I-085 sweep)."""
    return _commit_changed(
        ctx, plan, node, "sync", "sync progress.json with PRODUCT.md", lambda p: p == PROGRESS_PATH
    )


def feature_context(ctx: RunContext, thread_id: str, tier: str) -> RunContext:
    """The task graph's context for one feature run: the same sandbox (the product
    repository), effect log and prompts; its own thread, tier-pinned budget, cost ledger and
    artifact directory; no GitHub client (the milestone PR is the only one)."""
    budgets = BudgetsConfig.for_settings(ctx.settings)
    lim = budgets.tiers[tier]
    guard = BudgetGuard(
        BudgetLimits(tokens_max=lim.tokens_max, usd_max=lim.usd_max),
        reserve_output_fraction=budgets.reserve_output_fraction,
    )
    harness = ctx.harness.child(budget=guard)
    parent_ledger = ctx.harness.ledger.persist_path
    if parent_ledger is not None:
        harness.ledger.persist_to(parent_ledger.parent / f"{thread_id}.jsonl")
    return RunContext(
        settings=ctx.settings.model_copy(update={"gh_repo": None}),
        thread_id=thread_id,
        harness=harness,
        sandbox=ctx.sandbox,
        effects=ctx.effects,
        artifacts=ArtifactStore(ctx.artifacts.root.parent, thread_id),
        prompts=ctx.prompts,
        limits=LoopLimits(
            max_iters=lim.max_iters,
            max_stall=lim.max_stall,
            max_tool_calls_per_iter=T0_TOOL_CAP if tier == "T0" else None,
        ),
        budget=Budget(
            tokens_max=lim.tokens_max,
            usd_max=lim.usd_max,
            advisor_calls_max=lim.advisor_calls_max,
            max_iters=lim.max_iters,
            max_stall=lim.max_stall,
        ),
        github=None,
        escalation=ctx.escalation,
        tool_context=ctx.tool_context,
        quality_values=quality_values(ctx),
        tier=tier,
        tier_pin=tier,
        toolchain=ctx.toolchain,
        budget_from_tier=False,
    )


def quality_values(ctx: RunContext) -> dict[str, object]:
    """The placeholders of the profile's quality checks (M11): ``integrate`` and every
    feature's verify fill the same ones, so a feature is judged by the milestone's bar."""
    return {"coverage_floor": ctx.settings.coverage_floor}


def feature_task(
    spec: ProductSpec,
    plan: MilestonePlan,
    feat: MilestoneFeatureOutput,
    issue: int | None,
    *,
    repo_dir: str = ".",
) -> TaskSpec:
    """The typed handoff from the product graph to a task subgraph."""
    f = _feature(spec, feat.id)
    ids = ", ".join(plan.feature_ids())
    desc = [
        f"Product: {spec.title} (package `{spec.name}`, stack `{spec.stack}`).",
        f"Vision: {spec.vision}",
        "",
        f"Milestone {plan.id} ({plan.title}) builds {ids}; this task is {f.id}.",
        "The acceptance criteria below are black-box checks from PRODUCT.md: implement the "
        "feature and the tests that prove each criterion.",
    ]
    if feat.notes:
        desc += ["", f"Planner notes: {feat.notes}"]
    desc += [
        "",
        "Product memory: PRODUCT.md and .scio/product/progress.json in the repository root "
        "are maintained by the orchestrator; do not edit them. NOTES.md and CHANGELOG.md are "
        "yours to update.",
    ]
    return TaskSpec(
        title=f"{f.id}: {f.description}",
        description="\n".join(desc),
        acceptance=list(f.acceptance),
        repo_url=repo_dir,
        base_branch=plan.branch,
        issue_number=issue,
        tier_hint=feat.tier,
    )


def feature_run_from_state(
    run: FeatureRun, final: SCIOState, *, status: Literal["passed", "failed", "skipped"]
) -> FeatureRun:
    """Fold a finished task-graph state back into the product's record of the run."""
    return run.model_copy(
        update={
            "status": status,
            "exit_reason": final.exit_reason.value if final.exit_reason else None,
            "commit_sha": final.github.head_sha,
            "branch": final.github.branch,
            "usd": round(final.budgets.usd_used, 4),
            "tokens": final.budgets.tokens_used,
            "iterations": final.progress.iterations,
            "escalations": len(final.escalations),
            "escalations_resolved": sum(1 for e in final.escalations if e.outcome == "resolved"),
            "human_notes": list(final.human_notes),
        }
    )


def issue_title(f: ProductFeature) -> str:
    return f"{f.id}: {f.description}"


def issue_body(spec: ProductSpec, plan: MilestonePlan, f: ProductFeature, tier: str) -> str:
    lines = [
        f"Feature {f.id} of **{spec.title}** (`{spec.name}`), milestone {plan.id}: {plan.title}.",
        "",
        "## Acceptance criteria",
        *[f"- [ ] {a}" for a in f.acceptance],
        "",
        f"Tier: {tier}. Built by `scio build`; progress in `.scio/product/progress.json`.",
        "",
        f"<!-- scio:feature:{f.id} -->",
    ]
    return "\n".join(lines) + "\n"


def render_milestone_pr_body(state: ProductBuildState, spec: ProductSpec) -> str:
    plan = state.milestone
    assert plan is not None
    rows: list[str] = []
    closes: list[str] = []
    for mf in plan.features:
        f = _feature(spec, mf.id)
        run = state.latest_run(mf.id)
        status = run.status if run else "not run"
        commit = (run.commit_sha or "-")[:12] if run else "-"
        usd = f"${run.usd:.4f}" if run else "-"
        esc = f"{run.escalations_resolved}/{run.escalations}" if run else "-"
        rows.append(
            f"| {f.id} {f.description} | {mf.tier.value} | {status} | {commit} | {usd} | {esc} |"
        )
        if (n := state.issues.get(mf.id)) is not None:
            closes.append(f"Closes #{n}")
    gate = state.last_gate
    gate_line = (
        f"{gate.verdict}: {gate.reasoning[:400]}" if gate is not None else "(no gate verdict)"
    )
    integration = state.integration or "(integration checks not run)"
    runs = [r for r in state.feature_runs if r.feature_id in plan.feature_ids()]
    attempts = sum(1 for _ in runs)
    text = "\n".join(
        [
            "## Summary",
            f"Milestone {plan.id}: {plan.title}. {len(plan.features)} feature(s) built by "
            f"`scio build` on branch `{plan.branch}`, each through the task graph with its tier "
            "pinned; integration checks and the Opus milestone gate ran on the merged branch.",
            "",
            *closes,
            "",
            "## Plan vs. done",
            "| Feature | Tier | Status | Commit | Cost | Escalations resolved/total |",
            "|---|---|---|---|---:|---|",
            *rows,
            "",
            "## Test evidence",
            "```",
            integration[:3000],
            "```",
            "",
            "## Milestone gate",
            gate_line,
            "",
            "## Cost",
            f"Features: ${sum(r.usd for r in runs):.4f} over {attempts} run(s) · "
            f"orchestrator: ${state.budgets.usd_used:.4f} · product total so far: "
            f"${total_usd(state):.4f}",
            "",
            "## Risk notes",
            "Human merge required; `progress.json` is updated on `main` after the merge.",
            "",
        ]
    )
    return text


# ---------- nodes ----------


def load(ctx: RunContext) -> Node:
    def node(state: ProductBuildState) -> dict[str, Any]:
        sb = ctx.sandbox
        root = sb.root
        if not sb.exists(f"{root}/{PRODUCT_MD}") or not sb.exists(f"{root}/{PROGRESS_PATH}"):
            return _halt(
                ctx,
                "load_failed",
                f"{PRODUCT_MD} or {PROGRESS_PATH} missing in {root}; run scio new first",
            )
        if not sb.exists(f"{root}/.git"):
            return _halt(ctx, "load_failed", f"{root} is not a git repository")
        try:
            spec = parse_product_md(sb.read_text(f"{root}/{PRODUCT_MD}"))
        except ValueError as exc:
            return _halt(ctx, "load_failed", f"PRODUCT.md unreadable: {exc}")
        # The product's declared stack is the profile every command comes from (M10).
        try:
            ctx.adopt_toolchain(get_toolchain(spec.stack))
        except KeyError as exc:
            return _halt(ctx, "load_failed", f"PRODUCT.md names an unknown stack: {exc}")
        progress = _read_progress(ctx)
        notes = progress.sync_features(spec)
        entries = [fact(n) for n in notes]
        if notes:
            _write_progress(ctx, progress)
        if progress.bootstrap_commit:
            r = sb.exec(["git", "cat-file", "-e", progress.bootstrap_commit], timeout_s=30)
            if not r.ok:
                return _halt(
                    ctx, "load_failed", f"bootstrap commit {progress.bootstrap_commit[:12]} absent"
                )
        entries.append(
            fact(
                f"loaded {spec.name}: {len(spec.features)} features, "
                f"{len(progress.open_feature_ids())} open, "
                f"{len(progress.milestones)} milestone(s) done"
            )
        )
        # Which sandbox this build runs in and why (M9): Docker by default when it is there;
        # a local fallback says what it does not enforce.
        entries.append(fact(product_sandbox_backend(ctx.settings, ctx.toolchain)[1]))
        # The install phase (M9): the product environment with the approved dependencies,
        # network open for this step only; every later command runs inside it, offline.
        try:
            entries.append(fact(product_env(ctx, spec)))
        except RuntimeError as exc:
            return _halt(ctx, "load_failed", f"install phase: {exc}")
        return {"spec": spec, "phase": "plan", "ledger": entries}

    return node


def after_load(state: ProductBuildState) -> Literal["plan", "halt"]:
    return "halt" if state.exit_reason is not None else "plan"


def plan_milestone(ctx: RunContext) -> Node:
    def node(state: ProductBuildState) -> dict[str, Any]:
        spec = state.spec
        assert spec is not None
        progress = _read_progress(ctx)
        open_features = _open_features(spec, progress)
        if not open_features:
            return {
                "phase": "done",
                "milestone": None,
                **_fact("every feature in PRODUCT.md is done; nothing to plan"),
            }
        number = progress.next_milestone_number()
        done_lines = [
            f"- {m.id} {m.title}: {m.status}, features {', '.join(m.features)}"
            for m in progress.milestones
        ] or ["- none yet"]
        feature_lines = []
        for f in spec.features:
            rec = progress.feature(f.id)
            feature_lines.append(f"- {f.id} [{rec.status if rec else 'todo'}] {f.description}")
            feature_lines += [f"  - {a}" for a in f.acceptance]
        msg = "\n".join(
            [
                f"# Product: {spec.title} (package `{spec.name}`, stack `{spec.stack}`)",
                f"Vision: {spec.vision}",
                "",
                "## Milestones so far",
                *done_lines,
                "",
                "## Features (PRODUCT.md order)",
                *feature_lines,
                "",
                f"Open features: {', '.join(f.id for f in open_features)}",
                f"Propose milestone M{number}. Reply as JSON matching the schema.",
            ]
        )
        req = ctx.harness.build_request(
            "milestone_planner",
            system=ctx.prompts.get("milestone_planner").render(),
            messages=[Message.user(msg)],
            tags={"thread": ctx.thread_id, "milestone": f"M{number}"},
        )
        try:
            out: MilestonePlanOutput = ctx.harness.structured(req, MilestonePlanOutput).value
        except BudgetExceeded as exc:
            return _halt(ctx, "budget", f"budget exhausted at milestone planning: {exc}")
        plan, notes = normalise_milestone(out, open_features, number)
        entries = [
            fact(
                f"{plan.id} {plan.title}: "
                + ", ".join(f"{f.id}@{f.tier.value}" for f in plan.features),
                source=Role.MILESTONE_PLANNER,
            )
        ]
        entries += [fact(n) for n in notes]

        # The milestone branch from main (probe: it exists).
        def apply() -> str:
            _git(ctx, "checkout", "-q", "main")
            if not _branch_exists(ctx, plan.branch):
                _git(ctx, "branch", plan.branch)
            return git_out(ctx.sandbox, "rev-parse", plan.branch)

        sha = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="plan_milestone",
            intent=f"branch:{plan.branch}",
            apply=apply,
            probe=lambda: (
                git_out(ctx.sandbox, "rev-parse", plan.branch)
                if _branch_exists(ctx, plan.branch)
                else None
            ),
        )
        entries.append(fact(f"branch {plan.branch} at {sha[:12]}"))
        # The install phase's manifest and lockfile changes are committed on the new branch
        # before any feature's reset can delete them (I-085).
        _git(ctx, "checkout", "-q", plan.branch)
        try:
            product_env(ctx, spec)
        except RuntimeError as exc:
            return _halt(ctx, "load_failed", f"install phase: {exc}")
        installed = commit_install(ctx, plan, "plan_milestone")
        if installed:
            entries.append(fact(f"install phase changes committed at {installed[:12]}"))
        synced = commit_progress_sync(ctx, plan, "plan_milestone")
        if synced:
            entries.append(fact(f"progress.json sync with PRODUCT.md committed at {synced[:12]}"))
        return {
            "milestone": plan,
            "issues": {},
            "integration": None,
            "integration_ok": None,
            "last_gate": None,
            "pr": None,
            "merged_commit": None,
            "phase": "issues",
            "ledger": entries,
            **ctx.budget_update(),
        }

    return node


def after_plan(state: ProductBuildState) -> Literal["issues", "release", "halt"]:
    """Every feature done already (a build started after the last milestone merged with
    ``merge and stop``) goes to the release gate, which ends at once if the version is
    already tagged."""
    if state.exit_reason is not None:
        return "halt"
    return "release" if state.phase == "done" else "issues"


def issues(ctx: RunContext) -> Node:
    """A GitHub milestone and one issue per feature, idempotent by title; offline they are
    files under ``.scio/product/issues/`` committed on the milestone branch."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, plan = state.spec, state.milestone
        assert spec is not None and plan is not None
        entries: list[Any] = []
        numbers: dict[str, int] = {}
        gh = ctx.github
        if gh is not None:
            milestone_title = f"{plan.id}: {plan.title}"
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="issues",
                intent=f"milestone:{plan.id}",
                apply=lambda: gh.ensure_milestone(milestone_title, f"Product {spec.title}"),
                probe=lambda: gh.find_milestone(milestone_title),
            )
            for mf in plan.features:
                f = _feature(spec, mf.id)
                title = issue_title(f)
                body = issue_body(spec, plan, f, mf.tier.value)

                def create(t: str = title, b: str = body) -> int:
                    return gh.ensure_issue(
                        title=t, body=b, milestone=milestone_title, labels=("product-mode",)
                    ).number

                def find(t: str = title) -> int | None:
                    found = gh.find_issue(t)
                    return found.number if found else None

                number = run_effect(
                    ctx.effects,
                    thread_id=ctx.thread_id,
                    node="issues",
                    intent=f"issue:{plan.id}:{f.id}",
                    apply=create,
                    probe=find,
                )
                numbers[f.id] = int(number)
            entries.append(
                fact(
                    f"GitHub milestone {milestone_title!r} and issues "
                    + ", ".join(f"{k} #{v}" for k, v in numbers.items())
                )
            )
        else:
            sb = ctx.sandbox
            _git(ctx, "checkout", "-q", plan.branch)
            paths: list[str] = []
            for mf in plan.features:
                f = _feature(spec, mf.id)
                rel = f"{ISSUES_DIR}/{f.id}.md"
                text = f"# {issue_title(f)}\n\n{issue_body(spec, plan, f, mf.tier.value)}"
                run_effect(
                    ctx.effects,
                    thread_id=ctx.thread_id,
                    node="issues",
                    intent=f"issue-file:{plan.id}:{f.id}",
                    apply=partial(_write, sb, rel, text),
                    probe=partial(_exists, sb, rel),
                )
                paths.append(rel)
            marker = f"[scio:issues:{plan.id}]"

            def commit() -> str:
                _git(ctx, "add", "-A", ISSUES_DIR)
                status = git_out(sb, "status", "--porcelain", ISSUES_DIR)
                if status:
                    _git(ctx, "commit", "-q", "-m", f"chore: {plan.id} feature issues {marker}")
                return head_sha(sb)

            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="issues",
                intent=f"issues-commit:{plan.id}",
                apply=commit,
                probe=lambda: find_commit(sb, marker),
            )
            entries.append(fact(f"offline: {len(paths)} issue files committed on {plan.branch}"))
        return {"issues": numbers, "phase": "feature", "ledger": entries}

    return node


def feature(ctx: RunContext, saver: BaseCheckpointSaver | None) -> Node:
    """Run the next open feature of the milestone through the task graph on its own thread.

    The thread id is deterministic per attempt, so a crash mid-feature resumes the same
    subgraph; its ``merge_wait`` is answered by the orchestrator (the milestone PR is the
    human's stop); any other pause or a failed exit stops here for one human decision:
    ``retry`` (a new attempt), ``skip``, or anything else to stop the build.
    """

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, plan = state.spec, state.milestone
        assert spec is not None and plan is not None
        feat = state.next_feature()
        if feat is None:
            return {"phase": "integrate", **_fact("all milestone features passed or skipped")}
        previous = state.latest_run(feat.id)
        attempt = previous.attempt + 1 if previous is not None else 1
        thread = f"{ctx.thread_id}-{feat.id.lower()}-a{attempt}"
        tier = feat.tier.value
        product_env(ctx, spec)  # a resumed process has no environment yet (M9)
        fctx = feature_context(ctx, thread, tier)
        run = FeatureRun(
            feature_id=feat.id,
            thread_id=thread,
            attempt=attempt,
            tier=feat.tier,
            issue_number=state.issues.get(feat.id),
        )
        entries: list[Any] = []
        try:
            if thread_started(fctx, saver):
                entries.append(fact(f"{feat.id}: resuming thread {thread} (attempt {attempt})"))
                outcome = resume_graph(fctx, None, saver=saver)
            else:
                entries.append(fact(f"{feat.id}: starting thread {thread} at {tier}"))
                # A fresh attempt starts from the milestone branch with a clean tree: a
                # previous attempt that never passed verify leaves its edits uncommitted,
                # and git carries them across a checkout into the next feature's run (I-067).
                _git(ctx, "checkout", "-q", plan.branch)
                _git(ctx, "reset", "-q", "--hard")
                _git(ctx, "clean", "-fdq")
                task = feature_task(
                    spec, plan, feat, state.issues.get(feat.id), repo_dir=state.product_dir
                )
                outcome = run_graph(fctx, task, saver=saver)
            while outcome.paused and outcome.state.phase == "merge_wait":
                outcome = resume_graph(
                    fctx, f"merged into {plan.branch} by scio build", saver=saver
                )
        except HarnessError as exc:
            # A provider or harness failure inside one feature ends this attempt, recorded,
            # and the build gracefully; it never ends a product build as a traceback (I-100).
            # No interrupt here: a resumed node would re-run the failing call first (I-065).
            snap = fctx.harness.budget.snapshot()
            failed = run.model_copy(
                update={
                    "status": "failed",
                    "exit_reason": "error",
                    "usd": round(snap.usd_used, 4),
                    "tokens": snap.tokens_used,
                }
            )
            entries.append(
                fact(
                    f"{feat.id} attempt {attempt} failed: {type(exc).__name__}: {str(exc)[:300]}; "
                    f"the next `scio build` starts attempt {attempt + 1}"
                )
            )
            return {
                "feature_runs": [*state.feature_runs, failed],
                "exit_reason": "feature_failed",
                "phase": "halted",
                "ledger": entries,
                **ctx.budget_update(),
            }
        final = outcome.state
        if not outcome.paused and final.exit_reason is ExitReason.SUCCESS:
            feature_head = final.github.head_sha or head_sha(ctx.sandbox)
            branch = final.github.branch or ""

            def ff() -> str:
                _git(ctx, "checkout", "-q", plan.branch)
                _git(ctx, "merge", "-q", "--ff-only", branch)
                return head_sha(ctx.sandbox)

            def probe() -> str | None:
                r = ctx.sandbox.exec(
                    ["git", "merge-base", "--is-ancestor", feature_head, plan.branch], timeout_s=30
                )
                return git_out(ctx.sandbox, "rev-parse", plan.branch) if r.ok else None

            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="feature",
                intent=f"ff:{plan.id}:{feat.id}:a{attempt}",
                apply=ff,
                probe=probe,
            )
            record = feature_run_from_state(run, final, status="passed")
            entries.append(
                fact(
                    f"{feat.id} passed at {tier} in {record.iterations} iteration(s), "
                    f"${record.usd:.4f}, {record.escalations} escalation(s); "
                    f"{plan.branch} fast-forwarded to {feature_head[:12]}"
                )
            )
            return {
                "feature_runs": [*state.feature_runs, record],
                "phase": "feature",
                "ledger": entries,
                **ctx.budget_update(),
            }

        # A pause the orchestrator cannot answer, or a non-success exit: one human decision.
        reason = final.exit_reason.value if final.exit_reason else "paused"
        answer = str(
            interrupt(
                {
                    "question": f"Feature {feat.id} ({reason}, attempt {attempt}) needs a "
                    "decision: reply 'retry' to run it again, 'skip' to leave it out of the "
                    "milestone, anything else to stop the build.",
                    "feature": feat.id,
                    "thread": thread,
                    "exit_reason": reason,
                    "inner": outcome.interrupt,
                    "ledger_tail": [e.text for e in final.task_ledger[-5:]],
                    "cost_usd": total_usd(state),
                    "resume": 'scio build --product <dir> --decision "<retry|skip|stop>"',
                }
            )
        )
        if outcome.paused:  # close the subgraph's own interrupt with the same answer
            outcome = resume_graph(fctx, answer, saver=saver)
            final = outcome.state
        choice = answer.strip().lower()
        notes = [f"{feat.id} attempt {attempt}: human said {answer.strip()[:120]!r}"]
        if choice.startswith("retry"):
            record = feature_run_from_state(run, final, status="failed")
            entries.append(decision(f"{feat.id} failed ({reason}); retrying", source=Role.HUMAN))
            return {
                "feature_runs": [*state.feature_runs, record],
                "human_notes": notes,
                "phase": "feature",
                "ledger": entries,
                **ctx.budget_update(),
            }
        if choice.startswith("skip"):
            record = feature_run_from_state(run, final, status="skipped")
            entries.append(decision(f"{feat.id} skipped by human ({reason})", source=Role.HUMAN))
            return {
                "feature_runs": [*state.feature_runs, record],
                "human_notes": notes,
                "phase": "feature",
                "ledger": entries,
                **ctx.budget_update(),
            }
        record = feature_run_from_state(run, final, status="failed")
        entries.append(decision(f"{feat.id} failed ({reason}); build stopped", source=Role.HUMAN))
        return {
            "feature_runs": [*state.feature_runs, record],
            "human_notes": notes,
            "exit_reason": "feature_failed",
            "phase": "halted",
            "ledger": entries,
            **ctx.budget_update(),
        }

    return node


def after_feature(state: ProductBuildState) -> Literal["feature", "integrate", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    return "feature" if state.next_feature() is not None else "integrate"


def integration_checks(ctx: RunContext, spec: ProductSpec | None = None) -> tuple[str, bool]:
    """The quality bar on the current checkout: lint, the full suite, the profile's quality
    checks (coverage, build, audit, ...), the secret scan and the release files (M11), then
    the black-box acceptance run. A profile check whose tool is absent is reported as skipped
    and does not fail the milestone; a failing check does (M9)."""
    sb, tc = ctx.sandbox, ctx.toolchain
    lint = sb.exec(tc.lint_argv("."), timeout_s=120)
    findings = tc.lint_findings(lint.exit_code, lint.stdout, lint.stderr)
    tests = sb.exec(tc.test_argv(filter=None, path=None), timeout_s=sb.limits.timeout_s)
    outcome = tc.test_outcome(tests.exit_code, tests.stdout)
    ok = not findings and outcome == "passed"
    lines = [
        f"[lint] {'clean' if not findings else findings[:1500]}",
        f"[tests] {outcome}",
        tests.output[-2500:],
    ]
    values = quality_values(ctx)
    for check in tc.quality_checks():
        r = sb.exec(check.command(values), timeout_s=check.timeout_s)
        result = check.outcome(r.exit_code, r.output)
        tail = r.output.strip().splitlines()[-1][:200] if r.output.strip() else ""
        if result == "skipped":
            lines.append(f"[{check.name}] skipped: tool absent ({' '.join(check.argv[:3])})")
        elif result == "passed":
            lines.append(f"[{check.name}] passed" + (f": {tail}" if tail else ""))
        else:
            ok = False
            lines.append(f"[{check.name}] FAILED (exit {r.exit_code})\n{r.output[-1500:]}")
    # Stack-neutral, SCIO's own code: they always run, never "tool absent" (M11).
    release_lines, release_ok = release_report(sb, tc)
    lines += release_lines
    ok = ok and release_ok
    if spec is not None:
        acceptance, acc_ok = run_acceptance(ctx, spec)
        lines.append(acceptance)
        ok = ok and acc_ok
    return "\n".join(lines), ok


def integrate(ctx: RunContext) -> Node:
    def node(state: ProductBuildState) -> dict[str, Any]:
        plan = state.milestone
        assert plan is not None and state.spec is not None
        _git(ctx, "checkout", "-q", plan.branch)
        product_env(ctx, state.spec)
        commit_install(ctx, plan, "integrate")  # a resumed install that wrote again (I-085)
        text, ok = integration_checks(ctx, state.spec)
        entries = [
            fact(
                f"integration checks on {plan.branch}: {'green' if ok else 'FAILED'}",
                source=Role.TESTER,
            )
        ]
        if not ok:
            return {
                "integration": text,
                "integration_ok": False,
                "exit_reason": "integration_failed",
                "phase": "halted",
                "ledger": entries,
            }
        return {"integration": text, "integration_ok": True, "phase": "gate", "ledger": entries}

    return node


def after_integrate(state: ProductBuildState) -> Literal["gate", "halt"]:
    return "halt" if state.exit_reason is not None else "gate"


def milestone_gate(ctx: RunContext) -> Node:
    """One Opus verdict on the milestone; a failing check or a failed feature overrides it."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, plan = state.spec, state.milestone
        assert spec is not None and plan is not None
        lines = [f"{MILESTONE_GATE_HEADER}: {plan.id} {plan.title}", "", "## Features"]
        for mf in plan.features:
            f = _feature(spec, mf.id)
            run = state.latest_run(mf.id)
            summary = (
                f"run: tier {run.tier.value}, status {run.status}, exit {run.exit_reason}, "
                f"commit {(run.commit_sha or '-')[:12]}, ${run.usd:.4f}, "
                f"{run.escalations} escalation(s)"
                if run
                else "run: none"
            )
            lines += [f"### {f.id}: {f.description} ({summary})", *[f"- {a}" for a in f.acceptance]]
        lines += [
            "",
            "## Integration checks (ground truth)",
            state.integration or "(not run)",
            "",
            "## Diff summary vs main",
            git_out(ctx.sandbox, "diff", "--stat", "main", plan.branch) or "(no changes)",
            "",
            "Is the milestone delivered? Reply as JSON matching the schema.",
        ]
        req = ctx.harness.build_request(
            "milestone_gate",
            system=ctx.prompts.get("milestone_gate").render(),
            messages=[Message.user("\n".join(lines))],
            tags={"thread": ctx.thread_id, "milestone": plan.id},
        )
        try:
            out: GateOutput = ctx.harness.structured(req, GateOutput).value
        except BudgetExceeded as exc:
            return _halt(ctx, "budget", f"budget exhausted at the milestone gate: {exc}")
        failed = [
            mf.id
            for mf in plan.features
            if (r := state.latest_run(mf.id)) is None or r.status != "passed"
        ]
        if out.verdict == "pass" and (not state.integration_ok or failed):
            why = (
                "integration checks failed"
                if not state.integration_ok
                else ("features not passed: " + ", ".join(failed))
            )
            out = out.model_copy(
                update={
                    "verdict": "escalate_human",
                    "objective_met": False,
                    "reasoning": f"overridden: {why}. " + out.reasoning,
                }
            )
        entry = decision(
            f"milestone gate {out.verdict}: {out.reasoning[:300]}"
            + (f" unmet: {out.unmet_acceptance}" if out.unmet_acceptance else ""),
            source=Role.MILESTONE_GATE,
        )
        return {
            "last_gate": out,
            "phase": "pr" if out.verdict == "pass" else "gate",
            "ledger": [entry],
            **ctx.budget_update(),
        }

    return node


def after_gate(state: ProductBuildState) -> Literal["pr", "gate_decision", "halt"]:
    if state.exit_reason is not None or state.last_gate is None:
        return "halt"
    return "pr" if state.last_gate.verdict == "pass" else "gate_decision"


def gate_decision(ctx: RunContext) -> Node:
    """The gate did not pass on green checks: a human says 'proceed' or stops the build."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        gate = state.last_gate
        assert gate is not None
        answer = str(
            interrupt(
                {
                    "question": f"Milestone gate verdict {gate.verdict}: {gate.reasoning[:600]} "
                    "Reply 'proceed' to open the milestone PR anyway, anything else to stop.",
                    "unmet_acceptance": gate.unmet_acceptance,
                    "cost_usd": total_usd(state),
                    "resume": 'scio build --product <dir> --decision "<proceed|stop>"',
                }
            )
        )
        if answer.strip().lower().startswith("proceed"):
            return {
                "phase": "pr",
                "human_notes": [f"gate {gate.verdict} overruled: {answer.strip()[:120]}"],
                "ledger": [decision("human proceeds past the milestone gate", source=Role.HUMAN)],
            }
        return {
            "exit_reason": "gate_escalated",
            "phase": "halted",
            "human_notes": [f"gate {gate.verdict}: {answer.strip()[:120]}"],
            "ledger": [decision("build stopped at the milestone gate", source=Role.HUMAN)],
        }

    return node


def after_gate_decision(state: ProductBuildState) -> Literal["pr", "halt"]:
    return "halt" if state.exit_reason is not None else "pr"


def milestone_pr(ctx: RunContext) -> Node:
    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, plan = state.spec, state.milestone
        assert spec is not None and plan is not None
        body = render_milestone_pr_body(state, spec)
        title = f"{plan.id}: {plan.title}"
        sb = ctx.sandbox
        gh = ctx.github
        if gh is not None:
            default = gh.default_branch()
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="milestone_pr",
                intent=f"push:{plan.branch}",
                apply=lambda: git_push(sb, plan.branch, default_branch=default),
            )
            pr = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="milestone_pr",
                intent=f"pr:{plan.branch}",
                apply=lambda: (
                    gh.ensure_draft_pr(
                        head=plan.branch, base=default, title=title, body=body
                    ).__dict__
                ),
                probe=lambda: p.__dict__ if (p := gh.find_pr(plan.branch)) else None,
            )
            number = int(pr["number"])
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="milestone_pr",
                intent=f"ready:{number}",
                apply=lambda: gh.mark_ready(number),
            )
            record = MilestonePR(number=number, url=str(pr["url"]))
            entry = fact(f"milestone PR #{number} ready for review: {pr['url']}")
        else:
            rel = f"{MILESTONES_DIR}/{plan.id}-pr.md"
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="milestone_pr",
                intent=f"pr-file:{plan.id}",
                apply=lambda: _write(sb, rel, f"# {title}\n\n{body}"),
                probe=lambda: _exists(sb, rel),
            )
            record = MilestonePR(body_path=rel)
            entry = fact(f"offline: milestone PR body written to {rel}")
        return {"pr": record, "phase": "merge_wait", "ledger": [entry]}

    return node


BOOKKEEPING_MARKERS = ("[scio:progress:", "[scio:release:")
"""Commit markers of SCIO's own records on local ``main`` (progress, the changelog cut);
their content reaches origin inside the next PR, so they may be replaced by origin's."""
_PROGRESS_ID_RE = re.compile(r"\[scio:progress:(?P<id>[^\]]+)\]")


class SyncRefused(RuntimeError):
    """Local ``main`` cannot follow origin without losing something (I-102)."""


def sync_main(ctx: RunContext) -> str:
    """Bring local ``main`` to origin's after a merge confirmed on GitHub (I-102).

    A merge commit is a fast-forward. A squash or rebase merge gives origin new SHAs for
    content local ``main`` already has: the previous milestone's progress commit rode in the
    merged branch. Then local ``main`` is reset to origin's, but only when every commit that
    exists only locally is SCIO bookkeeping and origin's ``progress.json`` still records the
    milestones those commits recorded; anything else (a human's commit) halts the build."""
    sb = ctx.sandbox
    _git(ctx, "checkout", "-q", "main")
    _git(ctx, "fetch", "-q", "origin", "main")
    ancestor = sb.exec(["git", "merge-base", "--is-ancestor", "main", "origin/main"], timeout_s=30)
    if ancestor.ok:
        _git(ctx, "merge", "-q", "--ff-only", "origin/main")
        return head_sha(sb)
    log = git_out(sb, "log", "--format=%s", "origin/main..main")
    local_only = [s for s in log.splitlines() if s]
    foreign = [s for s in local_only if not any(m in s for m in BOOKKEEPING_MARKERS)]
    if foreign:
        raise SyncRefused(
            "local main has commits that are neither on origin nor SCIO bookkeeping: "
            + "; ".join(foreign[:5])
            + ". Push them in a PR or remove them, then resume the build"
        )
    recorded = {m.group("id") for s in local_only if (m := _PROGRESS_ID_RE.search(s))}
    shown = sb.exec(["git", "show", f"origin/main:{PROGRESS_PATH}"], timeout_s=30)
    on_origin = (
        {m.id for m in ProgressFile.from_json(shown.stdout).milestones} if shown.ok else set()
    )
    if recorded - on_origin:
        raise SyncRefused(
            f"origin main's progress.json does not record {sorted(recorded - on_origin)}, which "
            "local main does; the merge dropped SCIO's records. Restore them on origin, then "
            "resume the build"
        )
    _git(ctx, "reset", "-q", "--hard", "origin/main")
    return head_sha(sb)


def merge_wait(ctx: RunContext) -> Node:
    """The human approval stop of every product milestone (ADR 0007). With GitHub, the human
    merges the PR and replies 'merged'; offline, 'merge' lets the orchestrator merge locally."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, plan, pr = state.spec, state.milestone, state.pr
        assert spec is not None and plan is not None and pr is not None
        answer = str(
            interrupt(
                {
                    "question": f"Milestone {plan.id} ({plan.title}) is ready. "
                    + (
                        f"Merge PR #{pr.number} on GitHub, then reply 'merged'"
                        if pr.number
                        else "Reply 'merge' to merge the milestone branch into main locally"
                    )
                    + "; reply 'stop' to end the build.",
                    "pr": pr.model_dump(),
                    "branch": plan.branch,
                    "features": [
                        f"{mf.id}: {(r.status if (r := state.latest_run(mf.id)) else 'not run')}"
                        for mf in plan.features
                    ],
                    "cost_usd": total_usd(state),
                    "resume": 'scio build --product <dir> --decision "<merged|stop>"',
                }
            )
        )
        choice = answer.strip().lower()
        if choice.startswith(STOP_ANSWERS):
            return {
                "exit_reason": "stopped",
                "phase": "halted",
                "human_notes": [f"{plan.id} merge: {answer.strip()[:120]}"],
                "ledger": [decision(f"build stopped at {plan.id} merge", source=Role.HUMAN)],
            }
        if not choice.startswith(MERGE_ANSWERS):
            return {
                "phase": "merge_wait",
                "human_notes": [f"{plan.id} merge: {answer.strip()[:120]}"],
                "ledger": [
                    fact(f"unrecognised merge answer {answer.strip()[:60]!r}; asking again")
                ],
            }
        sb = ctx.sandbox
        marker = f"[scio:merge:{plan.id}]"
        if ctx.github is not None and pr.number is not None:
            if not ctx.github.pr_merged(pr.number):
                return {
                    "phase": "merge_wait",
                    "human_notes": [f"{plan.id}: {answer.strip()[:120]}"],
                    "ledger": [fact(f"PR #{pr.number} is not merged yet; asking again")],
                }

            try:
                merged = run_effect(
                    ctx.effects,
                    thread_id=ctx.thread_id,
                    node="merge_wait",
                    intent=f"pull:{plan.id}",
                    apply=lambda: sync_main(ctx),
                )
            except SyncRefused as exc:
                return _halt(ctx, "sync_failed", str(exc))
        else:

            def merge() -> str:
                _git(ctx, "checkout", "-q", "main")
                message = f"Merge {plan.branch}: {plan.title} {marker}"
                _git(ctx, "merge", "-q", "--no-ff", "-m", message, plan.branch)
                return head_sha(sb)

            merged = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="merge_wait",
                intent=f"merge:{plan.id}",
                apply=merge,
                probe=lambda: find_commit(sb, marker),
            )
        return {
            "merged_commit": str(merged),
            "stop_after_merge": "stop" in choice,  # "merge and stop": record, then end the thread
            "phase": "update",
            "human_notes": [f"{plan.id} merge: {answer.strip()[:120]}"],
            "ledger": [
                decision(f"{plan.id} merged into main at {str(merged)[:12]}", source=Role.HUMAN)
            ],
        }

    return node


def after_merge_wait(state: ProductBuildState) -> Literal["update", "merge_wait", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    return "update" if state.phase == "update" else "merge_wait"


def update_progress(ctx: RunContext) -> Node:
    """Record the merged milestone in ``progress.json`` and ``NOTES.md`` on ``main`` (one
    commit, idempotent), then loop to the next milestone or finish."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, plan, pr = state.spec, state.milestone, state.pr
        assert spec is not None and plan is not None and pr is not None
        sb = ctx.sandbox
        marker = f"[scio:progress:{plan.id}]"

        def apply() -> str:
            _git(ctx, "checkout", "-q", "main")
            progress = _read_progress(ctx)
            progress.build_thread = ctx.thread_id
            runs = [r for r in state.feature_runs if r.feature_id in plan.feature_ids()]
            record = MilestoneRecord(
                id=plan.id,
                title=plan.title,
                status="done",
                branch=plan.branch,
                features=plan.feature_ids(),
                pr_number=pr.number,
                pr_url=pr.url or pr.body_path,
                merged_commit=state.merged_commit,
                usd=round(sum(r.usd for r in runs), 4),
            )
            progress.milestones = [m for m in progress.milestones if m.id != plan.id] + [record]
            for mf in plan.features:
                run = state.latest_run(mf.id)
                rec = progress.feature(mf.id)
                if rec is None:
                    rec = FeatureRecord(id=mf.id, description=_feature(spec, mf.id).description)
                    progress.features.append(rec)
                rec.status = "done" if run and run.status == "passed" else "skipped"
                rec.commit = run.commit_sha if run else None
                rec.thread = run.thread_id if run else None
                rec.tier = mf.tier.value
                rec.issue = state.issues.get(mf.id)
                rec.usd = run.usd if run else 0.0
            _write_progress(ctx, progress)
            notes_path = f"{sb.root}/NOTES.md"
            notes = sb.read_text(notes_path) if sb.exists(notes_path) else "# Notes\n\n"
            line = (
                f"- {plan.id} {plan.title} merged by `scio build`: "
                + ", ".join(f"{r.feature_id} {r.status}" for r in runs)
                + f"; ${record.usd:.4f}.\n"
            )
            head, sep, tail = notes.partition("\n\n")
            sb.write_text(notes_path, f"{head}{sep}{line}{tail}" if sep else notes + line)
            paths = [
                p
                for p in (PROGRESS_PATH, "NOTES.md", MILESTONES_DIR)
                if sb.exists(f"{sb.root}/{p}")
            ]
            _git(ctx, "add", "-A", *paths)
            if git_out(sb, "status", "--porcelain", *paths):
                _git(
                    ctx, "commit", "-q", "-m", f"chore: record {plan.id} in progress.json {marker}"
                )
            return head_sha(sb)

        sha = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="update_progress",
            intent=f"progress:{plan.id}",
            apply=apply,
            probe=lambda: find_commit(sb, marker),
        )
        remaining = _read_progress(ctx).open_feature_ids()
        entries = [
            fact(
                f"progress.json updated for {plan.id} at {sha[:12]}; "
                f"{len(remaining)} open feature(s) left"
            )
        ]
        return {
            "milestones_done": [*state.milestones_done, plan.id],
            "milestone": None,  # recorded in progress.json; the next plan sets a new one
            "pr": None,
            "merged_commit": None,
            # the last milestone leads to the release gate; "merge and stop" ends the thread
            "phase": "done" if state.stop_after_merge else "plan" if remaining else "release_check",
            "ledger": entries,
        }

    return node


def after_update(state: ProductBuildState) -> Literal["plan", "release", "done"]:
    if state.phase == "plan":
        return "plan"
    return "release" if state.phase == "release_check" else "done"


# ---------- the release gate (M11) ----------

RELEASE_ANSWERS = ("release", "yes", "approve", "approved", "ok", "ship")


def release_check(ctx: RunContext) -> Node:
    """Once no milestone remains: the product-ready bar on ``main`` (every ``integrate``
    check, acceptance included) and the version the product reports about itself. A failing
    check ends the build without a release (ADR 0007: checks override any verdict); a
    version that is already tagged ends it with nothing to do."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec = state.spec
        assert spec is not None
        sb, tc = ctx.sandbox, ctx.toolchain
        _git(ctx, "checkout", "-q", "main")
        try:
            product_env(ctx, spec)
        except RuntimeError as exc:
            return _halt(ctx, "release_failed", f"install phase: {exc}")
        version, out = read_version(sb, tc, spec)
        if version is None:
            command = tc.skeleton_command(spec.name)
            return _halt(ctx, "release_failed", f"`{command}` printed no version: {out}")
        tag = f"v{version}"
        if (sha := tag_exists(sb, tag)) is not None:
            record = state.release or ReleaseRecord(
                version=version, tag=tag, report="already released", ok=True, commit=sha
            )
            return {
                "release": record,
                "phase": "done",
                **_fact(f"{tag} already exists at {sha[:12]}; nothing to release"),
            }
        report, ok = integration_checks(ctx, spec)
        record = ReleaseRecord(version=version, tag=tag, report=report, ok=ok)
        entry = fact(
            f"release check on main for {tag}: {'green' if ok else 'FAILED'}", source=Role.TESTER
        )
        if not ok:
            return {
                "release": record,
                "exit_reason": "release_failed",
                "phase": "halted",
                "ledger": [entry],
            }
        return {"release": record, "phase": "release_decision", "ledger": [entry]}

    return node


def after_release_check(state: ProductBuildState) -> Literal["decide", "done", "halt"]:
    if state.exit_reason is not None:
        return "halt"
    return "decide" if state.phase == "release_decision" else "done"


def release_decision(ctx: RunContext) -> Node:
    """The human stop before anything is tagged or published. It only asks: a node that
    interrupts does nothing else, so a resume cannot repeat a side effect (I-065)."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, rel = state.spec, state.release
        assert spec is not None and rel is not None
        where = "and publish a GitHub Release" if ctx.github is not None else "locally"
        answer = str(
            interrupt(
                {
                    "question": f"{spec.title} {rel.tag}: the product-ready bar is green on "
                    f"main. Reply 'release' to tag {rel.tag} {where}; 'stop' to end the "
                    "build without a release.",
                    "version": rel.version,
                    "tag": rel.tag,
                    "report": rel.report[-3000:],
                    "cost_usd": total_usd(state),
                    "resume": 'scio build --product <dir> --decision "<release|stop>"',
                }
            )
        )
        choice = answer.strip().lower()
        note = [f"{rel.tag} release: {answer.strip()[:120]}"]
        if choice.startswith(RELEASE_ANSWERS):
            return {
                "phase": "release",
                "human_notes": note,
                "ledger": [decision(f"human approves the {rel.tag} release", source=Role.HUMAN)],
            }
        if choice.startswith(STOP_ANSWERS):
            return {
                "phase": "done",
                "human_notes": note,
                "ledger": [decision(f"{rel.tag} not released", source=Role.HUMAN)],
            }
        return {
            "phase": "release_decision",
            "human_notes": note,
            "ledger": [fact(f"unrecognised release answer {answer.strip()[:60]!r}; asking again")],
        }

    return node


def after_release_decision(
    state: ProductBuildState,
) -> Literal["release", "release_decision", "done"]:
    if state.phase in ("release", "release_decision"):
        return state.phase
    return "done"


def release(ctx: RunContext) -> Node:
    """Cut the product's changelog for ``rel.tag``. Offline the cut is a commit on ``main``,
    tagged by ``release_publish``. With GitHub it goes where SCIO's own releases go
    (CONTRIBUTING step 6): a ``release/<tag>`` branch from local ``main`` (which also carries
    the last progress commit), pushed, with a PR the human merges; ``main`` is never pushed
    (I-103). Each step is an effect with a world probe (ADR 0003)."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, rel = state.spec, state.release
        assert spec is not None and rel is not None
        sb = ctx.sandbox
        marker = f"[scio:release:{rel.tag}]"
        changelog = f"{sb.root}/CHANGELOG.md"
        gh = ctx.github
        branch = f"release/{rel.tag}"

        def cut() -> str:
            _git(ctx, "checkout", "-q", "main")
            if gh is not None:
                _git(ctx, "checkout", "-q", "-B", branch)
            text = sb.read_text(changelog) if sb.exists(changelog) else "# Changelog\n"
            sb.write_text(changelog, cut_changelog(text, rel.version))
            _git(ctx, "add", "CHANGELOG.md")
            if git_out(sb, "status", "--porcelain", "CHANGELOG.md"):
                _git(ctx, "commit", "-q", "-m", f"chore(release): {rel.tag} {marker}")
            return head_sha(sb)

        commit = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="release",
            intent=f"cut:{rel.tag}",
            apply=cut,
            probe=lambda: find_commit(sb, marker),
        )
        if gh is None:
            return {
                "release": rel.model_copy(update={"commit": commit}),
                "phase": "release_publish",
                **_fact(f"changelog cut for {rel.tag} at {commit[:12]}"),
            }
        default = gh.default_branch()
        run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="release",
            intent=f"push:{branch}",
            apply=lambda: git_push(sb, branch, default_branch=default),
        )
        notes = release_notes(sb.read_text(changelog), rel.version)
        pr = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="release",
            intent=f"pr:{branch}",
            apply=lambda: (
                gh.ensure_draft_pr(
                    head=branch,
                    base=default,
                    title=f"chore(release): {rel.tag}",
                    body=f"Release {rel.tag} of {spec.title}, cut by `scio build`.\n\n{notes}",
                ).__dict__
            ),
            probe=lambda: p.__dict__ if (p := gh.find_pr(branch)) else None,
        )
        number = int(pr["number"])
        run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="release",
            intent=f"ready:{number}",
            apply=lambda: gh.mark_ready(number),
        )
        return {
            "release": rel.model_copy(update={"pr_number": number, "pr_url": str(pr["url"])}),
            "phase": "release_merge_wait",
            **_fact(f"release PR #{number} for {rel.tag} is ready: {pr['url']}"),
        }

    return node


def after_release(state: ProductBuildState) -> Literal["publish", "merge_wait"]:
    return "merge_wait" if state.phase == "release_merge_wait" else "publish"


def release_merge_wait(ctx: RunContext) -> Node:
    """With GitHub: the human merges the release PR (its own node, I-065). Once GitHub says
    it is merged, local ``main`` follows origin's (``sync_main``), so the tag that
    ``release_publish`` puts on ``main``'s head sits on ``main``'s history (I-103)."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        rel = state.release
        assert rel is not None and rel.pr_number is not None
        answer = str(
            interrupt(
                {
                    "question": f"Merge release PR #{rel.pr_number} ({rel.tag}) on GitHub, then "
                    "reply 'merged'; reply 'stop' to end the build without tagging.",
                    "pr": rel.pr_url,
                    "tag": rel.tag,
                    "cost_usd": total_usd(state),
                    "resume": 'scio build --product <dir> --decision "<merged|stop>"',
                }
            )
        )
        choice = answer.strip().lower()
        note = [f"{rel.tag} release PR: {answer.strip()[:120]}"]
        if choice.startswith(STOP_ANSWERS):
            return {
                "phase": "done",
                "human_notes": note,
                "ledger": [decision(f"{rel.tag} release PR not merged", source=Role.HUMAN)],
            }
        gh = ctx.github
        if not choice.startswith(MERGE_ANSWERS) or gh is None or not gh.pr_merged(rel.pr_number):
            return {
                "phase": "release_merge_wait",
                "human_notes": note,
                "ledger": [fact(f"release PR #{rel.pr_number} is not merged yet; asking again")],
            }
        try:
            merged = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="release_merge_wait",
                intent=f"pull:{rel.tag}",
                apply=lambda: sync_main(ctx),
            )
        except SyncRefused as exc:
            return _halt(ctx, "sync_failed", str(exc))
        return {
            "release": rel.model_copy(update={"commit": str(merged)}),
            "phase": "release_publish",
            "human_notes": note,
            "ledger": [decision(f"release PR #{rel.pr_number} merged", source=Role.HUMAN)],
        }

    return node


def after_release_merge_wait(
    state: ProductBuildState,
) -> Literal["publish", "release_merge_wait", "done"]:
    if state.exit_reason is not None:
        return "done"
    if state.phase in ("release_publish", "release_merge_wait"):
        return "publish" if state.phase == "release_publish" else "release_merge_wait"
    return "done"


def release_publish(ctx: RunContext) -> Node:
    """Tag ``main``'s head (offline: the changelog cut; with GitHub: the release PR's merge),
    keep the artifacts, and with GitHub push the tag and publish the Release with the
    artifacts attached. Each step is an effect with a world probe, so a resumed release
    neither tags nor publishes twice (ADR 0003)."""

    def node(state: ProductBuildState) -> dict[str, Any]:
        spec, rel = state.spec, state.release
        assert spec is not None and rel is not None and rel.commit is not None
        sb = ctx.sandbox
        changelog = f"{sb.root}/CHANGELOG.md"
        commit = rel.commit
        gh = ctx.github

        def tag() -> str:
            _git(ctx, "tag", "-a", rel.tag, "-m", f"{spec.title} {rel.tag}", commit)
            return commit

        run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="release_publish",
            intent=f"tag:{rel.tag}",
            apply=tag,
            probe=lambda: tag_exists(sb, rel.tag),
        )
        artifacts = run_effect(
            ctx.effects,
            thread_id=ctx.thread_id,
            node="release_publish",
            intent=f"artifacts:{rel.tag}",
            apply=lambda: build_artifacts(sb, spec, rel.version, rel.tag),
        )
        url: str | None = None
        if gh is not None:
            run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="release_publish",
                intent=f"push-tag:{rel.tag}",
                apply=lambda: _git(ctx, "push", "origin", f"refs/tags/{rel.tag}"),
            )
            notes = release_notes(sb.read_text(changelog), rel.version)
            url = run_effect(
                ctx.effects,
                thread_id=ctx.thread_id,
                node="release_publish",
                intent=f"gh-release:{rel.tag}",
                apply=lambda: gh.ensure_release(
                    tag=rel.tag,
                    title=f"{spec.title} {rel.tag}",
                    notes=notes,
                    assets=[f"{sb.root}/{a}" for a in artifacts],
                ),
                probe=lambda: gh.find_release(rel.tag),
            )
        shipped = rel.model_copy(update={"commit": commit, "artifacts": artifacts, "url": url})
        where = url or f"locally ({', '.join(artifacts)})"
        return {
            "release": shipped,
            "phase": "done",
            **_fact(f"released {rel.tag} at {commit[:12]}: {where}"),
        }

    return node


def done(_ctx: RunContext) -> Node:
    def node(state: ProductBuildState) -> dict[str, Any]:
        return {"phase": "done" if state.exit_reason is None else "halted"}

    return node


# ---------- graph ----------


def _unbound(name: str) -> Any:
    def node(_state: Any) -> dict[str, Any]:
        raise RuntimeError(f"node {name!r} compiled without a RunContext")

    return node


def build_product_build_graph(
    ctx: RunContext | None, saver: BaseCheckpointSaver | None = None
) -> Any:
    g: Any = StateGraph(ProductBuildState)

    def n(name: str, factory: Any) -> None:
        g.add_node(name, factory(ctx) if ctx is not None else _unbound(name))

    n("load", load)
    n("plan_milestone", plan_milestone)
    n("issues", issues)
    g.add_node("feature", feature(ctx, saver) if ctx is not None else _unbound("feature"))
    n("integrate", integrate)
    n("milestone_gate", milestone_gate)
    n("gate_decision", gate_decision)
    n("milestone_pr", milestone_pr)
    n("merge_wait", merge_wait)
    n("update_progress", update_progress)
    n("release_check", release_check)
    n("release_decision", release_decision)
    n("release", release)
    n("release_merge_wait", release_merge_wait)
    n("release_publish", release_publish)
    n("done", done)
    g.add_edge(START, "load")
    g.add_conditional_edges("load", after_load, {"plan": "plan_milestone", "halt": "done"})
    g.add_conditional_edges(
        "plan_milestone",
        after_plan,
        {"issues": "issues", "release": "release_check", "halt": "done"},
    )
    g.add_edge("issues", "feature")
    g.add_conditional_edges(
        "feature", after_feature, {"feature": "feature", "integrate": "integrate", "halt": "done"}
    )
    g.add_conditional_edges(
        "integrate", after_integrate, {"gate": "milestone_gate", "halt": "done"}
    )
    g.add_conditional_edges(
        "milestone_gate",
        after_gate,
        {"pr": "milestone_pr", "gate_decision": "gate_decision", "halt": "done"},
    )
    g.add_conditional_edges(
        "gate_decision", after_gate_decision, {"pr": "milestone_pr", "halt": "done"}
    )
    g.add_edge("milestone_pr", "merge_wait")
    g.add_conditional_edges(
        "merge_wait",
        after_merge_wait,
        {"update": "update_progress", "merge_wait": "merge_wait", "halt": "done"},
    )
    g.add_conditional_edges(
        "update_progress",
        after_update,
        {"plan": "plan_milestone", "release": "release_check", "done": "done"},
    )
    g.add_conditional_edges(
        "release_check",
        after_release_check,
        {"decide": "release_decision", "done": "done", "halt": "done"},
    )
    g.add_conditional_edges(
        "release_decision",
        after_release_decision,
        {"release": "release", "release_decision": "release_decision", "done": "done"},
    )
    g.add_conditional_edges(
        "release",
        after_release,
        {"publish": "release_publish", "merge_wait": "release_merge_wait"},
    )
    g.add_conditional_edges(
        "release_merge_wait",
        after_release_merge_wait,
        {
            "publish": "release_publish",
            "release_merge_wait": "release_merge_wait",
            "done": "done",
        },
    )
    g.add_edge("release_publish", "done")
    g.add_edge("done", END)
    return g


def initial_build_state(ctx: RunContext, *, gh_repo: str | None) -> ProductBuildState:
    return ProductBuildState(
        thread_id=ctx.thread_id,
        product_dir=str(ctx.sandbox.root),
        gh_repo=gh_repo,
        budgets=ctx.budget,
    )


def _outcome(raw: dict[str, Any]) -> BuildOutcome:
    interrupts = raw.pop("__interrupt__", None)
    payload = None
    if interrupts:
        first = interrupts[0]
        payload = first.value if hasattr(first, "value") else dict(first)
    return BuildOutcome(state=ProductBuildState.model_validate(raw), interrupt=payload)


def run_build(ctx: RunContext, *, gh_repo: str | None = None) -> BuildOutcome:
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        raw = graph.invoke(
            initial_build_state(ctx, gh_repo=gh_repo), config=config, durability=DURABILITY
        )
    return _outcome(raw)


def resume_build(ctx: RunContext, decision: str | None) -> BuildOutcome:
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        inp: Any = Command(resume=decision) if decision is not None else None
        raw = graph.invoke(inp, config=config, durability=DURABILITY)
    return _outcome(raw)


def build_started(ctx: RunContext) -> bool:
    """True when the build thread has a checkpoint (so ``scio build`` resumes it)."""
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        return bool(graph.get_state(config).values)


def build_finished(ctx: RunContext) -> bool:
    """True when the build thread exists and has ended (``done`` or ``halted``), so a new
    ``scio build`` must start another thread rather than resume this one."""
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        snapshot = graph.get_state(config)
    return bool(snapshot.values) and not snapshot.next


def load_build_state(ctx: RunContext) -> ProductBuildState:
    """The latest checkpointed state of a build thread (``scio report``)."""
    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        values = graph.get_state(config).values
    if not values:
        raise LookupError(f"no checkpoint for thread {ctx.thread_id!r}")
    return ProductBuildState.model_validate(values)


def progress_summary(path: Path) -> str:
    """``scio status``: the product's progress.json as text."""
    progress = ProgressFile.from_json(path.read_text(encoding="utf-8"))
    lines = [
        f"product {progress.product} (stack {progress.stack}) "
        f"bootstrap {(progress.bootstrap_commit or '-')[:12]} "
        f"build thread {progress.build_thread or '-'}",
        f"milestones: {len(progress.milestones)} done · features: "
        f"{sum(1 for f in progress.features if f.status == 'done')} done / "
        f"{len(progress.features)} total · ${sum(m.usd for m in progress.milestones):.4f}",
    ]
    for m in progress.milestones:
        pr = f"PR #{m.pr_number}" if m.pr_number else (m.pr_url or "-")
        lines.append(
            f"  {m.id} [{m.status}] {m.title}: {', '.join(m.features)} · {pr} · ${m.usd:.4f}"
        )
    for f in progress.features:
        extra = f" · {f.tier} · {(f.commit or '')[:12]}" if f.commit else ""
        lines.append(f"  {f.id} [{f.status}] {f.description}{extra}")
    return "\n".join(lines)
