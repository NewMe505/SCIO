"""Is a process still running, by its PID and its creation time (issue #102)?

The recovery supervisor records each supervised build's process as a PID plus the
process's creation time (``RunRecord.pid``, ``RunRecord.pid_created``, ADR 0019), because
Windows reuses PIDs. The dashboard reads that key to tell a running build from one whose
process is gone. This is the dashboard's own copy of ``recovery/procs.py``'s check: SCIO
never imports the supervisor, which is deployed apart from it. Standard library only:
``GetProcessTimes`` through ctypes on Windows, ``/proc/<pid>/stat`` on Linux. A platform with
neither cannot tell (``None``), and the dashboard then leaves the thread's status alone.
``tests/unit/test_recovery_contract.py`` checks that both sides read the same creation time.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

MATCH_S = 1.0
"""Two creation times name the same process when they differ by less than this, the
supervisor's ``ProcessKey.matches`` tolerance."""

FILETIME_EPOCH = 116444736000000000
"""1970-01-01T00:00:00Z as a Windows FILETIME (100 ns intervals since 1601-01-01)."""

STILL_ACTIVE = 259
"""``GetExitCodeProcess``'s answer for a process that has not exited."""


class Unknowable(Exception):
    """This machine cannot read a process's creation time."""


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

    def process_created(pid: int) -> float | None:
        """The creation time of running process ``pid`` in seconds since the epoch, or None
        when it is not running. An exited process whose object a handle still keeps is not
        running, and neither is one this user may not query, as in the supervisor."""
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
            return (ticks - FILETIME_EPOCH) / 1e7
        finally:
            _kernel32.CloseHandle(handle)

else:

    @functools.cache
    def _boot_time() -> float | None:
        """Read once per process: ``btime`` is recomputed from the wall clock on each read,
        so two reads can differ by a second (the supervisor caches it for the same reason)."""
        try:
            text = Path("/proc/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
        return None

    def process_created(pid: int) -> float | None:
        """The creation time of running process ``pid`` from ``/proc/<pid>/stat`` (field 22,
        clock ticks since boot) plus the boot time, or None when it is not running or is a
        zombie. Raises :class:`Unknowable` without ``/proc``."""
        boot = _boot_time()
        if boot is None or not Path("/proc/self/stat").exists():
            raise Unknowable("no /proc on this machine")
        if pid <= 0:
            return None
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise Unknowable(f"/proc/{pid}/stat cannot be read: {exc}") from exc
        # the command name (field 2) may hold spaces and parentheses: count from its last ")"
        fields = stat[stat.rindex(")") + 1 :].split()
        if fields[0] in ("Z", "X", "x"):
            return None
        return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def alive(pid: int, created: float) -> bool | None:
    """True while process ``pid`` runs and was created at ``created`` (within
    :data:`MATCH_S`); False once it is gone, or its PID now names another process; None when
    this machine cannot tell, or the key is not a real one (``pid`` 0: the supervisor saves a
    run before it starts the child; ``created`` 0: it could not read the creation time)."""
    if pid <= 0 or created <= 0:
        return None
    try:
        now = process_created(pid)
    except (Unknowable, ValueError, IndexError):
        return None
    return now is not None and abs(now - created) < MATCH_S
