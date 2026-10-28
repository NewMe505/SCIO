"""The dashboard's data layer (issue #68, ADR 0017) over real checkpoints: a Fake build paused
at ``merge_wait`` (its bootstrap, the build and both feature threads), tiny task graphs that
stop or crash, and hand-made unreadable rows. Costs match ``scio report``, and nothing the
layer does changes the DB."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections import defaultdict
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from scio.dashboard import data
from scio.dashboard.contract import DataSource, ThreadSummary, UnknownRoot, UnknownThread
from scio.dashboard.data import CheckpointDataSource, workspace
from scio.graph.checkpointer import serializer
from scio.harness.accounting import CostEntry, CostLedger
from scio.harness.types import Usage
from scio.product.build import build_thread_id, run_build
from scio.product.report import build_bootstrap_report, build_product_report
from scio.state.models import (
    Budget,
    GitHubRefs,
    ProductBuildState,
    ProductState,
    SCIOState,
    TaskSpec,
)
from tests.graph.test_product_build_fake import PRODUCT, _bootstrap, _ctx

ROOT = "run"
BUILD = build_thread_id(PRODUCT)
P1, P2 = f"{BUILD}-p1-a1", f"{BUILD}-p2-a1"
NEW = "new-1"


@dataclass(frozen=True)
class Built:
    source: CheckpointDataSource
    db: Path
    ledgers: Path


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Built:
    """One Fake bootstrap and build for the module: the build pauses at ``merge_wait``. In
    this layout the DB is ``ckpt.sqlite`` and the ledgers are in ``.scio/ledger``."""
    tmp = tmp_path_factory.mktemp("dashboard-build")
    harness = _bootstrap(tmp)
    ctx = _ctx(tmp, harness, BUILD)
    try:
        out = run_build(ctx)
    finally:
        ctx.close()
    assert out.paused and out.state.phase == "merge_wait"
    db, ledgers = tmp / "ckpt.sqlite", tmp / ".scio" / "ledger"
    ws = workspace(tmp, name=ROOT, checkpoints=db, ledger_dir=ledgers)
    return Built(CheckpointDataSource([ws]), db, ledgers)


def _channels(db: Path, thread_id: str) -> dict[str, Any]:
    with data._reader(db) as (_conn, saver):
        tup = saver.get_tuple({"configurable": {"thread_id": thread_id}})
    assert tup is not None
    return dict(tup.checkpoint["channel_values"])


def _rows(built: Built) -> dict[str, ThreadSummary]:
    return {s.thread_id: s for s in built.source.list_threads(ROOT)}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------- tiny task graphs, written the way scio run writes them ----------


class Boom(RuntimeError):
    pass


def _route(_state: SCIOState) -> dict[str, Any]:
    return {"phase": "route"}


def _ask(_state: SCIOState) -> dict[str, Any]:
    interrupt({"question": "Ship it?", "branch": "b"})
    return {"phase": "done"}


def _boom(_state: SCIOState) -> dict[str, Any]:
    raise Boom("the node fell over " + "x" * 3_000)


def _finish(_state: SCIOState) -> dict[str, Any]:
    return {"phase": "done"}


def _run_tiny(db: Path, thread_id: str, second: Any, *, usd: float = 0.125) -> None:
    """``route`` then ``second`` over :class:`SCIOState` on a writable saver. The state must be
    a real task state: anything else reads as ``error``, which outranks every other status."""
    g: Any = StateGraph(SCIOState)
    g.add_node("route", _route)
    g.add_node("implement", second)
    g.add_edge(START, "route")
    g.add_edge("route", "implement")
    g.add_edge("implement", END)
    state = SCIOState(
        thread_id=thread_id,
        task=TaskSpec(title="Tiny task", description="d", acceptance=["a"], repo_url="r"),
        budgets=Budget(tokens_max=1_000, usd_max=1.0, advisor_calls_max=0, usd_used=usd),
        github=GitHubRefs(owner="o", repo="r"),
    )
    with closing(sqlite3.connect(str(db), check_same_thread=False)) as conn:
        app = g.compile(checkpointer=SqliteSaver(conn, serde=serializer()))
        with suppress(Boom):
            app.invoke(state, {"configurable": {"thread_id": thread_id}}, durability="sync")


def _tiny(tmp_path: Path, name: str = "tiny") -> CheckpointDataSource:
    return CheckpointDataSource(
        [
            workspace(
                tmp_path, name=name, checkpoints=tmp_path / "ckpt.sqlite", ledger_dir=tmp_path / "l"
            )
        ]
    )


# ---------- the Fake build ----------


def test_threads_are_classified_by_their_channels(built: Built) -> None:
    listed = built.source.list_threads(ROOT)
    rows = {s.thread_id: s for s in listed}
    assert {t: (rows[t].kind, rows[t].graph) for t in rows} == {
        NEW: ("bootstrap", "product_new"),
        BUILD: ("build", "product_build"),
        P1: ("task", "t0"),
        P2: ("task", "t1"),
    }
    assert [rows[t].status for t in (NEW, P1, P2)] == ["done", "done", "done"]
    assert rows[NEW].title == PRODUCT and rows[NEW].product == PRODUCT
    assert rows[P2].title == "P2: Add and list todos"
    stamps = [s.updated_at for s in listed]
    assert None not in stamps and stamps == sorted(stamps, reverse=True)  # type: ignore[type-var]
    assert all(s.error is None and s.crash is None for s in listed)


def test_the_paused_build_waits_at_merge_wait_with_its_question(built: Built) -> None:
    s = _rows(built)[BUILD]
    assert s.status == "waiting" and s.phase == "merge_wait" and s.next_nodes == ["merge_wait"]
    assert s.waiting is not None and s.waiting.node == "merge_wait" and s.waiting.interrupt_id
    assert s.waiting.question.startswith("Milestone M1") and "merge" in s.waiting.question
    assert s.waiting.payload["question"] == s.waiting.question
    assert s.waiting.payload["branch"] == "milestone/1"
    assert s.title.startswith("M1: ") and s.product == PRODUCT and s.parent_thread is None


def test_feature_threads_are_tasks_linked_to_their_build(built: Built) -> None:
    rows = _rows(built)
    for tid in (P1, P2):
        assert (rows[tid].kind, rows[tid].parent_thread, rows[tid].product) == (
            "task",
            BUILD,
            PRODUCT,
        )
    detail = built.source.thread(ROOT, BUILD)
    assert [(f.feature_id, f.thread_id, f.status, f.present) for f in detail.feature_threads] == [
        ("P1", P1, "passed", True),
        ("P2", P2, "passed", True),
    ]
    assert detail.running_usd == 0.0
    feature = built.source.thread(ROOT, P2)
    assert feature.summary == rows[P2]
    assert feature.task is not None and (feature.build, feature.bootstrap) == (None, None)
    state = SCIOState.model_validate(_channels(built.db, P2))
    assert feature.task.thread_id == P2 and len(feature.timeline) == len(state.task_ledger)
    assert feature.verify_lines and len(feature.verify_lines) <= data.VERIFY_LINES
    assert feature.progress is None and feature.milestone_features == []


def test_costs_match_what_scio_report_prints(built: Built) -> None:
    rows = _rows(built)
    state = ProductBuildState.model_validate(_channels(built.db, BUILD))
    own = CostLedger.load_entries(built.ledgers / f"{BUILD}.jsonl")
    expected = build_product_report(state, own)
    assert rows[BUILD].usd == expected.total_usd > 0
    detail = built.source.thread(ROOT, BUILD)
    assert detail.build == expected and detail.summary == rows[BUILD]

    every = own + [
        e for t in (P1, P2) for e in CostLedger.load_entries(built.ledgers / f"{t}.jsonl")
    ]
    by_role: dict[str, float] = defaultdict(float)
    for e in every:
        by_role[e.role] += e.usd
    assert detail.cost_by_role == {r: round(u, 4) for r, u in by_role.items()}
    assert list(detail.cost_by_role.values()) == sorted(detail.cost_by_role.values(), reverse=True)
    assert sum(detail.cost_by_role.values()) == pytest.approx(sum(e.usd for e in every), abs=1e-3)
    assert detail.cost_by_feature == {r.feature_id: round(r.usd, 4) for r in state.feature_runs}

    boot = ProductState.model_validate(_channels(built.db, NEW))
    boot_entries = CostLedger.load_entries(built.ledgers / f"{NEW}.jsonl")
    assert rows[NEW].usd == build_bootstrap_report(boot, boot_entries).usd > 0
    for tid in (P1, P2):
        task = SCIOState.model_validate(_channels(built.db, tid))
        assert rows[tid].usd == task.budgets.usd_used > 0


def test_the_build_and_bootstrap_details(built: Built) -> None:
    state = ProductBuildState.model_validate(_channels(built.db, BUILD))
    detail = built.source.thread(ROOT, BUILD)
    assert state.milestone is not None and state.integration
    assert detail.milestone_title == state.milestone.title
    assert detail.milestone_features == ["P1", "P2"]
    assert detail.integration == state.integration.splitlines() and detail.integration_ok is True
    assert (detail.pr_number, detail.pr_url) == (None, None)  # offline: a PR body file instead
    assert detail.progress is not None and detail.progress.product == PRODUCT
    assert len(detail.timeline) == len(state.ledger) and detail.timeline[0].source
    assert (detail.bootstrap, detail.task) == (None, None)

    boot = ProductState.model_validate(_channels(built.db, NEW))
    new = built.source.thread(ROOT, NEW)
    assert new.bootstrap is not None and (new.build, new.task) == (None, None)
    assert new.questions == boot.questions and new.questions
    assert new.revision_notes == boot.revision_notes
    assert new.progress is not None and new.progress.product == PRODUCT
    assert new.cost_by_role and len(new.timeline) == len(boot.ledger)


def test_versions_follow_checkpoints_writes_and_ledgers(built: Built) -> None:
    rows = _rows(built)
    versions = built.source.versions(ROOT)
    assert set(versions) == set(rows)
    assert all(versions[t].checkpoint_id == rows[t].checkpoint_id for t in rows)
    assert all(v.writes > 0 for v in versions.values())
    size = (built.ledgers / f"{BUILD}.jsonl").stat().st_size
    assert versions[BUILD].ledger_bytes == size > 0


def test_next_nodes_light_nodes_of_the_threads_own_graph(built: Built) -> None:
    graphs = {g.name: set(g.nodes) for g in built.source.graphs()}
    for row in built.source.list_threads(ROOT):
        assert set(row.next_nodes) <= graphs[row.graph], (row.thread_id, row.next_nodes)


def test_the_data_layer_never_writes(built: Built) -> None:
    before = _sha(built.db)
    ledgers = {p.name: _sha(p) for p in built.ledgers.iterdir()}
    source: DataSource = built.source
    source.roots()
    for row in source.list_threads(ROOT):
        source.thread(ROOT, row.thread_id)
    source.graphs()
    source.versions(ROOT)
    with pytest.raises(UnknownThread):
        source.thread(ROOT, "nope")
    assert _sha(built.db) == before
    assert {p.name: _sha(p) for p in built.ledgers.iterdir()} == ledgers
    with (
        closing(data._connect(built.db)) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute("delete from writes")
    assert _sha(built.db) == before


def test_unknown_roots_and_threads_raise_and_never_reach_the_filesystem(built: Built) -> None:
    source = built.source
    with pytest.raises(UnknownRoot):
        source.list_threads("nope")
    with pytest.raises(UnknownRoot):
        source.thread("nope", BUILD)
    with pytest.raises(UnknownRoot):
        source.versions("nope")
    for tid in ("nope", "../../etc", f"../ledger/{BUILD}", "", BUILD + "/"):
        with pytest.raises(UnknownThread):
            source.thread(ROOT, tid)
    ws = source.roots()[0]
    assert data._ledger_path(ws, "run-1") == Path(ws.ledger_dir) / "run-1.jsonl"
    assert data._ledger_path(ws, "../x") is None and data._ledger_path(ws, "a/b") is None


def test_a_feature_attempt_in_progress_is_linked_and_counted(built: Built, tmp_path: Path) -> None:
    """``scio build`` appends a ``FeatureRun`` only when an attempt ends (the live graphcalc
    build ran P4 with no run for it): the attempt in progress is still linked, and its spend is
    in the build's costs and ``running_usd`` but not in ``summary.usd``."""
    db, ledgers = tmp_path / "ckpt.sqlite", tmp_path / "ledger"
    with closing(data._connect(built.db)) as src, closing(sqlite3.connect(db)) as dst:
        src.backup(dst)
    shutil.copytree(built.ledgers, ledgers)
    p3, p4, p5 = f"{BUILD}-p3-a1", f"{BUILD}-p4-a2", f"{BUILD}-p5-a1"
    stray = f"{BUILD}-notes"
    _run_tiny(db, p3, _ask, usd=0.3125)
    _run_tiny(db, p5, _ask, usd=0.0625)  # stopped at an interrupt: the link says so
    _run_tiny(db, stray, _finish, usd=0.5)
    with closing(sqlite3.connect(db, check_same_thread=False)) as conn:
        # P3 mid-node: its checkpoint before `implement` with no stop written yet
        conn.execute("delete from writes where thread_id = ? and channel = '__interrupt__'", (p3,))
        conn.commit()
        checkpoint = empty_checkpoint()  # an attempt whose checkpoint cannot be read: spend 0
        checkpoint["channel_values"] = {"thread_id": p4, "task": "not a task"}
        config = {"configurable": {"thread_id": p4, "checkpoint_ns": ""}}
        SqliteSaver(conn, serde=serializer()).put(config, checkpoint, {}, {})  # type: ignore[arg-type]
    for tid, role, usd in ((p3, "debugger", 0.25), (p3, "coder", 0.0625), (stray, "advisor", 0.5)):
        ledger = CostLedger()
        ledger.persist_to(ledgers / f"{tid}.jsonl")
        ledger.record(
            role=role,
            model="m",
            provider="fake",
            usage=Usage(output_tokens=1),
            usd=usd,
            latency_ms=1,
        )
    source = CheckpointDataSource(
        [workspace(tmp_path, name=ROOT, checkpoints=db, ledger_dir=ledgers)]
    )
    before = built.source.thread(ROOT, BUILD)
    detail = source.thread(ROOT, BUILD)
    links = [
        (f.feature_id, f.attempt, f.thread_id, f.status, f.present) for f in detail.feature_threads
    ]
    assert links == [
        ("P1", 1, P1, "passed", True),
        ("P2", 1, P2, "passed", True),
        ("P3", 1, p3, "running", True),
        ("P4", 2, p4, "running", True),
        ("P5", 1, p5, "waiting", True),
    ]
    assert detail.cost_by_feature["P3"] == 0.3125 and detail.cost_by_feature["P5"] == 0.0625
    assert detail.running_usd == 0.375 and detail.cost_by_feature["P4"] == 0.0
    assert {f: detail.cost_by_feature[f] for f in ("P1", "P2")} == before.cost_by_feature
    assert detail.cost_by_role["debugger"] == 0.25 and "advisor" not in detail.cost_by_role
    assert detail.cost_by_role["coder"] == pytest.approx(before.cost_by_role["coder"] + 0.0625)
    assert detail.summary.usd == before.summary.usd and detail.build == before.build
    assert before.running_usd == 0.0
    rows = {s.thread_id: s for s in source.list_threads(ROOT)}
    assert rows[stray].parent_thread == BUILD  # the build's child, but not a feature attempt
    assert rows[p3].parent_thread == BUILD and rows[p3].product == PRODUCT
    assert (rows[p3].status, rows[p4].status, rows[p5].status) == (
        "in_progress",
        "error",
        "waiting",
    )


