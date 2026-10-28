"""The dashboard's fixtures speak the contract (issue #68): the API and page tests run against
these payloads, so a contract change that the fixtures do not follow fails here first."""

from __future__ import annotations

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
    assert {"waiting", "done", "in_progress", "error"} <= {t.status for t in threads}
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


def test_the_fixture_source_is_a_data_source() -> None:
    fixture = FixtureDataSource()
    source: DataSource = fixture  # mypy checks the protocol here
    build = next(t for t in load_fixture("threads.json") if t["kind"] == "build")
    before = source.versions(build["root"])[build["thread_id"]]
    fixture.bump(build["root"], build["thread_id"])
    assert source.versions(build["root"])[build["thread_id"]] != before
