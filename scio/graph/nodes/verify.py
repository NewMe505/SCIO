"""``verify``: tests and lint are ground truth. Decides feature status, step completion,
exit reasons, and (T1/T2) the triage decision the next edge reads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from scio.aci.tools import RunTestsInput, ToolError, run_tests
from scio.graph.nodes.common import diff_stat, fact
from scio.graph.run_context import RunContext
from scio.loop.escalation import decide, exit_for, review_decision
from scio.loop.exits import check_exit
from scio.state.models import ExitReason, FeatureStatus, Role, SCIOState
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


SELECTOR_SEP = " ; "


def is_path_selector(selector: str) -> bool:
    """``path::name`` node ids and ``*.py`` paths are positional targets; else ``-k``."""
    s = selector.strip()
    return "::" in s or s.endswith(".py") or "/" in s or "\\" in s


def selector_input(root: str, selector: str) -> RunTestsInput:
    """Turn one reported test selector into a ``run_tests`` call (ledger I-031)."""
    s = selector.strip()
    if is_path_selector(s):
        path = s if s.startswith(root) or s[1:3] == ":\\" or s.startswith("/") else f"{root}/{s}"
        return RunTestsInput(path=path)
    return RunTestsInput(filter=s)


def join_selectors(selectors: list[str]) -> str:
    return SELECTOR_SEP.join(dict.fromkeys(s.strip() for s in selectors if s.strip()))


def split_selectors(test_filter: str) -> list[str]:
    return [s for s in (x.strip() for x in test_filter.split(SELECTOR_SEP)) if s]


def _run(ctx: RunContext, flt: str | None) -> tuple[str, bool]:
    try:
        out = run_tests(ctx.sandbox, RunTestsInput(filter=flt))
    except ToolError as exc:
        return str(exc), False
    return out, out.startswith("all tests passed")


def _run_selectors(ctx: RunContext, test_filter: str) -> tuple[str, bool]:
    """Run every selector in a feature's ``test_filter``; all must pass and collect tests."""
    chunks: list[str] = []
    ok_all = True
    for sel in split_selectors(test_filter):
        inp = selector_input(ctx.sandbox.root, sel)
        try:
            out = run_tests(ctx.sandbox, inp)
            ok = out.startswith("all tests passed")
        except ToolError as exc:
            out, ok = str(exc), False
        if out.startswith("no tests collected"):
            ok = False
            out += " (selector matched nothing: use 'path::test_name' or a -k keyword)"
        label = f"path={inp.path!r}" if inp.path else f"filter={inp.filter!r}"
        chunks.append(f"[run_tests {label}]\n{out}")
        ok_all &= ok
    return "\n\n".join(chunks), ok_all


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
            outputs.append(
                "[diff] no changes since the base commit; nothing to verify. A step must "
                "change code or tests; do not treat a step as exploration"
            )

        # 1. Feature tests for the features in scope (the current step, or all in T0).
        reported = state.last_worker_output.tests_added if state.last_worker_output else []
        fallback = join_selectors(reported) or None
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
            out, ok = _run_selectors(ctx, flt)
            if not ok and fallback and fallback != flt and "matched nothing" in out:
                # The plan guessed test names before the tests existed (I-037). The tests the
                # worker actually reports are the ground truth; adopt them if they pass.
                out2, ok2 = _run_selectors(ctx, fallback)
                out += f"\n\n[plan filter matched nothing; trying worker-reported tests]\n{out2}"
                if ok2:
                    ok = True
                    features = [
                        f.model_copy(update={"test_filter": fallback})
                        if f.id in scope_ids and f.test_filter == flt
                        else f
                        for f in features
                    ]
                    per_filter_ok[fallback] = True
            per_filter_ok[flt] = ok
            tests_ok &= ok
            outputs.append(out)
        if not any(f.test_filter for f in in_scope):
            tests_ok = False
            outputs.append(
                "[tests] no feature tests reported. In tests_added, list the pytest node ids "
                "(tests/test_x.py::test_name) or -k keywords of the tests that prove each "
                "feature; existing tests count, you do not have to write new ones"
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
        if all_ok and exit_reason is None and progress.stall_count > ctx.limits.max_stall:
            # Passing verifies that change nothing (no new commit, same step) are a loop,
            # not progress; the stall cap applies to them too (I-046).
            exit_reason = ExitReason.STALL
        if not all_ok and exit_reason is None and plan is not None:
            probe = state.model_copy(update={"progress": progress, "feature_list": features})
            triage_decision = decide(probe, ctx.escalation, root=ctx.sandbox.root)
            exit_reason = exit_for(triage_decision)
        if all_ok and exit_reason is None and plan is not None:
            # ADR 0008: a passing high-impact diff gets one advisor review before commit.
            probe = state.model_copy(update={"progress": progress})
            triage_decision = review_decision(probe, ctx.escalation)
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
        # ADR 0008: verify stores an advise decision for a passing high-impact diff.
        reviewing = state.triage is not None and state.triage.route == "advise"
        return "triage" if reviewing and state.plan is not None else "checkpoint"
    return "t0_loop" if state.plan is None else "triage"