# ---------- version changes, stops and crashes ----------


def test_a_stop_arriving_changes_the_writes_count_not_the_checkpoint(tmp_path: Path) -> None:
    after, before = tmp_path / "after.sqlite", tmp_path / "before.sqlite"
    _run_tiny(after, "run-stop", _ask)
    with closing(sqlite3.connect(after)) as src, closing(sqlite3.connect(before)) as dst:
        src.backup(dst)
        dst.execute("delete from writes where channel = '__interrupt__'")
        dst.commit()
    source = CheckpointDataSource(
        [
            workspace(tmp_path, name="after", checkpoints=after, ledger_dir=tmp_path / "l"),
            workspace(tmp_path, name="before", checkpoints=before, ledger_dir=tmp_path / "l"),
        ]
    )
    stopped, running = source.versions("after")["run-stop"], source.versions("before")["run-stop"]
    assert stopped.checkpoint_id == running.checkpoint_id and stopped.writes == running.writes + 1

    [waiting] = source.list_threads("after")
    assert waiting.status == "waiting" and waiting.next_nodes == ["implement"]
    assert waiting.waiting is not None and waiting.waiting.node == "implement"
    assert waiting.waiting.question == "Ship it?" and waiting.waiting.interrupt_id
    assert waiting.waiting.payload == {"question": "Ship it?", "branch": "b"}
    assert (waiting.kind, waiting.graph, waiting.title, waiting.usd) == (
        "task",
        "t0",
        "Tiny task",
        0.125,
    )
    [still] = source.list_threads("before")
    assert still.status == "in_progress" and still.waiting is None


