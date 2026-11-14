"""Command line: ``scio run`` and ``scio resume`` (one task), ``scio new``, ``scio build``
and ``scio status`` (product mode), ``scio report``, ``scio dashboard``.

    scio run --spec task.md --repo <url-or-path> [--issue N] [--sandbox DIR] [--thread ID]
    scio resume --thread ID [--sandbox DIR] [--decision "approved"]
    scio report --thread ID [--out DIR]           # a task thread, or a build-* thread
    scio new --prompt "<idea>" --dir DIR [--gh owner/name]   (resume: --thread ID --decision)
    scio build --product DIR [--gh owner/name] [--thread ID] [--decision "<text>"]
    scio status --product DIR
    scio dashboard [--root DIR ...] [--port 8765]  # read-only, on 127.0.0.1 (ADR 0017)

``run``, ``resume``, ``new`` and ``build`` take ``--outcome-file PATH``: one JSON object
written there on every exit path (ADR 0019 decision 11), for the recovery supervisor.

The spec file is Markdown: the first ``#`` heading is the title, checkbox lines are the
acceptance criteria, everything else is the description.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import signal
import sys
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from scio.graph.nodes.common import acceptance_from_issue_body
from scio.graph.run import RunOutcome, resume_graph, run_graph
from scio.graph.run_context import RunContext, product_sandbox_backend
from scio.harness.core import Harness
from scio.settings import get_settings
from scio.state.models import LedgerEntry, TaskSpec

OUTCOME_VERSION = 1
"""The version of the ``--outcome-file`` object (ADR 0019 decision 11). The recovery
supervisor reads it through its own copy of the documented shape (``OutcomeFile`` in
``recovery/model.py``); the two sides share the shape, never code, so scio never imports
recovery."""

OUTCOME_COMMANDS = ("build", "new", "run", "resume")
"""The subcommands that take ``--outcome-file``."""


@dataclass
class _Outcome:
    """What a command records for its ``--outcome-file`` while it runs. ``main`` writes it
    once, on whichever path the command leaves by; without the flag nothing is written."""

    command: str
    thread: str | None = None
    status: str | None = None
    exit_reason: str | None = None
    reason: str | None = None
    resume: str | None = None

    def refuse(self, message: str, *, stdout: bool = False) -> int:
        """Print a refusal where the command always printed it (stderr, or stdout for the
        one that always went there) and record it: exit 2, nothing ran."""
        print(message, file=None if stdout else sys.stderr)
        self.status, self.reason = "refused", message
        return 2

    def ended(
        self,
        code: int,
        *,
        thread: str,
        exit_reason: str | None,
        ledger: list[LedgerEntry],
        resume: str | None,
    ) -> int:
        """A command that ran to its end. Exit 1 is ``halted``, with the exit reason and the
        last ledger text (the build prints it as ``reason:``); exit 0 is ``paused`` at a human
        stop, whose printed hint is ``resume``, or ``done``."""
        self.thread, self.resume = thread, resume
        if code == 0:
            self.status = "paused" if resume is not None else "done"
        else:
            self.status, self.exit_reason = "halted", exit_reason
            self.reason = ledger[-1].text if ledger else None
        return code

    def payload(self, exit_code: int, exception: dict[str, Any] | None = None) -> dict[str, Any]:
        if exception is not None:
            status = "crashed"
        else:  # a path that recorded nothing still says what its exit code means
            status = self.status or {0: "done", 2: "refused"}.get(exit_code, "halted")
        return {
            "version": OUTCOME_VERSION,
            "command": self.command,
            "status": status,
            "exit_code": exit_code,
            "thread": self.thread,
            "exit_reason": self.exit_reason if status == "halted" else None,
            "reason": self.reason if exception is None else None,
            "resume": self.resume if exception is None else None,
            "exception": exception,
        }

    def crashed(self, exc: BaseException) -> dict[str, Any]:
        return self.payload(_uncaught_exit_code(exc), _exception_record(exc))


def _exception_record(exc: BaseException) -> dict[str, Any]:
    """``exc`` as an uncaught traceback prints it: the type as ``module.QualName`` (bare for
    builtins and ``__main__``), the message, and the frames of ``traceback.extract_tb``,
    outermost first."""
    kind = type(exc)
    name = kind.__qualname__
    if kind.__module__ not in ("builtins", "__main__"):
        name = f"{kind.__module__}.{name}"
    try:
        message = str(exc)
    except Exception:  # traceback prints this too when str() fails
        message = "<exception str() failed>"
    frames = [
        {"path": f.filename, "line": f.lineno or 0, "function": f.name}
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    return {"type": name, "message": message, "frames": frames}


def _uncaught_exit_code(exc: BaseException) -> int:
    """The code the interpreter exits with when ``exc`` leaves ``main`` uncaught."""
    if isinstance(exc, SystemExit):
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    if isinstance(exc, KeyboardInterrupt):
        # CPython exits through SIGINT: STATUS_CONTROL_C_EXIT on Windows, a signal elsewhere
        return 0xC000013A if os.name == "nt" else -int(signal.SIGINT)
    return 1


def _write_outcome(path: Path, make: Callable[[], dict[str, Any]]) -> None:
    """Write the outcome object atomically: a temporary file beside ``path``, then
    ``os.replace``, so a reader sees the whole object or none. A failure here never changes
    the command's own result: one warning line on stderr, and the command carries on."""
    tmp: Path | None = None
    try:
        text = json.dumps(make(), indent=2, default=str) + "\n"
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception as exc:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        print(f"scio: warning: could not write the outcome file {path}: {exc}", file=sys.stderr)


