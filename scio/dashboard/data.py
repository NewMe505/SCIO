"""The dashboard's data layer (issue #68, ADR 0017): :class:`CheckpointDataSource` reads each
workspace's checkpoint DB, cost ledgers and the products' ``progress.json``, read-only by
construction.

The DB is opened ``mode=ro``, so a write through it raises, and a missing DB is never created.
``SqliteSaver.setup`` never runs, because its ``PRAGMA journal_mode=WAL`` and ``CREATE TABLE``
are writes. Every public method opens and closes its own connection, because a reader that
holds a transaction stops a live writer's WAL checkpoint and the WAL then grows without bound.
The main DB file never changes; on a DB no writer has open, SQLite still leaves its empty
``-wal`` and ``-shm`` index files behind, which the next writer takes over. A file path is built
from a thread id only once the DB lists that thread, so an id that came from a URL never
reaches the filesystem.

The recovery supervisor's store (``state.sqlite``, ADR 0019) is read the same way, from where
the supervisor keeps it (:func:`scio.dashboard.supervisor.store_path`, resolved on each read
unless the source was given a path): its runs make a supervised thread whose process is gone
read ``stopped``, and :meth:`CheckpointDataSource.recovery` serves its open incidents (issue
#102). A missing store is no error; an unreadable one is skipped and logged once per streak,
and never fails a root's threads (guardrail 30)."""

from __future__ import annotations

import functools
import importlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Send
from pydantic import BaseModel, ValidationError

from scio.dashboard import liveness, supervisor
from scio.dashboard.contract import (
    FeatureLink,
    GraphDiagram,
    IncidentEvent,
    RecoveryIncident,
    RecoveryView,
    StopInfo,
    SupervisedRun,
    ThreadDetail,
    ThreadKind,
    ThreadStatus,
    ThreadSummary,
    ThreadVersion,
    TimelineItem,
    UnknownRoot,
    UnknownThread,
    Workspace,
)
from scio.graph.checkpointer import serializer
from scio.graph.mermaid import render_mermaid
from scio.graph.registry import GRAPHS
from scio.graph.report import build_report
from scio.harness.accounting import CostEntry
from scio.product.progress import PROGRESS_PATH, ProgressFile
from scio.product.report import build_bootstrap_report, build_product_report
from scio.state.models import ArtifactRef, LedgerEntry, ProductBuildState, ProductState, SCIOState

INTERRUPT = "__interrupt__"
RESUME = "__resume__"
ERROR = "__error__"
BRANCH_TO = "branch:to:"
TASKS = "__pregel_tasks"
"""The channel that holds pending ``Send`` packets (``.node``): T2's parallel explorers."""
CRASH_CHARS = 2_000
ERROR_CHARS = 500
VERIFY_LINES = 60
TAIL_BYTES = 256 * 1024
"""How much of a ``verify:last`` artifact is read for its last lines; test output can be large
and the open thread is refetched on every change."""
KIND_GRAPH: dict[ThreadKind, str] = {
    "build": "product_build",
    "bootstrap": "product_new",
    "task": "t0",
}
STOPPED: tuple[ThreadStatus, ...] = ("waiting", "crashed", "halted", "stopped")
"""The statuses of a feature attempt's thread that its build link shows instead of
``running``."""
INCIDENT_EVENTS = 5
"""How many of an incident's last timeline entries the recovery view carries."""
log = logging.getLogger(__name__)
_OLDEST = datetime.min.replace(tzinfo=UTC)
_LATEST_SQL = (
    "select thread_id, max(checkpoint_id) from checkpoints where checkpoint_ns = '' "
    "group by thread_id"
)
_WRITES_SQL = "select thread_id, count(*) from writes where checkpoint_ns = '' group by thread_id"
_GRAPHS_LOCK = threading.Lock()
_FEATURE_SUFFIX = re.compile(r"-(p\d+)-a(\d+)")
"""What ``scio build`` appends to the build's thread id for a feature attempt
(``f"{build}-{feat.id.lower()}-a{attempt}"``): ``-p4-a1``."""


def workspace(
    path: Path,
    *,
    name: str | None = None,
    checkpoints: Path | None = None,
    ledger_dir: Path | None = None,
) -> Workspace:
    """A root at ``path``; the DB defaults to ``path/.scio/checkpoints.sqlite`` and the
    ledgers to ``path/.scio/ledger``, the name to the directory's name."""
    root = path.resolve()
    db = (checkpoints or root / ".scio" / "checkpoints.sqlite").resolve()
    ledgers = (ledger_dir or root / ".scio" / "ledger").resolve()
    return Workspace(
        name=name or root.name or "root",
        path=str(root),
        checkpoints=str(db),
        ledger_dir=str(ledgers),
        present=db.is_file(),
    )


