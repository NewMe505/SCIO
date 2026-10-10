"""Escalation threshold tuning: sweep thresholds over the scenario tasks and report.

Usage::

    python -m evals.tune --provider fake --trials 2 --out evals/results

For every combination of ``repeat_threshold`` and ``max_stall`` the sweep runs each
scenario task and records pass rate, mean tokens, mean $, advisor calls and resolution
rate. The chosen setting is the cheapest one (mean $ over the scenarios) that keeps the
pass rate of the expected-to-pass scenarios at 1.0. The report is Markdown + JSON.
Results on the Fake provider measure *control flow* cost (how many turns each setting
takes), not model quality; re-run live before trusting the absolute numbers (I-019).
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

import yaml
from pydantic import BaseModel

from evals.runner import TASKS_DIR, load_tasks, run_trial
from evals.solvers import GraphSolver
from scio.settings import REPO_ROOT, Settings

DEFAULT_GRID: dict[str, list[int]] = {"repeat_threshold": [2, 3, 4], "max_stall": [1, 2, 3]}


class Cell(BaseModel):
    repeat_threshold: int
    max_stall: int
    pass_rate: float
    tokens_mean: float
    usd_mean: float
    advisor_calls_mean: float
    advisor_resolution: float


class TuningReport(BaseModel):
    provider: str
    trials: int
    scenarios: list[str]
    cells: list[Cell]
    chosen: Cell | None
    started_at: datetime
    finished_at: datetime

    def to_markdown(self) -> str:
        lines = [
            f"## Escalation threshold sweep on `{self.provider}` (trials={self.trials})",
            "",
            f"Scenarios: {', '.join(self.scenarios)}",
            "",
            "| repeat_threshold | max_stall | pass | tokens | $ | advisor calls | resolved |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for c in self.cells:
            mark = (
                " **←**"
                if self.chosen
                and (c.repeat_threshold, c.max_stall)
                == (self.chosen.repeat_threshold, self.chosen.max_stall)
                else ""
            )
            lines.append(
                f"| {c.repeat_threshold} | {c.max_stall} | {c.pass_rate:.2f} | "
                f"{c.tokens_mean:.0f} | {c.usd_mean:.4f} | "
                f"{c.advisor_calls_mean:.2f} | {c.advisor_resolution:.2f}{mark} |"
            )
        if self.chosen:
            lines += [
                "",
                f"Chosen: repeat_threshold={self.chosen.repeat_threshold}, "
                f"max_stall={self.chosen.max_stall} (cheapest setting with pass rate 1.0).",
            ]
        return "\n".join(lines) + "\n"


def _with_thresholds(repeat: int, stall: int) -> Path:
    """A temporary budgets.yaml with the given escalation thresholds."""
    base = yaml.safe_load((REPO_ROOT / "configs" / "budgets.yaml").read_text(encoding="utf-8"))
    base["escalation"]["repeat_threshold"] = repeat
    base["escalation"]["max_stall"] = stall
    for tier in base["tiers"].values():
        tier["max_stall"] = stall
    path = Path(tempfile.mkdtemp(prefix="scio-tune-")) / "budgets.yaml"
    path.write_text(yaml.safe_dump(base), encoding="utf-8", newline="\n")
    return path


def sweep(
    *,
    provider: str | None,
    trials: int,
    grid: dict[str, list[int]] | None = None,
    tasks_dir: Path = TASKS_DIR,
    scenario_ids: list[str] | None = None,
) -> TuningReport:
    grid = grid or DEFAULT_GRID
    started = datetime.now(UTC)
    tasks = [
        t
        for t in load_tasks(tasks_dir, "capability")
        if t.expect_pass and (scenario_ids is None or t.id in scenario_ids)
    ]
    cells: list[Cell] = []
    for repeat, stall in itertools.product(grid["repeat_threshold"], grid["max_stall"]):
        if stall < repeat - 1:
            continue  # ADR 0006: the stall exit would fire before the repeat signal
        budgets = _with_thresholds(repeat, stall)
        settings = (
            Settings(provider_override=provider, budgets_path=budgets)
            if provider
            else Settings(budgets_path=budgets)
        )
        results = []
        for task in tasks:
            for i in range(trials):
                results.append(run_trial(task, i + 1, GraphSolver(), settings))
        calls = sum(r.advisor_calls for r in results)
        cells.append(
            Cell(
                repeat_threshold=repeat,
                max_stall=stall,
                pass_rate=mean(1.0 if r.passed else 0.0 for r in results) if results else 0.0,
                tokens_mean=mean(r.tokens for r in results) if results else 0.0,
                usd_mean=mean(r.usd for r in results) if results else 0.0,
                advisor_calls_mean=calls / len(results) if results else 0.0,
                advisor_resolution=(sum(r.advisor_resolved for r in results) / calls)
                if calls
                else 0.0,
            )
        )
    passing = [c for c in cells if c.pass_rate >= 1.0]
    chosen = min(passing, key=lambda c: (c.usd_mean, c.advisor_calls_mean)) if passing else None
    return TuningReport(
        provider=provider or "config",
        trials=trials,
        scenarios=[t.id for t in tasks],
        cells=cells,
        chosen=chosen,
        started_at=started,
        finished_at=datetime.now(UTC),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep escalation thresholds over the scenario tasks.")
    ap.add_argument("--provider", default="fake")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "evals" / "results")
    args = ap.parse_args(argv)
    report = sweep(provider=args.provider, trials=args.trials)
    args.out.mkdir(parents=True, exist_ok=True)
    stamp = report.finished_at.strftime("%Y%m%dT%H%M%SZ")
    (args.out / f"tuning-{report.provider}-{stamp}.json").write_text(
        json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8", newline="\n"
    )
    md = args.out / f"tuning-{report.provider}-{stamp}.md"
    md.write_text(report.to_markdown(), encoding="utf-8", newline="\n")
    sys.stdout.write(report.to_markdown())
    sys.stdout.write(f"\nwrote {md}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
