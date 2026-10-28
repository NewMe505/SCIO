"""The dashboard page in a real browser (issue #68, ADR 0017).

The page (``scio/dashboard/static``) runs against a tiny Starlette server of this module's own:
the static files, the API routes over :class:`~tests.dashboard_support.FixtureDataSource`, and
a ``/ws`` that sends the hello and then whatever :class:`ChangeEvent` a test publishes. It
does not use ``scio.dashboard.app``, so the page is tested against the contract alone. The
Mermaid CDN is routed to a stub that records the text it is asked to draw, so the tests are
offline and deterministic; the second test kills the CDN and the page still works.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import queue
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Locator,
    Page,
    Request,
    Route,
    expect,
)

from scio.dashboard.contract import (
    ChangeEvent,
    ThreadDetail,
    ThreadRef,
    ThreadSummary,
    UnknownRoot,
    UnknownThread,
)
from tests.browser_support import requires_browser
from tests.dashboard_support import FixtureDataSource, load_fixture

if TYPE_CHECKING:
    import uvicorn
    from starlette.applications import Starlette

# a serializer warning or an unraisable error from a pending route is a bug here, not noise
pytestmark = pytest.mark.filterwarnings("error::UserWarning")

STATIC = Path(__file__).resolve().parents[2] / "scio" / "dashboard" / "static"
CDN = "https://cdnjs.cloudflare.com/**"
MERMAID_TAG = re.compile(
    r'<script id="mermaid-js" src="https://cdnjs\.cloudflare\.com/ajax/libs/mermaid/'
    r'\d+\.\d+\.\d+/mermaid\.min\.js" integrity="(sha512-[A-Za-z0-9+/=]+)" '
    r'crossorigin="anonymous"[^>]*\basync></script>'
)
STUB_MERMAID = """
window.__mermaidTexts = [];
window.mermaid = {
  initialize: function (config) { window.__mermaidConfig = config; },
  render: async function (id, text) {
    window.__mermaidTexts.push(text);
    return { svg: '<svg xmlns="http://www.w3.org/2000/svg" id="' + id + '" data-stub="mermaid"'
      + ' width="160" height="40"><text x="8" y="24">stub diagram</text></svg>' };
  },
};
"""
STUB_SRI = "sha512-" + base64.b64encode(hashlib.sha512(STUB_MERMAID.encode()).digest()).decode()
FORBIDDEN_IN_APP_JS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    'setTimeout("',
    'setInterval("',
)

SELECT_RESUME = """() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector('code.resume'));
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
}"""

BUILD = ("graphcalc-run", "build-todo_cli-m1")
P1 = ("graphcalc-run", "build-todo_cli-m1-p1-a1")
P2 = ("graphcalc-run", "build-todo_cli-m1-p2-a1")
BOOTSTRAP = ("graphcalc-run", "new-1")
TASK = ("SCIO", "run-5f0c2a1b")
CRASHED = ("SCIO", "build-demo-m1")


def _index_with_stub_digest() -> str:
    """``index.html`` as served to the tests: the pinned file's SRI digest swapped for the
    stub's, so the browser still checks integrity (and CORS) on what it loads."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    found = MERMAID_TAG.findall(html)
    assert len(found) == 1, "index.html must load exactly one pinned, integrity-checked Mermaid"
    swapped = html.replace(found[0], STUB_SRI)
    assert swapped.count(STUB_SRI) == 1
    return swapped


