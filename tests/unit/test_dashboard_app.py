"""The dashboard's server (issue #68, ADR 0017) over the fixture source: every endpoint serves
the contract's JSON, ``/ws`` says hello and then pushes only real changes, every route is a
read, and a foreign ``Host`` or ``Origin`` is refused."""

from __future__ import annotations

import logging
import mimetypes
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

# ruff exempts pytest.importorskip from E402: the imports below need the dashboard extra
from fastapi import FastAPI
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.testclient import WebSocketDenialResponse, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from scio.dashboard.app import create_app, is_local_origin
from scio.dashboard.contract import ChangeEvent, ThreadSummary, ThreadVersion
from tests.dashboard_support import DETAIL_FILES, FixtureDataSource, load_fixture

BASE = "http://127.0.0.1"
WS_URL = "ws://127.0.0.1/ws"  # absolute: websocket_connect joins onto ws://testserver, not BASE
POLL = 0.02
WAIT = 5.0  # the bound on every receive; a pushed event takes a few POLLs


@pytest.fixture
def source() -> FixtureDataSource:
    return FixtureDataSource()


@pytest.fixture
def client(source: FixtureDataSource) -> Iterator[TestClient]:
    with TestClient(create_app(source, poll_interval_s=POLL), base_url=BASE) as c:
        yield c


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    """Runs the blocking test-session receive, so every wait is bounded by WAIT."""
    executor = ThreadPoolExecutor(max_workers=1)
    yield executor
    executor.shutdown(wait=False, cancel_futures=True)


def receive(ws: WebSocketTestSession, pool: ThreadPoolExecutor) -> ChangeEvent:
    payload: Any = pool.submit(ws.receive_json).result(timeout=WAIT)
    return ChangeEvent.model_validate(payload)


def refs(event: ChangeEvent, field: str) -> list[tuple[str, str]]:
    return [(r.root, r.thread_id) for r in getattr(event, field)]


def a_thread(kind: str) -> tuple[str, str]:
    row = next(t for t in load_fixture("threads.json") if t["kind"] == kind)
    return row["root"], row["thread_id"]


# ---- HTTP ----


def test_roots_are_the_fixture_roots(client: TestClient) -> None:
    resp = client.get("/api/roots")
    assert resp.status_code == 200
    assert resp.json() == load_fixture("roots.json")


def test_threads_aggregate_every_root_newest_first(client: TestClient) -> None:
    resp = client.get("/api/threads")
    assert resp.status_code == 200
    assert resp.json() == load_fixture("threads.json")  # the fixture is in newest-first order


def test_threads_of_one_root(client: TestClient) -> None:
    for root in ("SCIO", "graphcalc-run"):
        resp = client.get("/api/threads", params={"root": root})
        assert resp.status_code == 200
        assert resp.json() == [t for t in load_fixture("threads.json") if t["root"] == root]


def test_a_thread_without_an_update_time_lists_last() -> None:
    undated = ThreadSummary(
        root="SCIO",
        thread_id="run-undated",
        kind="task",
        status="in_progress",
        graph="t0",
        phase="route",
        checkpoint_id="c-undated",
    )

    class UndatedSource(FixtureDataSource):
        def list_threads(self, root: str) -> list[ThreadSummary]:
            rows = super().list_threads(root)
            return [undated, *rows] if root == "SCIO" else rows

    with TestClient(create_app(UndatedSource()), base_url=BASE) as c:
        ids = [t["thread_id"] for t in c.get("/api/threads").json()]
    assert ids == [t["thread_id"] for t in load_fixture("threads.json")] + ["run-undated"]


def test_unknown_root_is_404(client: TestClient) -> None:
    for url in ("/api/threads?root=nowhere", "/api/threads/nowhere/run-5f0c2a1b"):
        resp = client.get(url)
        assert resp.status_code == 404, url
        assert "nowhere" in resp.json()["detail"]


def test_unknown_thread_is_404(client: TestClient) -> None:
    resp = client.get("/api/threads/SCIO/run-missing")
    assert resp.status_code == 404
    assert "run-missing" in resp.json()["detail"]


@pytest.mark.parametrize("name", DETAIL_FILES)
def test_each_detail_round_trips_its_fixture(client: TestClient, name: str) -> None:
    fixture = load_fixture(name)
    summary = fixture["summary"]
    resp = client.get(f"/api/threads/{summary['root']}/{summary['thread_id']}")
    assert resp.status_code == 200
    assert resp.json() == fixture


