"""``GitHubClient``: the only way SCIO touches GitHub.

Backed by the ``gh`` CLI (JSON output) through an injectable runner, so the client is
unit-tested with canned responses and integration-tested against a throwaway repo.

Guarantees:

* Every write is idempotent: look up the existing branch / comment / PR first.
* No write ever targets the default branch; nothing is force-pushed (pushes happen in
  the sandbox via :func:`scio.aci.tools.git_push`, which has the same guard).
* Every write is appended to :attr:`GitHubClient.writes` and logged.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote

log = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    code: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, stdin: str | None = None) -> RunResult: ...


def GhRunner(argv: Sequence[str], *, stdin: str | None = None) -> RunResult:
    proc = subprocess.run(
        ["gh", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass
class WriteRecord:
    op: str
    target: str
    noop: bool
    at: datetime = field(default_factory=lambda: datetime.now(UTC))
    detail: str = ""


@dataclass(frozen=True)
class IssueInfo:
    number: int
    title: str
    body: str
    labels: list[str]
    url: str


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    url: str
    is_draft: bool
    head: str
    base: str


class GitHubClient:
    def __init__(self, repo: str, *, runner: Runner = GhRunner) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"repo must be 'owner/name': {repo!r}")
        self.repo = repo
        self._run = runner
        self.writes: list[WriteRecord] = []
        self._default_branch: str | None = None

    # -- plumbing ------------------------------------------------------------

    def _gh(self, *argv: str, stdin: str | None = None, ok_codes: Sequence[int] = (0,)) -> str:
        r = self._run(argv, stdin=stdin)
        if r.code not in ok_codes:
            raise GitHubError(
                f"gh {' '.join(argv[:3])}... failed ({r.code}): {r.stderr.strip()[:400]}"
            )
        return r.stdout

    def _json(self, *argv: str, stdin: str | None = None) -> Any:
        out = self._gh(*argv, stdin=stdin)
        return json.loads(out) if out.strip() else None

    def _record(self, op: str, target: str, *, noop: bool, detail: str = "") -> None:
        rec = WriteRecord(op=op, target=target, noop=noop, detail=detail)
        self.writes.append(rec)
        log.info("github %s %s %s %s", "noop" if noop else "write", op, target, detail)

    def _guard_branch(self, branch: str) -> None:
        if branch == self.default_branch():
            raise GitHubError(f"refusing to write to the default branch {branch!r}")

    # -- reads ---------------------------------------------------------------

    def default_branch(self) -> str:
        if self._default_branch is None:
            data = self._json("repo", "view", self.repo, "--json", "defaultBranchRef")
            self._default_branch = str(data["defaultBranchRef"]["name"])
        return self._default_branch

    def get_issue(self, number: int) -> IssueInfo:
        d = self._json(
            "issue", "view", str(number), "-R", self.repo, "--json", "number,title,body,labels,url"
        )
        return IssueInfo(
            number=int(d["number"]),
            title=str(d["title"]),
            body=str(d.get("body") or ""),
            labels=[str(lb["name"]) for lb in d.get("labels", [])],
            url=str(d["url"]),
        )

    def branch_exists(self, name: str) -> bool:
        r = self._run(["api", f"repos/{self.repo}/git/ref/heads/{name}"])
        return r.code == 0

    def find_pr(self, head: str) -> PullRequestInfo | None:
        rows = self._json(
            "pr", "list", "-R", self.repo, "--head", head, "--state", "open",
            "--json", "number,url,isDraft,headRefName,baseRefName",
        )  # fmt: skip
        if not rows:
            return None
        d = rows[0]
        return PullRequestInfo(
            int(d["number"]),
            str(d["url"]),
            bool(d["isDraft"]),
            str(d["headRefName"]),
            str(d["baseRefName"]),
        )

    def find_milestone(self, title: str) -> int | None:
        rows = self._json("api", "--paginate", f"repos/{self.repo}/milestones?state=all") or []
        for m in rows:
            if str(m.get("title")) == title:
                return int(m["number"])
        return None

    def find_issue(self, title: str) -> IssueInfo | None:
        """The open or closed issue with exactly this title, if any."""
        rows = self._json(
            "issue", "list", "-R", self.repo, "--state", "all", "--limit", "200",
            "--search", f'in:title "{title}"', "--json", "number,title,body,labels,url",
        )  # fmt: skip
        for d in rows or []:
            if str(d.get("title")) == title:
                return IssueInfo(
                    number=int(d["number"]),
                    title=str(d["title"]),
                    body=str(d.get("body") or ""),
                    labels=[str(lb["name"]) for lb in d.get("labels", [])],
                    url=str(d["url"]),
                )
        return None

    def pr_merged(self, number: int) -> bool:
        d = self._json("pr", "view", str(number), "-R", self.repo, "--json", "state,mergedAt")
        return str(d.get("state", "")).upper() == "MERGED" or bool(d.get("mergedAt"))

    def find_comment(self, issue: int, marker: str) -> int | None:
        rows = self._json("api", "--paginate", f"repos/{self.repo}/issues/{issue}/comments") or []
        for c in rows:
            if marker in str(c.get("body") or ""):
                return int(c["id"])
        return None

    # -- idempotent writes ---------------------------------------------------

    def ensure_branch(self, name: str, from_sha: str) -> bool:
        """Create ``name`` at ``from_sha`` unless it exists. Returns True if created."""
        self._guard_branch(name)
        if self.branch_exists(name):
            self._record("ensure_branch", name, noop=True)
            return False
        self._gh(
            "api",
            "-X",
            "POST",
            f"repos/{self.repo}/git/refs",
            "-f",
            f"ref=refs/heads/{name}",
            "-f",
            f"sha={from_sha}",
        )
        self._record("ensure_branch", name, noop=False, detail=from_sha[:12])
        return True

    def ensure_milestone(self, title: str, description: str = "") -> int:
        """Create the milestone unless one with this title exists. Returns its number."""
        existing = self.find_milestone(title)
        if existing is not None:
            self._record("ensure_milestone", title, noop=True, detail=f"#{existing}")
            return existing
        d = self._json(
            "api",
            "-X",
            "POST",
            f"repos/{self.repo}/milestones",
            "--input",
            "-",
            stdin=json.dumps({"title": title, "description": description}),
        )
        self._record("ensure_milestone", title, noop=False, detail=f"#{d['number']}")
        return int(d["number"])

    def ensure_label(
        self, name: str, *, color: str = "5319e7", description: str = "created by SCIO"
    ) -> bool:
        """Create the label unless the repository has it (I-113: `gh issue create --label`
        fails on a label that does not exist, and a fresh repository has only GitHub's
        defaults). An existing label keeps its colour and description. True if created."""
        r = self._run(("api", f"repos/{self.repo}/labels/{quote(name, safe='')}"), stdin=None)
        if r.code == 0:
            self._record("ensure_label", name, noop=True)
            return False
        payload = {"name": name, "color": color, "description": description}
        self._gh(
            "api", "-X", "POST", f"repos/{self.repo}/labels", "--input", "-",
            stdin=json.dumps(payload),
        )  # fmt: skip
        self._record("ensure_label", name, noop=False)
        return True

    def ensure_issue(
        self, *, title: str, body: str, milestone: str | None, labels: Sequence[str] = ()
    ) -> IssueInfo:
        """Create the issue unless one with exactly this title exists (the title carries the
        feature id, so a resumed build finds its own issue). Its labels are created first when
        the repository lacks them (I-113)."""
        existing = self.find_issue(title)
        if existing is not None:
            self._record("ensure_issue", title, noop=True, detail=f"#{existing.number}")
            return existing
        for lb in labels:
            self.ensure_label(lb)
        argv = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        if milestone:
            argv += ["--milestone", milestone]
        for lb in labels:
            argv += ["--label", lb]
        out = self._gh(*argv, stdin=body)
        url = out.strip().splitlines()[-1] if out.strip() else ""
        m = re.search(r"/issues/(\d+)", url)
        if not m:
            created = self.find_issue(title)
            if created is None:
                raise GitHubError(f"issue created but not found by title {title!r}: {out!r}")
            self._record("ensure_issue", title, noop=False, detail=f"#{created.number}")
            return created
        number = int(m.group(1))
        self._record("ensure_issue", title, noop=False, detail=f"#{number}")
        return IssueInfo(number=number, title=title, body=body, labels=list(labels), url=url)

    def upsert_issue_comment(self, issue: int, body: str, *, marker: str) -> int:
        """Create or update the one comment carrying ``<!-- marker -->``. Returns its id."""
        tagged = f"{body.rstrip()}\n\n<!-- {marker} -->\n"
        existing = self.find_comment(issue, f"<!-- {marker} -->")
        payload = json.dumps({"body": tagged})
        if existing is not None:
            self._gh(
                "api",
                "-X",
                "PATCH",
                f"repos/{self.repo}/issues/comments/{existing}",
                "--input",
                "-",
                stdin=payload,
            )
            self._record(
                "upsert_issue_comment", f"#{issue}", noop=False, detail=f"updated {existing}"
            )
            return existing
        d = self._json(
            "api",
            "-X",
            "POST",
            f"repos/{self.repo}/issues/{issue}/comments",
            "--input",
            "-",
            stdin=payload,
        )
        self._record("upsert_issue_comment", f"#{issue}", noop=False, detail=f"created {d['id']}")
        return int(d["id"])

    def ensure_draft_pr(self, *, head: str, base: str, title: str, body: str) -> PullRequestInfo:
        """Open a draft PR for ``head`` unless one is already open.

        ``head`` is never the default branch.
        """
        self._guard_branch(head)
        existing = self.find_pr(head)
        if existing is not None:
            self._record("ensure_draft_pr", head, noop=True, detail=f"#{existing.number}")
            return existing
        out = self._gh(
            "pr", "create", "-R", self.repo, "--draft", "--head", head, "--base", base,
            "--title", title, "--body-file", "-", stdin=body,
        )  # fmt: skip
        url = out.strip().splitlines()[-1] if out.strip() else ""
        m = re.search(r"/pull/(\d+)", url)
        if not m:
            created = self.find_pr(head)
            if created is None:
                raise GitHubError(f"PR created but not found for head {head!r}: {out!r}")
            self._record("ensure_draft_pr", head, noop=False, detail=f"#{created.number}")
            return created
        number = int(m.group(1))
        self._record("ensure_draft_pr", head, noop=False, detail=f"#{number}")
        return PullRequestInfo(number, url, True, head, base)

    def update_pr_body(self, number: int, body: str) -> None:
        self._gh("pr", "edit", str(number), "-R", self.repo, "--body-file", "-", stdin=body)
        self._record("update_pr_body", f"#{number}", noop=False)

    def find_release(self, tag: str) -> str | None:
        """The URL of the GitHub Release for ``tag``, or None (the release gate's probe)."""
        r = self._run(["release", "view", tag, "-R", self.repo, "--json", "url"])
        if r.code != 0:
            return None
        return str(json.loads(r.stdout)["url"]) if r.stdout.strip() else None

    def ensure_release(self, *, tag: str, title: str, notes: str, assets: Sequence[str]) -> str:
        """Create the GitHub Release for an already pushed ``tag`` with ``assets`` attached,
        unless one exists. Returns its URL (M11)."""
        found = self.find_release(tag)
        if found is not None:
            self._record("release", tag, noop=True)
            return found
        out = self._gh(
            "release",
            "create",
            tag,
            "-R",
            self.repo,
            "--title",
            title,
            "--notes-file",
            "-",
            "--verify-tag",
            *assets,
            stdin=notes,
        )
        self._record("release", tag, noop=False, detail=f"{len(assets)} asset(s)")
        return out.strip().splitlines()[-1] if out.strip() else ""

    def mark_ready(self, number: int) -> bool:
        """Flip a draft PR to ready. Returns False if it was already ready."""
        d = self._json("pr", "view", str(number), "-R", self.repo, "--json", "isDraft")
        if not d["isDraft"]:
            self._record("mark_ready", f"#{number}", noop=True)
            return False
        self._gh("pr", "ready", str(number), "-R", self.repo)
        self._record("mark_ready", f"#{number}", noop=False)
        return True
