"""Live E2B sandbox checks (ledger I-029). Run with ``E2B_API_KEY`` set and the ``e2b`` extra."""

from __future__ import annotations

import os

import pytest

from scio.aci.sandbox.base import Limits

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("E2B_API_KEY"), reason="E2B_API_KEY not set"),
]


def test_exec_files_and_kill() -> None:
    pytest.importorskip("e2b")
    from scio.aci.sandbox.e2b import E2BSandbox

    sb = E2BSandbox(limits=Limits(timeout_s=60), session_timeout_s=300)
    try:
        r = sb.exec(["python3", "-c", "print('hi')"])
        assert r.ok and r.stdout.strip() == "hi"
        sb.write_text(f"{sb.root}/f.txt", "data")
        assert sb.exists(f"{sb.root}/f.txt") and sb.read_text(f"{sb.root}/f.txt") == "data"
        r = sb.exec(
            [
                "python3",
                "-c",
                "import socket; socket.create_connection(('example.com', 80), timeout=5)",
            ]
        )
        assert not r.ok  # internet is off unless Limits.network is True
    finally:
        sb.close()
