"""Solvers: how a trial is executed and graded.

* :class:`PlaceholderSolver` (M1): one structured call; passes if it validates.
* :class:`GraphSolver` (M3/M4): runs the SCIO graph (the router picks the tier) on a fresh
  clone of the task's fixture repository, resumes through the merge interrupt, then
  grades the final state with the task's hidden tests. On the Fake provider the model
  is :class:`evals.fake_scripts.ScriptedResponder`.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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

log = logging.getLogger(__name__)


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
    tier_pin: str | None = None
    scenario: dict[str, Any] = Field(default_factory=dict)
    expect_pass: bool = True


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


CACHE_PATTERNS = ("__pycache__", "*.pyc", ".pytest_cache", ".ruff_cache", ".mypy_cache")


def _make_writable(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    """``rmtree`` error handler: clear the read-only bit and retry (git objects on Windows)."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Delete a trial's working tree, read-only git objects included.

    ``shutil.rmtree(..., ignore_errors=True)`` silently left every ``origin/.git/objects``
    tree behind on Windows, one directory per trial (ledger I-049).
    """
    if path.exists():
        shutil.rmtree(path, onexc=_make_writable)


def make_origin(fixture: Path, dest: Path) -> Path:
    """Copy the fixture repository and turn it into a git repo with one commit on main."""
    # Caches left by running the fixture's tests in place would be committed as fixture
    # content; tracked .pyc files then change on every test run and end up in the agent's
    # commits (ledger I-048). The repo-local exclude only covers untracked files.
    shutil.copytree(fixture, dest, ignore=shutil.ignore_patterns(*CACHE_PATTERNS))
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
                try:
                    remove_tree(tmp)
                except OSError as exc:  # a leak must be visible, but never fail the trial
                    log.warning("could not remove trial directory %s: %s", tmp, exc)

    def _solve(self, task: EvalTaskLike, harness: Harness, tmp: Path) -> TrialOutcome:
        origin = make_origin(task.path / "repo", tmp / "origin")
        settings = harness.settings.model_copy(
            update={"checkpoint_dsn": f"sqlite:///{(tmp / 'ckpt.sqlite').as_posix()}"}
        )
        if settings.provider_override == "fake":
            from evals.fake_scripts import ScriptedResponder

            switches = {k: v for k, v in task.scenario.items() if k != "advisor_calls_max"}
            factory = self.responder_factory or (lambda: ScriptedResponder(**switches))
            harness.providers.register("fake", FakeProvider(responder=factory()))
        tier_defaults = BudgetsConfig.load(settings.budgets_path).tiers.get(task.tier)
        default_cap = tier_defaults.advisor_calls_max if tier_defaults else 0
        limits = BudgetLimits(
            tokens_max=task.tokens,
            usd_max=task.usd,
            advisor_calls_max=int(task.scenario.get("advisor_calls_max", default_cap)),
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
            tier_pin=task.tier_pin,
        )
        try:
            outcome = run_graph(ctx, task_spec(task, origin))
            if outcome.paused and outcome.state.phase == "merge_wait":
                outcome = resume_graph(ctx, "approved (eval)")
            state = outcome.state
            graded = self._grade(ctx, task)
        finally:
            ctx.close()  # an open effect-log handle blocks the cleanup on Windows (I-049)
        succeeded = state.exit_reason is ExitReason.SUCCESS and graded[0]
        passed = succeeded if task.expect_pass else (not succeeded and outcome.paused)
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
