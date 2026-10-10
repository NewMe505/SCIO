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