def _connect(path: Path) -> sqlite3.Connection:
    """The DB opened read-only: a write through it raises ``sqlite3.OperationalError``, and a
    missing file is an error rather than a new empty DB."""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)


@contextmanager
def _reader(path: Path) -> Iterator[tuple[sqlite3.Connection, SqliteSaver]]:
    with closing(_connect(path)) as conn:
        saver = SqliteSaver(conn, serde=serializer())
        saver.is_setup = True  # setup() sets WAL mode and creates tables: both are writes
        yield conn, saver


def _rows(conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    """No rows for a DB file a writer created but has not set up yet."""
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise


def _latest(conn: sqlite3.Connection) -> dict[str, str]:
    """Thread id -> its latest checkpoint id (uuid6, so the lexical max is the newest)."""
    return {str(tid): str(cid) for tid, cid in _rows(conn, _LATEST_SQL)}


@dataclass(frozen=True)
class _Thread:
    """One thread's latest checkpoint, read once and classified; ``state`` is None and
    ``error`` says why when it could not be read with its kind's model."""

    thread_id: str
    checkpoint_id: str
    updated_at: datetime | None
    kind: ThreadKind
    channels: dict[str, Any]
    pending: list[tuple[str, str, Any]]
    state: ProductBuildState | ProductState | SCIOState | None
    error: str | None = None


def _kind(channels: dict[str, Any]) -> ThreadKind:
    """By the channels, never the name: a feature run is named after its build (I-066, I-108),
    and ``product_dir`` counts a build that stopped before its first feature."""
    if "feature_runs" in channels or "product_dir" in channels:
        return "build"
    if "target_dir" in channels:
        return "bootstrap"
    return "task"


def _validate(
    kind: ThreadKind, channels: dict[str, Any]
) -> ProductBuildState | ProductState | SCIOState:
    if kind == "build":
        return ProductBuildState.model_validate(channels)
    if kind == "bootstrap":
        return ProductState.model_validate(channels)
    return SCIOState.model_validate(channels)


def _ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _load(saver: SqliteSaver, thread_id: str, checkpoint_id: str) -> _Thread:
    """Never raises: a checkpoint that cannot be deserialized or validated is still a row."""
    kind: ThreadKind = "task"
    channels: dict[str, Any] = {}
    updated_at: datetime | None = None
    try:
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None:
            raise LookupError(f"no checkpoint for thread {thread_id!r}")
        checkpoint_id = str(tup.checkpoint["id"])
        updated_at = _ts(tup.checkpoint.get("ts"))
        channels = dict(tup.checkpoint.get("channel_values") or {})
        pending = [(str(t), str(c), v) for t, c, v in tup.pending_writes or []]
        kind = _kind(channels)
        state = _validate(kind, channels)
    except Exception as exc:
        return _Thread(
            thread_id, checkpoint_id, updated_at, kind, channels, [], None, str(exc)[:ERROR_CHARS]
        )
    return _Thread(thread_id, checkpoint_id, updated_at, kind, channels, pending, state)


def _parent_id(thread_id: str, builds: Sequence[str]) -> str | None:
    """The longest build thread id this one extends (``build-x-m1`` for ``build-x-m1-p2-a1``)."""
    return max((b for b in builds if thread_id.startswith(b + "-")), key=len, default=None)


@dataclass(frozen=True)
class _Running:
    """A feature attempt in progress: its thread, and the feature id and attempt its id names."""

    thread: _Thread
    feature_id: str
    attempt: int


def _running(saver: SqliteSaver, latest: dict[str, str], build: _Thread) -> list[_Running]:
    """The build's feature threads that have no ``FeatureRun`` yet. ``scio build`` appends a
    run only when its attempt ends, so without these the feature running now would read "not
    run" and its spend would be missing from the build's costs. A child counts when this build
    is its parent by :func:`_parent_id` and its id ends the way ``scio build`` names attempts."""
    if not isinstance(build.state, ProductBuildState):
        return []
    recorded = {r.thread_id for r in build.state.feature_runs}
    prefix = build.thread_id + "-"
    out: list[_Running] = []
    for tid in latest:
        if not tid.startswith(prefix) or tid in recorded:
            continue
        match = _FEATURE_SUFFIX.fullmatch(tid[len(build.thread_id) :])
        if match is None:
            continue
        nearer = [b for b in latest if tid.startswith(b + "-") and len(b) > len(build.thread_id)]
        if any(_load(saver, b, latest[b]).kind == "build" for b in nearer):
            continue
        child = _load(saver, tid, latest[tid])
        if child.kind == "task":
            out.append(_Running(child, match.group(1).upper(), int(match.group(2))))
    return sorted(out, key=lambda r: (int(r.feature_id[1:]), r.attempt))


def _attempt_status(t: _Thread, gone: bool = False) -> str:
    """An attempt with no ``FeatureRun`` is ``running`` unless its thread has stopped, so a
    paused or dead attempt never reads as busy (the live graphcalc P4 waited at ``advisor_cap``).
    ``gone``: its build's supervised process is gone, which takes the attempt with it.
    ``done`` or ``error`` with no run recorded yet still read ``running``."""
    next_nodes = _next_nodes(t.channels) if t.state is not None else []
    status = _status(t.state, _stop(t.pending, next_nodes), _crash(t.pending), gone)
    return status if status in STOPPED else "running"


def _spent(t: _Thread, entries: list[CostEntry]) -> float:
    """An attempt's spend so far: its ledger's sum, which grows with every model call. The
    checkpoint's ``budgets.usd_used`` is written only when a node returns, so it lags a long
    worker turn; it is the fallback without ledger entries (0 when the checkpoint could not be
    read)."""
    if entries:
        return sum(e.usd for e in entries)
    return t.state.budgets.usd_used if isinstance(t.state, SCIOState) else 0.0


def _next_nodes(channels: dict[str, Any]) -> list[str]:
    """The ``branch:to:<node>`` channels and the nodes of the pending ``Send`` packets: while
    ``Send`` tasks run, the branch channels alone are empty."""
    nodes = {k.removeprefix(BRANCH_TO) for k in channels if k.startswith(BRANCH_TO)}
    packets = channels.get(TASKS)
    for packet in packets if isinstance(packets, list | tuple) else []:
        if isinstance(packet, Send) and isinstance(packet.node, str):
            nodes.add(packet.node)
    return sorted(nodes)


def _plain(value: Any) -> Any:
    """``value`` as JSON data, so a payload always serializes."""

    def default(obj: Any) -> Any:
        return obj.model_dump(mode="json") if isinstance(obj, BaseModel) else str(obj)

    try:
        return json.loads(json.dumps(value, default=default))
    except (TypeError, ValueError):
        return str(value)


def _stop(pending: list[tuple[str, str, Any]], next_nodes: list[str]) -> StopInfo | None:
    """The first pending ``__interrupt__`` write: a list of ``Interrupt`` (``.value``, ``.id``).
    None once the stop was answered: LangGraph keeps the ``__interrupt__`` row on a resume and
    adds a ``__resume__`` write; a new checkpoint appears only when the resumed node returns.
    Exact because every SCIO node interrupts at most once."""
    if any(channel == RESUME for _task, channel, _value in pending):
        return None
    for _task, channel, value in pending:
        if channel != INTERRUPT:
            continue
        items = list(value) if isinstance(value, list | tuple) else [value]
        first = items[0] if items else None
        interrupt_id = getattr(first, "id", None)
        plain = _plain(getattr(first, "value", first))
        if isinstance(plain, dict):
            payload, question = plain, str(plain.get("question", ""))
        else:
            payload = {} if plain is None else {"value": plain}
            question = plain if isinstance(plain, str) else ""
        return StopInfo(
            node=next_nodes[0] if len(next_nodes) == 1 else None,
            question=question,
            interrupt_id=str(interrupt_id) if interrupt_id else None,
            payload=payload,
        )
    return None


def _crash(pending: list[tuple[str, str, Any]]) -> str | None:
    """LangGraph stores a node's exception as its repr in an ``__error__`` write."""
    return next((str(v)[:CRASH_CHARS] for _t, c, v in pending if c == ERROR), None)


def _exit_reason(state: ProductBuildState | ProductState | SCIOState) -> str | None:
    if isinstance(state, SCIOState):
        return state.exit_reason.value if state.exit_reason else None
    return state.exit_reason


def _status(
    state: ProductBuildState | ProductState | SCIOState | None,
    stop: StopInfo | None,
    crash: str | None,
    gone: bool = False,
) -> ThreadStatus:
    """The order of the contract's ``ThreadStatus``: the API and the page never re-derive it.
    ``gone``: the thread's supervised run counts as stopped (:class:`_Supervised`), which
    replaces only ``in_progress``."""
    if state is None:
        return "error"
    if crash is not None:
        return "crashed"
    if stop is not None:
        return "waiting"
    exit_reason = _exit_reason(state)
    if (exit_reason and exit_reason != "success") or state.phase == "halted":
        return "halted"
    if state.phase == "done":
        return "done"
    if gone:
        return "stopped"
    return "in_progress"


@dataclass(frozen=True)
class _Supervised:
    """A build thread's latest supervised run, its process's liveness read once."""

    root: str
    run: supervisor.StoredRun
    alive: bool | None
    stopped: str | None

    @property
    def token(self) -> str:
        """What ``ThreadVersion.supervisor`` compares: a new run, a new status, or a process
        that died changes it."""
        return f"{self.run.run_id}:{self.run.status}:{'stopped' if self.stopped else 'on'}"

    def view(self, thread_id: str) -> SupervisedRun:
        run = self.run
        return SupervisedRun(
            root=self.root,
            thread_id=thread_id,
            run_id=run.run_id,
            status=run.status,
            alive=self.alive,
            pid=run.pid,
            exit_code=run.exit_code,
            started_at=supervisor.at(run.started_at),
            ended_at=supervisor.at(run.ended_at),
            incident_id=run.incident_id,
            stopped=self.stopped,
        )


Supervision = dict[str, _Supervised]
"""Build thread id -> its latest supervised run, for one root."""


def _supervised(
    ws: Workspace, runs: list[supervisor.StoredRun], alive: Callable[[int, float], bool | None]
) -> Supervision:
    """The latest run of each build thread in ``ws``. A running run's process is checked only
    by a real key: the supervisor saves a run with PID 0 before it starts the child, and
    creation time 0 when it could not read one, and neither means the process is gone."""
    out: Supervision = {}
    for thread, run in supervisor.latest_runs(runs, ws.path).items():
        known = run.status == supervisor.RUNNING and run.pid > 0 and run.pid_created > 0
        live = alive(run.pid, run.pid_created) if known else None
        out[thread] = _Supervised(ws.name, run, live, supervisor.stop_reason(run, live))
    return out


def _supervising(sup: Supervision, thread_id: str, parent: str | None) -> _Supervised | None:
    """The run of ``thread_id``, else of its build ``parent``: a feature run runs in its
    build's process, so it stops with it."""
    return sup.get(thread_id) or (sup.get(parent) if parent else None)


def _token(sup: Supervision, thread_id: str) -> str:
    """``ThreadVersion.supervisor`` for ``thread_id``: its own run's, else the longest build
    thread's it extends, so an open feature thread refreshes when its build's process dies.
    By the id alone (``versions`` reads no checkpoint); at worst one more refetch."""
    builds = [b for b in sup if thread_id == b or thread_id.startswith(b + "-")]
    return sup[max(builds, key=len)].token if builds else ""


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class _Streak:
    """Logs a source that cannot be read once per failure streak (guardrail 30): the first
    failure at warning, the repeats at debug, the recovery at info. Thread-safe: the API's
    threadpool and the WebSocket's polls share one source."""

    def __init__(self, what: str) -> None:
        self._what = what
        self._failing = False
        self._lock = threading.Lock()

    def failed(self, path: Path, exc: BaseException) -> None:
        with self._lock:
            first, self._failing = not self._failing, True
        level = logging.WARNING if first else logging.DEBUG
        log.log(
            level, "dashboard: %s %s cannot be read, skipped: %s", self._what, path, _reason(exc)
        )

    def succeeded(self, path: Path) -> None:
        with self._lock:
            if not self._failing:
                return
            self._failing = False
        log.info("dashboard: %s %s reads again", self._what, path)


def _ledger_path(ws: Workspace, thread_id: str) -> Path | None:
    """The thread's cost ledger. Callers pass only a thread id the DB lists; the check here
    keeps even such an id inside the ledger directory (a separator, or a Windows drive)."""
    base = Path(ws.ledger_dir)
    path = base / f"{thread_id}.jsonl"
    return path if path.parent == base and path.name == f"{thread_id}.jsonl" else None


def _read_ledger(path: Path) -> list[CostEntry]:
    """The dashboard's own reader, because a page must never fail on a ledger: it also skips a
    corrupt complete line, which ``CostLedger.load_entries`` still raises on. A torn last line
    (even one cut inside a UTF-8 character) is left out by both (I-127)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[CostEntry] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entries.append(CostEntry.model_validate_json(line))
        except ValidationError:
            continue
    return entries


def _entries(ws: Workspace, thread_id: str) -> list[CostEntry]:
    path = _ledger_path(ws, thread_id)
    return _read_ledger(path) if path is not None else []


def _ledger_bytes(ws: Workspace, thread_id: str) -> int:
    path = _ledger_path(ws, thread_id)
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _by_role(entries: list[CostEntry]) -> dict[str, float]:
    acc: dict[str, float] = defaultdict(float)
    for e in entries:
        acc[e.role] += e.usd
    return {role: round(usd, 4) for role, usd in sorted(acc.items(), key=lambda kv: -kv[1])}


def _timeline(ledger: list[LedgerEntry]) -> list[TimelineItem]:
    return [TimelineItem(at=e.at, source=e.source.value, kind=e.kind, text=e.text) for e in ledger]


def _resolve(ws: Workspace, path: str) -> Path:
    """A path the state recorded; SCIO may have run with a relative ``--product`` or work dir,
    which is relative to the root it ran in, not to the dashboard's working directory."""
    p = Path(path)
    return p if p.is_absolute() else Path(ws.path) / p


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _tail(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            start = max(0, fh.seek(0, os.SEEK_END) - TAIL_BYTES)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return (lines[1:] if start else lines)[-n:]


def _progress(ws: Workspace, product_dir: str) -> ProgressFile | None:
    try:
        text = (_resolve(ws, product_dir) / PROGRESS_PATH).read_text(encoding="utf-8")
        return ProgressFile.from_json(text)
    except (OSError, ValueError):
        return None


def _product(parent: _Thread | None) -> str | None:
    state = parent.state if parent else None
    return state.spec.name if isinstance(state, ProductBuildState) and state.spec else None


def _summary(
    ws: Workspace, t: _Thread, parent: _Thread | None, supervised: _Supervised | None = None
) -> ThreadSummary:
    """``supervised``: the thread's supervised run, or its build's (:func:`_supervising`)."""
    state = t.state
    next_nodes = _next_nodes(t.channels) if state is not None else []
    stop = _stop(t.pending, next_nodes)
    crash = _crash(t.pending)
    gone = supervised is not None and supervised.stopped is not None
    status = _status(state, stop, crash, gone)
    graph, title, product, usd = KIND_GRAPH[t.kind], "", _product(parent), 0.0
    if isinstance(state, ProductBuildState):
        milestone, spec = state.milestone, state.spec
        product = spec.name if spec else None
        title = f"{milestone.id}: {milestone.title}" if milestone else product or t.thread_id
        usd = build_product_report(state, []).total_usd  # the total reads the state only
    elif isinstance(state, ProductState):
        title = state.spec.name if state.spec else state.prompt[:80]
        product = state.spec.name if state.spec else None
        usd = build_bootstrap_report(state, _entries(ws, t.thread_id)).usd
    elif isinstance(state, SCIOState):
        graph = state.routing.tier.value.lower() if state.routing else "t0"
        title = state.task.title
        usd = state.budgets.usd_used
    raw_phase = t.channels.get("phase")
    return ThreadSummary(
        root=ws.name,
        thread_id=t.thread_id,
        kind=t.kind,
        status=status,
        graph=graph,
        phase=state.phase if state else raw_phase if isinstance(raw_phase, str) else "unknown",
        exit_reason=_exit_reason(state) if state else None,
        next_nodes=next_nodes,
        checkpoint_id=t.checkpoint_id,
        updated_at=t.updated_at,
        title=title,
        product=product,
        parent_thread=parent.thread_id if parent else None,
        usd=usd,
        waiting=stop if status == "waiting" else None,
        crash=crash if status == "crashed" else None,
        error=t.error,
    )


def _newest_first(summaries: list[ThreadSummary]) -> list[ThreadSummary]:
    return sorted(summaries, key=lambda s: (s.updated_at or _OLDEST, s.thread_id), reverse=True)


def _build_detail(
    ws: Workspace,
    summary: ThreadSummary,
    state: ProductBuildState,
    listed: set[str],
    running: list[_Running],
    gone: bool = False,
) -> ThreadDetail:
    """``summary.usd`` stays the figure ``scio report`` prints (finished runs); the attempts in
    progress add to ``cost_by_role``, ``cost_by_feature`` and ``running_usd`` only. ``gone``:
    the build's supervised process is gone, and its attempts with it."""
    own = _entries(ws, summary.thread_id)
    finished = [
        e
        for tid in dict.fromkeys(r.thread_id for r in state.feature_runs)
        if tid in listed and tid != summary.thread_id
        for e in _entries(ws, tid)
    ]
    attempts = [(a, _entries(ws, a.thread.thread_id)) for a in running]
    by_feature: dict[str, float] = defaultdict(float)
    for r in state.feature_runs:
        by_feature[r.feature_id] += r.usd
    running_usd = 0.0
    for a, entries in attempts:
        usd = _spent(a.thread, entries)
        by_feature[a.feature_id] += usd
        running_usd += usd
    milestone = state.milestone
    return ThreadDetail(
        summary=summary,
        build=build_product_report(state, own),
        timeline=_timeline(state.ledger),
        cost_by_role=_by_role(own + finished + [e for _a, entries in attempts for e in entries]),
        progress=_progress(ws, state.product_dir),
        milestone_title=milestone.title if milestone else None,
        milestone_features=milestone.feature_ids() if milestone else [],
        feature_threads=[
            *(
                FeatureLink(
                    feature_id=r.feature_id,
                    attempt=r.attempt,
                    thread_id=r.thread_id,
                    status=r.status,
                    present=r.thread_id in listed,
                )
                for r in state.feature_runs
            ),
            *(
                FeatureLink(
                    feature_id=a.feature_id,
                    attempt=a.attempt,
                    thread_id=a.thread.thread_id,
                    status=_attempt_status(a.thread, gone),
                    present=True,
                )
                for a in running
            ),
        ],
        cost_by_feature={fid: round(usd, 4) for fid, usd in by_feature.items()},
        running_usd=round(running_usd, 4),
        integration=(state.integration or "").splitlines(),
        integration_ok=state.integration_ok,
        pr_number=state.pr.number if state.pr else None,
        pr_url=state.pr.url if state.pr else None,
    )


def _bootstrap_detail(ws: Workspace, summary: ThreadSummary, state: ProductState) -> ThreadDetail:
    entries = _entries(ws, summary.thread_id)
    return ThreadDetail(
        summary=summary,
        bootstrap=build_bootstrap_report(state, entries),
        timeline=_timeline(state.ledger),
        cost_by_role=_by_role(entries),
        progress=_progress(ws, state.target_dir),
        questions=list(state.questions),
        revision_notes=list(state.revision_notes),
    )


def _task_detail(ws: Workspace, summary: ThreadSummary, state: SCIOState) -> ThreadDetail:
    entries = _entries(ws, summary.thread_id)

    def read(ref: ArtifactRef) -> str | None:
        return _read_text(_resolve(ws, ref.path))

    verify = state.artifacts.get("verify:last")
    return ThreadDetail(
        summary=summary,
        task=build_report(state, entries, read_artifact=read),
        timeline=_timeline(state.task_ledger),
        cost_by_role=_by_role(entries),
        verify_lines=_tail(_resolve(ws, verify.path), VERIFY_LINES) if verify else [],
    )


@functools.cache
def _diagrams() -> tuple[GraphDiagram, ...]:
    """Compiling every graph takes seconds, and the graphs change only with the code."""
    importlib.import_module("scio.graph.tiers")
    out: list[GraphDiagram] = []
    for name, factory in sorted(GRAPHS.items()):
        compiled = factory()
        nodes = [str(n) for n in compiled.get_graph().nodes if n not in ("__start__", "__end__")]
        out.append(GraphDiagram(name=name, mermaid=render_mermaid(compiled), nodes=nodes))
    return tuple(out)


class CheckpointDataSource:
    """:class:`scio.dashboard.contract.DataSource` over SQLite checkpoint DBs, and a
    :class:`~scio.dashboard.contract.RecoverySource` over the recovery supervisor's store.

    ``recovery_store``: the store's file; by default where the supervisor keeps it, resolved
    from the environment on each read (:func:`scio.dashboard.supervisor.store_path`).
    ``alive(pid, created)``: a supervised process's liveness (:func:`liveness.alive`).
    ``clock``: now, in seconds since the epoch, for the escalated incidents' window."""

    def __init__(
        self,
        workspaces: Sequence[Workspace],
        *,
        recovery_store: Path | None = None,
        alive: Callable[[int, float], bool | None] = liveness.alive,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._workspaces = list(workspaces)
        self._by_name = {w.name: w for w in self._workspaces}
        if len(self._by_name) != len(self._workspaces):
            raise ValueError("every root needs its own name (it is the URL segment)")
        self._recovery_store = recovery_store
        self._alive = alive
        self._clock = clock
        self._store_streak = _Streak("the recovery supervisor's store")

    @classmethod
    def from_roots(
        cls, roots: Sequence[Path], *, recovery_store: Path | None = None
    ) -> CheckpointDataSource:
        """One workspace per directory, at the default locations, names made unique."""
        taken: set[str] = set()
        out: list[Workspace] = []
        for path in roots:
            base = workspace(path).name
            name, n = base, 1
            while name in taken:
                n += 1
                name = f"{base}-{n}"
            taken.add(name)
            out.append(workspace(path, name=name))
        return cls(out, recovery_store=recovery_store)

    # ---- the recovery supervisor's store

    def _store(self) -> Path:
        return self._recovery_store if self._recovery_store is not None else supervisor.store_path()

    def _runs(self) -> list[supervisor.StoredRun]:
        """Every run that started; none without a store, or (logged once per streak) when
        the store cannot be read: a thread then reads as if no supervisor ran it."""
        path = self._store()
        if not path.is_file():
            return []
        try:
            with closing(_connect(path)) as conn:
                supervisor.check_schema(conn)
                runs = supervisor.read_runs(conn)
        except (sqlite3.Error, OSError, supervisor.StoreError) as exc:
            self._store_streak.failed(path, exc)
            return []
        self._store_streak.succeeded(path)
        return runs

    def _supervision(self, ws: Workspace) -> Supervision:
        return _supervised(ws, self._runs(), self._alive)

    def _listed(self, ws: Workspace) -> set[str]:
        """The root's thread ids; none when its DB is missing or cannot be read."""
        db = Path(ws.checkpoints)
        if not db.is_file():
            return set()
        try:
            with closing(_connect(db)) as conn:
                return set(_latest(conn))
        except sqlite3.Error:
            return set()

    def recovery(self) -> RecoveryView:
        """The supervisor's open incidents, and its escalated ones updated within
        :data:`~scio.dashboard.supervisor.ESCALATED_WINDOW_S` ("needs you"), each newest
        update first and linked to the build thread it interrupted; and the latest run of each
        build thread in the roots' workspaces. Closed incidents are left out. Never raises: a
        missing store is ``present`` False, and an unreadable one sets ``error`` (logged once
        per streak)."""
        path = self._store()
        view = RecoveryView(store=str(path), present=path.is_file())
        if not view.present:
            return view
        since = self._clock() - supervisor.ESCALATED_WINDOW_S
        try:
            with closing(_connect(path)) as conn:
                supervisor.check_schema(conn)
                runs = supervisor.read_runs(conn)
                open_, escalated, skipped = supervisor.read_incidents(
                    conn, events=INCIDENT_EVENTS, escalated_since=since
                )
        except (sqlite3.Error, OSError, supervisor.StoreError) as exc:
            self._store_streak.failed(path, exc)
            return view.model_copy(update={"error": _reason(exc)})
        self._store_streak.succeeded(path)
        listed: dict[str, set[str]] = {}
        groups: list[list[RecoveryIncident]] = []
        for read in (open_, escalated):
            group = [self._incident(i, thread, events, listed) for i, thread, events in read]
            skipped += group.count(None)
            groups.append([i for i in group if i is not None])
        supervised = [
            s.view(thread)
            for ws in self._workspaces
            for thread, s in sorted(_supervised(ws, runs, self._alive).items())
        ]
        error = f"{skipped} incident row(s) could not be read" if skipped else None
        return view.model_copy(
            update={
                "runs": supervised,
                "incidents": groups[0],
                "escalated": groups[1],
                "error": error,
            }
        )

    def _incident(
        self,
        incident: supervisor.StoredIncident,
        thread: str,
        events: list[tuple[float, str]],
        listed: dict[str, set[str]],
    ) -> RecoveryIncident | None:
        """The incident as the page shows it; None when its times are nonsense. ``listed``
        caches each root's thread ids across the incidents of one read."""
        created, updated = supervisor.at(incident.created_at), supervisor.at(incident.updated_at)
        if created is None or updated is None:
            return None
        ws = next(
            (w for w in self._workspaces if supervisor.same_path(w.path, incident.workspace)),
            None,
        )
        if ws is not None and thread and ws.name not in listed:
            listed[ws.name] = self._listed(ws)
        return RecoveryIncident(
            incident_id=incident.incident_id,
            state=incident.state,
            failure_class=incident.failure_class,
            summary=incident.summary,
            frame=incident.innermost_scio_frame,
            issue_number=incident.issue_number,
            pr_number=incident.pr_number,
            attempts=incident.attempts,
            replays=incident.replays,
            escalation=incident.escalation,
            workspace=incident.workspace,
            created_at=created,
            updated_at=updated,
            build_thread=thread,
            root=ws.name if ws is not None else None,
            present=ws is not None and thread in listed.get(ws.name, set()),
            events=[
                IncidentEvent(at=when, text=text)
                for t, text in events
                if (when := supervisor.at(t)) is not None
            ],
        )

    def recovery_version(self) -> str:
        """The store's and its WAL's size and modification time: a ``stat`` each."""
        return supervisor.store_version(self._store())

    def _workspace(self, root: str) -> Workspace:
        try:
            return self._by_name[root]
        except KeyError:
            raise UnknownRoot(root) from None

    def roots(self) -> list[Workspace]:
        """``present`` as of now: a root whose first SCIO run starts later shows up."""
        return [
            w.model_copy(update={"present": Path(w.checkpoints).is_file()})
            for w in self._workspaces
        ]

    def list_threads(self, root: str) -> list[ThreadSummary]:
        ws = self._workspace(root)
        db = Path(ws.checkpoints)
        if not db.is_file():
            return []
        with _reader(db) as (conn, saver):
            threads = [_load(saver, tid, cid) for tid, cid in _latest(conn).items()]
        sup = self._supervision(ws) if threads else {}
        by_id = {t.thread_id: t for t in threads}
        builds = [t.thread_id for t in threads if t.kind == "build"]
        summaries = []
        for t in threads:
            pid = _parent_id(t.thread_id, builds) if t.kind == "task" else None
            parent = by_id[pid] if pid else None
            summaries.append(_summary(ws, t, parent, _supervising(sup, t.thread_id, pid)))
        return _newest_first(summaries)

    def thread(self, root: str, thread_id: str) -> ThreadDetail:
        ws = self._workspace(root)
        db = Path(ws.checkpoints)
        if not db.is_file():
            raise UnknownThread(thread_id)
        with _reader(db) as (conn, saver):
            latest = _latest(conn)
            if thread_id not in latest:
                raise UnknownThread(thread_id)
            t = _load(saver, thread_id, latest[thread_id])
            running = _running(saver, latest, t) if t.kind == "build" else []
            parent = None
            if t.kind == "task":
                prefixes = [b for b in latest if thread_id.startswith(b + "-")]
                for b in sorted(prefixes, key=len, reverse=True):
                    candidate = _load(saver, b, latest[b])
                    if candidate.kind == "build":
                        parent = candidate
                        break
        parent_id = parent.thread_id if parent else None
        supervised = _supervising(self._supervision(ws), thread_id, parent_id)
        summary = _summary(ws, t, parent, supervised)
        state = t.state
        if isinstance(state, ProductBuildState):
            gone = supervised is not None and supervised.stopped is not None
            return _build_detail(ws, summary, state, set(latest), running, gone)
        if isinstance(state, ProductState):
            return _bootstrap_detail(ws, summary, state)
        if isinstance(state, SCIOState):
            return _task_detail(ws, summary, state)
        return ThreadDetail(summary=summary)

    def graphs(self) -> list[GraphDiagram]:
        with _GRAPHS_LOCK:
            return list(_diagrams())

    def versions(self, root: str) -> dict[str, ThreadVersion]:
        ws = self._workspace(root)
        db = Path(ws.checkpoints)
        if not db.is_file():
            return {}
        with closing(_connect(db)) as conn:
            latest = _latest(conn)
            writes = {str(tid): int(n) for tid, n in _rows(conn, _WRITES_SQL)}
        sup = self._supervision(ws) if latest else {}
        return {
            tid: ThreadVersion(
                checkpoint_id=cid,
                writes=writes.get(tid, 0),
                ledger_bytes=_ledger_bytes(ws, tid),
                supervisor=_token(sup, tid),
            )
            for tid, cid in latest.items()
        }
