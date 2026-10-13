"""Black-box acceptance against the running product (M9, ADR 0013).

A product keeps its acceptance tests under ``tests/acceptance/``; they talk to the product
from the outside (a subprocess for a CLI, HTTP for a server) and never import its internals
beyond the version. ``run_acceptance`` starts an ``http`` product on a free port with the
profile's app command, waits for its health path, runs the acceptance tests with ``APP_URL``
set, stops the server, and reports. A ``cli`` product's tests run as they are. The result
is one more line of the integration text that the milestone gate reads and any failure
overrides.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

from scio.aci.runtime import healthy
from scio.aci.sandbox.local import LocalSandbox
from scio.graph.run_context import RunContext
from scio.state.models import ProductSpec

ACCEPTANCE_DIR = "tests/acceptance"
HEALTH_TIMEOUT_S = 30


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_acceptance(ctx: RunContext, spec: ProductSpec) -> tuple[str, bool]:
    sb, tc = ctx.sandbox, ctx.toolchain
    acc_dir = f"{sb.root}/{ACCEPTANCE_DIR}"
    if not sb.exists(acc_dir):
        return f"[acceptance] skipped: no {ACCEPTANCE_DIR}", True
    env: dict[str, str] = {}
    proc: subprocess.Popen[str] | None = None
    label = "cli"
    if spec.app.kind == "http":
        if not isinstance(sb, LocalSandbox):
            return "[acceptance] skipped: an http product needs the local sandbox in M9", True
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        argv = [
            *tc.runtime().app_argv,
            spec.app.module or spec.name,
            *[a.replace("{port}", str(port)) for a in spec.app.args],
        ]
        if argv[0] == "python":
            argv[0] = sb.python or sys.executable
        proc = subprocess.Popen(
            argv,
            cwd=sb.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        up = False
        while time.monotonic() < deadline and proc.poll() is None:
            if healthy(url + spec.app.health_path):
                up = True
                break
            time.sleep(0.2)
        if not up:
            output = _stop(proc)
            return (
                f"[acceptance] FAILED: the app did not answer {spec.app.health_path} on {url} "
                f"within {HEALTH_TIMEOUT_S}s ({' '.join(argv[1:])})\n{output[-1500:]}",
                False,
            )
        env["APP_URL"] = url
        label = f"http {url}"
    try:
        r = sb.exec(tc.test_argv(filter=None, path=acc_dir), timeout_s=sb.limits.timeout_s, env=env)
    finally:
        app_output = _stop(proc) if proc is not None else ""
    outcome = tc.test_outcome(r.exit_code, r.stdout)
    ok = outcome == "passed"
    tail = r.output.strip().splitlines()[-1][:200] if r.output.strip() else ""
    text = f"[acceptance] {outcome} ({label}): {tail}"
    if not ok:
        text += f"\n{r.output[-1500:]}"
        if app_output.strip():
            text += f"\n[app output]\n{app_output[-800:]}"
    return text, ok


def _stop(proc: subprocess.Popen[str]) -> str:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.stdout.read() if proc.stdout else ""
