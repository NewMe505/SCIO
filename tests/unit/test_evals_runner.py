from __future__ import annotations

from pathlib import Path

import pytest

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
        "t008_snapshot_coupling",
    ]
    assert all(t.grader == "tests" and t.hidden_tests for t in tasks)
    assert all(t.trials >= 5 for t in tasks)
    assert "Acceptance criteria" in tasks[0].spec


def test_make_origin_leaves_caches_behind(tmp_path: Path) -> None:
    """I-048: caches on disk in a fixture must not become tracked fixture content."""
    import subprocess

    from evals.solvers import make_origin

    fixture = tmp_path / "fixture"
    (fixture / "pkg" / "__pycache__").mkdir(parents=True)
    (fixture / "pkg" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (fixture / "pkg" / "__pycache__" / "__init__.cpython-312.pyc").write_bytes(b"\x00")
    (fixture / ".pytest_cache").mkdir()
    (fixture / ".pytest_cache" / "v").write_text("", encoding="utf-8")
    origin = make_origin(fixture, tmp_path / "origin")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=origin, capture_output=True, text=True, check=True
    ).stdout.split()
    assert tracked == ["pkg/__init__.py"]
    assert not (origin / "pkg" / "__pycache__").exists()


def test_remove_tree_deletes_read_only_git_objects(tmp_path: Path) -> None:
    """I-049: ``rmtree(ignore_errors=True)`` left one directory per trial behind on Windows
    because git object files are read-only; the solver's cleanup must clear them."""
    import os
    import stat

    from evals.solvers import remove_tree

    tree = tmp_path / "trial"
    obj = tree / "origin" / ".git" / "objects" / "ab"
    obj.mkdir(parents=True)
    blob = obj / "cdef"
    blob.write_bytes(b"x")
    os.chmod(blob, stat.S_IREAD)
    remove_tree(tree)
    assert not tree.exists()
    remove_tree(tree)  # idempotent on a missing path


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


def test_cli_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # trial dirs land here
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
    # I-049: every trial directory is removed once the run has released its handles.
    assert sorted(tmp_path.glob("scio-*")) == []
