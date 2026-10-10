"""``verify``: tests and lint are ground truth. Decides feature status and exit reasons."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from scio.aci.tools import RunTestsInput, ToolError, run_tests
from scio.graph.nodes.common import diff_stat, fact
from scio.graph.run_context import RunContext
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
        all_ok = True

        # 0. Something must have changed since the base commit.
        changed = diff_stat(ctx.sandbox, state.github.base_sha)
        if changed.startswith("(no changes"):
            all_ok = False
            outputs.append("[diff] no changes since the base commit; nothing to verify")

        # 1. Feature tests: filters from the feature list, else from the worker's tests_added.
        reported = state.last_worker_output.tests_added if state.last_worker_output else []
        fallback = " or ".join(f"({t})" for t in reported) if reported else None
        features = [
            f if f.test_filter else f.model_copy(update={"test_filter": fallback})
            for f in state.feature_list
        ]
        per_filter_ok: dict[str | None, bool] = {}
        for flt in sorted({f.test_filter for f in features if f.test_filter}):
            out, ok = _run(ctx, flt)
            if out.startswith("no tests collected"):
                ok = False
            per_filter_ok[flt] = ok
            all_ok &= ok
            outputs.append(f"[run_tests filter={flt!r}]\n{out}")
        if not any(f.test_filter for f in features):
            all_ok = False
            outputs.append(
                "[tests] no feature tests reported; add tests and list them in tests_added"
            )

        # 2. Whole suite (regressions) and 3. lint.
        out, ok = _run(ctx, None)
        all_ok &= ok
        outputs.append(f"[run_tests all]\n{out}")
        lint_out = lint(ctx)
        if lint_out:
            all_ok = False
            outputs.append(f"[lint]\n{lint_out}")
        text = "\n\n".join(outputs)
        ref = ctx.artifacts.put("test_output", text, produced_by=Role.TESTER)

        features = [
            f.model_copy(
                update={
                    "status": FeatureStatus.PASSING
                    if all_ok and per_filter_ok.get(f.test_filter, False)
                    else FeatureStatus.FAILING,
                    "evidence": ref,
                }
            )
            for f in features
        ]
        progress = advance(state.progress, features, error_text=None if all_ok else text)
        exit_reason = state.exit_reason
        if not all_ok and exit_reason is None:
            exit_reason = check_exit(progress, ctx.budget, ctx.limits)
        entry = fact(
            f"verify: {'all checks passed' if all_ok else 'failing'} "
            f"(iteration {progress.iterations}, stall {progress.stall_count})",
            source=Role.TESTER,
        )
        return {
            "feature_list": features,
            "progress": progress,
            "artifacts": {**state.artifacts, "verify:last": ref},
            "exit_reason": exit_reason,
            "task_ledger": [entry],
            "phase": "checkpoint" if all_ok else "implement",
        }

    return node


def after_verify(state: SCIOState) -> Literal["checkpoint", "t0_loop", "halt"]:
    if state.all_passing():
        return "checkpoint"
    if state.exit_reason is not None:
        return "halt"
    return "t0_loop"
