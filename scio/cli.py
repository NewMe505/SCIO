"""Command line: ``scio run`` and ``scio resume``.

    scio run --spec task.md --repo <url-or-path> [--issue N] [--sandbox DIR] [--thread ID]
    scio resume --thread ID [--sandbox DIR] [--decision "approved"]
    scio report --thread ID [--out DIR]

The spec file is Markdown: the first ``#`` heading is the title, checkbox lines are the
acceptance criteria, everything else is the description.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import RunOutcome, resume_graph, run_graph
from scio.graph.run_context import RunContext
from scio.harness.core import Harness
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


def _print_product(outcome: Any) -> None:
    s = outcome.state
    print(f"product thread {s.thread_id} phase={s.phase} exit={s.exit_reason}")
    print(f"tokens {s.budgets.tokens_used} usd {s.budgets.usd_used:.4f} dir {s.target_dir}")
    if s.spec:
        print(f"  {s.spec.name}: {len(s.spec.features)} features, stack {s.spec.stack}")
    if s.bootstrap_commit:
        print(f"  bootstrap commit {s.bootstrap_commit[:12]}")
    if s.repo_url:
        print(f"  published {s.repo_url}")
    if outcome.interrupt:
        print("PAUSED for a human:")
        print(json.dumps(outcome.interrupt, indent=2, default=str))
        print(f'resume with: scio new --thread {s.thread_id} --decision "<text>"')


def _new(args: argparse.Namespace) -> int:
    """``scio new``: start or resume a product thread (ADR 0010)."""
    from scio.product import resume_product, run_product

    settings = get_settings()
    harness = None
    if settings.provider_override == "fake" and os.environ.get("SCIO_FAKE_SCRIPT") == "1":
        from evals.fake_scripts import ScriptedResponder
        from scio.harness.providers.fake import FakeProvider

        harness = Harness.from_settings(settings)
        harness.providers.register("fake", FakeProvider(responder=ScriptedResponder()))
    # A prompt starts a product thread (optionally under a chosen --thread id); no prompt
    # means resume the thread named by --thread (I-061).
    resuming = args.prompt is None
    if resuming and args.thread is None:
        print("scio new needs --prompt and --dir to start, or --thread to resume", file=sys.stderr)
        return 2
    if not resuming and args.dir is None:
        print("scio new --prompt also needs --dir (the target directory)", file=sys.stderr)
        return 2
    thread = args.thread or f"new-{uuid.uuid4().hex[:8]}"
    target = args.dir or Path(".scio") / "products" / thread
    ctx = RunContext.create(
        thread_id=thread,
        sandbox_root=target,
        tier="T1",  # two Opus calls and a scaffold; T1 caps are ample runaway guards
        tier_pin="T1",
        settings=settings,
        harness=harness,
    )
    try:
        if resuming:
            outcome = resume_product(ctx, args.decision)
        else:
            outcome = run_product(ctx, prompt=args.prompt, gh_repo=args.gh)
    finally:
        ctx.close()
    _print_product(outcome)
    return 0 if outcome.state.exit_reason is None else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scio")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--repo", required=True, help="git URL or local path of the target repository")
    run.add_argument("--issue", type=int, default=None)
    run.add_argument("--sandbox", type=Path, default=None)
    run.add_argument("--thread", default=None)
    run.add_argument(
        "--tier",
        default=None,
        choices=["T0", "T1", "T2"],
        help="pin the tier: the router is skipped and budgets are sized for it (I-053); "
        "default: the router decides and budgets follow its decision",
    )
    res = sub.add_parser("resume")
    res.add_argument("--thread", required=True)
    res.add_argument("--sandbox", type=Path, default=None)
    res.add_argument("--decision", default=None)
    res.add_argument("--tier", default=None, choices=["T0", "T1", "T2"])
    rep = sub.add_parser("report", help="write the observability report for a run")
    rep.add_argument("--thread", required=True)
    rep.add_argument("--sandbox", type=Path, default=None)
    rep.add_argument("--out", type=Path, default=Path(".scio") / "reports")
    new = sub.add_parser("new", help="product mode: a prompt becomes a bootstrapped repository")
    new.add_argument("--prompt", default=None, help="the product idea (omit when resuming)")
    new.add_argument("--dir", type=Path, default=None, help="target directory (created if absent)")
    new.add_argument("--gh", default=None, help="owner/name to publish to, after confirmation")
    new.add_argument("--thread", default=None, help="resume this product thread")
    new.add_argument("--decision", default=None, help="answer to the pending question")
    args = ap.parse_args(argv)
    if args.cmd == "new":
        return _new(args)

    settings = get_settings()
    harness = None
    if settings.provider_override == "fake" and os.environ.get("SCIO_FAKE_SCRIPT") == "1":
        # Offline demo: drive the Fake provider with the scripted eval behaviour.
        from evals.fake_scripts import ScriptedResponder
        from scio.harness.providers.fake import FakeProvider

        harness = Harness.from_settings(settings)
        harness.providers.register("fake", FakeProvider(responder=ScriptedResponder()))
    thread = args.thread or f"run-{uuid.uuid4().hex[:8]}"
    sandbox = args.sandbox or Path(".scio") / "sandbox" / thread
    pinned = getattr(args, "tier", None)
    ctx = RunContext.create(
        thread_id=thread,
        sandbox_root=sandbox,
        tier=pinned or "T0",
        tier_pin=pinned,  # I-053: an explicit --tier pins the route, not only the budget
        settings=settings,
        harness=harness,
    )
    if args.cmd == "report":
        from scio.graph.report import build_report, load_state, write_report
        from scio.harness.accounting import CostLedger

        state = load_state(ctx)
        entries = CostLedger.load_entries(Path(".scio") / "ledger" / f"{thread}.jsonl")
        report = build_report(state, entries)
        json_path, md_path = write_report(report, args.out)
        print(report.to_markdown())
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")
        return 0
    if args.cmd == "run":
        task = parse_spec(
            args.spec.read_text(encoding="utf-8"), repo_url=args.repo, issue=args.issue
        )
        outcome = run_graph(ctx, task)
    else:
        outcome = resume_graph(ctx, args.decision)
    _print(outcome)
    return (
        0
        if outcome.state.exit_reason is None or outcome.state.exit_reason.value == "success"
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
