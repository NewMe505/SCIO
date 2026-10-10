from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from scio.harness.github.client import GitHubClient, GitHubError, RunResult


class FakeGh:
    """Canned `gh` responses keyed by a prefix of argv; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.branches: set[str] = set()
        self.comments: dict[int, dict[int, str]] = {}  # issue -> {id: body}
        self.prs: dict[str, dict[str, object]] = {}  # head -> pr
        self.next_id = 100

    def __call__(self, argv: Sequence[str], *, stdin: str | None = None) -> RunResult:
        a = list(argv)
        self.calls.append((a, stdin))
        if a[:2] == ["repo", "view"]:
            return RunResult(0, json.dumps({"defaultBranchRef": {"name": "main"}}), "")
        if a[:2] == ["issue", "view"]:
            return RunResult(
                0,
                json.dumps(
                    {
                        "number": int(a[2]),
                        "title": "T",
                        "body": "B",
                        "labels": [{"name": "feature"}],
                        "url": "u",
                    }
                ),
                "",
            )
        if a[0] == "api":
            path = next(x for x in a[1:] if x.startswith("repos/"))
            if "/git/ref/heads/" in path:
                name = path.split("/git/ref/heads/", 1)[1]  # branch names may contain '/'
                return (
                    RunResult(0, "{}", "")
                    if name in self.branches
                    else RunResult(1, "", "Not Found")
                )
            if path.endswith("/git/refs"):
                ref = next(x for x in a if x.startswith("ref=")).removeprefix("ref=refs/heads/")
                self.branches.add(ref)
                return RunResult(0, "{}", "")
            if "/issues/" in path and path.endswith("/comments"):
                issue = int(path.split("/issues/")[1].split("/")[0])
                if "-X" in a and a[a.index("-X") + 1] == "POST":
                    cid = self.next_id
                    self.next_id += 1
                    self.comments.setdefault(issue, {})[cid] = json.loads(stdin or "{}")["body"]
                    return RunResult(0, json.dumps({"id": cid}), "")
                rows = [{"id": i, "body": b} for i, b in self.comments.get(issue, {}).items()]
                return RunResult(0, json.dumps(rows), "")
            if "/issues/comments/" in path:
                cid = int(path.rsplit("/", 1)[-1])
                for bodies in self.comments.values():
                    if cid in bodies:
                        bodies[cid] = json.loads(stdin or "{}")["body"]
                return RunResult(0, "{}", "")
        if a[:2] == ["pr", "list"]:
            head = a[a.index("--head") + 1]
            pr = self.prs.get(head)
            return RunResult(0, json.dumps([pr] if pr else []), "")
        if a[:2] == ["pr", "create"]:
            head = a[a.index("--head") + 1]
            n = self.next_id
            self.next_id += 1
            self.prs[head] = {
                "number": n,
                "url": f"https://github.com/o/r/pull/{n}",
                "isDraft": True,
                "headRefName": head,
                "baseRefName": a[a.index("--base") + 1],
                "body": stdin,
            }
            return RunResult(
                0, f"Creating draft pull request\nhttps://github.com/o/r/pull/{n}\n", ""
            )
        if a[:2] == ["pr", "view"]:
            n = int(a[2])
            pr = next(p for p in self.prs.values() if p["number"] == n)
            return RunResult(0, json.dumps({"isDraft": pr["isDraft"]}), "")
        if a[:2] == ["pr", "ready"]:
            n = int(a[2])
            next(p for p in self.prs.values() if p["number"] == n)["isDraft"] = False
            return RunResult(0, "", "")
        if a[:2] == ["pr", "edit"]:
            return RunResult(0, "", "")
        return RunResult(1, "", f"unhandled: {a}")


@pytest.fixture
def gh() -> tuple[GitHubClient, FakeGh]:
    fake = FakeGh()
    return GitHubClient("o/r", runner=fake), fake


def test_repo_name_validation() -> None:
    with pytest.raises(ValueError):
        GitHubClient("not a repo")


def test_get_issue_and_default_branch_cached(gh: tuple[GitHubClient, FakeGh]) -> None:
    client, fake = gh
    issue = client.get_issue(7)
    assert (issue.number, issue.title, issue.labels) == (7, "T", ["feature"])
    assert client.default_branch() == "main" and client.default_branch() == "main"
    assert sum(1 for a, _ in fake.calls if a[:2] == ["repo", "view"]) == 1


def test_ensure_branch_is_idempotent_and_guards_default(gh: tuple[GitHubClient, FakeGh]) -> None:
    client, fake = gh
    assert client.ensure_branch("agent/3-x", "abc123") is True
    assert client.ensure_branch("agent/3-x", "abc123") is False
    assert "agent/3-x" in fake.branches
    assert [(w.op, w.noop) for w in client.writes] == [
        ("ensure_branch", False),
        ("ensure_branch", True),
    ]
    with pytest.raises(GitHubError, match="default branch"):
        client.ensure_branch("main", "abc123")


def test_upsert_comment_creates_then_updates(gh: tuple[GitHubClient, FakeGh]) -> None:
    client, fake = gh
    cid = client.upsert_issue_comment(3, "plan v1", marker="scio:plan")
    cid2 = client.upsert_issue_comment(3, "plan v2", marker="scio:plan")
    assert cid == cid2 and len(fake.comments[3]) == 1
    assert (
        fake.comments[3][cid].startswith("plan v2")
        and "<!-- scio:plan -->" in fake.comments[3][cid]
    )
    assert [w.detail.split()[0] for w in client.writes] == ["created", "updated"]


def test_ensure_draft_pr_then_noop_then_ready(gh: tuple[GitHubClient, FakeGh]) -> None:
    client, fake = gh
    pr = client.ensure_draft_pr(head="agent/3-x", base="main", title="t", body="body\nline2")
    again = client.ensure_draft_pr(head="agent/3-x", base="main", title="t", body="body")
    assert pr.number == again.number and pr.is_draft
    assert sum(1 for a, _ in fake.calls if a[:2] == ["pr", "create"]) == 1
    assert fake.prs["agent/3-x"]["body"] == "body\nline2"  # passed via stdin, newlines intact
    assert client.mark_ready(pr.number) is True
    assert client.mark_ready(pr.number) is False
    client.update_pr_body(pr.number, "new body")
    ops = [(w.op, w.noop) for w in client.writes]
    assert ops == [
        ("ensure_draft_pr", False),
        ("ensure_draft_pr", True),
        ("mark_ready", False),
        ("mark_ready", True),
        ("update_pr_body", False),
    ]
    with pytest.raises(GitHubError, match="default branch"):
        client.ensure_draft_pr(head="main", base="main", title="t", body="b")


def test_gh_failure_raises_github_error() -> None:
    client = GitHubClient("o/r", runner=lambda argv, stdin=None: RunResult(1, "", "boom"))
    with pytest.raises(GitHubError, match="boom"):
        client.default_branch()
