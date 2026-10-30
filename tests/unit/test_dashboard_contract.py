"""The dashboard's fixtures speak the contract (issue #68): the API and page tests run against
these payloads, so a contract change that the fixtures do not follow fails here first."""

from __future__ import annotations

import ast
from pathlib import Path

from scio.dashboard.contract import (
    ChangeEvent,
    DataSource,
    GraphDiagram,
    ThreadDetail,
    ThreadSummary,
    Workspace,
)
from tests.dashboard_support import DETAIL_FILES, FixtureDataSource, load_fixture


def test_every_fixture_validates_against_its_contract_model() -> None:
    roots = [Workspace.model_validate(w) for w in load_fixture("roots.json")]
    threads = [ThreadSummary.model_validate(t) for t in load_fixture("threads.json")]
    details = [ThreadDetail.model_validate(load_fixture(f)) for f in DETAIL_FILES]
    graphs = [GraphDiagram.model_validate(g) for g in load_fixture("graphs.json")]
    event = ChangeEvent.model_validate(load_fixture("change_event.json"))

    names = {w.name for w in roots}
    assert {t.root for t in threads} <= names
    assert {t.kind for t in threads} == {"build", "bootstrap", "task"}
    assert {"waiting", "crashed", "done", "in_progress", "error"} <= {t.status for t in threads}
    assert all((t.status == "waiting") == (t.waiting is not None) for t in threads)
    assert all((t.status == "crashed") == (t.crash is not None) for t in threads)
    listed = {(t.root, t.thread_id) for t in threads}
    for d in details:
        assert (d.summary.root, d.summary.thread_id) in listed
        reports = {"build": d.build, "bootstrap": d.bootstrap, "task": d.task}
        assert [k for k, v in reports.items() if v is not None] == [d.summary.kind]
    assert {g.name for g in graphs} >= {"t0", "t1", "t2", "product_new", "product_build"}
    for t in threads:
        graph = next(g for g in graphs if g.name == t.graph)
        assert set(t.next_nodes) <= set(graph.nodes), (t.thread_id, t.next_nodes)
    assert {(r.root, r.thread_id) for r in event.changed} <= listed


def _interrupt_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """``interrupt(...)`` calls in ``fn``'s own body, not in the functions it defines."""
    count, todo = 0, list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            count += node.func.id == "interrupt"
        todo.extend(ast.iter_child_nodes(node))
    return count


def test_every_node_interrupts_at_most_once() -> None:
    """The dashboard reads a stop as answered when a ``__resume__`` write sits beside its
    ``__interrupt__`` (I-134). That is exact only while no node asks twice in one run: both
    asks would share one interrupt id. I-065 already says a node that interrupts does
    nothing else."""
    root = Path(__file__).resolve().parents[2] / "scio"
    twice = [
        f"{path.relative_to(root.parent).as_posix()}:{fn.lineno} {fn.name}"
        for path in root.rglob("*.py")
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and _interrupt_calls(fn) > 1
    ]
    assert twice == []
    probe = ast.parse("def node(s):\n    a = interrupt(1)\n    b = interrupt(2)\n")
    assert _interrupt_calls(probe.body[0]) == 2  # type: ignore[arg-type]


def test_the_fixture_source_is_a_data_source() -> None:
    fixture = FixtureDataSource()
    source: DataSource = fixture  # mypy checks the protocol here
    build = next(t for t in load_fixture("threads.json") if t["kind"] == "build")
    before = source.versions(build["root"])[build["thread_id"]]
    fixture.bump(build["root"], build["thread_id"], checkpoint=False, ledger_bytes=0)
    after = source.versions(build["root"])[build["thread_id"]]
    assert after != before and after.checkpoint_id == before.checkpoint_id  # a stop arriving
