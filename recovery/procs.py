"""Processes: identity (PID plus creation time), liveness, spawning with output to files,
following those files, and finding running SCIO processes. OWNER: wave-1 agent A.

What was measured on the dev machine (ADR 0019 section 12a):
- a child survives when its parent is killed, whatever the creation flags;
- ``commands.kill_tree`` (``taskkill /T /F``) reaches the python under
  ``sh -> env -> uv -> python``;
- under Task Scheduler, ``CREATE_BREAKAWAY_FROM_JOB`` raises access denied, and the retry
  without it starts a child that outlives the task;
- CreateProcess finds a bare program name on the parent's PATH, not on the PATH of the
  ``env`` it is given, so every spawn here resolves ``argv[0]`` first (I-183).
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time
import warnings
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from recovery.commands import WINDOWS, Runner, resolve_program, run_command
from recovery.model import ProcessKey

FILETIME_EPOCH = 116444736000000000
"""1970-01-01T00:00:00Z as a Windows FILETIME (100 ns intervals since 1601-01-01)."""

STILL_ACTIVE = 259
"""``GetExitCodeProcess``'s answer for a process that has not exited."""

CIM_QUERY = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine"
    " | ConvertTo-Json -Compress"
)
"""The PowerShell that lists every process with its command line, as JSON."""


def filetime_to_epoch(filetime: int) -> float:
    """A Windows FILETIME as seconds since the Unix epoch."""
    return (filetime - FILETIME_EPOCH) / 1e7


def proc_stat_created(stat: str, boot_time: float, clock_ticks: int) -> float | None:
    """The creation time from a Linux ``/proc/<pid>/stat`` line, or None for a zombie.

    Field 22 is the start time in clock ticks since boot. The command name (field 2) may
    hold spaces and parentheses, so the fields are counted from its last ``)``.
    """
    fields = stat[stat.rindex(")") + 1 :].split()
    # fields[0] is field 3 (the state), so field 22 is fields[19]
    if fields[0] in ("Z", "X", "x"):
        return None
    return boot_time + int(fields[19]) / clock_ticks


