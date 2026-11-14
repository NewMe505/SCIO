"""Run a process with a timeout that ends its whole tree, never only its first process (I-180).

``subprocess.run(..., timeout=)`` kills only the process it started. A child of that process
(a worker's Bash command, a test's server) lives on, and on Windows ``subprocess.run`` then
drains the pipes with no timeout, so a child still holding them turns the timeout into a
hang. :func:`run_tree` keeps ``subprocess.run``'s contract (a ``CompletedProcess``, or
``subprocess.TimeoutExpired`` with the output read so far, or ``OSError`` when the program
cannot start) and changes only what happens at the timeout: the tree is killed, and the
drain after the kill is bounded.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

KILL_WAIT_S = 30.0
"""How long the tree kill, and the drain after it, may take: a timeout never becomes a hang."""


def kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill ``proc`` and every process under it. On Windows ``taskkill /T`` walks the tree;
    on POSIX the process leads its own session (:func:`run_tree` starts it with
    ``start_new_session``), so its process group is the tree."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=KILL_WAIT_S,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()  # the process itself, whatever the tree kill reached


def run_tree(
    argv: Sequence[str],
    *,
    timeout_s: float,
    input: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    preexec_fn: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run(argv, input=, capture_output=True, text=True, timeout=)`` in UTF-8
    (undecodable bytes replaced), whose timeout kills the whole process tree."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=sys.platform != "win32",
        preexec_fn=preexec_fn,
    )
    try:
        stdout, stderr = proc.communicate(input, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        kill_tree(proc)
        out: str | bytes | None = exc.output
        err: str | bytes | None = exc.stderr
        with contextlib.suppress(subprocess.TimeoutExpired):
            out, err = proc.communicate(timeout=KILL_WAIT_S)
        raise subprocess.TimeoutExpired(exc.cmd, exc.timeout, output=out, stderr=err) from None
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
