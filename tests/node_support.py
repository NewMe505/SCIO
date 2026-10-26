"""Skip markers for tests that run Node (M10). The dev machine has Node 22; CI installs it
with ``setup-node``; anywhere else the tests skip rather than fail (I-074, I-080: a test never
depends on what a machine happens to have)."""

from __future__ import annotations

import shutil
import subprocess

import pytest


def _node_strips_types() -> bool:
    node = shutil.which("node")
    if node is None:
        return False
    try:
        r = subprocess.run(
            [node, "-p", "process.features.typescript"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and r.stdout.strip() in ("strip", "transform")


HAS_NODE = _node_strips_types()
requires_node = pytest.mark.skipif(
    not HAS_NODE, reason="needs Node >= 22.18 with TypeScript type stripping"
)