def test_a_crash_lists_as_crashed_with_the_exception(tmp_path: Path) -> None:
    _run_tiny(tmp_path / "ckpt.sqlite", "run-crash", _boom)
    source = _tiny(tmp_path)
    [row] = source.list_threads("tiny")
    assert row.status == "crashed" and row.waiting is None and row.next_nodes == ["implement"]
    assert row.crash is not None and row.crash.startswith("Boom('the node fell over")
    assert len(row.crash) == data.CRASH_CHARS
    detail = source.thread("tiny", "run-crash")
    assert detail.summary == row and detail.task is not None


def test_unreadable_threads_list_as_error_rows(tmp_path: Path) -> None:
    db = tmp_path / "ckpt.sqlite"
    with closing(sqlite3.connect(db, check_same_thread=False)) as conn:
        saver = SqliteSaver(conn, serde=serializer())
        for tid, channels in (
            ("run-junk", {"thread_id": "run-junk", "task": "not a task", "phase": "implement"}),
            ("build-junk-m1", {"feature_runs": "not a list"}),
        ):
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = channels
            saver.put({"configurable": {"thread_id": tid, "checkpoint_ns": ""}}, checkpoint, {}, {})
        conn.execute(
            "insert into checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, "
            "metadata) values ('run-raw', '', '1', 'msgpack', ?, ?)",
            (b"\xc1\xc1", b"{}"),
        )
        conn.commit()
    source = _tiny(tmp_path)
    rows = {s.thread_id: s for s in source.list_threads("tiny")}
    assert {t: (s.kind, s.status, s.graph, s.phase) for t, s in rows.items()} == {
        "run-junk": ("task", "error", "t0", "implement"),
        "build-junk-m1": ("build", "error", "product_build", "unknown"),
        "run-raw": ("task", "error", "t0", "unknown"),
    }
    assert all(s.error and len(s.error) <= data.ERROR_CHARS for s in rows.values())
    assert "validation errors for SCIOState" in (rows["run-junk"].error or "")
    assert "ProductBuildState" in (rows["build-junk-m1"].error or "")
    assert rows["run-raw"].checkpoint_id == "1" and rows["run-raw"].updated_at is None
    detail = source.thread("tiny", "run-junk")
    assert detail.summary == rows["run-junk"]
    assert (detail.build, detail.bootstrap, detail.task) == (None, None, None)
    assert set(source.versions("tiny")) == set(rows)


