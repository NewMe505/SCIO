"""Black-box acceptance against the running product (M9, ADR 0013).

A product keeps its acceptance tests under ``tests/acceptance/``; they talk to the product
from the outside (a subprocess for a CLI, HTTP for a server) and never import its internals
beyond the version. ``run_acceptance`` starts an ``http`` product on a free port with the
profile's app command, waits for its health path, runs the acceptance tests with ``APP_URL``
set, stops the server, and reports. A ``cli`` product's tests run as they are. The result
is one more line of the integration text that the milestone gate reads and any failure
overrides.

Where the app runs follows the sandbox: on a local sandbox it is a host subprocess; on any
other (Docker) it is started inside the sandbox with the profile's shell-less commands, its
health probed from inside, and stopped by pid, so the acceptance tests and the app share one
network namespace and no port leaves the container.
"""

from __future__ import annotations

import socket
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from scio.aci.runtime import healthy
from scio.aci.sandbox.base import Sandbox
from scio.aci.sandbox.local import INTERPRETER_WORD, LocalSandbox, resolve_command
from scio.graph.run_context import RunContext
from scio.state.models import ProductSpec
from scio.toolchain import Toolchain, python_toolchain

HEALTH_TIMEOUT_S = 30
APP_LOG = ".scio/sandbox/app.log"
APP_PID = ".scio/sandbox/app.pid"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class RunningApp:
    """A started product app: poll ``is_healthy`` until it answers, then ``stop`` it."""

    argv: list[str]
    url: str
    _toolchain: Toolchain = field(default_factory=python_toolchain)
    _proc: subprocess.Popen[str] | None = None
    _sandbox: Sandbox | None = None

    def is_healthy(self, path: str) -> bool:
        if self._sandbox is not None:
            probe = self._toolchain.health_probe_argv(self.url + path)
            return self._sandbox.exec(probe, timeout_s=10).ok
        return healthy(self.url + path)

    def exited(self) -> bool:
        if self._proc is not None:
            return self._proc.poll() is not None
        return False

    def stop(self) -> str:
        """Terminate the app and return its output tail."""
        if self._proc is not None:
            return _stop(self._proc)
        assert self._sandbox is not None
        sb = self._sandbox
        pid_file = f"{sb.root}/{APP_PID}"
        kill = (
            f"kill $(cat {APP_PID}) 2>/dev/null; sleep 0.5; "
            f"kill -9 $(cat {APP_PID}) 2>/dev/null; true"
        )
        sb.exec(
            ["sh", "-c", kill],
            timeout_s=15,
        )
        log_file = f"{sb.root}/{APP_LOG}"
        out = sb.read_text(log_file) if sb.exists(log_file) else ""
        for f in (pid_file, log_file):
            if sb.exists(f):
                sb.delete(f)
        return out


def start_app(
    sb: Sandbox, argv: Sequence[str], url: str, toolchain: Toolchain | None = None
) -> RunningApp:
    """Start ``argv`` (the interpreter word rewritten to the product environment) where the
    sandbox runs: a host subprocess on a local sandbox, a detached process inside any other,
    probed with the profile's health command."""
    argv = list(argv)
    tc = toolchain or python_toolchain()
    if isinstance(sb, LocalSandbox):
        argv = resolve_command(argv, sb.python)
        proc = subprocess.Popen(
            argv,
            cwd=sb.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return RunningApp(argv, url, tc, _proc=proc)
    if argv[0] == INTERPRETER_WORD and sb.python:
        argv[0] = sb.python
    quoted = " ".join(_sh_quote(a) for a in argv)
    sb.write_text(f"{sb.root}/{APP_LOG}", "")
    r = sb.exec(
        ["sh", "-c", f"nohup {quoted} > {APP_LOG} 2>&1 & echo $! > {APP_PID}"], timeout_s=30
    )
    if not r.ok:
        raise RuntimeError(f"could not start the app in the sandbox: {r.output[-500:]}")
    return RunningApp(argv, url, tc, _sandbox=sb)


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def wait_healthy(app: RunningApp, health_path: str, timeout_s: int = HEALTH_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not app.exited():
        if app.is_healthy(health_path):
            return True
        time.sleep(0.2)
    return False


def app_argv(spec: ProductSpec, tc: Toolchain, port: int | None = None) -> list[str]:
    """The command that starts the product the way a user does: the profile's ``app_argv``
    and target, then the ``## Runtime`` arguments with ``{port}`` filled in (none for a CLI,
    whose arguments are the test's to choose)."""
    target = [*tc.runtime().app_argv, tc.app_target(spec.name, spec.app.module)]
    if port is None:
        return target
    return [*target, *[a.replace("{port}", str(port)) for a in spec.app.args]]


def run_acceptance(ctx: RunContext, spec: ProductSpec) -> tuple[str, bool]:
    sb, tc = ctx.sandbox, ctx.toolchain
    acc_dir = f"{sb.root}/{tc.acceptance_dir}"
    if not sb.exists(acc_dir):
        return f"[acceptance] skipped: no {tc.acceptance_dir}", True
    env: dict[str, str] = {}
    app: RunningApp | None = None
    label = "cli"
    if spec.app.kind == "http":
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        argv = app_argv(spec, tc, port)
        app = start_app(sb, argv, url, tc)
        if not wait_healthy(app, spec.app.health_path):
            output = app.stop()
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
        app_output = app.stop() if app is not None else ""
    outcome: str = tc.test_outcome(r.exit_code, r.stdout)
    if outcome == "passed" and tc.skipped_only(r.stdout):
        outcome = "skipped only"  # every acceptance test skipped proves nothing (I-097)
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
