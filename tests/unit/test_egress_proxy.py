"""The install phase's registry gate: CONNECT to an allowlisted host tunnels, anything else
is refused before any upstream connection is attempted (M9, ADR 0013 decision 10)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from scio.aci.sandbox.egress_proxy import allowed, serve


def test_allowlist_matches_host_or_parent_domain_on_the_named_port() -> None:
    allow = ("pypi.org", "files.pythonhosted.org", "127.0.0.1:9000")
    assert allowed("pypi.org:443", allow)
    assert allowed("PYPI.org.:443", allow)
    assert allowed("mirror.files.pythonhosted.org:443", allow)
    assert allowed("127.0.0.1:9000", allow)
    assert not allowed("pypi.org:80", allow)  # a bare entry means 443 only
    assert not allowed("notpypi.org:443", allow)  # no suffix tricks
    assert not allowed("example.com:443", allow)
    assert not allowed("pypi.org", allow)  # no port
    assert not allowed("pypi.org:abc", allow)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def echo_server() -> Iterator[int]:
    """A TCP server that echoes what it receives, standing in for a registry."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(0.5)
    port = int(srv.getsockname()[1])
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            with conn:
                data = conn.recv(65536)
                if data:
                    conn.sendall(data)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield port
    stop.set()
    t.join(timeout=3)
    srv.close()


@pytest.fixture
def proxy(echo_server: int) -> Iterator[int]:
    port = _free_port()
    ready, stop = threading.Event(), threading.Event()
    t = threading.Thread(
        target=serve,
        args=(port, (f"127.0.0.1:{echo_server}", "pypi.org")),
        kwargs={"host": "127.0.0.1", "ready": ready, "stop": stop},
        daemon=True,
    )
    t.start()
    assert ready.wait(5)
    yield port
    stop.set()
    t.join(timeout=5)


def _request(port: int, head: bytes, then: bytes | None = None) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
        c.sendall(head)
        reply = c.recv(65536)
        if then is not None and reply.startswith(b"HTTP/1.1 200"):
            c.sendall(then)
            reply += c.recv(65536)
        return reply


def test_connect_to_an_allowlisted_host_tunnels_both_ways(proxy: int, echo_server: int) -> None:
    reply = _request(
        proxy, f"CONNECT 127.0.0.1:{echo_server} HTTP/1.1\r\nHost: x\r\n\r\n".encode(), b"ping!"
    )
    assert reply.startswith(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    assert reply.endswith(b"ping!")


def test_other_hosts_methods_and_ports_are_refused(proxy: int, echo_server: int) -> None:
    assert _request(proxy, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    # the right host on the wrong port is still refused (a bare entry means 443)
    assert _request(proxy, b"CONNECT pypi.org:80 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
    # plain HTTP proxying is not offered at all
    assert _request(proxy, b"GET http://pypi.org/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 405")
    # garbage is a refusal, not a crash; the proxy keeps serving
    assert _request(proxy, b"\r\n\r\n").startswith(b"HTTP/1.1 405")
    reply = _request(
        proxy, f"CONNECT 127.0.0.1:{echo_server} HTTP/1.1\r\n\r\n".encode(), b"still up"
    )
    assert reply.endswith(b"still up")