# ---------- edge cases ----------


def test_a_torn_last_ledger_line_is_skipped(tmp_path: Path) -> None:
    _run_tiny(tmp_path / "ckpt.sqlite", "run-torn", _finish)
    path = tmp_path / "l" / "run-torn.jsonl"
    ledger = CostLedger()
    ledger.persist_to(path)
    ledger.record(
        role="coder",
        model="m",
        provider="fake",
        usage=Usage(output_tokens=3),
        usd=0.25,
        latency_ms=1,
    )
    with path.open("ab") as fh:  # a writer cut off mid-append, inside a UTF-8 character
        fh.write(b'{"at": "2026-10-27T00:00:00Z", "role": "caf\xc3')
    assert [e.usd for e in data._read_ledger(path)] == [0.25]
    source = _tiny(tmp_path)
    detail = source.thread("tiny", "run-torn")
    assert detail.summary.status == "done" and detail.cost_by_role == {"coder": 0.25}
    assert detail.task is not None and [r.usd for r in detail.task.by_role] == [0.25]
    assert source.versions("tiny")["run-torn"].ledger_bytes == path.stat().st_size
    assert isinstance(data._read_ledger(path)[0], CostEntry)


def test_a_root_without_a_db_lists_nothing_and_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "never-ran"
    source = CheckpointDataSource.from_roots([root])
    [ws] = source.roots()
    assert (ws.name, ws.present) == ("never-ran", False)
    assert source.list_threads("never-ran") == [] and source.versions("never-ran") == {}
    with pytest.raises(UnknownThread):
        source.thread("never-ran", "run-1")
    assert not root.exists()


