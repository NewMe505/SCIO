from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from scio.harness.github.client import GitHubClient, GitHubError, RunResult


def _rest_listing(argv: Sequence[str], path: str, rows: list[dict[str, object]]) -> RunResult:
    """A REST list endpoint as GitHub serves it: ``state`` filters (``open`` by default), and
    without ``--paginate`` only the first page (``per_page``, 30 by default) comes back. With
    it, gh joins the pages into one JSON array (checked live on gh 2.103.3, I-150)."""
    q = parse_qs(urlsplit(path).query)
    state = q.get("state", ["open"])[0]
    if state != "all":
        rows = [r for r in rows if r.get("state") == state]
    if "--paginate" not in argv:
        rows = rows[: int(q.get("per_page", ["30"])[0])]
    return RunResult(0, json.dumps(rows), "")


class FakeGh:
    """Canned `gh` responses keyed by a prefix of argv; records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.branches: set[str] = set()
        self.comments: dict[int, dict[int, str]] = {}  # issue -> {id: body}
        self.prs: dict[str, dict[str, object]] = {}  # head -> pr
        self.milestones: dict[str, int] = {}  # title -> number
        self.closed_milestones: set[int] = set()
        self.issues: dict[int, dict[str, object]] = {}  # number -> issue (``state`` optional)
        self.search_lag = False
        """Leave issues created from now on out of ``issue list --search``: GitHub's search
        index lags a create by seconds, the REST listing does not (I-150)."""
        self.unindexed: set[int] = set()
        self.quiet_issue_create = False
        """Make ``issue create`` print no URL, so ``ensure_issue`` must find what it made."""
        self.merged: set[int] = set()
        self.releases: dict[str, dict[str, object]] = {}  # tag -> release
        self.labels: set[str] = set()  # a new repository has none of SCIO's (I-113)
        self.fail_next_release = False
        """Make the next ``release create`` fail once (a crash between tag and Release)."""
        self.origin: Path | None = None
        """A bare repository standing in for GitHub's: a PR's head is its branch tip there."""
        self.ci: dict[str, str | None] = {}
        """head SHA -> the bucket of its one ``ci/checks`` run; None: no checks reported."""
        self.ci_default: str | None = "pass"
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
        if a[:2] == ["issue", "list"]:
            # as gh does: issues only, newest first, at most --limit rows (30 by default). A
            # --search phrase ends at the next '"', so a title with a '"' in it finds nothing:
            # live, all 7 graphcalc titles with one got 0 hits (S7, I-150)
            query = a[a.index("--search") + 1] if "--search" in a else ""
            limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 30
            rows = sorted(self.issues.values(), key=lambda i: -int(str(i["number"])))
            if query:
                m = re.fullmatch(r'in:title "(.*)"', query)
                if m is None:
                    return RunResult(1, "", f"FakeGh: unhandled search {query!r}")
                phrase = m.group(1).lower()
                rows = [
                    i
                    for i in rows
                    if '"' not in phrase
                    and int(str(i["number"])) not in self.unindexed
                    and phrase in str(i["title"]).lower()
                ]
            return RunResult(0, json.dumps(rows[:limit]), "")
        if a[:2] == ["issue", "create"]:
            # as GitHub does: a label the repository lacks fails the whole create (I-113)
            missing = [a[i + 1] for i, x in enumerate(a) if x == "--label"]
            missing = [lb for lb in missing if lb not in self.labels]
            if missing:
                return RunResult(1, "", f"could not add label: '{missing[0]}' not found")
            if "--milestone" in a and a[a.index("--milestone") + 1] not in self.milestones:
                ms = a[a.index("--milestone") + 1]
                return RunResult(1, "", f"could not add to milestone '{ms}': not found")
            n = self.next_id
            self.next_id += 1
            self.issues[n] = {
                "number": n,
                "title": a[a.index("--title") + 1],
                "body": stdin,
                "labels": [{"name": a[i + 1]} for i, x in enumerate(a) if x == "--label"],
                "url": f"https://github.com/o/r/issues/{n}",
                "milestone": a[a.index("--milestone") + 1] if "--milestone" in a else None,
            }
            if self.search_lag:
                self.unindexed.add(n)
            if self.quiet_issue_create:
                return RunResult(0, "", "")
            return RunResult(0, f"https://github.com/o/r/issues/{n}\n", "")
        if a[:2] == ["release", "view"]:
            rel = self.releases.get(a[2])
            return (
                RunResult(0, json.dumps({"url": rel["url"]}), "")
                if rel
                else RunResult(1, "", "release not found")
            )
        if a[:2] == ["release", "create"]:
            if self.fail_next_release:
                self.fail_next_release = False
                return RunResult(1, "", "HTTP 502: Bad Gateway")
            tag = a[2]
            assets = a[a.index("--verify-tag") + 1 :]
            url = f"https://github.com/o/r/releases/tag/{tag}"
            self.releases[tag] = {"url": url, "notes": stdin, "assets": assets}
            return RunResult(0, url + "\n", "")
        if a[0] == "api":
            path = next(x for x in a[1:] if x.startswith("repos/"))
            if path.endswith("/labels") and "-X" in a and a[a.index("-X") + 1] == "POST":
                name = json.loads(stdin or "{}")["name"]
                self.labels.add(name)
                return RunResult(0, json.dumps({"name": name}), "")
            if "/labels/" in path:
                name = unquote(path.rsplit("/labels/", 1)[1])
                return (
                    RunResult(0, json.dumps({"name": name}), "")
                    if name in self.labels
                    else RunResult(1, "", "HTTP 404: Not Found")
                )
            if "/milestones" in path:
                if "-X" in a and a[a.index("-X") + 1] == "POST":
                    title = json.loads(stdin or "{}")["title"]
                    n = self.next_id
                    self.next_id += 1
                    self.milestones[title] = n
                    return RunResult(0, json.dumps({"number": n, "title": title}), "")
                rows = [
                    {
                        "number": n,
                        "title": t,
                        "state": "closed" if n in self.closed_milestones else "open",
                    }
                    for t, n in self.milestones.items()
                ]
                return _rest_listing(a, path, rows)
            if urlsplit(path).path.endswith("/issues") and "-X" not in a:
                return _rest_listing(a, path, self.rest_issues())
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
                "title": a[a.index("--title") + 1],
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
            merged = n in self.merged
            return RunResult(
                0,
                json.dumps(
                    {
                        "isDraft": pr["isDraft"],
                        "state": "MERGED" if merged else "OPEN",
                        "mergedAt": "2026-10-12T00:00:00Z" if merged else None,
                        "headRefOid": self.head_of(str(pr["headRefName"])),
                    }
                ),
                "",
            )
        if a[:2] == ["pr", "checks"]:
            # as gh 2.103 does: with --json it exits 0 whatever the checks say, and a head
            # with no checks is an error (I-141)
            n = int(a[2])
            pr = next(p for p in self.prs.values() if p["number"] == n)
            head = str(pr["headRefName"])
            bucket = self.ci.get(self.head_of(head), self.ci_default)
            if bucket is None:
                return RunResult(1, "", f"no checks reported on the '{head}' branch")
            run = {"pass": "SUCCESS", "fail": "FAILURE", "pending": "IN_PROGRESS"}[bucket]
            row = {
                "name": "checks",
                "bucket": bucket,
                "state": run,
                "workflow": "ci",
                "link": f"https://github.com/o/r/actions/runs/{n}/job/{n}",
            }
            return RunResult(0, json.dumps([row]), "")
        if a[:2] == ["pr", "ready"]:
            n = int(a[2])
            next(p for p in self.prs.values() if p["number"] == n)["isDraft"] = False
            return RunResult(0, "", "")
        if a[:2] == ["pr", "edit"]:
            return RunResult(0, "", "")
        return RunResult(1, "", f"unhandled: {a}")

    def rest_issues(self) -> list[dict[str, object]]:
        """``GET repos/o/r/issues`` rows, newest first: PRs are in it too, each with a
        ``pull_request`` key; ``url`` is the API URL and ``html_url`` the web one."""
        api = "https://api.github.com/repos/o/r"
        rows: list[dict[str, object]] = [
            {
                "number": n,
                "title": i["title"],
                "body": i["body"],
                "labels": i["labels"],
                "state": i.get("state", "open"),
                "milestone": {"title": i["milestone"]} if i.get("milestone") else None,
                "url": f"{api}/issues/{n}",
                "html_url": f"https://github.com/o/r/issues/{n}",
            }
            for n, i in self.issues.items()
        ]
        rows += [
            {
                "number": p["number"],
                "title": p.get("title", ""),
                "body": p.get("body"),
                "labels": [],
                "state": "closed" if p["number"] in self.merged else "open",
                "url": f"{api}/issues/{p['number']}",
                "html_url": p["url"],
                "pull_request": {"url": f"{api}/pulls/{p['number']}", "html_url": p["url"]},
            }
            for p in self.prs.values()
        ]
        return sorted(rows, key=lambda r: -int(str(r["number"])))

    def head_of(self, branch: str) -> str:
        """The branch tip on the fake GitHub: the bare origin's ref, else a made-up SHA."""
        if self.origin is None:
            return f"sha-of-{branch}"
        r = subprocess.run(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=self.origin,
            capture_output=True,
            text=True,
        )
        return r.stdout.strip()


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