class _UsageError(SystemExit):
    """argparse's exit 2, carrying the error line it printed."""

    def __init__(self, line: str, code: Any) -> None:
        super().__init__(code)
        self.line = line


class _Parser(argparse.ArgumentParser):
    """argparse, unchanged in what it prints and how it exits, but its refusal keeps the
    error line so an ``--outcome-file`` can record it."""

    def error(self, message: str) -> NoReturn:
        try:
            super().error(message)
        except SystemExit as exc:
            raise _UsageError(f"{self.prog}: error: {message}", exc.code) from None


def _usage_refusal(argv: list[str], line: str) -> None:
    """argparse refused the command line (exit 2). When it names an outcome command and an
    ``--outcome-file``, that file still gets its ``refused`` object."""
    if not argv or argv[0] not in OUTCOME_COMMANDS:
        return
    path = None
    for i, token in enumerate(argv):
        if token == "--outcome-file" and i + 1 < len(argv):
            path = argv[i + 1]
        elif token.startswith("--outcome-file="):
            path = token.split("=", 1)[1]
    if path:
        refusal = _Outcome(argv[0], status="refused", reason=line)
        _write_outcome(Path(path), lambda: refusal.payload(2))


def parse_spec(text: str, *, repo_url: str, issue: int | None) -> TaskSpec:
    title = next(
        (ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), "task"
    )
    acceptance = acceptance_from_issue_body(text)
    return TaskSpec(
        title=title, description=text, acceptance=acceptance, repo_url=repo_url, issue_number=issue
    )


def _task_resume(thread: str) -> str:
    return f"scio resume --thread {thread} --decision <text>"


def _product_resume(thread: str) -> str:
    return f'scio new --thread {thread} --decision "<text>"'


def _build_resume(product_dir: str, thread: str) -> str:
    # the thread named: the next milestone's number may name another one (I-163)
    return f'scio build --product {product_dir} --thread {thread} --decision "<text>"'


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
        print(f"resume with: {_task_resume(s.thread_id)}")


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
        print(f"resume with: {_product_resume(s.thread_id)}")


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
    if s.exit_reason is not None and s.ledger:
        # a halt says why here, not only in `scio report`'s timeline (I-116's release refusal)
        print(f"reason: {s.ledger[-1].text}")
    if s.release is not None:  # the release gate (M11)
        shipped = s.release.url or ", ".join(s.release.artifacts) or "not tagged"
        print(f"release {s.release.tag} {'green' if s.release.ok else 'FAILED'}: {shipped}")
        if not s.release.ok:
            print(s.release.report[-2000:])
    if outcome.interrupt:
        print("PAUSED for a human:")
        print(json.dumps(outcome.interrupt, indent=2, default=str))
        print(f"resume with: {_build_resume(s.product_dir, s.thread_id)}")


