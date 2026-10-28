"""The dashboard's contract (issue #68, ADR 0017), fixed before any layer was built.

The data layer (:mod:`scio.dashboard.data`) implements :class:`DataSource` over the checkpoint
DBs; the API (:mod:`scio.dashboard.app`, ``create_app(source, *, poll_interval_s=1.0,
static_dir=None, allowed_hosts=("127.0.0.1", "localhost"))``) reads only through a
:class:`DataSource` and serves these models as JSON
(``model_dump(mode="json")``); the page (``scio/dashboard/static/``) renders that JSON. The
example payloads in ``tests/fixtures/dashboard/`` validate against these models
(``tests/unit/test_dashboard_contract.py``), so the API and page tests need no real data.

HTTP surface (every route is a read; there is no write route)::

    GET /                                -> static/index.html
    GET /static/{file}                   -> static/app.js, static/style.css
    GET /api/roots                       -> list[Workspace]
    GET /api/threads[?root=NAME]         -> list[ThreadSummary], newest update first
    GET /api/threads/{root}/{thread_id}  -> ThreadDetail; 404 for an unknown root or thread
    GET /api/graphs                      -> list[GraphDiagram]
    WS  /ws                              -> ChangeEvent: one hello with nothing in it on
                                            connect, then one per poll that found a change
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from scio.graph.report import RunReport
from scio.product.progress import ProgressFile
from scio.product.report import BootstrapReport, ProductReport

ThreadKind = Literal["build", "bootstrap", "task"]
"""``build``: a ``scio build`` thread (its channels hold ``feature_runs`` or ``product_dir``,
the build's required input, so a build that stopped before its first feature still counts);
``bootstrap``: a ``scio new`` thread (``target_dir``); ``task``: every other thread, a
``scio run`` or a feature run inside a build. Decided by the checkpoint's channels, never by
the name: the same rules as ``scio report`` (I-066, I-108)."""

ThreadStatus = Literal["in_progress", "waiting", "crashed", "done", "halted", "error"]
"""Derived once, by the data layer, so the API and the page never re-derive it, in this order:
``error`` when the checkpoint could not be read with its kind's model; ``crashed`` when the
latest checkpoint has a pending ``__error__`` write (the process died in a node; a resume
clears it); ``waiting`` when a stop is pending (an ``__interrupt__`` write); ``halted`` when
``exit_reason`` is set and is not ``success``, or the phase is ``halted``; ``done`` when the
phase is ``done``; otherwise ``in_progress``. A process killed without an exception also
reads ``in_progress``: ``updated_at`` says how long it has been quiet."""


class Workspace(BaseModel):
    """One ``--root``: a directory SCIO ran in, holding ``.scio/checkpoints.sqlite`` and the
    cost ledgers ``.scio/ledger/<thread>.jsonl`` (the defaults of ``Settings.checkpoint_dsn``
    and ``RunContext.create``)."""

    name: str
    """The URL segment: the directory's name, made unique among the roots with ``-2``, ``-3``."""
    path: str
    checkpoints: str
    """The checkpoint DB file."""
    ledger_dir: str
    present: bool
    """The checkpoint DB exists; a root without one lists no threads."""


class StopInfo(BaseModel):
    """What a paused thread waits on: the ``__interrupt__`` write of its latest checkpoint."""

    node: str | None = None
    """The node that asked (the checkpoint's ``branch:to:<node>`` channel), when known."""
    question: str = ""
    """``payload["question"]``, or the interrupt value itself when it is a string."""
    interrupt_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    """The interrupt value; a value that is not a dict is carried as ``{"value": ...}``."""


class ThreadSummary(BaseModel):
    """One row of the threads list."""

    root: str
    """``Workspace.name`` of the root the thread lives in."""
    thread_id: str
    kind: ThreadKind
    status: ThreadStatus
    graph: str
    """The registered diagram (``GraphDiagram.name``) this thread runs: ``product_build``,
    ``product_new``, or a task's tier ``t0``/``t1``/``t2`` (``t0`` before it is routed)."""
    phase: str
    exit_reason: str | None = None
    next_nodes: list[str] = Field(default_factory=list)
    """Nodes the graph runs next (the checkpoint's ``branch:to:<node>`` channels): the
    current step, the node that asked when paused, empty once the thread ended."""
    checkpoint_id: str
    updated_at: datetime | None = None
    """The latest checkpoint's ``ts``."""
    title: str = ""
    """A task: its title; a build: ``M1: <milestone title>`` (the product name before a
    milestone is planned and after the build ends, when the milestone is cleared); a
    bootstrap: the product name, else the prompt's first 80 chars."""
    product: str | None = None
    """Builds and bootstraps: the spec's name; a feature run: its build's product."""
    parent_thread: str | None = None
    """A feature run: its build thread (``build-x-m1`` for ``build-x-m1-p2-a1``) when that
    thread is in the same root."""
    usd: float = 0.0
    """The figure ``scio report`` prints for the thread: a build's total (features plus
    orchestrator), a bootstrap's ``usd``, a task's ``usd_used``."""
    waiting: StopInfo | None = None
    crash: str | None = None
    """The pending ``__error__`` write's value as text (status ``crashed``), at most 2,000
    characters."""
    error: str | None = None
    """Why the checkpoint could not be read; the row still lists (status ``error``)."""


class FeatureLink(BaseModel):
    """A build's feature run and its own thread."""

    feature_id: str
    attempt: int
    thread_id: str
    status: str
    """The run's ``FeatureRun.status``: running, passed, failed or skipped."""
    present: bool
    """The feature thread has a checkpoint in the same root (the page links it only then)."""


class TimelineItem(BaseModel):
    at: datetime
    source: str
    """The role that wrote the entry, or ``human``."""
    kind: str
    """``fact``, ``guess`` or ``decision``."""
    text: str


class ThreadDetail(BaseModel):
    """One thread's page. Exactly one of ``build``, ``bootstrap`` and ``task`` is set, by
    ``summary.kind`` (``None`` for all three when ``summary.status`` is ``error``); they are
    the reports ``scio report`` writes, built by the same functions."""

    summary: ThreadSummary
    build: ProductReport | None = None
    bootstrap: BootstrapReport | None = None
    task: RunReport | None = None
    timeline: list[TimelineItem] = Field(default_factory=list)
    """The thread's own ledger (``ledger`` or ``task_ledger``), oldest first."""
    cost_by_role: dict[str, float] = Field(default_factory=dict)
    """Role -> usd from the cost ledgers, largest first: a build sums its own ledger and every
    feature thread's; other threads their own ledger."""
    progress: ProgressFile | None = None
    """The product's ``.scio/product/progress.json`` (a build's ``product_dir``, a bootstrap's
    ``target_dir``); ``None`` for a task, or when the file is absent or unreadable."""
    # ---- build threads ----
    milestone_title: str | None = None
    milestone_features: list[str] = Field(default_factory=list)
    """The current milestone's planned feature ids, in order: rows not yet run included."""
    feature_threads: list[FeatureLink] = Field(default_factory=list)
    """One per ``FeatureRun``, oldest first."""
    cost_by_feature: dict[str, float] = Field(default_factory=dict)
    """Feature id -> usd over every attempt."""
    integration: list[str] = Field(default_factory=list)
    """The integration checks' output lines; empty before they ran."""
    integration_ok: bool | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    # ---- bootstrap threads ----
    questions: list[str] = Field(default_factory=list)
    """The interview's questions (``ProductState.questions``)."""
    revision_notes: list[str] = Field(default_factory=list)
    """The human's notes on each rejected draft, one per revision round."""
    # ---- task threads ----
    verify_lines: list[str] = Field(default_factory=list)
    """The last 60 lines of the ``verify:last`` artifact; empty without one."""


class GraphDiagram(BaseModel):
    name: str
    """The registry name (``scio.graph.registry.GRAPHS``)."""
    mermaid: str
    """``render_mermaid`` of the compiled graph: the text ARCHITECTURE.md's generated
    sections carry."""
    nodes: list[str]
    """The graph's node ids without ``__start__`` and ``__end__``: the ids the Mermaid text
    uses, so ``ThreadSummary.next_nodes`` can be lit on it."""


class ThreadVersion(BaseModel):
    """What the WebSocket compares between polls."""

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str
    """The thread's latest checkpoint id."""
    writes: int = 0
    """The thread's rows in the ``writes`` table. A stop arrives as a pending write on a
    checkpoint that already exists, so the checkpoint id alone never shows a thread starting
    to wait; nor a crash, nor a parallel task's progress."""
    ledger_bytes: int = 0
    """The size of the thread's cost ledger file; 0 when there is none."""


class ThreadRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: str
    thread_id: str


class ChangeEvent(BaseModel):
    """Pushed on ``/ws``. The page refetches the threads list, and the open thread when it is
    in ``changed`` or is the ``parent_thread`` of a thread in it."""

    changed: list[ThreadRef] = Field(default_factory=list)
    """Threads whose :class:`ThreadVersion` differs from the last poll, new threads included."""
    removed: list[ThreadRef] = Field(default_factory=list)
    at: datetime


class UnknownRoot(KeyError):
    """No root has this name (the API answers 404)."""


class UnknownThread(KeyError):
    """The root has no checkpoint for this thread (the API answers 404)."""


class DataSource(Protocol):
    """Everything the API reads. Every method is synchronous, safe to call from any thread,
    and never writes: an implementation opens what it reads read-only and closes it before
    returning."""

    def roots(self) -> list[Workspace]: ...

    def list_threads(self, root: str) -> list[ThreadSummary]:
        """Every thread of ``root``, newest update first. Raises :class:`UnknownRoot`."""
        ...

    def thread(self, root: str, thread_id: str) -> ThreadDetail:
        """Raises :class:`UnknownRoot` or :class:`UnknownThread`."""
        ...

    def graphs(self) -> list[GraphDiagram]: ...

    def versions(self, root: str) -> dict[str, ThreadVersion]:
        """Thread id -> its version: two grouped queries (checkpoints, writes) and one
        ``stat`` per ledger, cheap enough to call every second. Raises :class:`UnknownRoot`."""
        ...