def test_a_listed_thread_without_a_detail_fixture_still_serves(client: TestClient) -> None:
    resp = client.get("/api/threads/SCIO/run-old0001")
    assert resp.status_code == 200
    assert resp.json()["summary"]["status"] == "error"


def test_graphs(client: TestClient) -> None:
    resp = client.get("/api/graphs")
    assert resp.status_code == 200
    assert resp.json() == load_fixture("graphs.json")


# ---- static page ----


def test_the_page_and_its_static_files(source: FixtureDataSource, tmp_path: Path) -> None:
    page_html = "<!doctype html><title>SCIO</title>\n"
    (tmp_path / "index.html").write_text(page_html, encoding="utf-8", newline="\n")
    (tmp_path / "app.js").write_text("console.log('scio')\n", encoding="utf-8", newline="\n")
    (tmp_path / "style.css").write_text("body { margin: 0; }\n", encoding="utf-8", newline="\n")
    # a Windows registry that maps .js to text/plain: create_app pins the page's types again
    mimetypes.add_type("text/plain", ".js")
    with TestClient(create_app(source, static_dir=tmp_path), base_url=BASE) as c:
        page = c.get("/")
        script = c.get("/static/app.js")
        style = c.get("/static/style.css")
        missing = c.get("/static/nope.js")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "content-disposition" not in page.headers  # shown, not downloaded
    assert page.text == page_html
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.text == "console.log('scio')\n"
    assert style.status_code == 200
    assert style.headers["content-type"].startswith("text/css")
    assert missing.status_code == 404


def test_a_missing_static_dir_does_not_stop_the_app(
    source: FixtureDataSource, tmp_path: Path
) -> None:
    create_app(source)  # the default dir, which may not exist yet
    with TestClient(create_app(source, static_dir=tmp_path / "absent"), base_url=BASE) as c:
        assert c.get("/").status_code == 404
        assert c.get("/api/roots").status_code == 200


def test_the_poll_interval_must_be_positive(source: FixtureDataSource) -> None:
    with pytest.raises(ValueError, match="poll_interval_s"):
        create_app(source, poll_interval_s=0)


# ---- read-only and local-only ----


def test_every_route_is_a_read(source: FixtureDataSource, tmp_path: Path) -> None:
    """The read-only guarantee's structural guard: no route accepts a write method."""
    (tmp_path / "app.js").write_text("", encoding="utf-8", newline="\n")
    app: FastAPI = create_app(source, static_dir=tmp_path)
    checked: set[str] = set()
    for route in app.routes:
        assert isinstance(route, APIRoute | APIWebSocketRoute | Mount), route
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        assert set(methods) <= {"GET", "HEAD"}, (route.path, methods)
        checked.add(route.path)
    assert checked >= {
        "/",
        "/api/roots",
        "/api/threads",
        "/api/threads/{root}/{thread_id:path}",
        "/api/graphs",
    }
    with TestClient(app, base_url=BASE) as c:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert c.request(method, "/api/roots").status_code == 405
            assert c.request(method, "/static/app.js").status_code == 405


def test_a_foreign_host_is_refused(source: FixtureDataSource) -> None:
    """DNS rebinding: a page at evil.example resolved to 127.0.0.1 sends Host evil.example."""
    with TestClient(create_app(source), base_url="http://evil.example") as c:
        assert c.get("/api/roots").status_code == 400
        assert c.get("/").status_code == 400
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            c.websocket_connect("ws://evil.example/ws"),
        ):
            pass
    assert denied.value.status_code == 400


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1", "http://127.0.0.1:8765", "https://localhost:8765", "http://localhost"],
)
def test_a_local_origin_is_local(origin: str) -> None:
    assert is_local_origin(origin)


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "null",
        "",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example:8765",
        "http://evil.example/?http://127.0.0.1",
        "http://user@127.0.0.1",
        "http://127.0.0.1:8765/",
        "ws://127.0.0.1:8765",
        "file://",
    ],
)
def test_a_foreign_origin_is_not_local(origin: str) -> None:
    assert not is_local_origin(origin)


def test_ws_refuses_a_foreign_origin(client: TestClient) -> None:
    foreign = {"Origin": "http://evil.example"}
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(WS_URL, headers=foreign),
    ):
        pass
    assert refused.value.code == 1008


