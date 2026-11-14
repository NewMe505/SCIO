"""GitHub through ``gh``: recovery issues, comments, PRs and CI by SHA. OWNER: wave-1 agent C.

The rules carried over from SCIO's ``GitHubClient`` (``scio/harness/github/client.py``)
and ``scripts/watch_ci.py``:
- Create a label before using it (guardrail 18).
- Find an issue by listing the repository's issues through the paginated REST API and
  matching a body marker. Never use search (guardrail 48).
- Treat a merge as real only when ``mergedAt`` is set (guardrail 8).
- Read CI by the commit's full SHA (I-104, I-172).
"""

from __future__ import annotations

from collections.abc import Sequence

from recovery.commands import Runner, run_command
from recovery.model import CiStatus, PullRequest

RECOVERY_LABELS: tuple[tuple[str, str, str], ...] = (
    ("bug", "d73a4a", "Something isn't working"),
    ("live-run", "5319e7", "Found by a live product run"),
    ("recovery", "0e8a16", "Opened by the external recovery supervisor (ADR 0019)"),
    ("needs-human", "b60205", "The recovery supervisor escalated this to the owner"),
)


class GitHubError(Exception):
    """A ``gh`` call failed. The message holds the argv and gh's stderr."""


def marker_for(signature: str) -> str:
    """``<!-- scio-recovery:<signature> -->``: the issue body marker that dedupes incidents."""
    raise NotImplementedError


def status_marker(incident_id: str) -> str:
    """``<!-- scio-recovery-status:<incident_id> -->``: the marker of the one comment the
    supervisor keeps up to date on an issue."""
    raise NotImplementedError


class GitHub:
    def __init__(self, repo: str, runner: Runner = run_command) -> None:
        self.repo = repo
        self.runner = runner

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Create the label if the repository lacks it; leave an existing one alone."""
        raise NotImplementedError

    def find_issue(self, marker: str) -> int | None:
        """The number of an issue, open or closed and not a PR, whose body contains
        ``marker``. Reads every page."""
        raise NotImplementedError

    def ensure_issue(self, title: str, body: str, labels: Sequence[str], marker: str) -> int:
        """Return the existing issue with ``marker``, or create it with the marker in its
        body, after ensuring every label in ``RECOVERY_LABELS`` it uses."""
        raise NotImplementedError

    def upsert_comment(self, issue: int, marker: str, body: str) -> None:
        """Edit the issue comment that holds ``marker``, or add one; the marker stays in it."""
        raise NotImplementedError

    def add_labels(self, issue: int, labels: Sequence[str]) -> None:
        raise NotImplementedError

    def pr(self, number: int) -> PullRequest | None:
        """``gh pr view N --json number,state,mergedAt,mergeCommit,headRefName,headRefOid,
        body,isDraft``; None when it does not exist."""
        raise NotImplementedError

    def find_pr_by_head(self, branch: str) -> PullRequest | None:
        """The newest PR, in any state, whose head branch is ``branch``."""
        raise NotImplementedError

    def pr_files(self, number: int) -> list[str]:
        """Paths the PR changes, with forward slashes."""
        raise NotImplementedError

    def ci_status(self, sha: str) -> CiStatus:
        """The CI verdict for a full commit SHA, across the workflow runs whose ``headSha``
        is ``sha``.

        - ``none``: no run yet.
        - ``pending``: any run is not completed.
        - ``success``: every completed run is ``success`` or ``skipped``.
        - ``failure``: anything else.
        """
        raise NotImplementedError
