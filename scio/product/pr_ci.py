"""A product PR's CI as GitHub reports it (I-141): what the merge stops say before a human
merges. SCIO's own checks run in its sandbox environment; the product's CI runs on a clean
runner, so only the PR's checks show what a fresh clone gets (the graphcalc run's S6: every
check passed in the sandbox while the PR's CI failed at collection).

Reads only. A failure to read is a status (``unread``), never an exception: the callers run
at a human stop, and a paused thread must stay paused, not crash.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from scio.harness.github.client import CheckInfo, GitHubClient, GitHubError
from scio.state.models import CiStatus

CI_WAIT_S = 900.0
"""How long a stop waits for unfinished checks before it asks anyway, saying so."""
CI_POLL_S = 15.0
NO_CHECKS_GRACE_S = 120.0
"""A head commit with no checks after this long has none (GitHub starts a run within
seconds of a push)."""


def _names(checks: list[CheckInfo]) -> str:
    return "; ".join(
        f"{c.workflow + '/' if c.workflow else ''}{c.name}" + (f" ({c.link})" if c.link else "")
        for c in checks
    )


def read_ci(gh: GitHubClient, number: int, *, head: str = "") -> CiStatus:
    """The checks on PR ``number``'s head. With ``head``, checks of an older head (GitHub
    has not picked up the push yet) read as pending, never as this commit's result."""
    on = gh.pr_head(number)
    if head and on != head:
        return CiStatus(state="pending", head=head, detail=f"GitHub still shows {on[:12]}")
    checks = gh.pr_checks(number)
    if not checks:
        return CiStatus(state="none", head=on)
    failed = [c for c in checks if c.bucket in ("fail", "cancel")]
    if failed:
        return CiStatus(state="failed", head=on, detail=_names(failed))
    pending = [c for c in checks if c.bucket == "pending"]
    if pending:
        return CiStatus(state="pending", head=on, detail=_names(pending))
    return CiStatus(state="passed", head=on)


def wait_for_ci(
    gh: GitHubClient,
    number: int,
    *,
    head: str = "",
    wait_s: float | None = None,
    poll_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CiStatus:
    """Poll until the checks pass or fail, or ``wait_s`` runs out (then the last status,
    ``pending``). A failing check ends the wait at once; no checks at all ends it after
    ``NO_CHECKS_GRACE_S``."""
    wait = CI_WAIT_S if wait_s is None else wait_s
    poll = CI_POLL_S if poll_s is None else poll_s
    start = clock()
    while True:
        try:
            status = read_ci(gh, number, head=head)
        except (GitHubError, ValueError) as exc:  # ValueError: gh printed something not JSON
            return CiStatus(state="unread", head=head, detail=f"gh: {str(exc)[:300]}")
        elapsed = clock() - start
        if status.state in ("passed", "failed"):
            return status
        if status.state == "none" and elapsed >= min(NO_CHECKS_GRACE_S, wait):
            return status
        if elapsed >= wait:
            return status
        sleep(poll)
