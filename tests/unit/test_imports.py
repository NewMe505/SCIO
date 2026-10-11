"""Every package must import cleanly on its own, in a fresh interpreter (ledger I-030).

Test modules import the harness first, which hid a circular import between
``scio.harness.core`` and ``scio.prompt.repair`` for four milestones.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

PACKAGES = [
    "scio",
    "scio.settings",
    "scio.state",
    "scio.prompt",
    "scio.prompt.repair",
    "scio.harness",
    "scio.harness.core",
    "scio.context",
    "scio.aci",
    "scio.loop",
    "scio.graph",
    "scio.graph.build",
    "scio.cli",
    "evals",
    "evals.runner",
    "evals.tune",
]


@pytest.mark.parametrize("module", PACKAGES)
def test_module_imports_alone(module: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # keep PATH: DLLs on Windows
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