def test_a_missing_gh_is_a_github_error_not_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The I-180 sweep (issue #105): a ``gh`` that cannot start raised ``FileNotFoundError``
    past the ``GitHubError`` every caller handles (``scio build`` stops on it, I-113)."""

    def no_gh(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "The system cannot find the file specified", argv[0])

    monkeypatch.setattr(subprocess, "run", no_gh)
    with pytest.raises(GitHubError, match="cannot start gh"):
        GitHubClient("o/r").default_branch()


def test_ensure_milestone_and_issue_are_idempotent_by_title(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """ADR 0011: a resumed build must find its own milestone and issues, never create twins."""
    client, fake = gh
    m1 = client.ensure_milestone("M1: Skeleton", "Product Todo CLI")
    assert client.ensure_milestone("M1: Skeleton") == m1 and len(fake.milestones) == 1
    assert client.find_milestone("M2: Later") is None
    issue = client.ensure_issue(
        title="P2: Add and list",
        body="- [ ] c\n",
        milestone="M1: Skeleton",
        labels=("product-mode",),
    )
    again = client.ensure_issue(title="P2: Add and list", body="other", milestone="M1: Skeleton")
    assert again.number == issue.number and len(fake.issues) == 1
    assert fake.issues[issue.number]["milestone"] == "M1: Skeleton"
    assert fake.issues[issue.number]["labels"] == [{"name": "product-mode"}]
    found = client.find_issue("P2: Add and list")
    assert (
        found is not None and found.number == issue.number and found.url.endswith(str(issue.number))
    )
    assert client.find_issue("P2: Add") is None  # a prefix is not the title
    ops = [(w.op, w.noop) for w in client.writes]
    assert ops == [
        ("ensure_milestone", False),
        ("ensure_milestone", True),
        ("ensure_label", False),  # the repository had no product-mode label (I-113)
        ("ensure_issue", False),
        ("ensure_issue", True),
    ]
    assert fake.labels == {"product-mode"}


def test_an_issue_label_is_created_once_and_an_existing_one_is_left_alone(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-113: `gh issue create --label` fails on a label the repository lacks; graphcalc's
    first `scio build --gh` crashed at its first issue. The client creates a missing label
    (once) and never touches one the owner already has."""
    client, fake = gh
    fake.labels.add("bug")
    client.ensure_issue(title="P1: a", body="b", milestone=None, labels=("product-mode", "bug"))
    client.ensure_issue(title="P2: c", body="d", milestone=None, labels=("product-mode",))
    posts = [a for a, _ in fake.calls if a[:3] == ["api", "-X", "POST"] and "labels" in a[3]]
    assert len(posts) == 1 and fake.labels == {"bug", "product-mode"}
    assert [(w.op, w.target, w.noop) for w in client.writes if w.op == "ensure_label"] == [
        ("ensure_label", "product-mode", False),
        ("ensure_label", "bug", True),
        ("ensure_label", "product-mode", True),
    ]
    raw = fake(["issue", "create", "-R", "o/r", "--title", "x", "--label", "nope"])
    assert raw.code == 1 and "could not add label: 'nope' not found" in raw.stderr


GRAPHCALC_P5 = (
    'P5: Non-real results return 200 with an exact form and a rectangular complex {"real","imag"}'
)


def test_a_title_with_double_quotes_is_found_and_never_duplicated(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-150, graphcalc S7: `find_issue` searched `in:title "<title>"`, and the `"` inside
    P5's title ended the phrase, so GitHub found nothing and a resumed build opened a second
    P5 issue (#12 beside #8). The lookup lists the issues instead, so any title is found."""
    client, fake = gh
    issue = client.ensure_issue(
        title=GRAPHCALC_P5, body="- [ ] c\n", milestone=None, labels=("product-mode",)
    )
    found = client.find_issue(GRAPHCALC_P5)
    assert found is not None and found.number == issue.number
    assert found.url == f"https://github.com/o/r/issues/{issue.number}"  # the web URL, not API
    assert (found.title, found.body, found.labels) == (GRAPHCALC_P5, "- [ ] c\n", ["product-mode"])
    again = client.ensure_issue(title=GRAPHCALC_P5, body="other", milestone=None)
    assert again.number == issue.number and len(fake.issues) == 1
    fake.issues[issue.number]["state"] = "closed"  # a merged milestone closed it
    closed = client.find_issue(GRAPHCALC_P5)
    assert closed is not None and closed.number == issue.number
    # the fake refuses the phrase as GitHub does, so the old search could not pass here
    search = f'in:title "{GRAPHCALC_P5}"'
    raw = fake(["issue", "list", "-R", "o/r", "--state", "all", "--search", search])
    assert raw.code == 0 and json.loads(raw.stdout) == []


def test_find_issue_ignores_pull_requests_and_picks_the_oldest_twin(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-150: `GET repos/{repo}/issues` lists PRs too (with a `pull_request` key); a PR whose
    title equals a feature's is not that feature's issue. Two issues with one title (S7 left
    graphcalc #8 and #12) resolve to the lower number, whatever order GitHub lists them in."""
    client, fake = gh
    pr = client.ensure_draft_pr(head="agent/1-p1", base="main", title="P1: Add", body="b")
    assert client.find_issue("P1: Add") is None
    issue = client.ensure_issue(title="P1: Add", body="b", milestone=None)
    assert issue.number != pr.number and len(fake.issues) == 1
    found = client.find_issue("P1: Add")
    assert found is not None and found.number == issue.number
    twin = fake.next_id
    fake.next_id += 1
    fake.issues[twin] = {**fake.issues[issue.number], "number": twin}
    found = client.find_issue("P1: Add")
    assert found is not None and found.number == issue.number < twin


def test_find_issue_reads_every_page_of_the_listing(gh: tuple[GitHubClient, FakeGh]) -> None:
    """I-150: products have no feature cap, and the old search stopped at `--limit 200`. The
    listing is paginated (`--paginate`, 100 a page), so the oldest of 250 issues is found."""
    client, fake = gh
    for n in range(1, 251):
        fake.issues[n] = {
            "number": n,
            "title": f"P{n}: feature {n}",
            "body": "",
            "labels": [],
            "url": f"https://github.com/o/r/issues/{n}",
        }
    found = client.find_issue("P1: feature 1")
    assert found is not None and found.number == 1
    (listing,) = [a for a, _ in fake.calls if a[0] == "api"]
    assert "--paginate" in listing and "repos/o/r/issues?state=all&per_page=100" in listing


def test_ensure_issue_finds_what_it_created_when_gh_prints_no_url(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-150: `ensure_issue`'s fallback looks the new issue up by title at once. GitHub's
    search index lags a create by seconds; the REST listing does not, so the fallback holds."""
    client, fake = gh
    fake.quiet_issue_create = True
    fake.search_lag = True
    issue = client.ensure_issue(title="P2: Add and list", body="b", milestone=None)
    assert issue.number in fake.issues
    assert issue.url == f"https://github.com/o/r/issues/{issue.number}"
    again = client.ensure_issue(title="P2: Add and list", body="b", milestone=None)
    assert again.number == issue.number and len(fake.issues) == 1
    assert [(w.op, w.noop) for w in client.writes] == [
        ("ensure_issue", False),
        ("ensure_issue", True),
    ]


def test_find_milestone_by_prefix_reuses_a_renamed_milestone(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-150, graphcalc S7: a re-plan retitled M2 ("...variables and tab UI"), so the exact
    title lookup missed GitHub milestone #2 and made #3. The id prefix finds the milestone
    whatever its title says now, with its real title; of two, the lower number wins."""
    client, fake = gh
    fake.milestones = {
        "M1: Server skeleton": 1,
        "M2: Calculator errors, history, variables and tab UI": 3,  # listed first
        "M2: Calculator errors, history, variables and UI": 2,
        "M20: Later": 5,  # "M2: " is not its prefix
    }
    fake.closed_milestones = {2}  # state=all: a closed milestone still counts
    assert client.find_milestone_by_prefix("M2: ") == (
        2,
        "M2: Calculator errors, history, variables and UI",
    )
    assert client.find_milestone_by_prefix("M20: ") == (5, "M20: Later")
    assert client.find_milestone_by_prefix("M3: ") is None
    with pytest.raises(ValueError):
        client.find_milestone_by_prefix("")
    assert client.find_milestone("M2: Calculator errors, history, variables and tab UI") == 3
    assert all("--paginate" in a and any("state=all" in x for x in a) for a, _ in fake.calls)


def test_pr_merged_reads_the_merge_state(gh: tuple[GitHubClient, FakeGh]) -> None:
    client, fake = gh
    pr = client.ensure_draft_pr(head="milestone/1", base="main", title="M1", body="b")
    assert client.pr_merged(pr.number) is False
    fake.merged.add(pr.number)
    assert client.pr_merged(pr.number) is True


def test_ensure_release_creates_once_with_its_assets(gh: tuple[GitHubClient, FakeGh]) -> None:
    """M11: the release gate's GitHub step is idempotent by tag."""
    client, fake = gh
    assert client.find_release("v0.1.0") is None
    url = client.ensure_release(
        tag="v0.1.0", title="Todo v0.1.0", notes="### Added\n- x\n", assets=["a.tar.gz"]
    )
    assert url == "https://github.com/o/r/releases/tag/v0.1.0"
    assert fake.releases["v0.1.0"]["assets"] == ["a.tar.gz"]
    assert fake.releases["v0.1.0"]["notes"] == "### Added\n- x\n"
    again = client.ensure_release(tag="v0.1.0", title="Todo v0.1.0", notes="n", assets=[])
    assert again == url and client.find_release("v0.1.0") == url
    creates = [a for a, _ in fake.calls if a[:2] == ["release", "create"]]
    assert len(creates) == 1 and "--verify-tag" in creates[0]
    assert [(w.op, w.noop) for w in client.writes] == [("release", False), ("release", True)]


def test_pr_checks_reads_the_head_commits_checks_and_none_is_empty(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-141: the merge stop's only window on the product's CI. ``--json`` exits 0 for a red
    check, so the bucket decides; a head with no checks is ``[]``, not an error."""
    client, fake = gh
    pr = client.ensure_draft_pr(head="milestone/1", base="main", title="M1", body="b")
    assert [(c.name, c.bucket, c.workflow) for c in client.pr_checks(pr.number)] == [
        ("checks", "pass", "ci")
    ]
    fake.ci[fake.head_of("milestone/1")] = "fail"
    (check,) = client.pr_checks(pr.number)
    assert check.bucket == "fail" and check.link.startswith("https://github.com/o/r/actions/")
    fake.ci_default, fake.ci = None, {}
    assert client.pr_checks(pr.number) == []
    assert client.pr_head(pr.number) == "sha-of-milestone/1"
    broken = GitHubClient("o/r", runner=lambda argv, stdin=None: RunResult(4, "", "auth"))
    with pytest.raises(GitHubError, match="auth"):
        broken.pr_checks(1)


def test_wait_for_ci_says_failed_passed_pending_or_unread(
    gh: tuple[GitHubClient, FakeGh],
) -> None:
    """I-141: a failing check ends the wait at once; checks of an older head than the one
    pushed never count for it; the wait is bounded; a ``gh`` failure is a status."""
    from scio.product.pr_ci import wait_for_ci

    client, fake = gh
    pr = client.ensure_draft_pr(head="milestone/1", base="main", title="M1", body="b")
    head = fake.head_of("milestone/1")
    assert wait_for_ci(client, pr.number, head=head).state == "passed"
    fake.ci[head] = "fail"
    red = wait_for_ci(client, pr.number, head=head)
    assert red.state == "failed" and red.line().startswith(f"CI failed on {head[:12]}: ci/checks")
    sleeps: list[float] = []
    clock = iter(range(0, 10_000, 15))
    stale = wait_for_ci(
        client, pr.number, head="new-sha", wait_s=60, poll_s=15,
        sleep=sleeps.append, clock=lambda: float(next(clock)),
    )  # fmt: skip
    assert stale.state == "pending" and "GitHub still shows" in stale.detail
    assert sleeps == [15.0] * 3  # read at 15, 30, 45 s; at 60 s the wait is over
    fake.ci[head] = "pending"
    assert wait_for_ci(client, pr.number, head=head, wait_s=0).state == "pending"
    broken = GitHubClient("o/r", runner=lambda argv, stdin=None: RunResult(1, "", "HTTP 502"))
    unread = wait_for_ci(broken, 1, head=head, wait_s=0)
    assert unread.state == "unread" and "HTTP 502" in unread.detail