def _build(args: argparse.Namespace, rec: _Outcome) -> int:
    """``scio build``: start or resume the milestone loop of a bootstrapped product
    (ADR 0011). Without ``--thread``: the thread ``progress.json`` recorded the last milestone
    in, while it is unfinished (``merged`` carries a thread on into the next milestone, I-163);
    else ``build-<product>-m<next milestone>``. A paused thread is resumed, an ended one (done,
    halted, or ``merge and stop``) is left alone and a fresh thread starts (``-r2``, ``-r3``
    ... after a halt). A ``--decision`` answers a thread waiting at a stop, never starts or
    resumes one that is not: it is refused with exit 2 and nothing runs."""
    from scio.harness.github.client import GitHubClient, GitHubError
    from scio.product.build import (
        build_finished,
        build_started,
        build_thread_id,
        build_waiting,
        resume_build,
        run_build,
    )
    from scio.product.progress import PROGRESS_PATH, ProgressFile

    progress_path = args.product / PROGRESS_PATH
    if not progress_path.is_file():
        return rec.refuse(f"{progress_path} not found: run scio new first")
    progress = ProgressFile.from_json(progress_path.read_text(encoding="utf-8"))
    settings = get_settings()
    # Docker by default for product builds when it is there (M9) and the image can run the
    # product's stack, read from PRODUCT.md before any sandbox exists (I-101); the reason
    # lands in the build ledger from `load`.
    from scio.product.progress import PRODUCT_MD, declared_stack
    from scio.toolchain import get_toolchain

    # Only the stack line: a PRODUCT.md that no longer parses is the graph's to refuse, with
    # its reason, at the step that reads it (I-148), not a traceback here.
    stack = declared_stack((args.product / PRODUCT_MD).read_text(encoding="utf-8")) or "python"
    backend, _reason = product_sandbox_backend(settings, get_toolchain(stack))
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
    thread = rec.thread = base
    ctx = make(thread)
    try:
        # I-163: `merged` carries a thread into the next milestone, so the thread that recorded
        # the last milestone may be the one running now; the count of recorded milestones
        # names a thread that may not exist.
        recorded = progress.build_thread if args.thread is None else None
        if recorded and recorded != base:
            rctx = make(recorded)
            if build_started(rctx) and not build_finished(rctx):
                if build_started(ctx) and not build_finished(ctx):
                    rctx.close()
                    rec.thread = None  # two threads, and the human names the one
                    return rec.refuse(
                        f"scio build: two build threads are unfinished, {recorded} and {base}; "
                        "name the one to resume with --thread"
                    )
                ctx.close()
                ctx, thread = rctx, recorded
                rec.thread = thread
            else:
                rctx.close()
        attempt = 1
        while args.thread is None and thread.startswith(base) and build_finished(ctx):
            ctx.close()
            attempt += 1
            thread = rec.thread = f"{base}-r{attempt}"
            ctx = make(thread)
        if args.decision is not None and not build_waiting(ctx):
            # a decision answers a stop; it never starts or resumes a build that asked nothing
            where = "is not paused at a stop" if build_started(ctx) else "has no checkpoint"
            return rec.refuse(
                f"scio build: thread {thread} {where}, so --decision {args.decision!r} has "
                "nothing to answer; nothing was run. Run scio build without --decision to "
                "start or resume it, or name the waiting thread with --thread."
            )
        if build_started(ctx):
            outcome = resume_build(ctx, args.decision)
        else:
            outcome = run_build(ctx, gh_repo=gh_repo)
    except GitHubError as exc:
        # A GitHub refusal in an orchestrator node (a missing label, I-113; a rate limit) is
        # not a traceback: the checkpoint holds every finished node, and the same command
        # resumes at the one that failed.
        stopped = f"scio build stopped on GitHub: {exc}"
        hint = (
            f"Every finished step is checkpointed (thread {thread}); fix the cause and run the "
            "same scio build command again to resume."
        )
        print(f"{stopped}\n{hint}", file=sys.stderr)
        rec.status, rec.exit_reason, rec.reason, rec.resume = "halted", "github", stopped, hint
        return 1
    finally:
        ctx.close()
    _print_build(outcome)
    s = outcome.state
    return rec.ended(
        0 if s.exit_reason is None else 1,
        thread=s.thread_id,
        exit_reason=s.exit_reason,
        ledger=s.ledger,
        resume=_build_resume(s.product_dir, s.thread_id) if outcome.interrupt else None,
    )


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


