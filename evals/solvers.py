"""Solvers: how a trial is executed and graded.

* :class:`PlaceholderSolver` (M1): one structured call; passes if it validates.
* :class:`GraphSolver` (M3/M4): runs the SCIO graph (the router picks the tier) on a fresh
  clone of the task's fixture repository, resumes through the merge interrupt, then
  grades the final state with the task's hidden tests. On the Fake provider the model
  is :class:`evals.fake_scripts.ScriptedResponder`.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from scio.aci.tools import RunTestsInput, ToolError, run_tests
from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import resume_graph, run_graph
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetLimits, BudgetsConfig
from scio.harness.core import Harness
from scio.harness.providers.fake import FakeProvider
from scio.harness.types import Message
from scio.prompt.registry import PromptRegistry
from scio.state.models import ExitReason, TaskSpec


class TrialOutcome(BaseModel):
    passed: bool
    advisor_calls: int = 0
    advisor_resolved: int = 0
    notes: str = ""


class PlaceholderAnswer(BaseModel):
    step_id: str
    summary: str
    confidence: Literal["high", "medium", "low"]


class EvalTaskLike(BaseModel):
    """The subset of ``EvalTask`` solvers need (avoids a circular import with the runner)."""

    id: str
    spec: str
    path: Path
    hidden_tests: str | None = None
    tokens: int = 100_000
    usd: float = 1.0
    tier: str = "T0"


class PlaceholderSolver:
    """One structured call through the harness. Passes if the output validates."""

    def __init__(self) -> None:
        self._prompts = PromptRegistry()

    def solve(self, task: EvalTaskLike, harness: Harness) -> TrialOutcome:
        system = self._prompts.get("coder").render()
        request = harness.build_request(
            "coder",
            system=system,
            messages=[
                Message.user(f"Task:\n{task.spec}\n\nReply with a JSON summary for step S1.")
            ],
            max_tokens=512,
            tags={"task_id": task.id},
        )
        result = harness.structured(request, PlaceholderAnswer)
        return TrialOutcome(passed=result.value.step_id != "", notes=f"attempts={result.attempts}")


def make_origin(fixture: Path, dest: Path) -> Path:
    """Copy the fixture repository and turn it into a git repo with one commit on main."""
    shutil.copytree(fixture, dest)
    git = ["git", "-c", "core.autocrlf=false", "-c", "user.name=fixture", "-c", "user.email=f@x"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run([*git, "add", "-A"], cwd=dest, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], cwd=dest, check=True)
    return dest


def task_spec(task: EvalTaskLike, origin: Path) -> TaskSpec:
    return TaskSpec(
        title=task.spec.splitlines()[0].lstrip("# ").strip() or task.id,
        description=task.spec,
        acceptance=acceptance_from_issue_body(task.spec),
        repo_url=str(origin),
        base_branch="main",
    )


class GraphSolver:
    def __init__(
        self,
        *,
        responder_factory: Callable[[], Any] | None = None,
        keep_dirs: bool = False,
    ) -> None:
        self.responder_factory = responder_factory
        self.keep_dirs = keep_dirs

    def solve(self, task: EvalTaskLike, harness: Harness) -> TrialOutcome:
        tmp = Path(tempfile.mkdtemp(prefix=f"scio-{task.id}-"))
        try:
            return self._solve(task, harness, tmp)
        finally:
            if not self.keep_dirs:
                shutil.rmtree(tmp, ignore_errors=True)

    def _solve(self, task: EvalTaskLike, harness: Harness, tmp: Path) -> TrialOutcome:
        origin = make_origin(task.path / "repo", tmp / "origin")
        settings = harness.settings.model_copy(
            update={"checkpoint_dsn": f"sqlite:///{(tmp / 'ckpt.sqlite').as_posix()}"}
        )
        if settings.provider_override == "fake":
            from evals.fake_scripts import ScriptedResponder

            factory = self.responder_factory or ScriptedResponder
            harness.providers.register("fake", FakeProvider(responder=factory()))
        tier_defaults = BudgetsConfig.load(settings.budgets_path).tiers.get(task.tier)
        limits = BudgetLimits(
            tokens_max=task.tokens,
            usd_max=task.usd,
            advisor_calls_max=tier_defaults.advisor_calls_max if tier_defaults else 0,
            max_iters=tier_defaults.max_iters if tier_defaults else 30,
            max_stall=tier_defaults.max_stall if tier_defaults else 2,
        )
        ctx = RunContext.create(
            thread_id=f"{task.id}-{uuid.uuid4().hex[:8]}",
            sandbox_root=tmp / "work",
            tier=task.tier,
            settings=settings,
            harness=harness,
            work_dir=tmp / ".scio",
            budget_limits=limits,
        )
        outcome = run_graph(ctx, task_spec(task, origin))
        if outcome.paused and outcome.state.phase == "merge_wait":
            outcome = resume_graph(ctx, "approved (eval)")
        state = outcome.state
        graded = self._grade(ctx, task)
        passed = state.exit_reason is ExitReason.SUCCESS and graded[0]
        exit_name = state.exit_reason.value if state.exit_reason else None
        tier = state.routing.tier.value if state.routing else "?"
        notes = (
            f"tier={tier} exit={exit_name} phase={state.phase} "
            f"iterations={state.progress.iterations} tool_calls={state.progress.tool_calls} "
            f"hidden={graded[1]}"
        )
        resolved = sum(1 for e in state.escalations if e.outcome == "resolved")
        return TrialOutcome(
            passed=passed,
            advisor_calls=len(state.escalations),
            advisor_resolved=resolved,
            notes=notes,
        )

    def _grade(self, ctx: RunContext, task: EvalTaskLike) -> tuple[bool, str]:
        if not task.hidden_tests:
            return True, "no hidden tests"
        src = task.path / task.hidden_tests
        dest = f"{ctx.sandbox.root}/tests/{src.name}"
        ctx.sandbox.write_text(dest, src.read_text(encoding="utf-8"))
        try:
            out = run_tests(ctx.sandbox, RunTestsInput(path=dest))
        except ToolError as exc:
            return False, str(exc)[:200]
        return out.startswith("all tests passed"), out.splitlines()[0][:200]
