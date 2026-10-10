"""Run an eval suite: every task n times, final state graded, metrics reported.

Usage::

    python -m evals.runner --suite regression --provider fake --trials 5

The solver is pluggable (``--solver module:Class``). M1 ships :class:`PlaceholderSolver`,
which exercises the harness end to end (budget, retry, structured output) without a
sandbox. M3 replaces it with the real graph; the runner and metrics do not change.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, Field

from evals.report import SuiteReport, TrialResult, summarize
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.core import Harness
from scio.harness.types import Message
from scio.prompt.registry import PromptRegistry
from scio.settings import REPO_ROOT, Settings

TASKS_DIR = REPO_ROOT / "evals" / "tasks"
RESULTS_DIR = REPO_ROOT / "evals" / "results"


class TaskBudget(BaseModel):
    tokens: int = 100_000
    usd: float = 1.0


class EvalTask(BaseModel):
    id: str
    suite: Literal["regression", "capability"]
    title: str
    spec_file: str = "spec.md"
    tier_hint: str | None = None
    trials: int = 5
    budget: TaskBudget = Field(default_factory=TaskBudget)
    grader: Literal["placeholder", "tests"] = "placeholder"
    path: Path

    @property
    def spec(self) -> str:
        return (self.path / self.spec_file).read_text(encoding="utf-8")


def load_tasks(root: Path = TASKS_DIR, suite: str | None = None) -> list[EvalTask]:
    tasks: list[EvalTask] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        f = d / "task.yaml"
        if not f.exists():
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        task = EvalTask.model_validate({**data, "path": d})
        if suite is None or task.suite == suite:
            tasks.append(task)
    return tasks


class TrialOutcome(BaseModel):
    passed: bool
    advisor_calls: int = 0
    advisor_resolved: int = 0
    notes: str = ""


class Solver(Protocol):
    def solve(self, task: EvalTask, harness: Harness) -> TrialOutcome: ...


class PlaceholderAnswer(BaseModel):
    step_id: str
    summary: str
    confidence: Literal["high", "medium", "low"]


class PlaceholderSolver:
    """One structured call through the harness. Passes if the output validates."""

    def __init__(self) -> None:
        self._prompts = PromptRegistry()

    def solve(self, task: EvalTask, harness: Harness) -> TrialOutcome:
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


def load_solver(spec: str) -> Solver:
    module_name, _, cls_name = spec.partition(":")
    cls = getattr(importlib.import_module(module_name), cls_name)
    solver: Solver = cls()
    return solver


def run_trial(task: EvalTask, trial: int, solver: Solver, settings: Settings) -> TrialResult:
    budget = BudgetGuard(BudgetLimits(tokens_max=task.budget.tokens, usd_max=task.budget.usd))
    harness = Harness.from_settings(settings, budget=budget)
    t0 = time.perf_counter()
    try:
        outcome = solver.solve(task, harness)
        error = None
    except Exception as exc:
        outcome = TrialOutcome(passed=False)
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
    snap = harness.budget.snapshot()
    return TrialResult(
        task_id=task.id,
        trial=trial,
        passed=outcome.passed,
        tokens=snap.tokens_used,
        usd=snap.usd_used,
        latency_s=time.perf_counter() - t0,
        advisor_calls=outcome.advisor_calls,
        advisor_resolved=outcome.advisor_resolved,
        error=error,
    )


def run_suite(
    suite: str,
    *,
    provider: str | None,
    trials: int | None,
    solver: Solver,
    tasks_dir: Path = TASKS_DIR,
) -> SuiteReport:
    settings = Settings(provider_override=provider) if provider else Settings()
    started = datetime.now(UTC)
    tasks = load_tasks(tasks_dir, suite)
    results: list[TrialResult] = []
    metrics = []
    k = trials or (max(t.trials for t in tasks) if tasks else 0)
    for task in tasks:
        n = trials or task.trials
        task_results = [run_trial(task, i + 1, solver, settings) for i in range(n)]
        results.extend(task_results)
        metrics.append(summarize(task.id, task_results))
    return SuiteReport(
        suite=suite,
        provider=provider or "config",
        started_at=started,
        finished_at=datetime.now(UTC),
        k=k,
        tasks=metrics,
        results=results,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a SCIO eval suite.")
    ap.add_argument("--suite", choices=["regression", "capability"], default="regression")
    ap.add_argument("--provider", default=None, help="provider override (e.g. fake)")
    ap.add_argument("--trials", type=int, default=None, help="override per-task trial count")
    ap.add_argument("--solver", default="evals.runner:PlaceholderSolver")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR)
    ap.add_argument("--min-pass-pow-k", type=float, default=None, help="exit 1 below this")
    args = ap.parse_args(argv)

    report = run_suite(
        args.suite, provider=args.provider, trials=args.trials, solver=load_solver(args.solver)
    )
    json_path, md_path = report.write(args.out)
    sys.stdout.write(report.to_markdown())
    sys.stdout.write(f"\nwrote {json_path}\nwrote {md_path}\n")
    for r in report.results:
        if r.error:
            sys.stdout.write(f"\n[{r.task_id} trial {r.trial}] {r.error}\n")
    if args.min_pass_pow_k is not None and report.pass_pow_k < args.min_pass_pow_k:
        sys.stdout.write(f"\nFAIL: pass^k {report.pass_pow_k:.2f} < {args.min_pass_pow_k:.2f}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
