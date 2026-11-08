from __future__ import annotations

from scio.cli import parse_spec
from scio.graph.nodes.common import acceptance_from_issue_body, features_from_acceptance, slug
from scio.graph.nodes.worker import PRBody, render_pr_body
from scio.state.models import Budget, FeatureItem, FeatureStatus, GitHubRefs, SCIOState, TaskSpec


def test_parse_spec_and_acceptance() -> None:
    text = "# Add thing\n\nsome prose\n\n- [ ] first\n- [x] second\n* [ ] third\n- not a checkbox\n"
    task = parse_spec(text, repo_url="r", issue=7)
    assert task.title == "Add thing" and task.issue_number == 7
    assert task.acceptance == ["first", "second", "third"]
    feats = features_from_acceptance(["- [ ] a", "b", "  "])
    assert [(f.id, f.description) for f in feats] == [("F1", "a"), ("F2", "b")]
    assert acceptance_from_issue_body("x\n- [ ] y\n") == ["y"]
    assert slug("Add `slugify()` to textutils!") == "add-slugify-to-textutils"


def test_render_pr_body_has_template_sections() -> None:
    st = SCIOState(
        thread_id="t",
        task=TaskSpec(title="T", description="d", acceptance=["a"], repo_url="r", issue_number=3),
        budgets=Budget(tokens_max=10, usd_max=1, advisor_calls_max=0, tokens_used=5, usd_used=0.01),
        github=GitHubRefs(owner="o", repo="r"),
        feature_list=[FeatureItem(id="F1", description="a", status=FeatureStatus.PASSING)],
    )
    body = render_pr_body(
        st, PRBody(summary="s", plan_vs_done=["S1 x"], test_evidence="ok", risk_notes="none"), None
    )
    for section in (
        "## Summary",
        "Closes #3",
        "## Plan vs. done",
        "| S1 x | done |",
        "- [x] F1: a",
        "## Test evidence",
        "## Risk notes",
        "Tokens: 5",
    ):
        assert section in body
    failing = render_pr_body(
        st, PRBody(summary="s", plan_vs_done=[], test_evidence="", risk_notes=""), "broken link"
    )
    assert "Docs check still failing" in failing


def test_diff_stat_counts_untracked_files_as_changes(tmp_path) -> None:
    """I-063: a feature delivered only by new files was invisible to `git diff --stat`, so
    verify reported no changes and the run stalled."""
    import subprocess

    from scio.aci.sandbox.local import LocalSandbox
    from scio.graph.nodes.common import diff_stat, untracked_files

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"],
        cwd=repo,
        check=True,
    )
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    sb = LocalSandbox(repo)
    assert diff_stat(sb, base) == "(no changes since base)"
    (repo / "tests").mkdir()
    (repo / "tests" / "test_new.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    assert untracked_files(sb) == ["tests/test_new.py"]
    assert diff_stat(sb, base) == " new file: tests/test_new.py"
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    stat = diff_stat(sb, base)
    assert "a.py" in stat and stat.endswith(" new file: tests/test_new.py")


def test_python_m_scio_is_the_console_entry_point() -> None:
    """``python -m scio`` must keep working: the venv's ``scio.exe`` launcher is blocked by
    Application Control on some machines (I-075)."""
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-m", "scio", "--help"], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, r.stderr
    assert "usage: scio" in r.stdout and "build" in r.stdout


def test_the_state_first_inspection_lists_the_live_run_issues() -> None:
    """A live product run's SCIO bugs reach this repository as `live-run` issues, and every
    session's state-first inspection lists them (the owner's decision, 2026-11-05, #81): the
    graphcalc run's S7 to S9 sat in its log for days, waiting for a paste."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if "**State-First Inspection:**" in ln)
    assert "gh issue list --label live-run --state open" in line


def test_the_local_ci_line_runs_python_tools_as_modules() -> None:
    """CLAUDE.md's CI-equivalent runs every Python tool as ``python -m``: Application Control
    blocks the venv's launchers on the dev machine (``pytest.exe``, ``scio.exe``, and
    ``mypy.exe`` since 2026-10-31; I-075, I-146). Since issue #88 the line names
    ``scripts/gate.py``, and every step of the gate starts this interpreter, never a launcher;
    the checks and thresholds are CI's (16 test workers, 5 eval trials, pass^5 = 1.00)."""
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- Run the CI-equivalent locally"))
    assert "`uv run python scripts/gate.py`" in line, line[:120]
    spec = importlib.util.spec_from_file_location("gate", root / "scripts" / "gate.py")
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    sys.modules["gate"] = gate  # @dataclass looks its module up there
    spec.loader.exec_module(gate)
    names = [name for name, _ in gate.steps()]
    assert names == ["ruff check", "ruff format", "mypy", "pytest", "evals", "docs"], names
    for name, argv in gate.steps():
        assert argv[0] == sys.executable, (name, argv)  # the interpreter, not a launcher
        assert argv[1] == "-m" or argv[1].endswith(".py"), (name, argv)
    argv = dict(gate.steps())
    assert argv["pytest"][-2:] == ["-n", "16"]
    evals = argv["evals"]
    assert evals[evals.index("--trials") + 1] == "5"
    assert evals[evals.index("--min-pass-pow-k") + 1] == "1.0"