class Hub:
    """Fans published ChangeEvents out to every open ``/ws`` connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inboxes: list[queue.Queue[str]] = []
        self.closed = threading.Event()
        self.generation = 0
        """A connection opened in an older generation closes itself (a dropped socket)."""

    def drop_connections(self) -> None:
        with self._lock:
            self.generation += 1

    def connections(self) -> int:
        with self._lock:
            return len(self._inboxes)

    def subscribe(self) -> queue.Queue[str]:
        inbox: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._inboxes.append(inbox)
        return inbox

    def unsubscribe(self, inbox: queue.Queue[str]) -> None:
        with self._lock:
            if inbox in self._inboxes:
                self._inboxes.remove(inbox)

    def publish(self, event: ChangeEvent) -> None:
        text = event.model_dump_json()
        with self._lock:
            for inbox in self._inboxes:
                inbox.put(text)


class PageServer:
    """The page's static files plus the API over the fixtures, in a uvicorn thread.
    ``overrides`` changes a thread's summary (in the list and in its detail) the way a new
    checkpoint would; ``detail_overrides`` changes other fields of its detail."""

    def __init__(self, index_html: str | None = None) -> None:
        self.source = FixtureDataSource()
        self.overrides: dict[tuple[str, str], dict[str, Any]] = {}
        self.detail_overrides: dict[tuple[str, str], dict[str, Any]] = {}
        self.hub = Hub()
        self.index_html = index_html if index_html is not None else _index_with_stub_digest()
        self.url = ""
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._died: list[BaseException] = []

    def summary(self, row: ThreadSummary) -> ThreadSummary:
        update = self.overrides.get((row.root, row.thread_id))
        return row.model_copy(update=update) if update else row

    def threads(self, root: str | None) -> list[ThreadSummary]:
        names = [root] if root else [w.name for w in self.source.roots()]
        rows = [self.summary(t) for name in names for t in self.source.list_threads(name)]
        oldest = datetime.min.replace(tzinfo=UTC)
        return sorted(rows, key=lambda t: t.updated_at or oldest, reverse=True)

    def publish(self, *changed: tuple[str, str]) -> None:
        refs = [ThreadRef(root=root, thread_id=tid) for root, tid in changed]
        self.hub.publish(ChangeEvent(changed=refs, at=datetime.now(UTC)))

    def calls(self, name: str) -> int:
        return self.source.calls.count(name)

    def start(self) -> None:
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = uvicorn.Config(
            _build_app(self),
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
        server = uvicorn.Server(config)

        def run() -> None:
            try:
                server.run(sockets=[sock])
            except BaseException as exc:  # reported by start() or stop()
                self._died.append(exc)

        self._server = server
        self._thread = threading.Thread(target=run, name="dashboard-page-server", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if not self._thread.is_alive():
                raise RuntimeError(f"the page server died on start: {self._died!r}")
            if time.monotonic() > deadline:
                raise TimeoutError("the page server did not start in 20 s")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{port}"

    def stop(self) -> None:
        self.hub.closed.set()  # first, so every /ws loop ends and shutdown can finish
        _wait_until(lambda: self.hub.connections() == 0, "every /ws handler has returned", 5.0)
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=15)
            assert not self._thread.is_alive(), "the page server did not stop"
        assert not self._died, self._died


def _build_app(server: PageServer) -> Starlette:
    from starlette.applications import Starlette
    from starlette.requests import Request as HttpRequest
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Mount, Route, WebSocketRoute
    from starlette.staticfiles import StaticFiles
    from starlette.websockets import WebSocket, WebSocketDisconnect

    async def index(request: HttpRequest) -> Response:
        return HTMLResponse(server.index_html)

    async def roots(request: HttpRequest) -> Response:
        return JSONResponse([w.model_dump(mode="json") for w in server.source.roots()])

    async def threads(request: HttpRequest) -> Response:
        try:
            rows = server.threads(request.query_params.get("root"))
        except UnknownRoot:
            return JSONResponse({"detail": "unknown root"}, status_code=404)
        return JSONResponse([t.model_dump(mode="json") for t in rows])

    async def thread(request: HttpRequest) -> Response:
        root, tid = request.path_params["root"], request.path_params["thread_id"]
        try:
            detail = server.source.thread(root, tid)
        except (UnknownRoot, UnknownThread):
            return JSONResponse({"detail": "unknown thread"}, status_code=404)
        payload = detail.model_dump(mode="json")
        payload["summary"] = server.summary(detail.summary).model_dump(mode="json")
        payload.update(server.detail_overrides.get((root, tid), {}))
        # validated, so an override a test gets wrong fails here, not in the page
        return JSONResponse(ThreadDetail.model_validate(payload).model_dump(mode="json"))

    async def graphs(request: HttpRequest) -> Response:
        return JSONResponse([g.model_dump(mode="json") for g in server.source.graphs()])

    async def until_closed_by_the_page(websocket: WebSocket) -> None:
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass

    async def changes(websocket: WebSocket) -> None:
        await websocket.accept()
        inbox = server.hub.subscribe()
        generation = server.hub.generation
        # A handler that only sends never sees the page's close frame; uvicorn's shutdown
        # then answers a closing connection and raises InvalidState. So it also receives.
        gone = asyncio.ensure_future(until_closed_by_the_page(websocket))
        try:
            await websocket.send_text(ChangeEvent(at=datetime.now(UTC)).model_dump_json())
            while not server.hub.closed.is_set() and not gone.done():
                if server.hub.generation != generation:
                    await websocket.close()
                    return
                try:
                    text = inbox.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.02)
                    continue
                await websocket.send_text(text)
        except (WebSocketDisconnect, RuntimeError):  # the page went away mid-send
            return
        finally:
            gone.cancel()
            server.hub.unsubscribe(inbox)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/roots", roots),
            Route("/api/threads", threads),
            Route("/api/threads/{root}/{thread_id}", thread),
            Route("/api/graphs", graphs),
            WebSocketRoute("/ws", changes),
            Mount("/static", app=StaticFiles(directory=STATIC)),
        ]
    )


@dataclass
class Watched:
    """A page with every page error, console error and off-machine request recorded."""

    page: Page
    url: str
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    foreign_requests: list[str] = field(default_factory=list)

    def goto(self, hash_route: str = "#/") -> None:
        # not "load": that waits for the async Mermaid script, which may never answer
        self.page.goto(self.url + "/" + hash_route, wait_until="domcontentloaded")
        if hash_route.startswith("#/thread/"):  # a hash change is a same-document navigation
            ref = hash_route.removeprefix("#/thread/")
            self.page.wait_for_selector(f'.head[data-thread="{ref}"]', timeout=10_000)
        else:
            self.page.wait_for_selector(".root-section", timeout=10_000)
        self.settle()

    def settle(self) -> None:
        """The socket said hello and the refetch it started has finished."""
        self.page.wait_for_selector('#conn[data-state="live"]', timeout=10_000)
        self.wait_idle()

    def wait_idle(self) -> None:
        # selectors, not wait_for_function: its polling evals a string, which the page's
        # CSP (no 'unsafe-eval') refuses, so it fails whenever the first check is false
        self.page.wait_for_selector('html[data-refresh="idle"]', state="attached")

    def mermaid_texts(self) -> list[str]:
        texts: list[str] = self.page.evaluate("window.__mermaidTexts || []")
        return texts

    def wait_for_mermaid_text(self, needle: str) -> None:
        _wait_until(lambda: any(needle in t for t in self.mermaid_texts()), f"Mermaid got {needle}")

    def assert_clean(self) -> None:
        assert self.page_errors == [], self.page_errors
        assert self.console_errors == [], self.console_errors
        assert self.foreign_requests == [], self.foreign_requests


CdnMode = Literal["stub", "abort", "hang"]


@pytest.fixture(scope="module")
def page_server() -> Iterator[PageServer]:
    pytest.importorskip("starlette")
    pytest.importorskip("uvicorn")
    server = PageServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def served(page_server: PageServer) -> PageServer:
    page_server.overrides.clear()
    page_server.detail_overrides.clear()
    return page_server


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        chromium = p.chromium.launch()
        try:
            yield chromium
        finally:
            chromium.close()


OpenPage = Callable[..., Watched]


@pytest.fixture
def open_page(browser: Browser, served: PageServer) -> Iterator[OpenPage]:
    contexts: list[BrowserContext] = []
    hanging: list[Route] = []

    def opener(cdn: CdnMode = "stub", width: int = 1280, height: int = 900) -> Watched:
        context = browser.new_context(viewport={"width": width, "height": height})
        contexts.append(context)
        page = context.new_page()
        watched = Watched(page=page, url=served.url)

        def on_console(message: ConsoleMessage) -> None:
            if message.type == "error":
                watched.console_errors.append(message.text)

        def on_request(request: Request) -> None:
            if not request.url.startswith((served.url, "https://cdnjs.cloudflare.com/", "data:")):
                watched.foreign_requests.append(request.url)

        def on_cdn(route: Route) -> None:
            if cdn == "stub":
                route.fulfill(
                    status=200,
                    body=STUB_MERMAID,
                    headers={
                        "Content-Type": "application/javascript; charset=utf-8",
                        "Access-Control-Allow-Origin": "*",
                    },
                )
            elif cdn == "abort":
                route.abort()
            else:  # "hang": not answered while the test runs, the way a dead CDN behaves
                hanging.append(route)

        page.on("pageerror", lambda exc: watched.page_errors.append(str(exc)))
        page.on("console", on_console)
        page.on("request", on_request)
        page.route(CDN, on_cdn)
        return watched

    yield opener
    for route in hanging:  # answered at last, so no handler is cancelled at close
        route.abort()
    for context in contexts:
        context.close()


def _wait_until(check: Callable[[], bool], what: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting until {what}"
        time.sleep(0.02)


def _publish_and_wait_for_the_refetch(
    watched: Watched, served: PageServer, ref: tuple[str, str]
) -> None:
    """Publish a change and wait until the page has refetched the list and finished that
    refresh: the page marks itself busy when the event arrives, before it fetches."""
    listed = served.calls(f"list_threads:{ref[0]}")
    served.publish(ref)
    _wait_until(lambda: served.calls(f"list_threads:{ref[0]}") > listed, "the list is refetched")
    watched.wait_idle()


def _row(page: Page, ref: tuple[str, str]) -> Locator:
    return page.locator(f'tr[data-thread="{ref[0]}/{ref[1]}"]')


def _href(ref: tuple[str, str]) -> str:
    return f"#/thread/{ref[0]}/{ref[1]}"


def test_the_page_never_writes_html_from_data_or_evaluates_code() -> None:
    """Thread text is model output: the page builds its DOM with textContent only, and
    loads one pinned, integrity-checked Mermaid from the CDN and nothing else off-machine."""
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    found = [construct for construct in FORBIDDEN_IN_APP_JS if construct in app_js]
    assert found == [], f"app.js must not use {found}"
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert len(MERMAID_TAG.findall(html)) == 1
    assert '<script src="/static/app.js"></script>' in html
    assert '<link rel="stylesheet" href="/static/style.css">' in html
    # app.js comes first, so it hears the Mermaid tag's load or error event
    assert html.index("/static/app.js") < html.index('id="mermaid-js"')
    srcs = re.findall(r'\b(?:src|href)="([^"]+)"', html)
    assert all(s.startswith(("/static/", "#", "data:", "https://cdnjs.")) for s in srcs), srcs


@requires_browser
def test_the_list_shows_every_thread_grouped_with_its_badges(
    open_page: OpenPage, served: PageServer
) -> None:
    watched = open_page()
    watched.goto()
    page = watched.page
    threads = load_fixture("threads.json")
    for t in threads:
        ref = (t["root"], t["thread_id"])
        link = _row(page, ref).locator("a.tid")
        expect(link).to_have_text(t["thread_id"])
        expect(link).to_have_attribute("href", _href(ref))
        expect(_row(page, ref).locator(".c-cost")).to_have_text(f"${t['usd']:.4f}")
    assert page.locator("tr.row").count() == len(threads)
    # grouped by workspace, then product; the feature runs sit under their build
    product = page.locator('section[data-root="graphcalc-run"] tbody[data-product="todo_cli"]')
    order = product.locator("tr.row").evaluate_all("rows => rows.map(r => r.dataset.thread)")
    assert order[:3] == [
        "graphcalc-run/" + BUILD[1],
        "graphcalc-run/" + P1[1],
        "graphcalc-run/" + P2[1],
    ]
    expect(_row(page, P1)).to_have_class(re.compile(r"\bchild\b"))
    expect(page.locator('section[data-root="SCIO"] tbody[data-product="none"]')).to_have_count(1)
    # waiting: the question and the resume command, as text to copy
    build = next(t for t in threads if t["thread_id"] == BUILD[1])
    waiting = page.locator(f'tr[data-sub-for="{BUILD[0]}/{BUILD[1]}"]')
    expect(waiting.locator(".waiting-for-you")).to_have_text("waiting for you")
    expect(waiting.locator(".question")).to_have_text(build["waiting"]["question"])
    expect(waiting.locator("code.resume")).to_have_text(build["waiting"]["payload"]["resume"])
    expect(_row(page, BUILD).locator(".badge")).to_have_text("waiting")
    # crashed: a red badge, the crash text in a details; an error row muted with its error
    crashed = next(t for t in threads if t["thread_id"] == CRASHED[1])
    expect(_row(page, CRASHED).locator(".badge.s-crashed")).to_have_attribute(
        "title", crashed["crash"]
    )
    crash_box = page.locator(f'tr[data-sub-for="{CRASHED[0]}/{CRASHED[1]}"] details pre')
    expect(crash_box).to_have_text(crashed["crash"])
    error_row = _row(page, ("SCIO", "run-old0001"))
    expect(error_row).to_have_class(re.compile(r"\bmuted-row\b"))
    expect(page.locator('tr[data-sub-for="SCIO/run-old0001"]')).to_contain_text("SCIOState")
    # read-only: nothing on the page acts
    assert page.locator("button, form, input, select, textarea").count() == 0
    watched.assert_clean()


@requires_browser
def test_the_build_view_lights_the_current_node_and_links_its_features(
    open_page: OpenPage, served: PageServer
) -> None:
    watched = open_page()
    watched.goto()
    page = watched.page
    _row(page, BUILD).locator("a.tid").click()
    expect(page.locator("h1.thread-title")).to_have_text("M1: Milestone 1")
    watched.settle()
    for feature, ref in (("P1", P1), ("P2", P2)):
        row = page.locator(f'.features tr[data-feature="{feature}"]')
        expect(row).to_have_count(1)
        expect(row.locator("a.feature-link")).to_have_attribute("href", _href(ref))
        expect(row.locator(".badge")).to_have_text("passed")
    watched.wait_for_mermaid_text("class merge_wait current")
    expect(page.locator('.diagram svg[data-stub="mermaid"]')).to_have_count(1)
    config = page.evaluate("window.__mermaidConfig")
    assert config == {"startOnLoad": False, "securityLevel": "strict"}
    detail = load_fixture("thread_build.json")
    expect(page.locator(".head .waiting-box .question")).to_have_text(
        detail["summary"]["waiting"]["question"]
    )
    expect(page.locator(".integration-lines")).to_contain_text("[acceptance] passed (cli)")
    expect(page.locator(".integration-card .badge")).to_have_text("passed")
    expect(page.locator(".total-cost")).to_have_text("$0.0824")
    bars = len(detail["cost_by_role"]) + len(detail["cost_by_feature"])
    expect(page.locator(".cost-card .bar-row")).to_have_count(bars)
    expect(page.locator(".cost-card svg rect.bar").first).to_be_attached()
    expect(page.locator(".timeline-card tbody tr")).to_have_count(len(detail["timeline"]))
    expect(page.locator(".progress-card")).to_contain_text("0 of 3")
    # a deep link survives a reload
    page.reload(wait_until="domcontentloaded")
    watched.settle()
    expect(page.locator("h1.thread-title")).to_have_text("M1: Milestone 1")
    watched.assert_clean()


@requires_browser
def test_the_build_view_marks_unrun_and_unlinked_features(
    open_page: OpenPage, served: PageServer
) -> None:
    detail = load_fixture("thread_build.json")
    links = detail["feature_threads"]
    served.detail_overrides[BUILD] = {
        "milestone_features": ["P1", "P2", "P3"],  # P3 planned, never run
        "feature_threads": [links[0], {**links[1], "present": False}],  # P2's thread is gone
        "pr_url": "javascript:alert(1)",  # model output never becomes a live link
        "pr_number": 7,
    }
    watched = open_page()
    watched.goto(_href(BUILD))
    page = watched.page
    order = page.locator(".features tr[data-feature]").evaluate_all(
        "rows => rows.map(r => r.dataset.feature)"
    )
    assert order == ["P1", "P2", "P3"]
    p3 = page.locator('.features tr[data-feature="P3"]')
    expect(p3.locator(".badge")).to_have_text("not run")
    expect(p3.locator(".feature-thread")).to_have_text("not run")
    expect(page.locator('.features tr[data-feature="P1"] a.feature-link')).to_have_count(1)
    p2 = page.locator('.features tr[data-feature="P2"]')
    expect(p2.locator("a")).to_have_count(0)
    expect(p2.locator(".feature-thread")).to_have_text(P2[1])
    expect(page.locator(".integration-card a")).to_have_count(0)
    expect(page.locator(".integration-card")).to_contain_text("PR #7")
    watched.assert_clean()


@requires_browser
def test_a_live_redraw_waits_while_text_is_selected(
    open_page: OpenPage, served: PageServer
) -> None:
    """Copying the resume command is not cut short by a refresh."""
    watched = open_page()
    watched.goto()
    page = watched.page
    page.evaluate(SELECT_RESUME)
    served.overrides[TASK] = {"phase": "review"}
    _publish_and_wait_for_the_refetch(watched, served, TASK)
    expect(_row(page, TASK).locator(".phase")).to_have_text("implement")
    assert page.evaluate("String(window.getSelection())").startswith("scio build")
    page.evaluate("window.getSelection().removeAllRanges()")
    expect(_row(page, TASK).locator(".phase")).to_have_text("review", timeout=1500)
    # the wait is bounded: a selection left behind holds a redraw for 3 s at most ...
    page.evaluate(SELECT_RESUME)
    served.overrides[TASK] = {"phase": "debug"}
    _publish_and_wait_for_the_refetch(watched, served, TASK)
    expect(_row(page, TASK).locator(".phase")).to_have_text("review")
    expect(_row(page, TASK).locator(".phase")).to_have_text("debug", timeout=5000)
    # ... and none once the window loses focus (the user went to the terminal)
    page.evaluate(SELECT_RESUME)
    served.overrides[TASK] = {"phase": "advise"}
    _publish_and_wait_for_the_refetch(watched, served, TASK)
    expect(_row(page, TASK).locator(".phase")).to_have_text("debug")
    page.evaluate("window.dispatchEvent(new Event('blur'))")
    expect(_row(page, TASK).locator(".phase")).to_have_text("advise", timeout=1500)
    watched.assert_clean()


@requires_browser
def test_a_dropped_socket_reconnects_and_catches_up(
    open_page: OpenPage, served: PageServer
) -> None:
    watched = open_page()
    watched.goto()
    page = watched.page
    page.evaluate("window.__marker = 'kept'")
    served.hub.drop_connections()
    page.wait_for_selector('#conn[data-state="reconnecting"]', timeout=5000)
    # changed while the page was not listening: no event will ever name it
    served.overrides[TASK] = {"phase": "gate"}
    page.wait_for_selector('#conn[data-state="live"]', timeout=5000)  # the 1 s backoff
    expect(_row(page, TASK).locator(".phase")).to_have_text("gate")  # the hello refetched
    assert page.evaluate("window.__marker") == "kept"
    watched.assert_clean()


@requires_browser
def test_the_feature_bootstrap_and_summary_only_views_render(
    open_page: OpenPage, served: PageServer
) -> None:
    watched = open_page()
    page = watched.page
    watched.goto(_href(P2))
    feature = load_fixture("thread_feature.json")
    expect(page.locator("h1.thread-title")).to_have_text(feature["task"]["title"])
    expect(page.locator(".run-card")).to_contain_text("T1")
    expect(page.locator(".verify-lines")).to_contain_text("all tests passed: 5 passed in 0.14s")
    expect(page.locator(".escalations-card")).to_contain_text("No escalations.")
    expect(page.locator(".cost-card table.roles tbody tr")).to_have_count(
        len(feature["task"]["by_role"])
    )
    expect(page.locator(".head a.parent-link")).to_have_attribute("href", _href(BUILD))
    watched.wait_for_mermaid_text("triage(triage)")  # the t1 graph, nothing lit: it ended
    assert not any("current\n" in t for t in watched.mermaid_texts() if "triage(" in t)

    watched.goto(_href(BOOTSTRAP))
    bootstrap = load_fixture("thread_bootstrap.json")
    expect(page.locator(".questions li")).to_have_text(bootstrap["questions"])
    expect(page.locator(".revisions li")).to_have_text(
        [f"Round 1: {bootstrap['revision_notes'][0]}"]
    )
    expect(page.locator(".product-card")).to_contain_text("64a835e9ee6d")
    expect(page.locator(".cost-card .bar-row")).to_have_count(2)

    # a build with only its summary: the header, its crash and the lit graph still show
    watched.goto(_href(CRASHED))
    expect(page.locator(".head .crash-box pre")).to_contain_text("could not add label")
    watched.wait_for_mermaid_text("class issues current")
    watched.assert_clean()


@requires_browser
def test_a_change_event_refreshes_the_list_in_place(
    open_page: OpenPage, served: PageServer
) -> None:
    watched = open_page(height=420)
    watched.goto()
    page = watched.page
    page.evaluate("window.__marker = 'kept'; window.scrollTo(0, 120)")
    scrolled = page.evaluate("window.scrollY")
    assert scrolled > 0
    expect(_row(page, TASK).locator(".phase")).to_have_text("implement")
    served.overrides[TASK] = {"phase": "verify", "next_nodes": ["verify"]}
    served.publish(TASK)
    expect(_row(page, TASK).locator(".phase")).to_have_text("verify")
    assert page.evaluate("window.__marker") == "kept"  # no reload
    assert page.evaluate("window.scrollY") == scrolled
    watched.assert_clean()


@requires_browser
def test_a_feature_change_refreshes_its_open_build(open_page: OpenPage, served: PageServer) -> None:
    watched = open_page()
    watched.goto(_href(BUILD))
    page = watched.page
    page.evaluate("window.__marker = 'kept'")
    detail_call = f"thread:{BUILD[0]}/{BUILD[1]}"
    fetched = served.calls(detail_call)
    # a change to an unrelated thread refetches the list, not the open build
    _publish_and_wait_for_the_refetch(watched, served, TASK)
    assert served.calls(detail_call) == fetched
    # a change to its feature run P2 refetches the build
    served.overrides[BUILD] = {
        "phase": "update_progress",
        "next_nodes": ["update_progress"],
        "status": "in_progress",
        "waiting": None,
    }
    served.publish(P2)
    expect(page.locator(".head-phase")).to_have_text("update_progress")
    expect(page.locator(".head .waiting-box")).to_have_count(0)
    watched.wait_for_mermaid_text("class update_progress current")
    assert served.calls(detail_call) == fetched + 1
    assert page.evaluate("window.__marker") == "kept"
    watched.assert_clean()


@requires_browser
def test_the_page_fits_a_phone_screen(open_page: OpenPage, served: PageServer) -> None:
    watched = open_page(width=390, height=844)
    page = watched.page
    for route in ("#/", _href(BUILD), _href(P2), _href(BOOTSTRAP)):
        watched.goto(route)
        if route != "#/":
            page.wait_for_selector('.diagram[data-drawn="svg"]', state="attached")
        width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert width[0] <= width[1], f"{route} scrolls sideways: {width}"
    watched.assert_clean()


@requires_browser
@pytest.mark.parametrize("cdn", ["abort", "hang"])
def test_the_page_works_without_the_cdn(
    open_page: OpenPage, served: PageServer, cdn: CdnMode
) -> None:
    watched = open_page(cdn=cdn)
    watched.goto()
    page = watched.page
    assert page.locator("tr.row").count() == len(load_fixture("threads.json"))
    watched.goto(_href(BUILD))
    expect(page.locator(".features tr[data-feature]")).to_have_count(2)
    fallback = page.locator(".graph-card .diagram-fallback")
    expect(fallback.locator("pre.diagram-text")).to_contain_text("merge_wait(merge_wait)")
    expect(fallback.locator(".current-node")).to_have_text("merge_wait")
    assert page.evaluate("typeof window.mermaid") == "undefined"
    assert watched.page_errors == []
    # the aborted script is the only thing the console may complain about
    assert all("Failed to load resource" in e for e in watched.console_errors), (
        watched.console_errors
    )
