from __future__ import annotations

from pathlib import Path

from evals.report import TrialResult, summarize
from evals.runner import PlaceholderSolver, load_tasks, main, run_suite


def test_tasks_load() -> None:
    tasks = load_tasks(suite="regression")
    assert [t.id for t in tasks] == [
        "t001_add_function",
        "t002_fix_failing_test",
        "t003_update_docs",
        "t004_two_features",
        "t005_cli_json_flag",
        "t006_multi_file_remove",
        "t007_red_ci",
    ]
    assert all(t.grader == "tests" and t.hidden_tests for t in tasks)
    assert all(t.trials >= 5 for t in tasks)
    assert "Acceptance criteria" in tasks[0].spec


def test_metrics() -> None:
    rs = [
        TrialResult(
            task_id="t",
            trial=1,
            passed=True,
            tokens=10,
            usd=0.1,
            latency_s=1.0,
            advisor_calls=1,
            advisor_resolved=1,
        ),
        TrialResult(task_id="t", trial=2, passed=False, tokens=30, usd=0.3, latency_s=3.0),
    ]
    m = summarize("t", rs)
    assert m.pass_at_1 == 0.5 and not m.all_passed and m.tokens_mean == 20 and m.advisor_calls == 1


def test_regression_suite_on_fake_is_fully_passing(tmp_path: Path) -> None:
    report = run_suite("regression", provider="fake", trials=2, solver=PlaceholderSolver())
    assert report.pass_at_1 == 1.0 and report.pass_pow_k == 1.0
    assert all(r.tokens > 0 and r.usd > 0 for r in report.results)
    json_path, md_path = report.write(tmp_path)
    assert json_path.exists() and "pass^2" in md_path.read_text(encoding="utf-8")


def test_cli_exit_codes(tmp_path: Path) -> None:
    assert (
        main(
            [
                "--provider",
                "fake",
                "--trials",
                "1",
                "--out",
                str(tmp_path),
                "--min-pass-pow-k",
                "1.0",
            ]
        )
        == 0
    )