def test_from_roots_names_each_root_uniquely_at_the_default_locations(tmp_path: Path) -> None:
    a, b, c = tmp_path / "one" / "app", tmp_path / "two" / "app", tmp_path / "app-2"
    for p in (a, b, c):
        p.mkdir(parents=True)
    (a / ".scio").mkdir()
    (a / ".scio" / "checkpoints.sqlite").write_bytes(b"")  # created, not yet set up
    source = CheckpointDataSource.from_roots([a, c, b])
    roots = source.roots()
    assert [w.name for w in roots] == ["app", "app-2", "app-3"]
    assert [w.present for w in roots] == [True, False, False]
    assert roots[0].path == str(a.resolve())
    assert roots[0].checkpoints == str(a.resolve() / ".scio" / "checkpoints.sqlite")
    assert roots[0].ledger_dir == str(a.resolve() / ".scio" / "ledger")
    assert source.list_threads("app") == [] and source.versions("app") == {}
    assert (a / ".scio" / "checkpoints.sqlite").read_bytes() == b""
    (b / ".scio").mkdir()
    (b / ".scio" / "checkpoints.sqlite").write_bytes(b"")
    assert source.roots()[2].present is True  # a root whose first run starts later shows up
    with pytest.raises(ValueError, match="own name"):
        CheckpointDataSource([roots[0], roots[0]])


def test_graphs_lists_every_registered_graph_once_compiled() -> None:
    graphs = CheckpointDataSource([]).graphs()
    assert [g.name for g in graphs] == ["product_build", "product_new", "t0", "t1", "t2"]
    build = next(g for g in graphs if g.name == "product_build")
    assert "merge_wait" in build.nodes and {"__start__", "__end__"}.isdisjoint(build.nodes)
    assert all(g.mermaid.strip() and g.nodes for g in graphs)
    assert CheckpointDataSource([]).graphs() == graphs
