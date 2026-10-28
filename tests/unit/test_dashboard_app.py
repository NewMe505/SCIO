"""The dashboard's server (issue #68, ADR 0017) over the fixture source: every endpoint serves
the contract's JSON, one unreadable root hides no other, ``/ws`` says hello and then pushes
only real changes, every route is a read, a foreign ``Host`` or ``Origin`` is refused, and no
site can frame the page."""

from __future__ import annotations

import logging
import mimetypes
import sqlite3
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

from scio.dashboard.app import FRAME_HEADERS, create_app, is_same_origin
from scio.dashboard.contract import ChangeEvent, ThreadDetail, ThreadSummary, ThreadVersion
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


def assert_unframeable(resp: Any) -> None:
    assert resp.headers["content-security-policy"] == "frame-ancestors 'none'", resp.url
    assert resp.headers["x-frame-options"] == "DENY", resp.url
    # the header names no script or style directive, so the page's meta CSP stays in force
    assert set(FRAME_HEADERS["Content-Security-Policy"].split()) == {"frame-ancestors", "'none'"}


class UnreadableRootSource(FixtureDataSource):
    """The SCIO root's checkpoint DB is not a database: every read of it raises."""

    bad = "SCIO"

    def list_threads(self, root: str) -> list[ThreadSummary]:
        if root == self.bad:
            raise sqlite3.DatabaseError("file is not a database")
        return super().list_threads(root)

    def thread(self, root: str, thread_id: str) -> ThreadDetail:
        if root == self.bad:
            raise sqlite3.DatabaseError("file is not a database")
        return super().thread(root, thread_id)


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


def test_an_unreadable_root_hides_no_other_root(caplog: pytest.LogCaptureFixture) -> None:
    """The page lists every root with one call; a root whose DB is not a database is skipped
    and logged once per streak, and asked for by name it answers 503 with the reason."""
    caplog.set_level(logging.WARNING, logger="scio.dashboard.app")
    good = [t for t in load_fixture("threads.json") if t["root"] != UnreadableRootSource.bad]
    with TestClient(create_app(UnreadableRootSource()), base_url=BASE) as c:
        for _ in range(3):
            resp = c.get("/api/threads")
            assert resp.status_code == 200
            assert resp.json() == good
        alone = c.get("/api/threads", params={"root": "SCIO"})
        detail = c.get("/api/threads/SCIO/run-5f0c2a1b")
        other = c.get("/api/threads", params={"root": "graphcalc-run"})
    for resp in (alone, detail):
        assert resp.status_code == 503
        assert "SCIO" in resp.json()["detail"]
        assert "file is not a database" in resp.json()["detail"]
    assert other.status_code == 200 and other.json() == good
    listing = [r for r in caplog.records if "listing" in r.getMessage()]
    assert len(listing) == 1, [r.getMessage() for r in listing]  # one per failure streak
    assert "SCIO" in listing[0].getMessage()
    assert "file is not a database" in listing[0].getMessage()


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
    for resp in (page, script, style, missing):
        assert_unframeable(resp)


def test_no_site_can_frame_the_page_or_the_api(client: TestClient) -> None:
    """A meta CSP cannot set frame-ancestors, so every HTTP response carries the header,
    errors included."""
    for url in ("/api/roots", "/api/threads", "/api/threads/SCIO/run-missing", "/api/graphs"):
        assert_unframeable(client.get(url))
    assert_unframeable(client.post("/api/roots"))  # a 405


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
        for url in ("/api/roots", "/"):
            resp = c.get(url)
            assert resp.status_code == 400
            assert_unframeable(resp)  # the frame headers wrap the Host check too
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            c.websocket_connect("ws://evil.example/ws"),
        ):
            pass
    assert denied.value.status_code == 400


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("http://127.0.0.1:8765", "127.0.0.1:8765"),
        ("https://localhost:8765", "localhost:8765"),
        ("http://127.0.0.1", "127.0.0.1"),
        ("http://LOCALHOST:8765", "localhost:8765"),
        ("http://127.0.0.1", "127.0.0.1:80"),  # the default port, spelled or not
        ("https://localhost:443", "localhost"),
    ],
)
def test_the_page_this_server_served_is_same_origin(origin: str, host: str) -> None:
    assert is_same_origin(origin, host)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",  # another local page: a dev server on another port
        "http://127.0.0.1:3000",
        "http://127.0.0.1",  # the same host on the default port is another origin
        "http://localhost:8765",  # the same port under the other loopback name
        "http://evil.example",
        "http://evil.example:8765",
        "null",
        "",
        "http://127.0.0.1.evil.example:8765",
        "http://evil.example/?http://127.0.0.1:8765",
        "http://user@127.0.0.1:8765",
        "http://127.0.0.1:8765/",
        "ws://127.0.0.1:8765",
        "file://",
    ],
)
def test_any_other_origin_is_foreign(origin: str) -> None:
    assert not is_same_origin(origin, "127.0.0.1:8765")


def test_a_missing_host_is_never_same_origin() -> None:
    assert not is_same_origin("http://127.0.0.1:8765", None)


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "http://localhost:3000", "http://127.0.0.1:3000"]
)
def test_ws_refuses_a_foreign_origin(client: TestClient, origin: str) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(WS_URL, headers={"Origin": origin}),
    ):
        pass
    assert refused.value.code == 1008


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        (WS_URL, "http://127.0.0.1"),  # TestClient sends Host 127.0.0.1 for this URL (I-128)
        ("ws://127.0.0.1:8765/ws", "http://127.0.0.1:8765"),
        ("ws://localhost:8765/ws", "http://localhost:8765"),
    ],
)
def test_ws_accepts_the_page_this_server_served(
    client: TestClient, pool: ThreadPoolExecutor, url: str, origin: str
) -> None:
    with client.websocket_connect(url, headers={"Origin": origin}) as ws:
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