def test_ws_accepts_a_local_origin(client: TestClient, pool: ThreadPoolExecutor) -> None:
    with client.websocket_connect(WS_URL, headers={"Origin": "http://localhost:8765"}) as ws:
        hello = receive(ws, pool)
    assert hello.changed == [] and hello.removed == []


# ---- WebSocket pushes ----


def test_ws_says_hello_first_with_nothing_in_it(
    client: TestClient, pool: ThreadPoolExecutor
) -> None:
    with client.websocket_connect(WS_URL) as ws:
        hello = receive(ws, pool)
    assert hello.changed == [] and hello.removed == []


def test_ws_pushes_a_new_checkpoint_and_never_an_empty_event(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    root, tid = a_thread("build")
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        time.sleep(POLL * 8)  # several quiet polls: had any sent an event, it would come next
        source.bump(root, tid)
        event = receive(ws, pool)
    assert refs(event, "changed") == [(root, tid)]
    assert event.removed == []


def test_ws_pushes_a_change_right_after_the_hello(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    """The baseline is taken before the hello, so a change the page could not have fetched
    yet is never absorbed into it."""
    root, tid = a_thread("task")
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        source.bump(root, tid)
        event = receive(ws, pool)
    assert refs(event, "changed") == [(root, tid)]


def test_ws_pushes_a_stop_that_adds_no_checkpoint(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    root, tid = a_thread("build")
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        source.bump(root, tid, checkpoint=False, ledger_bytes=0)  # only a pending write
        event = receive(ws, pool)
    assert refs(event, "changed") == [(root, tid)]


def test_ws_pushes_a_new_thread(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        source.bump("SCIO", "run-brand-new")
        event = receive(ws, pool)
    assert refs(event, "changed") == [("SCIO", "run-brand-new")]


def test_ws_pushes_a_removed_thread(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    root, tid = a_thread("bootstrap")
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        source.drop(root, tid)
        event = receive(ws, pool)
    assert event.changed == []
    assert refs(event, "removed") == [(root, tid)]


def test_ws_ignores_what_the_client_sends(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    root, tid = a_thread("task")
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        ws.send_text("hello from the page")
        source.bump(root, tid)
        event = receive(ws, pool)
    assert refs(event, "changed") == [(root, tid)]


def test_ws_stops_polling_once_the_client_leaves(
    client: TestClient, source: FixtureDataSource, pool: ThreadPoolExecutor
) -> None:
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        ws.close()  # the server reads the disconnect at its next wait between polls
        deadline = time.monotonic() + WAIT
        polls = source.calls.count("versions:SCIO")
        while True:  # settled: no poll during ten intervals; a server that keeps polling never is
            time.sleep(POLL * 10)
            now = source.calls.count("versions:SCIO")
            if now == polls:
                break
            assert time.monotonic() < deadline, "the server kept polling after the client left"
            polls = now


def test_ws_skips_a_failing_root_and_keeps_the_socket(
    client: TestClient,
    source: FixtureDataSource,
    pool: ThreadPoolExecutor,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A root whose read raises is logged once and skipped: its threads are not reported
    removed, the other roots still push, and the root pushes again once it reads."""
    healthy = source.versions
    broken = {"SCIO"}

    def versions(root: str) -> dict[str, ThreadVersion]:
        if root in broken:
            raise OSError("database is locked")
        return healthy(root)

    monkeypatch.setattr(source, "versions", versions)
    caplog.set_level(logging.WARNING, logger="scio.dashboard.app")
    with client.websocket_connect(WS_URL) as ws:
        receive(ws, pool)
        time.sleep(POLL * 8)  # several failing polls
        source.bump("graphcalc-run", "new-1")
        event = receive(ws, pool)
        assert refs(event, "changed") == [("graphcalc-run", "new-1")]
        assert event.removed == []  # SCIO's threads did not vanish because it failed
        broken.clear()
        event = receive(ws, pool)  # SCIO read for the first time: all its threads are new
        assert {tid for root, tid in refs(event, "changed") if root == "SCIO"} == {
            t["thread_id"] for t in load_fixture("threads.json") if t["root"] == "SCIO"
        }
    warnings = [r for r in caplog.records if "SCIO" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]  # one per failure streak
    assert "database is locked" in warnings[0].getMessage()
