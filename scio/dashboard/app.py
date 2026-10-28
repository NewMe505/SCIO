"""The dashboard's server (issue #68, ADR 0017): :func:`create_app` serves a
:class:`~scio.dashboard.contract.DataSource` as JSON, the page in ``static/``, and ``/ws``, which
pushes a :class:`~scio.dashboard.contract.ChangeEvent` whenever a poll of
:meth:`~scio.dashboard.contract.DataSource.versions` finds a thread new, changed or gone. Every
route is a read; there is no write route (``test_every_route_is_a_read`` guards it). One root
that cannot be read never hides the others: the threads list skips it and logs why. The server
binds 127.0.0.1, yet any web page the user visits can reach it: ``TrustedHostMiddleware``
refuses a foreign ``Host`` (DNS rebinding), ``/ws`` accepts only the page this server served
(``Origin`` equal to ``Host``, so not another local dev server either), and every response
forbids framing. Needs the ``dashboard`` extra; the CLI imports this module lazily."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from scio.dashboard.contract import (
    ChangeEvent,
    DataSource,
    ThreadRef,
    ThreadSummary,
    ThreadVersion,
    UnknownRoot,
    UnknownThread,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
"""The page: ``index.html``, ``app.js``, ``style.css``."""

ORIGIN = re.compile(r"(?P<scheme>https?)://(?P<netloc>[^/?#@\s]+)", re.IGNORECASE)
"""An ``Origin`` header as a browser serializes it: a scheme and ``host[:port]``, nothing else
(no user info, no path). ``null`` and every other scheme do not match."""

DEFAULT_PORTS = {"http": "80", "https": "443"}

PAGE_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
"""Pinned for the page's files: on Windows ``mimetypes`` reads the registry, which can map
``.js`` to ``text/plain``, and a browser drops a stylesheet that is not ``text/css``."""

FRAME_HEADERS = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY"}
"""On every HTTP response: no site may frame the page (clickjacking). A ``<meta>`` CSP cannot
set ``frame-ancestors``; the browser enforces this header and the page's own meta policy both,
and this one names no script or style directive, so it loosens nothing."""

POLICY_VIOLATION = 1008
"""The WebSocket close code for a refused ``Origin`` (RFC 6455 section 7.4.1)."""

Snapshot = dict[str, dict[str, ThreadVersion]]
"""Root name -> thread id -> its version, as of one poll."""


def _host_port(netloc: str, scheme: str) -> str:
    """``host[:port]`` lowercased, the scheme's default port dropped, so ``Origin`` and
    ``Host`` compare equal however each spells the port."""
    netloc = netloc.lower()
    default = DEFAULT_PORTS.get(scheme, "")
    return netloc.removesuffix(f":{default}") if default else netloc


def is_same_origin(origin: str, host: str | None) -> bool:
    """``origin`` is ``http(s)://`` plus the request's own ``Host`` header, port included: the
    page this server served. Another port on localhost (a dev server), another host, ``null``
    or a missing ``Host`` is a foreign page."""
    match = ORIGIN.fullmatch(origin)
    if match is None or host is None:
        return False
    scheme = match["scheme"].lower()
    return _host_port(match["netloc"], scheme) == _host_port(host, scheme)


def newest_first(rows: Sequence[ThreadSummary]) -> list[ThreadSummary]:
    """Newest ``updated_at`` first, a thread without one last; ties keep their order."""

    def key(t: ThreadSummary) -> tuple[bool, float]:
        return (t.updated_at is None, -t.updated_at.timestamp() if t.updated_at else 0.0)

    return sorted(rows, key=key)


def diff_versions(previous: Snapshot, current: Snapshot) -> ChangeEvent:
    """A thread in ``current`` whose version is new or differs goes to ``changed``; a thread of
    ``previous`` that ``current`` no longer has goes to ``removed``."""
    changed = [
        ThreadRef(root=root, thread_id=tid)
        for root, threads in current.items()
        for tid, version in threads.items()
        if previous.get(root, {}).get(tid) != version
    ]
    removed = [
        ThreadRef(root=root, thread_id=tid)
        for root, threads in previous.items()
        for tid in threads
        if tid not in current.get(root, {})
    ]
    return ChangeEvent(changed=changed, removed=removed, at=datetime.now(UTC))


def _reason(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class FailureStreaks:
    """Logs a root that cannot be read without filling the terminal: the first failure of a
    streak at warning, the repeats at debug, the recovery at info. Thread-safe, since the sync
    endpoints run in FastAPI's threadpool and share one."""

    def __init__(self, what: str) -> None:
        self._what = what
        self._failing: set[str] = set()
        self._lock = threading.Lock()

    def failed(self, root: str, exc: BaseException) -> None:
        with self._lock:
            first = root not in self._failing
            self._failing.add(root)
        level = logging.WARNING if first else logging.DEBUG
        log.log(level, "dashboard: %s root %s failed, skipped: %s", self._what, root, _reason(exc))

    def succeeded(self, root: str) -> None:
        with self._lock:
            if root not in self._failing:
                return
            self._failing.discard(root)
        log.info("dashboard: %s root %s works again", self._what, root)


