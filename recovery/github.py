"""GitHub through ``gh``: recovery issues, comments, PRs and CI by SHA. OWNER: wave-1 agent C.

The rules carried over from SCIO's ``GitHubClient`` (``scio/harness/github/client.py``)
and ``scripts/watch_ci.py``:
- Create a label before using it (guardrail 18).
- Find an issue by listing the repository's issues through the paginated REST API and
  matching a body marker. Never use search (guardrail 48).
- Treat a merge as real only when ``mergedAt`` is set (guardrail 8).
- Read CI by the commit's full SHA (I-104, I-172).

Every call is ``["gh", *args]`` through the injected :class:`~recovery.commands.Runner`
(``run_command`` does not add the binary). Bodies go on stdin (``--body-file -`` or
``--input -``), never in argv. The argv shapes, for a fake:
- ``gh api repos/<R>/labels/<name>``: the label probe (a miss is ``HTTP 404``);
- ``gh label create <name> -R <R> --color C --description D``;
- ``gh api --paginate repos/<R>/issues?state=all&per_page=100``;
- ``gh issue create -R <R> --title T --body-file - --label L ...``;
- ``gh issue edit N -R <R> --add-label L ...``;
- ``gh api --paginate repos/<R>/issues/N/comments?per_page=100``;
- ``gh api -X POST repos/<R>/issues/N/comments --input -`` and
  ``gh api -X PATCH repos/<R>/issues/comments/<id> --input -``;
- ``gh pr view N -R <R> --json <PR_FIELDS>``;
- ``gh pr list -R <R> --head B --state all --limit 100 --json <PR_FIELDS>``;
- ``gh api --paginate repos/<R>/pulls/N/files?per_page=100``;
- ``gh run list -R <R> --commit SHA --limit 100 --json <RUN_FIELDS>``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, cast
from urllib.parse import quote

from recovery.commands import CommandResult, Runner, run_command
from recovery.model import CiStatus, PrState, PullRequest

RECOVERY_LABELS: tuple[tuple[str, str, str], ...] = (
    ("bug", "d73a4a", "Something isn't working"),
    ("live-run", "5319e7", "Found by a live product run"),
    ("recovery", "0e8a16", "Opened by the external recovery supervisor (ADR 0019)"),
    ("needs-human", "b60205", "The recovery supervisor escalated this to the owner"),
)

DEFAULT_LABEL_COLOR = "ededed"
DEFAULT_LABEL_DESCRIPTION = "Used by the external recovery supervisor (ADR 0019)"

GH_TIMEOUT_S = 120.0
"""A hung ``gh`` must not hang the supervisor: the runner kills it and the call fails."""

PR_FIELDS = "number,state,mergedAt,mergeCommit,headRefName,headRefOid,body,isDraft"
RUN_FIELDS = "databaseId,headSha,status,conclusion,workflowName"

PR_NOT_FOUND = "could not resolve to a pullrequest"
"""gh's error for a PR number that does not exist (lower case):
``GraphQL: Could not resolve to a PullRequest with the number of N. (repository.pullRequest)``."""

_PR_STATES: frozenset[str] = frozenset({"OPEN", "CLOSED", "MERGED"})
_CI_SUCCESS: frozenset[str] = frozenset({"success", "skipped"})


class GitHubError(Exception):
    """A ``gh`` call failed. The message holds the argv and gh's stderr."""

    def __init__(
        self,
        message: str,
        *,
        argv: Sequence[str] = (),
        stderr: str = "",
        code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.argv = list(argv)
        self.stderr = stderr
        self.code = code


def marker_for(signature: str) -> str:
    """``<!-- scio-recovery:<signature> -->``: the issue body marker that dedupes incidents."""
    return f"<!-- scio-recovery:{signature} -->"


def status_marker(incident_id: str) -> str:
    """``<!-- scio-recovery-status:<incident_id> -->``: the marker of the one comment the
    supervisor keeps up to date on an issue."""
    return f"<!-- scio-recovery-status:{incident_id} -->"


def with_marker(body: str, marker: str) -> str:
    """``body`` with ``marker`` on its own line at the end, unless it already holds it."""
    if marker in body:
        return body
    return f"{body.rstrip()}\n\n{marker}\n"


def parse_pages(stdout: str) -> list[Any]:
    """The rows of ``gh api --paginate`` output, whatever way gh printed the pages.

    gh may join the pages' arrays into one array, print each page's array one after
    another (``[...][...]``), or, with ``--slurp``, wrap the pages in an outer array.
    Each JSON value is decoded in turn, and arrays are flattened one level, so all three
    give the same rows."""
    decoder = json.JSONDecoder()
    rows: list[Any] = []
    pos, end = 0, len(stdout)
    while True:
        while pos < end and stdout[pos].isspace():
            pos += 1
        if pos >= end:
            return rows
        value, pos = decoder.raw_decode(stdout, pos)
        if not isinstance(value, list):
            rows.append(value)
            continue
        for item in value:
            if isinstance(item, list):
                rows.extend(item)
            else:
                rows.append(item)


def _describe(argv: Sequence[str], result: CommandResult) -> str:
    how = "timed out" if result.timed_out else f"exit {result.code}"
    stderr = result.stderr.strip()
    if len(stderr) > 2000:
        stderr = "..." + stderr[-2000:]
    return f"{' '.join(argv)} failed ({how}): {stderr}"


def _merged_at(value: Any) -> str | None:
    """``mergedAt`` as a timestamp, or None when the PR is not merged. gh prints null for
    an unmerged PR; an empty string or Go's zero time is treated the same way."""
    if not value:
        return None
    text = str(value)
    return None if text.startswith("0001-01-01") else text


def _pull_request(data: dict[str, Any]) -> PullRequest:
    state = str(data.get("state") or "").upper()
    if state not in _PR_STATES:
        raise GitHubError(f"unexpected PR state {data.get('state')!r} in {data!r}")
    commit = data.get("mergeCommit")
    merge_sha = str(commit.get("oid") or "") if isinstance(commit, dict) else ""
    return PullRequest(
        number=int(data["number"]),
        state=cast(PrState, state),
        merged_at=_merged_at(data.get("mergedAt")),
        merge_sha=merge_sha,
        head_ref=str(data.get("headRefName") or ""),
        head_sha=str(data.get("headRefOid") or ""),
        body=str(data.get("body") or ""),
        is_draft=bool(data.get("isDraft")),
    )


class GitHub:
    def __init__(self, repo: str, runner: Runner = run_command) -> None:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError(f"repo must be 'owner/name': {repo!r}")
        self.repo = repo
        self.runner = runner

    # plumbing

    def _call(self, args: Sequence[str], *, stdin: str | None = None) -> CommandResult:
        return self.runner(["gh", *args], stdin=stdin, timeout_s=GH_TIMEOUT_S)

    def _gh(self, args: Sequence[str], *, stdin: str | None = None) -> str:
        argv = ["gh", *args]
        result = self._call(args, stdin=stdin)
        if not result.ok:
            raise GitHubError(
                _describe(argv, result), argv=argv, stderr=result.stderr, code=result.code
            )
        return result.stdout

    def _json(self, args: Sequence[str], *, stdin: str | None = None) -> Any:
        out = self._gh(args, stdin=stdin)
        try:
            return json.loads(out) if out.strip() else None
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh {' '.join(args)} printed no JSON: {out[:400]!r}") from exc

    def _pages(self, path: str) -> list[dict[str, Any]]:
        args = ["api", "--paginate", path]
        out = self._gh(args)
        try:
            rows = parse_pages(out)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh {' '.join(args)} printed no JSON: {out[:400]!r}") from exc
        return [r for r in rows if isinstance(r, dict)]

    # labels

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Create the label if the repository lacks it; leave an existing one alone."""
        probe = ["api", f"repos/{self.repo}/labels/{quote(name, safe='')}"]
        found = self._call(probe)
        if found.ok:
            return
        if found.timed_out or "404" not in found.stderr:
            argv = ["gh", *probe]
            raise GitHubError(
                _describe(argv, found), argv=argv, stderr=found.stderr, code=found.code
            )
        create = ["label", "create", name, "-R", self.repo, "--color", color]
        create += ["--description", description]
        made = self._call(create)
        if made.ok or (not made.timed_out and "already exists" in made.stderr):
            return  # another process created it between the probe and the create
        argv = ["gh", *create]
        raise GitHubError(_describe(argv, made), argv=argv, stderr=made.stderr, code=made.code)

    def _ensure_labels(self, labels: Sequence[str]) -> list[str]:
        known = {name: (color, desc) for name, color, desc in RECOVERY_LABELS}
        unique = list(dict.fromkeys(labels))
        for name in unique:
            color, desc = known.get(name, (DEFAULT_LABEL_COLOR, DEFAULT_LABEL_DESCRIPTION))
            self.ensure_label(name, color, desc)
        return unique

    # issues and comments

    def find_issue(self, marker: str) -> int | None:
        """The number of an issue, open or closed and not a PR, whose body contains
        ``marker``. Reads every page."""
        if not marker:
            raise ValueError("an empty marker matches every issue")
        rows = self._pages(f"repos/{self.repo}/issues?state=all&per_page=100")
        found = [
            int(r["number"])
            for r in rows
            if "pull_request" not in r and marker in str(r.get("body") or "")
        ]
        return min(found) if found else None

    def ensure_issue(self, title: str, body: str, labels: Sequence[str], marker: str) -> int:
        """Return the existing issue with ``marker``, or create it with the marker in its
        body, after ensuring every label in ``RECOVERY_LABELS`` it uses."""
        existing = self.find_issue(marker)
        if existing is not None:
            return existing
        args = ["issue", "create", "-R", self.repo, "--title", title, "--body-file", "-"]
        for name in self._ensure_labels(labels):
            args += ["--label", name]
        out = self._gh(args, stdin=with_marker(body, marker))
        lines = out.strip().splitlines()
        m = re.search(r"/issues/(\d+)", lines[-1]) if lines else None
        if m is not None:
            return int(m.group(1))
        created = self.find_issue(marker)
        if created is None:
            raise GitHubError(f"issue created but not found by its marker {marker!r}: {out!r}")
        return created

    def _find_comment(self, issue: int, marker: str) -> int | None:
        rows = self._pages(f"repos/{self.repo}/issues/{issue}/comments?per_page=100")
        for row in rows:
            if marker in str(row.get("body") or ""):
                return int(row["id"])
        return None

    def upsert_comment(self, issue: int, marker: str, body: str) -> None:
        """Edit the issue comment that holds ``marker``, or add one; the marker stays in it."""
        if not marker:
            raise ValueError("an empty marker matches every comment")
        payload = json.dumps({"body": with_marker(body, marker)})
        existing = self._find_comment(issue, marker)
        if existing is not None:
            path = f"repos/{self.repo}/issues/comments/{existing}"
            self._gh(["api", "-X", "PATCH", path, "--input", "-"], stdin=payload)
            return
        path = f"repos/{self.repo}/issues/{issue}/comments"
        self._gh(["api", "-X", "POST", path, "--input", "-"], stdin=payload)

    def add_labels(self, issue: int, labels: Sequence[str]) -> None:
        """Add ``labels`` to ``issue``, creating each one first (guardrail 18)."""
        unique = self._ensure_labels(labels)
        if not unique:
            return
        args = ["issue", "edit", str(issue), "-R", self.repo]
        for name in unique:
            args += ["--add-label", name]
        self._gh(args)

    # pull requests

    def pr(self, number: int) -> PullRequest | None:
        """``gh pr view N --json number,state,mergedAt,mergeCommit,headRefName,headRefOid,
        body,isDraft``; None when it does not exist."""
        args = ["pr", "view", str(number), "-R", self.repo, "--json", PR_FIELDS]
        result = self._call(args)
        if not result.ok:
            if not result.timed_out and PR_NOT_FOUND in result.stderr.lower():
                return None
            argv = ["gh", *args]
            raise GitHubError(
                _describe(argv, result), argv=argv, stderr=result.stderr, code=result.code
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh {' '.join(args)} printed no JSON: {result.stdout!r}") from exc
        return _pull_request(data)

    def find_pr_by_head(self, branch: str) -> PullRequest | None:
        """The newest PR, in any state, whose head branch is ``branch``."""
        args = ["pr", "list", "-R", self.repo, "--head", branch, "--state", "all"]
        args += ["--limit", "100", "--json", PR_FIELDS]
        rows = self._json(args) or []
        prs = [_pull_request(r) for r in rows if str(r.get("headRefName")) == branch]
        return max(prs, key=lambda p: p.number) if prs else None

    def pr_files(self, number: int) -> list[str]:
        """Paths the PR changes, with forward slashes."""
        rows = self._pages(f"repos/{self.repo}/pulls/{number}/files?per_page=100")
        return [str(r["filename"]).replace("\\", "/") for r in rows if r.get("filename")]

    # CI

    def ci_status(self, sha: str) -> CiStatus:
        """The CI verdict for a full commit SHA, across the workflow runs whose ``headSha``
        is ``sha``.

        - ``none``: no run yet.
        - ``pending``: any run is not completed.
        - ``success``: every completed run is ``success`` or ``skipped``.
        - ``failure``: anything else.
        """
        args = ["run", "list", "-R", self.repo, "--commit", sha, "--limit", "100"]
        args += ["--json", RUN_FIELDS]
        rows = self._json(args) or []
        # exact match: a short SHA never matches GitHub's full one, so it reads as no run
        # rather than another commit's verdict (I-172)
        runs = [r for r in rows if str(r.get("headSha") or "") == sha]
        if not runs:
            return "none"
        if any(str(r.get("status") or "").lower() != "completed" for r in runs):
            return "pending"
        if all(str(r.get("conclusion") or "").lower() in _CI_SUCCESS for r in runs):
            return "success"
        return "failure"