def ps_created(output: str) -> float | None:
    """The creation time from ``ps -o stat= -o lstart=`` (run with ``LC_ALL=C``), or None
    when the output is empty, a zombie's, or unreadable."""
    line = output.strip()
    if not line:
        return None
    state, _, started = line.partition(" ")
    if state.startswith("Z"):
        return None
    try:
        return time.mktime(time.strptime(started.strip(), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _created_by_ps(pid: int) -> float | None:
    env = {**os.environ, "LC_ALL": "C"}
    result = run_command(
        ["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)], env=env, timeout_s=10
    )
    return ps_created(result.stdout) if result.ok else None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    )
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    def _created(pid: int) -> float | None:
        if not 0 < pid <= 0xFFFFFFFF:
            return None
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if code.value != STILL_ACTIVE:
                # Exited: an open handle somewhere (a Popen's, say) keeps the object alive.
                return None
            creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            ticks = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return filetime_to_epoch(ticks)
        finally:
            _kernel32.CloseHandle(handle)

else:

    @functools.cache
    def _boot_time() -> float | None:
        """Read once per process: ``btime`` is whole seconds, recomputed from the wall
        clock on each read, so a clock adjustment could move it by one second between
        two reads and make a live process's key stop matching."""
        try:
            text = Path("/proc/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
        return None

    def _created(pid: int) -> float | None:
        if pid <= 0:
            return None
        if not Path("/proc/self/stat").exists():
            return _created_by_ps(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            return None
        except OSError:
            return _created_by_ps(pid)
        boot = _boot_time()
        if boot is None:
            return _created_by_ps(pid)
        try:
            return proc_stat_created(stat, boot, os.sysconf("SC_CLK_TCK"))
        except (ValueError, IndexError):
            return _created_by_ps(pid)


def process_key(pid: int) -> ProcessKey | None:
    """The key of a running process, or None if ``pid`` is not running.

    Windows uses ``GetProcessTimes`` through ctypes, and an exited process whose object a
    handle still keeps is not running. POSIX uses ``/proc/<pid>/stat`` plus the boot time,
    with ``ps -o lstart=`` as a fallback. A process this user may not query reads as not
    running.
    """
    created = _created(pid)
    return None if created is None else ProcessKey(pid, created)


def self_key() -> ProcessKey:
    """This process's key. Raises ``RuntimeError`` rather than guess a creation time: a
    wrong one would make this process's lease look dead to every other supervisor."""
    key = process_key(os.getpid())
    if key is None:
        raise RuntimeError(f"cannot read the creation time of this process ({os.getpid()})")
    return key


def alive(key: ProcessKey) -> bool:
    """True when ``key.pid`` is running and its creation time matches ``key``.

    False for a reused PID.
    """
    current = process_key(key.pid)
    return current is not None and current.matches(key)


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
    DEVNULL. On POSIX the child gets its own session, so ``kill_tree`` can reach it. On
    Windows it gets its own process group, so a Ctrl+C reaches only the supervisor, which
    then kills the tree. ``argv[0]`` is resolved on ``env``'s PATH
    (``commands.resolve_program``).
    """
    command = [resolve_program(argv[0], env), *argv[1:]]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
        return subprocess.Popen(
            command,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            close_fds=True,
            creationflags=flags,
            start_new_session=not WINDOWS,
        )


def complete_prefix(data: bytes) -> int:
    """How many leading bytes of ``data`` can be written now.

    An incomplete UTF-8 sequence at the end waits for its last bytes, and so does a final
    CR, so that a CRLF is never split across two calls.
    """
    n = len(data)
    for back in range(1, min(4, n) + 1):
        byte = data[n - back]
        if byte & 0xC0 == 0x80:  # a continuation byte: look further back
            continue
        if byte >= 0xF0:
            need = 4
        elif byte >= 0xE0:
            need = 3
        elif byte >= 0xC0:
            need = 2
        else:
            need = 1
        if need > back:
            n -= back
        break
    if n and data[n - 1] == 0x0D:
        n -= 1
    return n


def _write(stream: TextIO, text: str) -> None:
    """Echo is a courtesy: an encoding the stream cannot represent, or a reader that went
    away, never stops the supervisor."""
    try:
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, "replace").decode(encoding, "replace"))
        stream.flush()
    except (OSError, ValueError):
        return


def follow(streams: Sequence[tuple[Path, TextIO]], offsets: list[int]) -> list[int]:
    """Copy any bytes appended to each file since ``offsets[i]`` to its stream.

    Returns the new offsets. Decodes as UTF-8, replacing invalid bytes, and never
    splits a character across two calls. CRLF becomes LF, which a text stream turns back
    into the platform's line end. A missing file keeps its offset.
    """
    out = list(offsets)
    for i, (path, stream) in enumerate(streams):
        try:
            with path.open("rb") as fh:
                fh.seek(out[i])
                data = fh.read()
        except OSError:
            continue
        cut = complete_prefix(data)
        if cut == 0:
            continue
        _write(stream, data[:cut].decode("utf-8", "replace").replace("\r\n", "\n"))
        out[i] += cut
    return out


def spawn_detached(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], log_path: Path
) -> int:
    """Start ``argv`` so that it outlives this process.

    Windows uses ``CREATE_NEW_PROCESS_GROUP``, adding ``CREATE_BREAKAWAY_FROM_JOB`` when
    the job allows it (it raises access denied under Task Scheduler, and then the spawn
    is retried without it). POSIX uses ``start_new_session``. Output, stdout and stderr
    both, is appended to ``log_path``. ``argv[0]`` is resolved on ``env``'s PATH.
    Returns the PID.
    """
    command = [resolve_program(argv[0], env), *argv[1:]]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:

        def start(flags: int) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                command,
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=flags,
                start_new_session=not WINDOWS,
            )

        if sys.platform == "win32":
            # No console of its own: closing the console that started a background tick or
            # a replay must not take it down.
            group = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                popen = start(group | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            except FileNotFoundError:
                raise
            except OSError:  # a job without breakaway: access denied
                popen = start(group)
        else:
            popen = start(0)
    pid = popen.pid
    # Never waited on by design: silence Popen's "still running" warning as it goes.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        del popen
    return pid


_SCIO_MODULE = re.compile(r"""(?:^|[\s"'])-m\s*["']?scio(?:\.[\w.]+)?["']?(?=\s|$)(.*)$""", re.S)


def is_scio_command(command_line: str | None) -> bool:
    """True for a command line that runs the ``scio`` package as a module (``-m scio``, or
    one of its submodules), except ``scio dashboard``. ``-m scio_x`` is another module."""
    if not command_line:
        return False
    match = _SCIO_MODULE.search(command_line)
    if match is None:
        return False
    words = [w.strip("\"'") for w in match.group(1).split()]
    subcommand = next((w for w in words if w and not w.startswith("-")), "")
    return subcommand != "dashboard"


def parse_cim_processes(output: str) -> list[tuple[int, str]]:
    """``(pid, command line)`` from :data:`CIM_QUERY`'s JSON: a list of objects, or one
    object when there is one process. A null command line reads as ""."""
    text = output.strip()
    if not text:
        return []
    data = json.loads(text)
    items = [data] if isinstance(data, dict) else data
    rows: list[tuple[int, str]] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("ProcessId"), int):
            rows.append((item["ProcessId"], str(item.get("CommandLine") or "")))
    return rows


def parse_ps_processes(output: str) -> list[tuple[int, str]]:
    """``(pid, command line)`` from ``ps -eo pid=,args=``."""
    rows: list[tuple[int, str]] = []
    for line in output.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), args.strip()))
    return rows


def scio_pids(rows: Iterable[tuple[int, str]], *, exclude: int) -> list[int]:
    """The PIDs in ``rows`` whose command line :func:`is_scio_command`, except ``exclude``."""
    return [pid for pid, line in rows if pid != exclude and is_scio_command(line)]


def scio_processes(*, runner: Runner = run_command) -> list[int]:
    """PIDs of running ``python -m scio`` processes, ``scio dashboard`` excluded.

    Windows reads command lines with
    ``Get-CimInstance Win32_Process | Select ProcessId, CommandLine``; POSIX uses
    ``ps -eo pid=,args=``. live-wt never moves while this is non-empty.

    Raises ``RuntimeError`` when the process list cannot be read: not knowing is not the
    same as none running.
    """
    if WINDOWS:
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", CIM_QUERY]
        result = runner(argv, timeout_s=120)
        parse = parse_cim_processes
    else:
        # -ww: never truncate a command line, even when the output is a pipe
        result = runner(["ps", "-ww", "-eo", "pid=,args="], timeout_s=60)
        parse = parse_ps_processes
    if not result.ok:
        raise RuntimeError(f"cannot list processes: {result.stderr.strip()[:300]}")
    try:
        rows = parse(result.stdout)
    except ValueError as exc:
        raise RuntimeError(f"cannot read the process list: {exc}") from exc
    return scio_pids(rows, exclude=os.getpid())