def _upgrade(args: argparse.Namespace) -> int:
    """``scio upgrade``: additive, one marker commit (issue #58)."""
    from scio.aci.sandbox.local import LocalSandbox
    from scio.product.upgrade import UpgradeRefused, upgrade_product

    try:
        report = upgrade_product(LocalSandbox(args.product))
    except UpgradeRefused as exc:
        print(f"scio upgrade: {exc}", file=sys.stderr)
        return 2
    for line in report.lines():
        print(line)
    return 0


DASHBOARD_HOST = "127.0.0.1"
"""``scio dashboard`` binds the loopback interface only (ADR 0017): it shows every thread's
state and costs, and nothing on the network should see that."""
DASHBOARD_SHUTDOWN_S = 3  # seconds; uvicorn types the bound as int
"""How long a stopping dashboard waits for its connections. uvicorn's last shutdown step,
``asyncio.Server.wait_closed``, waits for every client transport, and on Windows one the
browser reset can stay attached forever, through a second Ctrl+C too (I-132); uvicorn bounds
that wait only by ``timeout_graceful_shutdown``."""


def _dashboard(args: argparse.Namespace) -> int:
    """``scio dashboard``: a live, read-only view of every thread under the roots (ADR 0017).
    A root is a directory SCIO ran in; it holds ``.scio/checkpoints.sqlite``."""
    try:
        import uvicorn

        from scio.dashboard.app import create_app
    except ImportError:
        # `uv sync --extra X` uninstalls every extra it does not name (I-124)
        print(
            "scio dashboard needs the dashboard extra: UV_SYSTEM_CERTS=1 uv sync --all-extras "
            "(or add --extra dashboard to the uv sync you use; extras it omits are removed)",
            file=sys.stderr,
        )
        return 2
    from scio.dashboard.data import CheckpointDataSource

    source = CheckpointDataSource.from_roots(args.root or [Path(".")])
    for w in source.roots():
        note = "" if w.present else " (no checkpoint DB yet: it lists once SCIO runs there)"
        print(f"root {w.name}: {w.path}{note}")
    print(f"scio dashboard on http://{DASHBOARD_HOST}:{args.port} (read-only; Ctrl+C stops it)")
    uvicorn.run(
        create_app(source),
        host=DASHBOARD_HOST,
        port=args.port,
        log_level="warning",
        timeout_graceful_shutdown=DASHBOARD_SHUTDOWN_S,
    )
    return 0


def _status(args: argparse.Namespace) -> int:
    from scio.product.build import progress_summary
    from scio.product.progress import PROGRESS_PATH

    progress_path = args.product / PROGRESS_PATH
    if not progress_path.is_file():
        print(f"{progress_path} not found: run scio new first", file=sys.stderr)
        return 2
    print(progress_summary(progress_path))
    return 0


def _new(args: argparse.Namespace, rec: _Outcome) -> int:
    """``scio new``: start or resume a product thread (ADR 0010)."""
    from scio.product import resume_product, run_product

    settings = get_settings()
    harness = _scripted_harness(settings)
    # A prompt, or a PRODUCT.md given with --spec (M11: bootstrapped as written, no interview),
    # starts a product thread; neither means resume the thread named by --thread (I-061).
    resuming = args.prompt is None and args.spec is None
    if resuming and args.thread is None:
        return rec.refuse(
            "scio new needs --prompt (or --spec) and --dir to start, or --thread to resume"
        )
    if not resuming and args.dir is None:
        return rec.refuse("scio new --prompt/--spec also needs --dir (the target directory)")
    spec_md = args.spec.read_text(encoding="utf-8") if args.spec is not None else None
    thread = rec.thread = args.thread or f"new-{uuid.uuid4().hex[:8]}"
    target: Path
    if resuming:
        # The thread's checkpoint knows where the product lives (I-076): a resume that omits
        # --dir, or names another one, continues in the recorded directory.
        from scio.product.graph import product_target_dir

        recorded = product_target_dir(settings.checkpoint_dsn, thread)
        if recorded is None:
            return rec.refuse(
                f"no product thread {thread!r} to resume (start with --prompt and --dir)",
                stdout=True,
            )
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
            prompt = args.prompt or f"bootstrap from {args.spec}"
            outcome = run_product(ctx, prompt=prompt, gh_repo=args.gh, spec_md=spec_md)
    finally:
        ctx.close()
    _print_product(outcome)
    s = outcome.state
    return rec.ended(
        0 if s.exit_reason is None else 1,
        thread=s.thread_id,
        exit_reason=s.exit_reason,
        ledger=s.ledger,
        resume=_product_resume(s.thread_id) if outcome.interrupt else None,
    )


