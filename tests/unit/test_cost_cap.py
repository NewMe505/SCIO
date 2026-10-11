"""The suite cost cap stops starting tasks and the report says so."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

from evals.report import SuiteReport, TaskMetrics, TrialResult
from evals.runner import run_suite
from evals.solvers import PlaceholderSolver
from scio.settings import REPO_ROOT


def test_cap_skips_tasks_after_spend_reaches_it() -> None:
    # Fake-provider trials cost ~$0.0007 each (placeholder) or ~$0.03 (graph); a tiny cap
    # lets the first task run and skips the rest.
    report = run_suite(
        "regression", provider="fake", trials=1, solver=PlaceholderSolver(), max_usd=0.0001
    )
    assert report.capped and report.usd_cap == 0.0001
    assert len(report.tasks) == 1 and len(report.skipped_tasks) == 4
    md = report.to_markdown()
    assert "CAPPED" in md and "of cap $0.00" in md and report.usd_total > 0


def test_no_cap_means_nothing_skipped() -> None:
    report = run_suite("regression", provider="fake", trials=1, solver=PlaceholderSolver())
    assert not report.capped and report.usd_cap is None and "of cap" not in report.to_markdown()


def _load_nightly() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "nightly_live", REPO_ROOT / "scripts" / "nightly_live.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_history_row_format(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    nl = _load_nightly()
    monkeypatch.setattr(nl, "HISTORY", tmp_path / "EVAL_HISTORY.md")
    now = datetime(2026, 10, 11, 3, 17, tzinfo=UTC)
    report = SuiteReport(
        suite="regression",
        provider="anthropic+sonnet_opus_advisor",
        started_at=now,
        finished_at=now,
        k=3,
        tasks=[
            TaskMetrics(
                task_id="t",
                trials=3,
                pass_at_1=1.0,
                all_passed=True,
                tokens_mean=1,
                usd_mean=0.1,
                latency_s_mean=1,
                advisor_calls=0,
                advisor_resolved=0,
            )
        ],
        results=[
            TrialResult(task_id="t", trial=i, passed=True, tokens=1, usd=0.1, latency_s=1)
            for i in range(3)
        ],
        usd_cap=6.0,
        skipped_tasks=["t9"],
    )
    row = nl.append_history(report, "sonnet_opus_advisor")
    text = (tmp_path / "EVAL_HISTORY.md").read_text(encoding="utf-8")
    assert text.startswith("# Eval history") and text.endswith(row)
    assert (
        "| 2026-10-11 03:17 | regression | anthropic+sonnet_opus_advisor | 3 | 1.00 | 1.00 |" in row
    )
    assert "$0.30 of $6 | yes: t9 |" in row
