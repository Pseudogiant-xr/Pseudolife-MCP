"""The local lock fixtures for the CLI lease rows, independent of either arm."""

import errno
import hashlib
import os
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}


def lock_file_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if safe != name:
        safe += "-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"lease-{safe}.lock"


def _try_lock(handle: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _BUSY:
            return False
        raise
    return True


def take(path: Path) -> BinaryIO | None:
    """Return an open lock handle, or None on contention."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    taken = False
    try:
        taken = _try_lock(handle)
        return handle if taken else None
    finally:
        if not taken:
            handle.close()


def release(handle: BinaryIO) -> None:
    with handle, suppress(OSError):
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def probe(path: Path) -> bool | None:
    """Return True for held, False for free, and None for a missing file."""
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    taken = False
    try:
        taken = _try_lock(handle)
        return not taken
    finally:
        if taken:
            release(handle)
        else:
            handle.close()


if __name__ == "__main__":
    lock = take(Path(sys.argv[1]))
    try:
        print("HELD" if lock is not None else "BUSY", flush=True)
        sys.stdin.readline()
    finally:
        if lock is not None:
            release(lock)
