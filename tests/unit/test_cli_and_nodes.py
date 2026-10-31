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


def test_the_local_ci_line_runs_python_tools_as_modules() -> None:
    """CLAUDE.md's CI-equivalent runs every Python tool as ``python -m``: Application Control
    blocks the venv's launchers on the dev machine (``pytest.exe``, ``scio.exe``, and
    ``mypy.exe`` since 2026-10-31; I-075, I-146). ``ruff.exe`` is ruff's own binary."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("- Run the CI-equivalent locally"))
    chain = next(c for c in re.findall(r"`([^`]+)`", line) if "&&" in c)
    tools = [part.split()[2] for part in chain.split("&&")]
    assert len(tools) == 6 and set(tools) <= {"python", "ruff"}, tools
