"""Running external commands (git, gh, claude, uv, the launcher) through one injectable seam.

Every module that runs a command takes a :class:`Runner`, so tests pass a fake that
records argv and returns canned results. This mirrors SCIO's ``GhRunner`` and
``claude_runner``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        stdin: str | None = None,
        timeout_s: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


def kill_tree(pid: int) -> None:
    """Kill ``pid`` and every process under it. On Windows ``taskkill /T /F`` reaches the
    python under ``sh -> env -> uv -> python`` (measured, ADR 0019 section 12a). On POSIX
    the process must have been started with ``start_new_session=True``."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            os.kill(pid, signal.SIGKILL)


def resolve_program(argv0: str, env: Mapping[str, str] | None) -> str:
    """Resolve ``argv0`` the way the child's own environment would.

    On Windows, CreateProcess finds a bare program name on the **parent's** PATH and
    ignores the PATH in ``env`` (measured, I-183). A tick started by Task Scheduler has no
    ``sh`` on its PATH, but a replay's recorded environment does. So when ``env`` is given
    and ``argv0`` has no directory part, look it up on ``env``'s PATH first. Every spawn in
    this package goes through here."""
    if env is None or os.path.dirname(argv0):
        return argv0
    path = next((v for k, v in env.items() if k.upper() == "PATH"), None)
    found = shutil.which(argv0, path=path) if path else None
    return found or argv0


def run_command(
    argv: Sequence[str],
    *,
    cwd: str | Path | None = None,
    stdin: str | None = None,
    timeout_s: float | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """The default :class:`Runner`. It never raises for a failed command:
    - a missing binary returns code 127;
    - a timeout kills the whole process tree and returns ``timed_out=True`` with code -1.
    """
    if not argv:
        return CommandResult(127, "", "empty argv")
    resolved = [resolve_program(argv[0], env), *argv[1:]]
    try:
        proc = subprocess.Popen(
            resolved,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=not WINDOWS,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{argv[0]}: {exc}")
    try:
        out, err = proc.communicate(input=stdin, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        out, err = proc.communicate()
        return CommandResult(-1, out or "", err or "", timed_out=True)
    return CommandResult(proc.returncode, out or "", err or "")
