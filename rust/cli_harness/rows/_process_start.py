"""The frozen conductor watcher's process-start token: with a pid it tells a
running watcher from a reused pid. The conductor-watch row writes live
records with it, as a watcher of that release wrote them.

Copied verbatim, by syntax tree, from pseudolife_memory/conductor_watch_cli.py at 6ca817d70:
_process_start :677-701, _posix_process_start :704-719.

The source is frozen, so the copy cannot drift from it; never edit it by hand.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _process_start(pid: int) -> str | None:
    """An opaque token for when ``pid`` started, or None when it cannot be
    read. With the pid it tells a running watcher from a reused pid; stdlib
    only, since a shim runtime has no psutil."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel32.CloseHandle(handle)
    return _posix_process_start(pid)


def _posix_process_start(pid: int, proc: Path = Path("/proc")) -> str | None:
    """``_process_start`` outside Windows: field 22 of Linux's
    ``/proc/<pid>/stat``, else ``ps``'s start time. ``proc`` is a test seam."""
    try:
        # Field 22 of /proc/<pid>/stat, counted after the parenthesised name.
        # A name that does not decode (ValueError: UnicodeDecodeError) is an
        # unreadable stat like any other.
        return (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        pass
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return out or None
