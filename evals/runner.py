"""Run an eval suite: every task n times, final state graded, metrics reported.

Usage::

    python -m evals.runner --suite regression --provider fake --trials 5

Solvers live in :mod:`evals.solvers` and are chosen per task by ``grader``:
``placeholder`` → one structured call; ``tests`` → the T0 graph on the task's fixture
repository, graded by hidden tests. ``--solver module:Class`` overrides for every task.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, Field

from evals.report import SuiteReport, TrialResult, summarize
from evals.solvers import EvalTaskLike, GraphSolver, PlaceholderSolver, TrialOutcome
from scio.harness.budget import BudgetGuard, BudgetLimits
from scio.harness.core import Harness
from scio.settings import REPO_ROOT, Settings

TASKS_DIR = REPO_ROOT / "evals" / "tasks"
RESULTS_DIR = REPO_ROOT / "evals" / "results"


class TaskBudget(BaseModel):
    tokens: int = 100_000
    usd: float = 1.0


class EvalTask(BaseModel):
    id: str
    suite: Literal["regression", "capability", "product"]
    title: str
    spec_file: str = "spec.md"
    tier_hint: str | None = None
    tier_pin: str | None = None
    """Run this tier without consulting the router. For tasks built to exercise one tier's
    path (t008 and the debugger): the classifier under-tiers them and skips those nodes
    (ledger I-050). Also sizes the budget, so set ``tier_hint`` to the same value."""
    trials: int = 5
    budget: TaskBudget = Field(default_factory=TaskBudget)
    grader: Literal["placeholder", "tests", "product"] = "placeholder"
    """``product`` (M11): a whole product built from the task's ``PRODUCT.md`` to a release,
    graded by the hidden black-box tests in ``hidden_tests`` (a directory)."""
    hidden_tests: str | None = None
    fixture: str | None = None
    """Another task's id whose ``repo/`` and ``hidden/`` this task reuses (scenario tasks)."""
    scenario: dict[str, Any] = Field(default_factory=dict)
    """Fake-provider switches (``coder_mode``, ``advisor_verdict``, ``advisor_calls_max``)."""
    expect_pass: bool = True
    """False for scenarios whose correct outcome is a halt (e.g. the advisor cap)."""
    providers: list[str] = Field(default_factory=list)
    """Providers this task is meaningful on (empty = all). Scenario tasks inject failure modes
    into the Fake provider and are listed as ``[fake]`` (ledger I-042)."""
    path: Path

    @property
    def fixture_path(self) -> Path:
        return self.path.parent / self.fixture if self.fixture else self.path

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


class Solver(Protocol):
    def solve(self, task: EvalTaskLike, harness: Harness) -> TrialOutcome: ...


def to_task_like(task: EvalTask) -> EvalTaskLike:
    return EvalTaskLike(
        id=task.id,
        spec=task.spec,
        path=task.fixture_path,
        hidden_tests=task.hidden_tests,
        tokens=task.budget.tokens,
        usd=task.budget.usd,
        tier=task.tier_pin or task.tier_hint or "T0",
        tier_pin=task.tier_pin,
        scenario=task.scenario,
        expect_pass=task.expect_pass,
    )


def select_solver(task: EvalTask, override: Solver | None) -> Solver:
    if override is not None:
        return override
    if task.grader == "product":
        from evals.product_solver import ProductSolver

        return ProductSolver()
    return GraphSolver() if task.grader == "tests" else PlaceholderSolver()


def load_solver(spec: str | None) -> Solver | None:
    if not spec or spec == "auto":
        return None
    module_name, _, cls_name = spec.partition(":")
    cls = getattr(importlib.import_module(module_name), cls_name)
    solver: Solver = cls()
    return solver


