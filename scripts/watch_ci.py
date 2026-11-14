"""Wait for the CI run of a pushed head and watch it to the end (issue #88).

    uv run python scripts/watch_ci.py [--sha SHA] [--branch BRANCH] [--workflow ci]

``gh pr checks --watch`` right after a push either exits at once ("no checks reported", the
run is not registered yet) or reports the previous head's finished checks, which is I-104's
mistake: a green verdict on code that was not tested. This waits for the run whose
``headSha`` is the pushed commit (``git rev-parse HEAD`` by default), then
``gh run watch <id> --exit-status``, so every job of that run (tests, evals, docker) is waited
for. Exits with the run's verdict: 0 when it succeeded, 1 otherwise, 2 when no run for the
head appeared in time.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

APPEAR_TIMEOUT_S = 300
POLL_S = 10


def _out(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True).stdout.strip()


def run_for_head(branch: str, sha: str, workflow: str) -> dict[str, object] | None:
    """The newest run of ``workflow`` on ``branch`` whose head is ``sha``, if any yet."""
    rows = json.loads(
        _out(
            [
                "gh",
                "run",
                "list",
                "--branch",
                branch,
                "--workflow",
                workflow,
                "--limit",
                "20",
                "--json",
                "databaseId,headSha,status,conclusion,url",
            ]
        )
        or "[]"
    )
    return next((r for r in rows if str(r.get("headSha")) == sha), None)


FULL_SHA_LENGTH = 40


def full_sha(rev: str | None) -> str:
    """The full SHA of ``rev`` (default ``HEAD``).

    GitHub reports full SHAs, so a short one given on the command line would never match
    and the watcher would time out (I-172). A full 40-hex SHA is taken as given and never
    looked up here. GitHub may have made that commit (`gh pr update-branch`), so it need
    not exist in this clone (I-199)."""
    if rev and len(rev) == FULL_SHA_LENGTH and all(c in "0123456789abcdefABCDEF" for c in rev):
        return rev.lower()
    return _out(["git", "rev-parse", "--verify", f"{rev or 'HEAD'}^{{commit}}"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sha", help="the pushed commit, full or short (default: HEAD)")
    ap.add_argument("--branch", help="the pushed branch (default: the current one)")
    ap.add_argument("--workflow", default="ci", help="the workflow's name (default: ci)")
    args = ap.parse_args()
    try:
        sha = full_sha(args.sha)
    except subprocess.CalledProcessError:
        print(
            f"watch_ci: cannot resolve {args.sha or 'HEAD'} here; `git fetch` first, or pass "
            "the full 40-character SHA (I-199)",
            file=sys.stderr,
        )
        return 2
    branch = args.branch or _out(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    deadline = time.monotonic() + APPEAR_TIMEOUT_S
    run = run_for_head(branch, sha, args.workflow)
    while run is None:
        if time.monotonic() > deadline:
            print(f"no {args.workflow} run for {sha[:12]} on {branch} within {APPEAR_TIMEOUT_S} s")
            return 2
        time.sleep(POLL_S)
        run = run_for_head(branch, sha, args.workflow)
    run_id = str(run["databaseId"])
    print(f"watching run {run_id} for {sha[:12]} on {branch}: {run.get('url')}", flush=True)
    watched = subprocess.run(
        ["gh", "run", "watch", run_id, "--exit-status", "--interval", "30"],
        stdout=subprocess.DEVNULL,
    )
    final = json.loads(_out(["gh", "run", "view", run_id, "--json", "conclusion,jobs"]))
    for job in final.get("jobs", []):
        print(f"  {job.get('name')}: {job.get('conclusion')}")
    print(f"run {run_id}: {final.get('conclusion')}")
    return 0 if watched.returncode == 0 and final.get("conclusion") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