def _add_outcome_file(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--outcome-file",
        type=Path,
        default=None,
        help="write how the command ended here as one JSON object, on every exit path "
        "(ADR 0019); for the recovery supervisor",
    )


def _parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="scio")
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
    new.add_argument(
        "--spec",
        type=Path,
        default=None,
        help="a PRODUCT.md to bootstrap from as written, without the interview (M11)",
    )
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
    upgrade = sub.add_parser(
        "upgrade", help="product mode: bring an older product up to the current product bar"
    )
    upgrade.add_argument("--product", type=Path, required=True)
    dash = sub.add_parser(
        "dashboard", help="a live, read-only view of every thread (needs the dashboard extra)"
    )
    dash.add_argument(
        "--root",
        type=Path,
        action="append",
        default=None,
        help="a directory SCIO ran in (repeatable; default: the current directory)",
    )
    dash.add_argument("--port", type=int, default=8765)
    for parser in (run, res, new, build):
        _add_outcome_file(parser)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _parser()
    try:
        args = ap.parse_args(argv)
    except _UsageError as exc:
        _usage_refusal(sys.argv[1:] if argv is None else argv, exc.line)
        raise
    rec = _Outcome(command=args.cmd)
    path: Path | None = getattr(args, "outcome_file", None)
    try:
        code = _dispatch(args, rec)  # one call site: the traceback is the same with or without
    except BaseException as exc:
        # The file, then the same exception: the traceback and the exit code stay as they
        # were, and nothing here turns a crash into a clean stop (ADR 0019 decision 11).
        if path is not None:
            _write_outcome(path, functools.partial(rec.crashed, exc))
        raise
    if path is not None:
        _write_outcome(path, lambda: rec.payload(code))
    return code


def _dispatch(args: argparse.Namespace, rec: _Outcome) -> int:
    if args.cmd == "dashboard":
        return _dashboard(args)
    if args.cmd == "new":
        return _new(args, rec)
    if args.cmd == "build":
        return _build(args, rec)
    if args.cmd == "status":
        return _status(args)
    if args.cmd == "upgrade":
        return _upgrade(args)
    return _task(args, rec)


def _task(args: argparse.Namespace, rec: _Outcome) -> int:
    """``scio run``, ``scio resume`` (one task) and ``scio report``."""
    settings = get_settings()
    harness = _scripted_harness(settings)
    thread = rec.thread = args.thread or f"run-{uuid.uuid4().hex[:8]}"
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
        from scio.product.graph import load_product_state

        bootstrap = load_product_state(settings.checkpoint_dsn, thread)
        if bootstrap is not None:
            # A `scio new` thread (ADR 0010): the interview, the drafts, the bootstrap; the
            # task model cannot read it (I-108).
            from scio.product.report import build_bootstrap_report, write_product_report

            boot = build_bootstrap_report(bootstrap, entries)
            json_path, md_path = write_product_report(boot, args.out)
            print(boot.to_markdown())
        elif _is_build_thread(ctx):
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
    s = outcome.state
    return rec.ended(
        0 if s.exit_reason is None or s.exit_reason.value == "success" else 1,
        thread=s.thread_id,
        exit_reason=s.exit_reason.value if s.exit_reason else None,
        ledger=s.task_ledger,
        resume=_task_resume(s.thread_id) if outcome.interrupt else None,
    )


if __name__ == "__main__":
    sys.exit(main())
