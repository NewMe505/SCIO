"""Command line: ``scio run`` and ``scio resume`` (one task), ``scio new``, ``scio build``
and ``scio status`` (product mode), ``scio report``.

    scio run --spec task.md --repo <url-or-path> [--issue N] [--sandbox DIR] [--thread ID]
    scio resume --thread ID [--sandbox DIR] [--decision "approved"]
    scio report --thread ID [--out DIR]           # a task thread, or a build-* thread
    scio new --prompt "<idea>" --dir DIR [--gh owner/name]   (resume: --thread ID --decision)
    scio build --product DIR [--gh owner/name] [--thread ID] [--decision "<text>"]
    scio status --product DIR

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
from scio.graph.run_context import RunContext, product_sandbox_backend
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


def _scripted_harness(settings: Any) -> Harness | None:
    """Offline demo: drive the Fake provider with the scripted eval behaviour."""
    if settings.provider_override == "fake" and os.environ.get("SCIO_FAKE_SCRIPT") == "1":
        from evals.fake_scripts import ScriptedResponder
        from scio.harness.providers.fake import FakeProvider

        harness = Harness.from_settings(settings)
        harness.providers.register("fake", FakeProvider(responder=ScriptedResponder()))
        return harness
    return None


def _print_build(outcome: Any) -> None:
    s = outcome.state
    print(f"build thread {s.thread_id} phase={s.phase} exit={s.exit_reason} dir {s.product_dir}")
    print(
        f"milestones done {', '.join(s.milestones_done) or '-'} · current "
        f"{s.milestone.id + ' ' + s.milestone.title if s.milestone else '-'}"
    )
    for r in s.feature_runs:
        print(
            f"  {r.feature_id} a{r.attempt} {r.tier.value} {r.status} exit={r.exit_reason} "
            f"${r.usd:.4f} escalations={r.escalations_resolved}/{r.escalations}"
        )
    from scio.product.build import total_usd

    print(f"usd {total_usd(s):.4f} (orchestrator {s.budgets.usd_used:.4f})")
    if outcome.interrupt:
        print("PAUSED for a human:")
        print(json.dumps(outcome.interrupt, indent=2, default=str))
        print(f'resume with: scio build --product {s.product_dir} --decision "<text>"')


def _build(args: argparse.Namespace) -> int:
    """``scio build``: start or resume the milestone loop of a bootstrapped product
    (ADR 0011). The thread is ``build-<product>-m<next milestone>`` unless ``--thread`` names
    another; a paused thread is resumed, an ended one (done, halted, or ``merge and stop``)
    is left alone and a fresh thread starts (``-r2``, ``-r3`` ... after a halt)."""
    from scio.harness.github.client import GitHubClient
    from scio.product.build import (
        build_finished,
        build_started,
        build_thread_id,
        resume_build,
        run_build,
    )
    from scio.product.progress import PROGRESS_PATH, ProgressFile

    progress_path = args.product / PROGRESS_PATH
    if not progress_path.is_file():
        print(f"{progress_path} not found: run scio new first", file=sys.stderr)
        return 2
    progress = ProgressFile.from_json(progress_path.read_text(encoding="utf-8"))
    settings = get_settings()
    # Docker by default for product builds when it is there (M9); the reason lands in the
    # build ledger from `load`.
    backend, _reason = product_sandbox_backend(settings)
    settings = settings.model_copy(update={"sandbox_backend": backend})
    gh_repo = args.gh or settings.gh_repo
    harness = _scripted_harness(settings)
    github = GitHubClient(gh_repo) if gh_repo else None

    def make(thread: str) -> RunContext:
        return RunContext.create(
            thread_id=thread,
            sandbox_root=args.product,
            tier="T1",  # the orchestrator's own calls (planner, gate); features get their own
            tier_pin="T1",
            settings=settings,
            harness=harness,
            github=github,
        )

    base = args.thread or build_thread_id(progress.product, progress.next_milestone_number())
    thread = base
    ctx = make(thread)
    try:
        attempt = 1
        while args.thread is None and build_finished(ctx):
            ctx.close()
            attempt += 1
            thread = f"{base}-r{attempt}"
            ctx = make(thread)
        if build_started(ctx):
            outcome = resume_build(ctx, args.decision)
        else:
            outcome = run_build(ctx, gh_repo=gh_repo)
    finally:
        ctx.close()
    _print_build(outcome)
    return 0 if outcome.state.exit_reason is None else 1


def _is_build_thread(ctx: RunContext) -> bool:
    """True when the thread's checkpoint holds a ``ProductBuildState`` (it has feature runs).

    Read through the build graph: ``get_state`` only returns the channels the reading graph
    declares, so a task-graph read would never show ``feature_runs``."""
    from scio.graph.checkpointer import open_checkpointer
    from scio.product.build import build_product_build_graph

    config = {"configurable": {"thread_id": ctx.thread_id}}
    with open_checkpointer(ctx.settings.checkpoint_dsn) as saver:
        graph: Any = build_product_build_graph(ctx, saver).compile(checkpointer=saver)
        values = graph.get_state(config).values
    if not values:
        raise LookupError(f"no checkpoint for thread {ctx.thread_id!r}")
    return "feature_runs" in values


def _status(args: argparse.Namespace) -> int:
    from scio.product.build import progress_summary
    from scio.product.progress import PROGRESS_PATH

    progress_path = args.product / PROGRESS_PATH
    if not progress_path.is_file():
        print(f"{progress_path} not found: run scio new first", file=sys.stderr)
        return 2
    print(progress_summary(progress_path))
    return 0


def _new(args: argparse.Namespace) -> int:
    """``scio new``: start or resume a product thread (ADR 0010)."""
    from scio.product import resume_product, run_product

    settings = get_settings()
    harness = _scripted_harness(settings)
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
    target: Path
    if resuming:
        # The thread's checkpoint knows where the product lives (I-076): a resume that omits
        # --dir, or names another one, continues in the recorded directory.
        from scio.product.graph import product_target_dir

        recorded = product_target_dir(settings.checkpoint_dsn, thread)
        if recorded is None:
            print(f"no product thread {thread!r} to resume (start with --prompt and --dir)")
            return 2
        target = Path(recorded)
        if args.dir is not None and Path(args.dir).resolve() != target.resolve():
            print(f"note: thread {thread} was started in {target}; --dir {args.dir} ignored")
    else:
        target = args.dir
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
    build = sub.add_parser("build", help="product mode: run the next milestone of a product")
    build.add_argument("--product", type=Path, required=True, help="the bootstrapped repository")
    build.add_argument("--gh", default=None, help="owner/name for milestones, issues and PRs")
    build.add_argument("--thread", default=None, help="build thread (default build-<product>)")
    build.add_argument("--decision", default=None, help="answer to the pending question")
    status = sub.add_parser("status", help="product mode: progress of a product")
    status.add_argument("--product", type=Path, required=True)
    args = ap.parse_args(argv)
    if args.cmd == "new":
        return _new(args)
    if args.cmd == "build":
        return _build(args)
    if args.cmd == "status":
        return _status(args)

    settings = get_settings()
    harness = _scripted_harness(settings)
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

        entries = CostLedger.load_entries(Path(".scio") / "ledger" / f"{thread}.jsonl")
        if _is_build_thread(ctx):
            # A product build thread (ADR 0011): milestones and feature runs, not one task.
            # Decided by the checkpoint's shape, not the name: a feature run inside a build
            # is a task thread named build-<product>-m<n>-<pk>-a<k> (I-066).
            from scio.product.build import load_build_state
            from scio.product.report import build_product_report, write_product_report

            product = build_product_report(load_build_state(ctx), entries)
            json_path, md_path = write_product_report(product, args.out)
            print(product.to_markdown())
        else:
            state = load_state(ctx)
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
