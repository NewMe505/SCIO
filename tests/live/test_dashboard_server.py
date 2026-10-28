"""The real ``create_app`` serving the real page under uvicorn (issue #68): the API and the page
were built in parallel against the contract, so this is where their routes and the WebSocket's
event shape meet. Also: the server stops cleanly, and in bounded time, with a browser still
connected and after a tab has closed (a send-only ``/ws`` handler made uvicorn's shutdown hang
or raise, I-130; a reset connection could hold ``asyncio.Server.wait_closed`` forever on
Windows, I-132). The server runs with the CLI's own shutdown bound."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from scio.cli import DASHBOARD_SHUTDOWN_S  # noqa: E402
from scio.dashboard.app import create_app  # noqa: E402
from tests.browser_support import requires_browser  # noqa: E402
from tests.dashboard_support import FixtureDataSource, load_fixture  # noqa: E402

pytestmark = requires_browser
WAIT_S = 10.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Server:
    """uvicorn in a thread, configured as ``scio dashboard`` runs it; ``stop`` asks it to exit
    and returns what ``run`` raised."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            timeout_graceful_shutdown=DASHBOARD_SHUTDOWN_S,
        )
        self.server = uvicorn.Server(config)
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self.server.run()
        except BaseException as exc:  # the test asserts there was none
            self.error = exc

    def start(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + WAIT_S
        while not self.server.started:
            assert time.monotonic() < deadline and self.thread.is_alive(), self.error
            time.sleep(0.02)
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> BaseException | None:
        self.server.should_exit = True
        self.thread.join(DASHBOARD_SHUTDOWN_S + WAIT_S)
        assert not self.thread.is_alive(), "uvicorn did not stop"
        return self.error


@pytest.fixture
def served() -> Iterator[tuple[FixtureDataSource, _Server, str]]:
    source = FixtureDataSource()
    server = _Server(create_app(source, poll_interval_s=0.05))
    base = server.start()
    try:
        yield source, server, base
    finally:
        if server.thread.is_alive():
            server.stop()


def test_the_real_app_serves_the_page_live_and_stops_cleanly(
    served: tuple[FixtureDataSource, _Server, str],
) -> None:
    from playwright.sync_api import sync_playwright

    source, server, base = served
    build = next(t for t in load_fixture("threads.json") if t["kind"] == "build")
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            # the diagram falls back to text without the CDN; this test is about the API
            context.route("https://cdnjs.cloudflare.com/**", lambda route: route.abort())
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base + "/", wait_until="domcontentloaded")
            page.get_by_text(build["thread_id"]).first.wait_for(timeout=WAIT_S * 1000)
            page.get_by_text(build["waiting"]["question"]).first.wait_for(timeout=WAIT_S * 1000)
            page.evaluate("window.__notReloaded = true")

            # a new checkpoint reaches the page through the real /ws and the real API
            source.set_summary(build["root"], build["thread_id"], phase="update_progress")
            page.get_by_text("update_progress").first.wait_for(timeout=WAIT_S * 1000)
            assert page.evaluate("window.__notReloaded === true")

            # one tab closes, another stays open: the server must still stop cleanly
            other = context.new_page()
            other.goto(base + "/", wait_until="domcontentloaded")
            other.get_by_text(build["thread_id"]).first.wait_for(timeout=WAIT_S * 1000)
            page.close()
            time.sleep(0.3)
            assert server.stop() is None
            assert errors == []
        finally:
            browser.close()
