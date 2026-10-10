"""Command line: ``scio run`` and ``scio resume``.

    scio run --spec task.md --repo <url-or-path> [--issue N] [--sandbox DIR] [--thread ID]
    scio resume --thread ID [--sandbox DIR] [--decision "approved"]

The spec file is Markdown: the first ``#`` heading is the title, checkbox lines are the
acceptance criteria, everything else is the description.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import RunOutcome, resume_t0, run_t0
from scio.graph.run_context import RunContext
from scio.settings import get_settings
from scio.state.models import TaskSpec


def parse_spec(text: str, *, repo_url: str, issue: int | None) -> TaskSpec:
    title = next(
        (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "task"
    )
    acceptance = acceptance_from_issue_body(text)
    return TaskSpec(
        title=title, description=text, acceptance=acceptance, repo_url=repo_url, issue_number=issue
    )


def _print(outcome: RunOutcome) -> None:
    s = outcome.state
    exit_name = s.exit_reason.value if s.exit_reason else None
    print(f"thread {s.thread_id} phase={s.phase} exit={exit_name}")
    print(
        f"tokens {s.budgets.tokens_used} usd {s.budgets.usd_used:.4f} "
        f"branch {s.github.branch} pr {s.github.pr_number}"
    )
    for f in s.feature_list:
        print(f"  {f.id} {f.status.value} {f.description}")
    if outcome.interrupt:
        print("PAUSED for a human:")
        print(json.dumps(outcome.interrupt, indent=2, default=str))
        print(f"resume with: scio resume --thread {s.thread_id} --decision <text>")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--repo", required=True, help="git URL or local path of the target repository")
    run.add_argument("--issue", type=int, default=None)
    run.add_argument("--sandbox", type=Path, default=None)
    run.add_argument("--thread", default=None)
    run.add_argument("--tier", default="T0")
    res = sub.add_parser("resume")
    res.add_argument("--thread", required=True)
    res.add_argument("--sandbox", type=Path, default=None)
    res.add_argument("--decision", default=None)
    res.add_argument("--tier", default="T0")
    args = ap.parse_args(argv)

    settings = get_settings()
    thread = args.thread or f"run-{uuid.uuid4().hex[:8]}"
    sandbox = args.sandbox or Path(".scio") / "sandbox" / thread
    ctx = RunContext.create(
        thread_id=thread, sandbox_root=sandbox, tier=args.tier, settings=settings
    )
    if args.cmd == "run":
        task = parse_spec(
            args.spec.read_text(encoding="utf-8"), repo_url=args.repo, issue=args.issue
        )
        outcome = run_t0(ctx, task)
    else:
        outcome = resume_t0(ctx, args.decision)
    _print(outcome)
    return (
        0
        if outcome.state.exit_reason is None or outcome.state.exit_reason.value == "success"
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
