"""A CONNECT-only egress proxy with an allowlist: the registry gate of the Docker sandbox's
install phase (M9, ADR 0013 decision 10).

The sandbox container lives on an internal Docker network that has no route out. During
``exec(network=True)`` SCIO connects this proxy (a sidecar container on the default bridge,
running this module from the same Python image) to that network and points ``HTTPS_PROXY``
at it; afterwards it disconnects it again. The proxy honours ``CONNECT host:port`` for the
allowlisted hosts only (a bare host means port 443; ``host:port`` names another port) and
answers 403 to everything else, so pip can reach ``pypi.org`` and ``files.pythonhosted.org``
and nothing else leaves the build. Standard library only, so the sidecar needs no image of
its own. ``python -m scio.aci.sandbox.egress_proxy PORT HOST [HOST...]`` serves forever.
"""

from __future__ import annotations

import contextlib
import select
import socket
import sys
import threading
from collections.abc import Iterable

MAX_HEADER = 64 * 1024
DEFAULT_PORT = 443


def allowed(target: str, allowlist: Iterable[str]) -> bool:
    """``target`` is ``host:port``; an allowlist entry is ``host`` (port 443) or ``host:port``,
    matching the host exactly or as a parent domain."""
    host, _, port = target.rpartition(":")
    if not host or not port.isdigit():
        return False
    host = host.lower().rstrip(".")
    for entry in allowlist:
        e_host, _, e_port = entry.lower().rpartition(":")
        if not e_host or not e_port.isdigit():
            e_host, e_port = entry.lower(), str(DEFAULT_PORT)
        e_host = e_host.rstrip(".")
        if e_port == port and (host == e_host or host.endswith("." + e_host)):
            return True
    return False


def _read_head(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < MAX_HEADER:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _reply(conn: socket.socket, status: str) -> None:
    body = f"{status}\n".encode()
    head = (
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    with contextlib.suppress(OSError):
        conn.sendall(head + body)


def _pump(a: socket.socket, b: socket.socket) -> None:
    sockets = [a, b]
    while True:
        ready, _, _ = select.select(sockets, [], [], 300)
        if not ready:
            return
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError:
                return
            if not data:
                return
            other = b if s is a else a
            try:
                other.sendall(data)
            except OSError:
                return


def handle(conn: socket.socket, allowlist: tuple[str, ...]) -> str:
    """Serve one client connection; returns a one-word outcome for logs and tests."""
    try:
        head = _read_head(conn)
        line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            _reply(conn, "405 Method Not Allowed")
            return "refused-method"
        target = parts[1]
        if not allowed(target, allowlist):
            _reply(conn, "403 Forbidden")
            return "refused-host"
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=30)
        except OSError:
            _reply(conn, "502 Bad Gateway")
            return "upstream-failed"
        with upstream:
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            _pump(conn, upstream)
        return "tunnelled"
    finally:
        with contextlib.suppress(OSError):
            conn.close()


def serve(
    port: int,
    allowlist: Iterable[str],
    *,
    host: str = "0.0.0.0",
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Accept forever (or until ``stop`` is set), one thread per connection."""
    allow = tuple(allowlist)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(64)
        srv.settimeout(0.5)
        if ready is not None:
            ready.set()
        while stop is None or not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=handle, args=(conn, allow), daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 2:
        print("usage: egress_proxy PORT HOST [HOST...]", file=sys.stderr)
        return 2
    print(f"egress proxy on :{args[0]} allowing {', '.join(args[1:])}", flush=True)
    serve(int(args[0]), args[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
