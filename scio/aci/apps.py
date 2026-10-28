"""Starting a product's app where its sandbox runs (M9; shared with ``run_app`` since issue
#58): a host subprocess on a local sandbox, a detached process inside any other (Docker),
probed with the profile's health command from inside, stopped by pid.

Inside a sandbox the app runs as ``nohup sh -c '<app> & echo $! > app.pid; wait $!; echo
$? > app.exit'``: the pid file holds the app's own pid, so ``stop`` frees its port, and
"exited" means the exit file exists, with the app's real exit code in it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field

from scio.aci.sandbox.base import Sandbox
from scio.aci.sandbox.local import (
    INTERPRETER_WORD,
    LocalSandbox,
    env_path,
    resolve_command,
    with_interpreter_first,
)
from scio.toolchain import Toolchain, python_toolchain

HEALTH_TIMEOUT_S = 30
APP_LOG = ".scio/sandbox/app.log"
APP_PID = ".scio/sandbox/app.pid"
APP_EXIT = ".scio/sandbox/app.exit"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def healthy(url: str) -> bool:
    """True when a local HTTP URL answers 2xx or 3xx within a second."""
    try:
        with urllib.request.urlopen(url, timeout=1) as resp:
            return bool(200 <= int(resp.status) < 400)
    except (urllib.error.URLError, OSError, ValueError):
        return False


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
        assert self._sandbox is not None
        return self._sandbox.exists(f"{self._sandbox.root}/{APP_EXIT}")

    def exit_code(self) -> int | None:
        """The app's exit code once it has exited, else None."""
        if self._proc is not None:
            return self._proc.poll()
        assert self._sandbox is not None
        path = f"{self._sandbox.root}/{APP_EXIT}"
        if not self._sandbox.exists(path):
            return None
        text = self._sandbox.read_text(path).strip()
        return int(text) if text.lstrip("-").isdigit() else None

    def stop(self) -> str:
        """Terminate the app and return its output tail."""
        if self._proc is not None:
            return _stop(self._proc)
        assert self._sandbox is not None
        sb = self._sandbox
        kill = (
            f"kill $(cat {APP_PID}) 2>/dev/null; sleep 0.5; "
            f"kill -9 $(cat {APP_PID}) 2>/dev/null; true"
        )
        sb.exec(["sh", "-c", kill], timeout_s=15)
        log_file = f"{sb.root}/{APP_LOG}"
        out = sb.read_text(log_file) if sb.exists(log_file) else ""
        for rel in (APP_PID, APP_LOG, APP_EXIT):
            if sb.exists(f"{sb.root}/{rel}"):
                sb.delete(f"{sb.root}/{rel}")
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
        env = with_interpreter_first(os.environ, sb.python)  # the app's children too (I-114)
        argv = resolve_command(argv, sb.python, path=env_path(env))
        proc = subprocess.Popen(
            argv,
            cwd=sb.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return RunningApp(argv, url, tc, _proc=proc)
    if argv[0] == INTERPRETER_WORD and sb.python:
        argv[0] = sb.python
    for rel in (APP_PID, APP_EXIT):
        if sb.exists(f"{sb.root}/{rel}"):
            sb.delete(f"{sb.root}/{rel}")
    sb.write_text(f"{sb.root}/{APP_LOG}", "")
    inner = (
        " ".join(_sh_quote(a) for a in argv)
        + f" & echo $! > {APP_PID}; wait $!; echo $? > {APP_EXIT}"
    )
    r = sb.exec(["sh", "-c", f"nohup sh -c {_sh_quote(inner)} > {APP_LOG} 2>&1 &"], timeout_s=30)
    if not r.ok:
        raise RuntimeError(f"could not start the app in the sandbox: {r.output[-500:]}")
    deadline = time.monotonic() + 10  # the inner shell writes the pid a moment later
    while time.monotonic() < deadline and not sb.exists(f"{sb.root}/{APP_PID}"):
        time.sleep(0.1)
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


def _stop(proc: subprocess.Popen[str]) -> str:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    return proc.stdout.read() if proc.stdout else ""
