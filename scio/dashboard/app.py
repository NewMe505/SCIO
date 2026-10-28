"""The dashboard's server (issue #68, ADR 0017): :func:`create_app` serves a
:class:`~scio.dashboard.contract.DataSource` as JSON, the page in ``static/``, and ``/ws``, which
pushes a :class:`~scio.dashboard.contract.ChangeEvent` whenever a poll of
:meth:`~scio.dashboard.contract.DataSource.versions` finds a thread new, changed or gone. Every
route is a read; there is no write route (``test_every_route_is_a_read`` guards it). The server
binds 127.0.0.1, yet any web page the user visits can reach it: ``TrustedHostMiddleware``
refuses a foreign ``Host`` (DNS rebinding) and ``/ws`` refuses a foreign ``Origin``. Needs the
``dashboard`` extra; the CLI imports this module lazily."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
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

LOCAL_ORIGIN = re.compile(r"https?://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?", re.IGNORECASE)
"""The only ``Origin`` a ``/ws`` connection may carry: a page served from this machine. A
browser always sends it on a WebSocket handshake; a missing one is a non-browser client."""

PAGE_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
"""Pinned for the page's files: on Windows ``mimetypes`` reads the registry, which can map
``.js`` to ``text/plain``, and a browser drops a stylesheet that is not ``text/css``."""

POLICY_VIOLATION = 1008
"""The WebSocket close code for a refused ``Origin`` (RFC 6455 section 7.4.1)."""

Snapshot = dict[str, dict[str, ThreadVersion]]
"""Root name -> thread id -> its version, as of one poll."""


def is_local_origin(origin: str) -> bool:
    """``origin`` is ``http(s)://127.0.0.1[:port]`` or ``http(s)://localhost[:port]``; anything
    else, ``null`` included, is a foreign page."""
    return LOCAL_ORIGIN.fullmatch(origin) is not None


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


async def _snapshot(source: DataSource, previous: Snapshot, failing: set[str]) -> Snapshot:
    """Every root's versions. A root whose read raises keeps its previous versions this round,
    so its threads are neither removed nor changed; the first failure of a streak is logged at
    warning, the repeats at debug, so a broken root does not fill the terminal every second."""
    try:
        names = [w.name for w in await asyncio.to_thread(source.roots)]
    except Exception as exc:
        log.warning("dashboard: listing the roots failed: %s", exc)
        return previous
    current: Snapshot = {}
    for name in names:
        try:
            current[name] = await asyncio.to_thread(source.versions, name)
        except Exception as exc:
            level = logging.DEBUG if name in failing else logging.WARNING
            log.log(level, "dashboard: reading root %s failed, skipped this poll: %s", name, exc)
            failing.add(name)
            if name in previous:
                current[name] = previous[name]
            continue
        if name in failing:
            failing.discard(name)
            log.info("dashboard: root %s reads again", name)
    return current


async def _push_changes(websocket: WebSocket, source: DataSource, poll_interval_s: float) -> None:
    """Accept, take the baseline, send the hello, then poll every ``poll_interval_s`` and push
    a :class:`ChangeEvent` only when something changed. The baseline comes before the hello: a
    change that lands after the page got the hello is always pushed. Between polls the socket
    is read, so a closed tab ends the loop at once; what the client sends is ignored."""
    await websocket.accept()
    failing: set[str] = set()
    previous = await _snapshot(source, {}, failing)
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
        current = await _snapshot(source, previous, failing)
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
    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

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
        names = [root] if root is not None else [w.name for w in source.roots()]
        rows: list[ThreadSummary] = []
        for name in names:
            try:
                rows.extend(source.list_threads(name))
            except UnknownRoot:
                raise HTTPException(status_code=404, detail=f"no root named {name!r}") from None
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
        return JSONResponse(detail.model_dump(mode="json"))

    @app.get("/api/graphs")
    def graphs() -> JSONResponse:
        return JSONResponse([g.model_dump(mode="json") for g in source.graphs()])

    @app.websocket("/ws")
    async def changes(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin is not None and not is_local_origin(origin):
            log.info("dashboard: refused a /ws connection from origin %r", origin)
            await websocket.close(code=POLICY_VIOLATION)
            return
        try:
            await _push_changes(websocket, source, poll_interval_s)
        except (WebSocketDisconnect, WebSocketDisconnected):
            return  # the client went away mid-send: nothing to report

    return app
