"""``verify``: tests and lint are ground truth. Decides feature status, step completion,
exit reasons, and (T1/T2) the triage decision the next edge reads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from scio.aci.tools import RunTestsInput, ToolError, run_tests
from scio.graph.nodes.common import diff_stat, fact
from scio.graph.run_context import RunContext
from scio.loop.escalation import decide, exit_for
from scio.loop.exits import check_exit
from scio.state.models import FeatureStatus, Role, SCIOState
from scio.state.signatures import advance

Node = Callable[[SCIOState], dict[str, Any]]


def lint(ctx: RunContext) -> str:
    """Ruff output on the sandbox tree, or '' when clean or ruff is unavailable."""
    r = ctx.sandbox.exec(
        [
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "E9,F63,F7,F82,F821",
            "--output-format",
            "concise",
            ".",
        ],
        timeout_s=120,
    )
    if r.exit_code in (0, 127):
        return ""
    return r.stdout.strip()[:3000] if r.exit_code == 1 else ""


def _run(ctx: RunContext, flt: str | None) -> tuple[str, bool]:
    try:
        out = run_tests(ctx.sandbox, RunTestsInput(filter=flt))
    except ToolError as exc:
        return str(exc), False
    return out, out.startswith("all tests passed")


def verify(ctx: RunContext) -> Node:
    """Ground truth, in order: changes exist; feature tests exist and pass; suite passes; lint."""

    def node(state: SCIOState) -> dict[str, Any]:
        outputs: list[str] = []
        tests_ok = True
        no_changes = False

        # 0. Something must have changed since the base commit.
        changed = diff_stat(ctx.sandbox, state.github.base_sha)
        if changed.startswith("(no changes"):
            tests_ok = False
            no_changes = True
            outputs.append("[diff] no changes since the base commit; nothing to verify")

        # 1. Feature tests for the features in scope (the current step, or all in T0).
        reported = state.last_worker_output.tests_added if state.last_worker_output else []
        fallback = " or ".join(f"({t})" for t in reported) if reported else None
        step = state.current_step()
        scope_ids = (
            set(step.feature_ids) if step is not None else {f.id for f in state.feature_list}
        )
        features = [
            f
            if f.test_filter or f.id not in scope_ids
            else f.model_copy(update={"test_filter": fallback})
            for f in state.feature_list
        ]
        in_scope = [f for f in features if f.id in scope_ids]
        per_filter_ok: dict[str | None, bool] = {}
        for flt in sorted({f.test_filter for f in in_scope if f.test_filter}):
            out, ok = _run(ctx, flt)
            if out.startswith("no tests collected"):
                ok = False
            per_filter_ok[flt] = ok
            tests_ok &= ok
            outputs.append(f"[run_tests filter={flt!r}]\n{out}")
        if not any(f.test_filter for f in in_scope):
            tests_ok = False
            outputs.append(
                "[tests] no feature tests reported; add tests and list them in tests_added"
            )

        # 2. Whole suite (regressions) and 3. lint.
        out, ok = _run(ctx, None)
        tests_ok &= ok
        outputs.append(f"[run_tests all]\n{out}")
        lint_out = lint(ctx)
        if lint_out:
            outputs.append(f"[lint]\n{lint_out}")
        all_ok = tests_ok and not lint_out
        text = "\n\n".join(outputs)
        ref = ctx.artifacts.put("test_output", text, produced_by=Role.TESTER)

        features = [
            f.model_copy(
                update={
                    "status": FeatureStatus.PASSING
                    if all_ok and f.id in scope_ids and per_filter_ok.get(f.test_filter, False)
                    else (
                        FeatureStatus.PASSING
                        if f.status is FeatureStatus.PASSING and f.id not in scope_ids
                        else FeatureStatus.FAILING
                    ),
                    "evidence": ref if f.id in scope_ids else f.evidence,
                }
            )
            for f in features
        ]
        progress = advance(state.progress, features, error_text=None if all_ok else text)
        progress.last_failure_kind = (
            "none"
            if all_ok
            else "no_changes"
            if no_changes
            else "lint_only"
            if tests_ok
            else "tests"
        )
        plan = state.plan
        if all_ok and plan is not None and step is not None:
            steps = [
                s.model_copy(update={"done": True}) if s.id == step.id else s for s in plan.steps
            ]
            plan = plan.model_copy(update={"steps": steps})  # git_checkpoint advances the step

        # Escalation outcome: a pending advisor call is resolved by the next passing verify.
        escalations = list(state.escalations)
        if escalations and escalations[-1].outcome == "pending":
            escalations[-1] = escalations[-1].model_copy(
                update={"outcome": "resolved" if all_ok else "unresolved"}
            )

        exit_reason = state.exit_reason
        triage_decision = None
        if not all_ok and exit_reason is None:
            exit_reason = check_exit(progress, ctx.budget, ctx.limits)
        if not all_ok and exit_reason is None and plan is not None:
            probe = state.model_copy(update={"progress": progress, "feature_list": features})
            triage_decision = decide(probe, ctx.escalation, root=ctx.sandbox.root)
            exit_reason = exit_for(triage_decision)
        entry = fact(
            f"verify: {'all checks passed' if all_ok else progress.last_failure_kind} "
            f"(iteration {progress.iterations}, stall {progress.stall_count})"
            + (
                f"; triage -> {triage_decision.route} ({triage_decision.reason})"
                if triage_decision
                else ""
            ),
            source=Role.TESTER,
        )
        return {
            "feature_list": features,
            "progress": progress,
            "plan": plan,
            "escalations": escalations,
            "triage": triage_decision,
            "artifacts": {**state.artifacts, "verify:last": ref},
            "exit_reason": exit_reason,
            "task_ledger": [entry],
            "phase": "checkpoint" if all_ok else "implement",
        }

    return node


def after_verify(state: SCIOState) -> Literal["checkpoint", "t0_loop", "triage", "halt"]:
    """T0: loop back to the coder. T1/T2: the triage edge decides debug / advise / halt."""
    if state.exit_reason is not None:
        return "halt"
    if state.progress.last_failure_kind == "none":
        return "checkpoint"
    return "t0_loop" if state.plan is None else "triage"