def run_trial(task: EvalTask, trial: int, solver: Solver, settings: Settings) -> TrialResult:
    budget = BudgetGuard(BudgetLimits(tokens_max=task.budget.tokens, usd_max=task.budget.usd))
    harness = Harness.from_settings(settings, budget=budget)
    t0 = time.perf_counter()
    try:
        outcome = solver.solve(to_task_like(task), harness)
        error = None
    except Exception as exc:
        outcome = TrialOutcome(passed=False)
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
    snap = harness.budget.snapshot()
    return TrialResult(
        task_id=task.id,
        trial=trial,
        passed=outcome.passed,
        tokens=snap.tokens_used + outcome.tokens_elsewhere,
        usd=snap.usd_used + outcome.usd_elsewhere,
        latency_s=time.perf_counter() - t0,
        advisor_calls=outcome.advisor_calls,
        advisor_resolved=outcome.advisor_resolved,
        error=error,
        notes=outcome.notes,
    )


def run_suite(
    suite: str,
    *,
    provider: str | None,
    trials: int | None,
    solver: Solver | None = None,
    tasks_dir: Path = TASKS_DIR,
    config: Path | None = None,
    parallel: int = 1,
    max_usd: float | None = None,
) -> SuiteReport:
    kwargs: dict[str, Any] = {}
    if provider:
        kwargs["provider_override"] = provider
    if config is not None:
        kwargs["role_overrides_path"] = config
    settings = Settings(**kwargs)
    started = datetime.now(UTC)
    tasks = load_tasks(tasks_dir, suite)
    results: list[TrialResult] = []
    metrics = []
    k = trials or (max(t.trials for t in tasks) if tasks else 0)
    skipped: list[str] = []
    not_applicable: list[str] = []
    for task in tasks:
        if task.providers and (provider or "config") not in task.providers:
            not_applicable.append(task.id)
            continue
        spent = sum(r.usd for r in results)
        if max_usd is not None and spent >= max_usd:
            skipped.append(task.id)  # the cap is checked before each task starts
            continue
        n = trials or task.trials
        chosen = select_solver(task, solver)
        if parallel > 1:
            # Trials are independent (own sandbox, checkpoint, budget); only the provider's
            # rate limit is shared, and the harness retries 429s with backoff.
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                task_results = list(
                    pool.map(lambda i, t=task, c=chosen: run_trial(t, i + 1, c, settings), range(n))
                )
        else:
            task_results = [run_trial(task, i + 1, chosen, settings) for i in range(n)]
        results.extend(task_results)
        metrics.append(summarize(task.id, task_results))
    label = f"{provider or 'config'}" + (f"+{config.stem}" if config else "")
    return SuiteReport(
        suite=suite,
        provider=label,
        started_at=started,
        finished_at=datetime.now(UTC),
        k=k,
        tasks=metrics,
        results=results,
        usd_cap=max_usd,
        skipped_tasks=skipped,
        not_applicable=not_applicable,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a SCIO eval suite.")
    ap.add_argument(
        "--suite", choices=["regression", "capability", "product"], default="regression"
    )
    ap.add_argument("--provider", default=None, help="provider override (e.g. fake)")
    ap.add_argument("--trials", type=int, default=None, help="override per-task trial count")
    ap.add_argument("--solver", default="auto", help="module:Class, or auto (by task grader)")
    ap.add_argument("--out", type=Path, default=RESULTS_DIR)
    ap.add_argument(
        "--config", type=Path, default=None, help="A/B role-override YAML (evals/configs/*.yaml)"
    )
    ap.add_argument("--min-pass-pow-k", type=float, default=None, help="exit 1 below this")
    ap.add_argument("--parallel", type=int, default=1, help="trials to run concurrently per task")
    ap.add_argument(
        "--max-usd", type=float, default=None, help="stop starting tasks once spend reaches this"
    )
    args = ap.parse_args(argv)

    report = run_suite(
        args.suite,
        provider=args.provider,
        trials=args.trials,
        solver=load_solver(args.solver),
        config=args.config,
        parallel=max(1, args.parallel),
        max_usd=args.max_usd,
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
