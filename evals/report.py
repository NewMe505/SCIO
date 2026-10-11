"""Metrics and reports for eval runs.

* ``pass@1``: fraction of trials that passed.
* ``pass^k``: fraction of tasks whose *every* trial passed (tau-bench definition). It
  exposes unreliability that pass@1 hides.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import mean

from pydantic import BaseModel, Field


class TrialResult(BaseModel):
    task_id: str
    trial: int
    passed: bool
    tokens: int
    usd: float
    latency_s: float
    advisor_calls: int = 0
    advisor_resolved: int = 0
    error: str | None = None
    notes: str = ""


class TaskMetrics(BaseModel):
    task_id: str
    trials: int
    pass_at_1: float
    all_passed: bool
    tokens_mean: float
    usd_mean: float
    latency_s_mean: float
    advisor_calls: int
    advisor_resolved: int


class SuiteReport(BaseModel):
    suite: str
    provider: str
    started_at: datetime
    finished_at: datetime
    k: int
    tasks: list[TaskMetrics]
    results: list[TrialResult] = Field(default_factory=list)
    usd_cap: float | None = None
    """Suite-level spend ceiling; tasks not started because of it are in ``skipped_tasks``."""
    skipped_tasks: list[str] = Field(default_factory=list)
    not_applicable: list[str] = Field(default_factory=list)
    """Tasks whose ``providers`` list excludes this provider (not run, not counted)."""

    @property
    def usd_total(self) -> float:
        return sum(r.usd for r in self.results)

    @property
    def capped(self) -> bool:
        return bool(self.skipped_tasks)

    @property
    def pass_at_1(self) -> float:
        return mean(t.pass_at_1 for t in self.tasks) if self.tasks else 0.0

    @property
    def pass_pow_k(self) -> float:
        return mean(1.0 if t.all_passed else 0.0 for t in self.tasks) if self.tasks else 0.0

    @property
    def advisor_call_rate(self) -> float:
        n = sum(t.trials for t in self.tasks)
        return sum(t.advisor_calls for t in self.tasks) / n if n else 0.0

    @property
    def advisor_resolution_rate(self) -> float:
        calls = sum(t.advisor_calls for t in self.tasks)
        return sum(t.advisor_resolved for t in self.tasks) / calls if calls else 0.0

    def to_markdown(self) -> str:
        lines = [
            f"## Eval suite `{self.suite}` on `{self.provider}` (k={self.k})",
            "",
            f"pass@1 **{self.pass_at_1:.2f}** · pass^{self.k} **{self.pass_pow_k:.2f}** · "
            f"advisor call rate {self.advisor_call_rate:.2f} · "
            f"advisor resolution {self.advisor_resolution_rate:.2f}",
            f"spend ${self.usd_total:.4f}"
            + (f" of cap ${self.usd_cap:.2f}" if self.usd_cap is not None else "")
            + (f" · CAPPED, skipped: {', '.join(self.skipped_tasks)}" if self.capped else "")
            + (
                f" · not applicable here: {', '.join(self.not_applicable)}"
                if self.not_applicable
                else ""
            ),
            "",
            "| task | pass@1 | all pass | tokens | $ | latency s | advisor |",
            "|---|---:|:---:|---:|---:|---:|---:|",
        ]
        for t in self.tasks:
            lines.append(
                f"| {t.task_id} | {t.pass_at_1:.2f} | {'yes' if t.all_passed else 'no'} | "
                f"{t.tokens_mean:.0f} | {t.usd_mean:.4f} | {t.latency_s_mean:.2f} | "
                f"{t.advisor_resolved}/{t.advisor_calls} |"
            )
        return "\n".join(lines) + "\n"

    def write(self, out_dir: Path) -> tuple[Path, Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.finished_at.strftime("%Y%m%dT%H%M%SZ")
        json_path = out_dir / f"{self.suite}-{self.provider}-{stamp}.json"
        md_path = out_dir / f"{self.suite}-{self.provider}-{stamp}.md"
        json_path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2), encoding="utf-8", newline="\n"
        )
        md_path.write_text(self.to_markdown(), encoding="utf-8", newline="\n")
        return json_path, md_path


def summarize(task_id: str, results: list[TrialResult]) -> TaskMetrics:
    return TaskMetrics(
        task_id=task_id,
        trials=len(results),
        pass_at_1=mean(1.0 if r.passed else 0.0 for r in results),
        all_passed=all(r.passed for r in results),
        tokens_mean=mean(r.tokens for r in results),
        usd_mean=mean(r.usd for r in results),
        latency_s_mean=mean(r.latency_s for r in results),
        advisor_calls=sum(r.advisor_calls for r in results),
        advisor_resolved=sum(r.advisor_resolved for r in results),
    )
