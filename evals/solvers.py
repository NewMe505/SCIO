"""Solvers: how a trial is executed and graded.

* :class:`PlaceholderSolver` (M1): one structured call; passes if it validates.
* :class:`GraphSolver` (M3): runs the T0 graph on a fresh clone of the task's fixture
  repository, resumes through the merge interrupt, then grades the final state with the
  task's hidden tests. On the Fake provider the model is :class:`ScriptedResponder`.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from scio.aci.tools import RunTestsInput, ToolError, run_tests
from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import resume_t0, run_t0
from scio.graph.run_context import RunContext
from scio.harness.budget import BudgetLimits
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


class EvalTaskLike(BaseModel):
    """The subset of ``EvalTask`` solvers need (avoids a circular import with the runner)."""

    id: str
    spec: str
    path: Path
    hidden_tests: str | None = None
    tokens: int = 100_000
    usd: float = 1.0


def make_origin(fixture: Path, dest: Path) -> Path:
    """Copy the fixture repository and turn it into a git repo with one commit on main."""
    shutil.copytree(fixture, dest)
    git = ["git", "-c", "core.autocrlf=false", "-c", "user.name=fixture", "-c", "user.email=f@x"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=dest, check=True)
    subprocess.run([*git, "add", "-A"], cwd=dest, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fixture"], cwd=dest, check=True)
    return dest


class GraphSolver:
    def __init__(self, *, responder_factory: type | None = None, keep_dirs: bool = False) -> None:
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
        thread_id = f"{task.id}-{uuid.uuid4().hex[:8]}"
        ctx = RunContext.create(
            thread_id=thread_id,
            sandbox_root=tmp / "work",
            tier="T0",
            settings=settings,
            harness=harness,
            work_dir=tmp / ".scio",
            budget_limits=BudgetLimits(tokens_max=task.tokens, usd_max=task.usd),
        )
        spec = TaskSpec(
            title=task.spec.splitlines()[0].lstrip("# ").strip() or task.id,
            description=task.spec,
            acceptance=acceptance_from_issue_body(task.spec),
            repo_url=str(origin),
            base_branch="main",
        )
        outcome = run_t0(ctx, spec)
        if outcome.paused and outcome.state.phase == "merge_wait":
            outcome = resume_t0(ctx, "approved (eval)")
        state = outcome.state
        graded = self._grade(ctx, task)
        passed = state.exit_reason is ExitReason.SUCCESS and graded[0]
        exit_name = state.exit_reason.value if state.exit_reason else None
        notes = (
            f"exit={exit_name} phase={state.phase} iterations={state.progress.iterations} "
            f"tool_calls={state.progress.tool_calls} hidden={graded[1]}"
        )
        return TrialOutcome(passed=passed, notes=notes)

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