class FrameDenyMiddleware:
    """Adds :data:`FRAME_HEADERS` to every HTTP response, the 404s, 405s and the Host check's
    400s included (it is added last, so it wraps the other middleware)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in FRAME_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _snapshot(source: DataSource, previous: Snapshot, streaks: FailureStreaks) -> Snapshot:
    """Every root's versions. A root whose read raises keeps its previous versions this round,
    so its threads are neither removed nor changed."""
    try:
        names = [w.name for w in await asyncio.to_thread(source.roots)]
    except Exception as exc:
        log.warning("dashboard: listing the roots failed: %s", _reason(exc))
        return previous
    current: Snapshot = {}
    for name in names:
        try:
            current[name] = await asyncio.to_thread(source.versions, name)
        except Exception as exc:
            streaks.failed(name, exc)
            if name in previous:
                current[name] = previous[name]
            continue
        streaks.succeeded(name)
    return current


async def _push_changes(websocket: WebSocket, source: DataSource, poll_interval_s: float) -> None:
    """Accept, take the baseline, send the hello, then poll every ``poll_interval_s`` and push
    a :class:`ChangeEvent` only when something changed. The baseline comes before the hello: a
    change that lands after the page got the hello is always pushed. Between polls the socket
    is read, so a closed tab ends the loop at once; what the client sends is ignored."""
    await websocket.accept()
    streaks = FailureStreaks("polling")  # per connection: each socket logs its own streaks
    previous = await _snapshot(source, {}, streaks)
    await websocket.send_text(ChangeEvent(at=datetime.now(UTC)).model_dump_json())
    loop = asyncio.get_running_loop()
    while True:
        try:
            async with asyncio.timeout_at(loop.time() + poll_interval_s):
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return
        except TimeoutError:
            pass
        current = await _snapshot(source, previous, streaks)
        event = diff_versions(previous, current)
        previous = current
        if event.changed or event.removed:
            await websocket.send_text(event.model_dump_json())


def create_app(
    source: DataSource,
    *,
    poll_interval_s: float = 1.0,
    static_dir: Path | None = None,
    allowed_hosts: Sequence[str] = ("127.0.0.1", "localhost"),
) -> FastAPI:
    """The dashboard over ``source``. ``static_dir`` (default :data:`STATIC_DIR`) is read per
    request, so a missing directory never stops the app from starting: ``/`` answers 404 then.
    ``allowed_hosts`` are the ``Host`` headers served; any other gets 400."""
    if poll_interval_s <= 0:
        raise ValueError(f"poll_interval_s must be positive, got {poll_interval_s}")
    static = static_dir if static_dir is not None else STATIC_DIR
    for suffix, media_type in PAGE_TYPES.items():
        mimetypes.add_type(media_type, suffix)  # FileResponse guesses with this table
    app = FastAPI(title="SCIO dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(FrameDenyMiddleware)  # added last: outermost, so the 400s carry it too
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")
    listing = FailureStreaks("listing the threads of")

    # Plain `def` endpoints: the source does blocking sqlite I/O, and FastAPI runs sync
    # endpoints in its threadpool. They return JSONResponse, so FastAPI infers no
    # response_model and the contract's JSON (model_dump(mode="json")) goes out as is.

    @app.get("/")
    def index() -> FileResponse:
        page = static / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail=f"the dashboard page is missing: {page}")
        return FileResponse(page)

    @app.get("/api/roots")
    def roots() -> JSONResponse:
        return JSONResponse([w.model_dump(mode="json") for w in source.roots()])

    @app.get("/api/threads")
    def threads(root: str | None = None) -> JSONResponse:
        """Every root's threads, or ``root``'s. A root that cannot be read (a file that is not
        a database, a foreign schema) is skipped and logged, so it never hides the others;
        asked for by name, it answers 503 with the reason."""
        names = [root] if root is not None else [w.name for w in source.roots()]
        rows: list[ThreadSummary] = []
        for name in names:
            try:
                rows.extend(source.list_threads(name))
            except UnknownRoot:
                raise HTTPException(status_code=404, detail=f"no root named {name!r}") from None
            except Exception as exc:
                listing.failed(name, exc)
                if root is not None:
                    raise HTTPException(
                        status_code=503, detail=f"root {name!r} cannot be read: {_reason(exc)}"
                    ) from None
                continue
            listing.succeeded(name)
        return JSONResponse([t.model_dump(mode="json") for t in newest_first(rows)])

    @app.get("/api/threads/{root}/{thread_id:path}")
    def thread(root: str, thread_id: str) -> JSONResponse:
        try:
            detail = source.thread(root, thread_id)
        except UnknownRoot:
            raise HTTPException(status_code=404, detail=f"no root named {root!r}") from None
        except UnknownThread:
            raise HTTPException(
                status_code=404, detail=f"root {root!r} has no thread {thread_id!r}"
            ) from None
        except Exception as exc:
            log.warning("dashboard: reading %s/%s failed: %s", root, thread_id, _reason(exc))
            raise HTTPException(
                status_code=503, detail=f"root {root!r} cannot be read: {_reason(exc)}"
            ) from None
        return JSONResponse(detail.model_dump(mode="json"))

    @app.get("/api/graphs")
    def graphs() -> JSONResponse:
        return JSONResponse([g.model_dump(mode="json") for g in source.graphs()])

    @app.websocket("/ws")
    async def changes(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin is not None and not is_same_origin(origin, websocket.headers.get("host")):
            log.info("dashboard: refused a /ws connection from origin %r", origin)
            await websocket.close(code=POLICY_VIOLATION)
            return
        try:
            await _push_changes(websocket, source, poll_interval_s)
        except (WebSocketDisconnect, WebSocketDisconnected):
            return  # the client went away mid-send: nothing to report

    return app
