"""Processes: identity (PID plus creation time), liveness, spawning with output to files,
following those files, and finding running SCIO processes. OWNER: wave-1 agent A.

What was measured on the dev machine (ADR 0019 section 12a):
- a child survives when its parent is killed, whatever the creation flags;
- ``commands.kill_tree`` (``taskkill /T /F``) reaches the python under
  ``sh -> env -> uv -> python``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

from recovery.model import ProcessKey


def process_key(pid: int) -> ProcessKey | None:
    """The key of a running process, or None if ``pid`` is not running.

    Windows uses ``GetProcessTimes`` through ctypes. POSIX uses ``/proc/<pid>/stat``
    plus the boot time, with ``ps -o lstart=`` as a fallback.
    """
    raise NotImplementedError


def self_key() -> ProcessKey:
    """This process's key."""
    raise NotImplementedError


def alive(key: ProcessKey) -> bool:
    """True when ``key.pid`` is running and its creation time matches ``key``.

    False for a reused PID.
    """
    raise NotImplementedError


def spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout_path: Path,
    stderr_path: Path,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` with its stdout and stderr written to files, not pipes.

    A supervisor that dies therefore neither blocks nor kills the child. Stdin is
    DEVNULL. On POSIX the child gets its own session, so ``kill_tree`` can reach it.
    """
    raise NotImplementedError


def follow(streams: Sequence[tuple[Path, TextIO]], offsets: list[int]) -> list[int]:
    """Copy any bytes appended to each file since ``offsets[i]`` to its stream.

    Returns the new offsets. Decodes as UTF-8, replacing invalid bytes, and never
    splits a character across two calls.
    """
    raise NotImplementedError


def spawn_detached(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], log_path: Path
) -> int:
    """Start ``argv`` so that it outlives this process.

    Windows uses ``CREATE_NEW_PROCESS_GROUP``, adding ``CREATE_BREAKAWAY_FROM_JOB`` when
    the job allows it. POSIX uses ``start_new_session``. Output goes to ``log_path``.
    Returns the PID.
    """
    raise NotImplementedError


def scio_processes() -> list[int]:
    """PIDs of running ``python -m scio`` processes, ``scio dashboard`` excluded.

    Windows reads command lines with
    ``Get-CimInstance Win32_Process | Select ProcessId, CommandLine``; POSIX uses
    ``ps -eo pid=,args=``. live-wt never moves while this is non-empty.
    """
    raise NotImplementedError
