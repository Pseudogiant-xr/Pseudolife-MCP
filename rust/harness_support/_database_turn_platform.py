"""OS operations for an enabled machine's database turn."""
from dataclasses import dataclass
import errno
import os
from pathlib import Path


@dataclass(frozen=True)
class Identity:
    pid: int
    started: str
    owner: str
    parent: int


def try_lock(path: Path):
    if os.name == "nt":
        from . import _database_turn_windows

        return _database_turn_windows.try_lock(path)
    import fcntl

    stream = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b", buffering=0)
    try:
        # FileStream.Lock on the PowerShell side uses the same record lock.
        fcntl.lockf(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB, 1, 0, os.SEEK_SET)
    except OSError as error:
        stream.close()
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return stream


def identity(pid: int):
    # This dependency is already part of the harness environment and is
    # imported only on an opted-in machine, never on the no-marker path.
    import psutil

    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        if os.name == "nt":
            from . import _database_turn_windows

            started = _database_turn_windows.process_started(pid)
            if started is None:
                return None
            owner = process.username()
            parent = process.ppid()
        else:
            # Raw kernel start ticks avoid cross-language float rounding.
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            started, parent = stat[19], int(stat[1])
            owner = f"uid:{process.uids().real}"
        if not owner:
            raise OSError("process owner is unavailable")
        return Identity(pid, str(started), owner, parent)
    except (psutil.NoSuchProcess, ProcessLookupError, FileNotFoundError):
        return None
    except psutil.AccessDenied:
        raise OSError("cannot verify process ownership") from None


def contain_process_tree():
    if os.name == "nt":
        from . import _database_turn_windows

        _database_turn_windows.contain_process_tree()
