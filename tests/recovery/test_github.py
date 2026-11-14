"""The supervisor's GitHub: labels, the incident issue, its status comment, PRs and CI by
SHA, through a fake ``gh`` that refuses what the real one refuses (guardrail 18)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from recovery.commands import CommandResult
from recovery.github import (
    RECOVERY_LABELS,
    GitHub,
    GitHubError,
    marker_for,
    parse_pages,
    status_marker,
)
from recovery.model import PullRequest

REPO = "o/r"
NOT_FOUND = "gh: Not Found (HTTP 404)"
SHA = "a" * 40
OTHER_SHA = "b" * 40

# the --json fields gh 2.103.3 accepts, as `gh pr view --help` and `gh run list --help` list them
_PR_HELP_FIELDS = (
    "additions assignees author autoMergeRequest baseRefName baseRefOid body changedFiles"
    " closed closedAt closingIssuesReferences comments commits createdAt deletions files"
    " fullDatabaseId headRefName headRefOid headRepository headRepositoryOwner id"
    " isCrossRepository isDraft labels latestReviews maintainerCanModify mergeCommit"
    " mergeStateStatus mergeable mergedAt mergedBy milestone number potentialMergeCommit"
    " projectCards projectItems reactionGroups reviewDecision reviewRequests reviews state"
    " statusCheckRollup title updatedAt url"
)
_RUN_HELP_FIELDS = (
    "attempt conclusion createdAt databaseId displayTitle event headBranch headSha name"
    " number startedAt status updatedAt url workflowDatabaseId workflowName"
)
PR_JSON_FIELDS = frozenset(_PR_HELP_FIELDS.split())
RUN_JSON_FIELDS = frozenset(_RUN_HELP_FIELDS.split())


def _ok(data: object) -> CommandResult:
    return CommandResult(0, json.dumps(data), "")


def _fail(stderr: str) -> CommandResult:
    return CommandResult(1, "", stderr)


def _flag(cmd: Sequence[str], name: str, default: str | None = None) -> str | None:
    return cmd[cmd.index(name) + 1] if name in cmd else default


def _all(cmd: Sequence[str], name: str) -> list[str]:
    return [cmd[i + 1] for i, x in enumerate(cmd) if x == name]


class FakeGh:
    """A ``gh`` with GitHub's state in memory. It records every call, applies gh's defaults
    (an issue listing is ``state=open`` and one page of ``per_page`` 30 without
    ``--paginate``, ``pr list`` is open PRs only, ``run list`` returns 20), and refuses:
    an unknown label on ``issue create`` or ``issue edit``, a ``label create`` of a label
    that exists, a comment edit or a listing on an id that does not exist, an unknown
    ``--json`` field, and any command without ``-R`` for this repository."""

    def __init__(self, repo: str = REPO, *, joined_pages: bool = True) -> None:
        self.repo = repo
        self.joined_pages = joined_pages
        """True: ``--paginate`` joins the pages' arrays into one. False: it prints each
        page's array after the other, ``[...][...]``."""
        self.calls: list[tuple[list[str], str | None]] = []
        self.labels: dict[str, tuple[str, str]] = {}
        self.issues: dict[int, dict[str, Any]] = {}
        self.prs: dict[int, dict[str, Any]] = {}
        self.pr_file_paths: dict[int, list[str]] = {}
        self.comments: dict[int, dict[int, str]] = {}
        self.runs: list[dict[str, Any]] = []
        self.quiet_issue_create = False
        """``issue create`` prints no URL, so the client must find what it made."""
        self.failures: list[tuple[list[str], CommandResult]] = []
        self._next_number = 1
        self._next_comment = 9000

    # seeding

    def _number(self) -> int:
        n = self._next_number
        self._next_number += 1
        return n

    def add_issue(
        self, title: str, body: str, *, state: str = "open", labels: Sequence[str] = ()
    ) -> int:
        n = self._number()
        self.issues[n] = {"title": title, "body": body, "state": state, "labels": list(labels)}
        return n

    def add_pr(
        self,
        head: str,
        *,
        state: str = "OPEN",
        body: str = "",
        draft: bool = True,
        files: Sequence[str] = (),
    ) -> int:
        n = self._number()
        merged = state == "MERGED"
        self.prs[n] = {
            "number": n,
            "title": f"PR {n}",
            "state": state,
            "mergedAt": "2026-11-14T10:00:00Z" if merged else None,
            "mergeCommit": {"oid": f"{n:040x}"} if merged else None,
            "headRefName": head,
            "headRefOid": f"{n + 0xABC:040x}",
            "body": body,
            "isDraft": draft,
        }
        self.pr_file_paths[n] = list(files)
        return n

    def add_comment(self, issue: int, body: str) -> int:
        cid = self._next_comment
        self._next_comment += 1
        self.comments.setdefault(issue, {})[cid] = body
        return cid

    def add_run(
        self, sha: str, status: str = "completed", conclusion: str = "success", name: str = "ci"
    ) -> None:
        self.runs.append(
            {
                "databaseId": 500 + len(self.runs),
                "headSha": sha,
                "status": status,
                "conclusion": conclusion if status == "completed" else "",
                "workflowName": name,
            }
        )

    def fail_on(self, prefix: Sequence[str], result: CommandResult) -> None:
        """Answer every call whose argv starts with ``prefix`` with ``result``."""
        self.failures.append((list(prefix), result))

    def writes(self) -> list[list[str]]:
        """The argv of every call that changes GitHub, in order."""
        return [a for a, _ in self.calls if _is_write(a)]

    # the gh command line

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        a = list(argv)
        self.calls.append((a, stdin))
        for prefix, result in self.failures:
            if a[: len(prefix)] == prefix:
                return result
        if a[:1] != ["gh"] or len(a) < 3:
            return _fail(f"FakeGh: not a gh command: {a}")
        cmd = a[1:]
        if cmd[0] == "api":
            return self._api(cmd, stdin)
        if _flag(cmd, "-R") != self.repo:
            return _fail(f"FakeGh: {cmd[:2]} without -R {self.repo} acts on the cwd's repository")
        handlers = {
            ("label", "create"): self._label_create,
            ("issue", "create"): self._issue_create,
            ("issue", "edit"): self._issue_edit,
            ("pr", "view"): self._pr_view,
            ("pr", "list"): self._pr_list,
            ("run", "list"): self._run_list,
        }
        handler = handlers.get((cmd[0], cmd[1]))
        if handler is None:
            return _fail(f"FakeGh: unhandled {a}")
        return handler(cmd, stdin)

    def _fields(self, cmd: Sequence[str], known: frozenset[str]) -> list[str] | CommandResult:
        fields = (_flag(cmd, "--json") or "").split(",")
        unknown = [f for f in fields if f not in known]
        if unknown:
            return _fail(f'Unknown JSON field: "{unknown[0]}"')
        return fields

    def _label_create(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        name = cmd[2]
        if name in self.labels:
            return _fail(
                f'label with name "{name}" already exists; use `--force` to update its color'
                " and description"
            )
        self.labels[name] = (_flag(cmd, "--color") or "", _flag(cmd, "--description") or "")
        return CommandResult(0, "", "")

    def _issue_create(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        if _flag(cmd, "--body-file") != "-" or stdin is None:
            return _fail("FakeGh: the body must come on stdin through --body-file -")
        labels = _all(cmd, "--label")
        missing = [lb for lb in labels if lb not in self.labels]
        if missing:  # as GitHub does: a missing label fails the whole create (I-113)
            return _fail(f"could not add label: '{missing[0]}' not found")
        n = self.add_issue(_flag(cmd, "--title") or "", stdin, labels=labels)
        if self.quiet_issue_create:
            return CommandResult(0, "", "")
        return CommandResult(0, f"https://github.com/{self.repo}/issues/{n}\n", "")

    def _issue_edit(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        n = int(cmd[2])
        if n not in self.issues:
            return _fail(
                f"GraphQL: Could not resolve to an issue or pull request with the number of {n}."
            )
        labels = _all(cmd, "--add-label")
        missing = [lb for lb in labels if lb not in self.labels]
        if missing:
            return _fail(f"'{missing[0]}' not found")
        for lb in labels:
            if lb not in self.issues[n]["labels"]:
                self.issues[n]["labels"].append(lb)
        return CommandResult(0, f"https://github.com/{self.repo}/issues/{n}\n", "")

    def _pr_view(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        fields = self._fields(cmd, PR_JSON_FIELDS)
        if isinstance(fields, CommandResult):
            return fields
        n = int(cmd[2])
        if n not in self.prs:
            return _fail(
                f"GraphQL: Could not resolve to a PullRequest with the number of {n}."
                " (repository.pullRequest)"
            )
        return _ok({f: self.prs[n].get(f) for f in fields})

    def _pr_list(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        fields = self._fields(cmd, PR_JSON_FIELDS)
        if isinstance(fields, CommandResult):
            return fields
        head = _flag(cmd, "--head")
        state = _flag(cmd, "--state", "open")
        wanted = {
            "open": {"OPEN"},
            "closed": {"CLOSED", "MERGED"},
            "merged": {"MERGED"},
            "all": {"OPEN", "CLOSED", "MERGED"},
        }[state or "open"]
        rows = [
            p
            for p in sorted(self.prs.values(), key=lambda p: -int(p["number"]))
            if p["state"] in wanted and (head is None or p["headRefName"] == head)
        ]
        limit = int(_flag(cmd, "--limit", "30") or "30")
        return _ok([{f: p.get(f) for f in fields} for p in rows[:limit]])

    def _run_list(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        fields = self._fields(cmd, RUN_JSON_FIELDS)
        if isinstance(fields, CommandResult):
            return fields
        sha = _flag(cmd, "--commit")
        rows = [r for r in reversed(self.runs) if sha is None or r["headSha"] == sha]
        limit = int(_flag(cmd, "--limit", "20") or "20")
        return _ok([{f: r.get(f) for f in fields} for r in rows[:limit]])

    def _api(self, cmd: Sequence[str], stdin: str | None) -> CommandResult:
        method = (_flag(cmd, "-X") or "GET").upper()
        path = next((x for x in cmd[1:] if x.startswith("repos/")), None)
        if path is None:
            return _fail(f"FakeGh: unhandled api call {cmd}")
        parts = urlsplit(path)
        query = parse_qs(parts.query)
        if not parts.path.startswith(f"repos/{self.repo}/"):
            return _fail(NOT_FOUND)
        segs = parts.path.removeprefix(f"repos/{self.repo}/").split("/")
        if method != "GET" and (_flag(cmd, "--input") != "-" or stdin is None):
            return _fail("FakeGh: a write's payload must come on stdin through --input -")
        if method == "GET":
            if segs[0] == "labels" and len(segs) == 2:
                name = unquote(segs[1])
                return _ok({"name": name}) if name in self.labels else _fail(NOT_FOUND)
            if segs == ["issues"]:
                rows = self._rest_issues()
                state = query.get("state", ["open"])[0]
                if state != "all":
                    rows = [r for r in rows if r["state"] == state]
                return self._listing(cmd, query, rows)
            if len(segs) == 3 and segs[0] == "issues" and segs[2] == "comments":
                n = int(segs[1])
                if n not in self.issues and n not in self.prs:
                    return _fail(NOT_FOUND)
                comments = sorted(self.comments.get(n, {}).items())
                return self._listing(cmd, query, [{"id": c, "body": b} for c, b in comments])
            if len(segs) == 3 and segs[0] == "pulls" and segs[2] == "files":
                n = int(segs[1])
                if n not in self.prs:
                    return _fail(NOT_FOUND)
                files = [{"filename": p, "status": "modified"} for p in self.pr_file_paths[n]]
                return self._listing(cmd, query, files)
        if method == "POST" and len(segs) == 3 and segs[0] == "issues" and segs[2] == "comments":
            n = int(segs[1])
            if n not in self.issues:
                return _fail(NOT_FOUND)
            body = str(json.loads(stdin or "{}")["body"])
            return _ok({"id": self.add_comment(n, body), "body": body})
        if method == "PATCH" and len(segs) == 3 and segs[:2] == ["issues", "comments"]:
            cid = int(segs[2])
            for bodies in self.comments.values():
                if cid in bodies:
                    bodies[cid] = str(json.loads(stdin or "{}")["body"])
                    return _ok({"id": cid, "body": bodies[cid]})
            return _fail(NOT_FOUND)  # as GitHub does: no comment, nothing to edit
        return _fail(f"FakeGh: unhandled api {method} {path}")

    def _rest_issues(self) -> list[dict[str, Any]]:
        """``GET repos/o/r/issues`` rows, newest first. PRs are in it too, each with a
        ``pull_request`` key (a merged PR is ``closed`` there)."""
        rows: list[dict[str, Any]] = [
            {
                "number": n,
                "title": i["title"],
                "body": i["body"],
                "state": i["state"],
                "labels": [{"name": lb} for lb in i["labels"]],
            }
            for n, i in self.issues.items()
        ]
        rows += [
            {
                "number": n,
                "title": p["title"],
                "body": p["body"],
                "state": "open" if p["state"] == "OPEN" else "closed",
                "labels": [],
                "pull_request": {"url": f"https://api.github.com/repos/{self.repo}/pulls/{n}"},
            }
            for n, p in self.prs.items()
        ]
        return sorted(rows, key=lambda r: -int(r["number"]))

    def _listing(
        self, cmd: Sequence[str], query: dict[str, list[str]], rows: list[dict[str, Any]]
    ) -> CommandResult:
        per_page = int(query.get("per_page", ["30"])[0])
        pages = [rows[i : i + per_page] for i in range(0, len(rows), per_page)] or [[]]
        if "--paginate" not in cmd:
            return _ok(pages[0])
        if self.joined_pages:
            return _ok(rows)
        return CommandResult(0, "".join(json.dumps(p) for p in pages), "")


def _is_write(argv: Sequence[str]) -> bool:
    cmd = list(argv[1:])
    if cmd[:2] in (["label", "create"], ["issue", "create"], ["issue", "edit"]):
        return True
    if cmd[:2] in (["pr", "create"], ["pr", "edit"], ["pr", "merge"], ["issue", "comment"]):
        return True
    return cmd[:1] == ["api"] and (_flag(cmd, "-X") or "GET").upper() != "GET"


@pytest.fixture
def fake() -> FakeGh:
    return FakeGh()


@pytest.fixture
def gh(fake: FakeGh) -> GitHub:
    return GitHub(REPO, runner=fake)


# markers


def test_the_markers_are_exact_strings() -> None:
    assert marker_for("0123abcd") == "<!-- scio-recovery:0123abcd -->"
    assert status_marker("inc-1") == "<!-- scio-recovery-status:inc-1 -->"


def test_a_repository_that_is_not_owner_slash_name_is_refused() -> None:
    for bad in ("", "o", "o/r/x", "o r/x"):
        with pytest.raises(ValueError):
            GitHub(bad, runner=FakeGh())


# the fake refuses what gh refuses (guardrail 18)


def test_the_fake_refuses_an_unknown_label_and_an_edit_of_a_missing_comment(
    fake: FakeGh,
) -> None:
    n = fake.add_issue("t", "b")
    create = ["gh", "issue", "create", "-R", REPO, "--title", "x", "--body-file", "-"]
    refused = fake([*create, "--label", "recovery"], stdin="b")
    assert refused.code == 1 and "'recovery' not found" in refused.stderr
    edit = fake(["gh", "issue", "edit", str(n), "-R", REPO, "--add-label", "recovery"])
    assert edit.code == 1 and "'recovery' not found" in edit.stderr
    patch = ["gh", "api", "-X", "PATCH", f"repos/{REPO}/issues/comments/77", "--input", "-"]
    missing = fake(patch, stdin=json.dumps({"body": "x"}))
    assert missing.code == 1 and "404" in missing.stderr
    fake.labels["recovery"] = ("0e8a16", "d")
    again = fake(["gh", "label", "create", "recovery", "-R", REPO, "--color", "000000"])
    assert again.code == 1 and "already exists" in again.stderr
    assert fake.labels["recovery"] == ("0e8a16", "d")


# labels


def test_a_label_is_created_once_and_an_existing_one_is_left_alone(
    gh: GitHub, fake: FakeGh
) -> None:
    fake.labels["bug"] = ("ffffff", "the owner's own bug label")
    gh.ensure_label("bug", "d73a4a", "Something isn't working")
    gh.ensure_label("recovery", "0e8a16", "the supervisor's")
    gh.ensure_label("recovery", "0e8a16", "the supervisor's")
    assert fake.labels["bug"] == ("ffffff", "the owner's own bug label")
    assert fake.labels["recovery"] == ("0e8a16", "the supervisor's")
    create = ["gh", "label", "create", "recovery", "-R", REPO, "--color", "0e8a16"]
    assert fake.writes() == [[*create, "--description", "the supervisor's"]]


def test_a_label_created_by_another_process_meanwhile_is_not_an_error(
    gh: GitHub, fake: FakeGh
) -> None:
    fake.labels["recovery"] = ("0e8a16", "made by a racing tick")
    fake.fail_on(["gh", "api", f"repos/{REPO}/labels/recovery"], _fail(NOT_FOUND))
    gh.ensure_label("recovery", "0e8a16", "d")
    assert fake.labels["recovery"] == ("0e8a16", "made by a racing tick")


def test_a_label_probe_that_fails_for_another_reason_raises_and_creates_nothing(
    gh: GitHub, fake: FakeGh
) -> None:
    fake.fail_on(["gh", "api"], _fail("gh: Bad credentials (HTTP 401)"))
    with pytest.raises(GitHubError, match="401"):
        gh.ensure_label("recovery", "0e8a16", "d")
    assert fake.writes() == []


# issues


@pytest.mark.parametrize("joined", [True, False], ids=["joined-pages", "concatenated-pages"])
def test_find_issue_reads_every_page_and_skips_prs(joined: bool) -> None:
    fake = FakeGh(joined_pages=joined)
    marker = marker_for("sig1")
    fake.add_pr("fix/1-x", state="MERGED", body=f"a PR that quotes it: {marker}")
    target = fake.add_issue("recovery: the old one", f"what happened\n\n{marker}\n", state="closed")
    for k in range(250):
        fake.add_issue(f"filler {k}", "nothing to see")
    gh = GitHub(REPO, runner=fake)
    assert gh.find_issue(marker) == target  # closed, oldest, on the third page
    assert gh.find_issue(marker_for("sig2")) is None
    listing = f"repos/{REPO}/issues?state=all&per_page=100"
    assert fake.calls[0][0] == ["gh", "api", "--paginate", listing]


def test_find_issue_ignores_a_pr_that_carries_the_marker(gh: GitHub, fake: FakeGh) -> None:
    marker = marker_for("sig1")
    fake.add_pr("fix/1-x", body=marker)
    assert gh.find_issue(marker) is None


def test_ensure_issue_creates_its_labels_first_and_a_second_call_creates_nothing(
    gh: GitHub, fake: FakeGh
) -> None:
    marker = marker_for("sig1")
    title = 'recovery: harness_suspect scio/x.py:run: "boom" after 30 s'
    n = gh.ensure_issue(title, "what happened", ["bug", "live-run", "recovery"], marker)
    issue = fake.issues[n]
    assert issue["title"] == title and issue["labels"] == ["bug", "live-run", "recovery"]
    assert issue["body"] == f"what happened\n\n{marker}\n"
    colors = {name: (color, desc) for name, color, desc in RECOVERY_LABELS}
    assert all(fake.labels[lb] == colors[lb] for lb in ("bug", "live-run", "recovery"))
    writes = fake.writes()
    assert [w[1:3] for w in writes] == [["label", "create"]] * 3 + [["issue", "create"]]
    create = writes[-1]
    assert create[create.index("--body-file") + 1] == "-" and "what happened" not in create
    assert gh.ensure_issue("a different title", "again", ["bug"], marker) == n
    assert fake.writes() == writes and list(fake.issues) == [n]


def test_ensure_issue_keeps_a_marker_already_in_the_body(gh: GitHub, fake: FakeGh) -> None:
    marker = marker_for("sig1")
    n = gh.ensure_issue("t", f"{marker}\nbody", ["recovery"], marker)
    assert fake.issues[n]["body"] == f"{marker}\nbody"


def test_ensure_issue_finds_what_it_created_when_gh_prints_no_url(gh: GitHub, fake: FakeGh) -> None:
    fake.quiet_issue_create = True
    marker = marker_for("sig1")
    n = gh.ensure_issue("t", "b", ["recovery"], marker)
    assert marker in fake.issues[n]["body"]


def test_add_labels_creates_a_missing_label_before_naming_it(gh: GitHub, fake: FakeGh) -> None:
    n = fake.add_issue("t", "b", labels=["recovery"])
    fake.labels["recovery"] = ("0e8a16", "d")
    gh.add_labels(n, ["needs-human", "recovery"])
    assert fake.issues[n]["labels"] == ["recovery", "needs-human"]
    create = ["gh", "label", "create", "needs-human", "-R", REPO, "--color", "b60205"]
    edit = ["gh", "issue", "edit", str(n), "-R", REPO]
    assert fake.writes() == [
        [*create, "--description", "The recovery supervisor escalated this to the owner"],
        [*edit, "--add-label", "needs-human", "--add-label", "recovery"],
    ]
    gh.add_labels(n, [])
    assert len(fake.writes()) == 2


# the status comment


def test_upsert_comment_creates_one_comment_then_edits_that_same_one(
    gh: GitHub, fake: FakeGh
) -> None:
    n = fake.add_issue("t", "b")
    for k in range(120):  # the supervisor's comment lands on the second page
        fake.add_comment(n, f"a human's comment {k}")
    human = dict(fake.comments[n])
    marker = status_marker("inc-1")
    gh.upsert_comment(n, marker, "state: issue_open")
    (cid,) = set(fake.comments[n]) - set(human)
    gh.upsert_comment(n, marker, "state: repairing")
    assert set(fake.comments[n]) == set(human) | {cid}
    assert fake.comments[n][cid] == f"state: repairing\n\n{marker}\n"
    assert all(fake.comments[n][k] == v for k, v in human.items())
    post, patch = fake.writes()
    assert post == ["gh", "api", "-X", "POST", f"repos/{REPO}/issues/{n}/comments", "--input", "-"]
    edited = f"repos/{REPO}/issues/comments/{cid}"
    assert patch == ["gh", "api", "-X", "PATCH", edited, "--input", "-"]


def test_one_incidents_status_marker_never_matches_anothers(gh: GitHub, fake: FakeGh) -> None:
    n = fake.add_issue("t", "b")
    other = fake.add_comment(n, f"state: closed\n\n{status_marker('inc-10')}\n")
    gh.upsert_comment(n, status_marker("inc-1"), "state: detected")
    assert fake.comments[n][other] == f"state: closed\n\n{status_marker('inc-10')}\n"
    assert len(fake.comments[n]) == 2


# pull requests


@pytest.mark.parametrize(
    ("state", "merged"), [("OPEN", False), ("CLOSED", False), ("MERGED", True)]
)
def test_pr_parses_an_open_a_closed_and_a_merged_pr(
    gh: GitHub, fake: FakeGh, state: str, merged: bool
) -> None:
    n = fake.add_pr("fix/9-recovery-abc123", state=state, body="Closes #9", draft=False)
    assert gh.pr(n) == PullRequest(
        number=n,
        state=state,  # type: ignore[arg-type]
        merged_at="2026-11-14T10:00:00Z" if merged else None,
        merge_sha=f"{n:040x}" if merged else "",
        head_ref="fix/9-recovery-abc123",
        head_sha=f"{n + 0xABC:040x}",
        body="Closes #9",
        is_draft=False,
    )


def test_pr_is_none_for_a_missing_pr_and_raises_on_any_other_failure(
    gh: GitHub, fake: FakeGh
) -> None:
    assert gh.pr(404) is None
    n = fake.add_pr("fix/9-x")
    fake.fail_on(["gh", "pr", "view"], _fail("HTTP 502: Bad Gateway"))
    with pytest.raises(GitHubError) as err:
        gh.pr(n)
    assert f"gh pr view {n} -R {REPO} --json" in str(err.value)
    assert "502" in str(err.value) and err.value.argv[:3] == ["gh", "pr", "view"]


def test_find_pr_by_head_returns_the_newest_pr_in_any_state(gh: GitHub, fake: FakeGh) -> None:
    fake.add_pr("fix/9-x", state="CLOSED")
    fake.add_pr("fix/other", state="OPEN")
    newest = fake.add_pr("fix/9-x", state="MERGED")
    found = gh.find_pr_by_head("fix/9-x")
    assert found is not None and found.number == newest and found.merged_at is not None
    assert gh.find_pr_by_head("fix/none") is None


@pytest.mark.parametrize("joined", [True, False], ids=["joined-pages", "concatenated-pages"])
def test_pr_files_reads_every_page(joined: bool) -> None:
    fake = FakeGh(joined_pages=joined)
    paths = [f"scio/mod{k}.py" for k in range(130)] + ["docs/ISSUE_LEDGER.md"]
    n = fake.add_pr("fix/9-x", files=paths)
    assert GitHub(REPO, runner=fake).pr_files(n) == paths


# CI by full SHA


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], "none"),
        ([("completed", "success"), ("in_progress", "")], "pending"),
        ([("queued", "")], "pending"),
        ([("completed", "success"), ("completed", "skipped")], "success"),
        ([("completed", "skipped")], "success"),
        ([("completed", "success"), ("completed", "failure")], "failure"),
        ([("completed", "cancelled")], "failure"),
    ],
)
def test_ci_status_aggregates_every_run_of_the_commit(
    gh: GitHub, fake: FakeGh, runs: list[tuple[str, str]], expected: str
) -> None:
    fake.add_run(OTHER_SHA, "completed", "failure")  # another commit's verdict never counts
    for status, conclusion in runs:
        fake.add_run(SHA, status, conclusion)
    assert gh.ci_status(SHA) == expected
    (argv, _) = fake.calls[-1]
    assert argv[:7] == ["gh", "run", "list", "-R", REPO, "--commit", SHA]


def test_ci_status_reads_more_than_gh_s_default_twenty_runs(gh: GitHub, fake: FakeGh) -> None:
    fake.add_run(SHA, "completed", "failure")  # the oldest of 26
    for _ in range(25):
        fake.add_run(SHA, "completed", "success")
    assert gh.ci_status(SHA) == "failure"


def test_ci_status_never_matches_a_short_sha(gh: GitHub, fake: FakeGh) -> None:
    fake.add_run(SHA, "completed", "success")
    fake.fail_on(["gh", "run", "list"], _ok([{"headSha": SHA, "status": "completed"}]))
    assert gh.ci_status(SHA[:12]) == "none"  # I-172: a short SHA is not that commit's green


# failures and the write log


def test_a_gh_failure_raises_with_its_argv_and_stderr(gh: GitHub, fake: FakeGh) -> None:
    fake.fail_on(["gh", "api", "--paginate"], _fail("HTTP 401: Bad credentials"))
    with pytest.raises(GitHubError) as err:
        gh.find_issue(marker_for("s"))
    message = str(err.value)
    assert f"gh api --paginate repos/{REPO}/issues?state=all&per_page=100" in message
    assert "Bad credentials" in message and err.value.stderr == "HTTP 401: Bad credentials"
    fake.fail_on(["gh", "run", "list"], CommandResult(-1, "", "", timed_out=True))
    with pytest.raises(GitHubError, match="timed out"):
        gh.ci_status(SHA)


def test_every_write_is_recorded_on_the_fake_with_its_payload(gh: GitHub, fake: FakeGh) -> None:
    marker = marker_for("sig1")
    n = gh.ensure_issue("recovery: t", "b", ["recovery"], marker)
    gh.upsert_comment(n, status_marker("inc-1"), "one")
    gh.upsert_comment(n, status_marker("inc-1"), "two")
    gh.add_labels(n, ["needs-human"])
    gh.find_issue(marker)
    gh.pr(1)
    gh.ci_status(SHA)
    writes = [(a, s) for a, s in fake.calls if _is_write(a)]
    assert [a[1:4] for a, _ in writes] == [
        ["label", "create", "recovery"],
        ["issue", "create", "-R"],
        ["api", "-X", "POST"],
        ["api", "-X", "PATCH"],
        ["label", "create", "needs-human"],
        ["issue", "edit", str(n)],
    ]
    assert writes[1][1] == f"b\n\n{marker}\n"
    assert json.loads(writes[3][1] or "{}") == {"body": f"two\n\n{status_marker('inc-1')}\n"}


def test_parse_pages_reads_joined_concatenated_and_slurped_output() -> None:
    rows = [{"n": 1}, {"n": 2}, {"n": 3}]
    assert parse_pages(json.dumps(rows)) == rows
    assert parse_pages('[{"n": 1}, {"n": 2}]\n[{"n": 3}]') == rows
    assert parse_pages(json.dumps([rows[:2], rows[2:]])) == rows
    assert parse_pages("") == [] and parse_pages("[]\n[]") == []
