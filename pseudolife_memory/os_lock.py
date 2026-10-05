"""Named, non-blocking, exclusive OS file locks: the truth behind a lease.

The same mechanism as the test suite's own lock (``tests/suite_lock.py``):
``msvcrt.locking`` with ``LK_NBLCK`` on the first byte on Windows,
``fcntl.flock`` with ``LOCK_EX | LOCK_NB`` on POSIX. The OS drops the lock the
moment the process holding it exits or dies, so there is no pid file to go
stale and nothing to clean up after a crash. Both backends also exclude a
second handle inside the same process (Windows byte-range locks belong to the
handle; ``flock`` to the open file description).

Lock files live in ``~/.pseudolife-mcp/locks/`` (``PSEUDOLIFE_LEASE_LOCK_DIR``
overrides it) as ``lease-<safe>.lock``. ``<safe>`` keeps ``[A-Za-z0-9._-]``,
replaces anything else with ``_`` and, whenever it replaced something,
appends ``-`` and the first 8 hex digits of the name's SHA-256, so
``claim:a/b`` and ``claim_a_b`` get different files. The files are never
deleted: a lock file's existence means nothing, only the OS lock on it does.
On a case-insensitive filesystem (Windows, macOS by default) names that differ
only in case share a file; that can only over-exclude (one waits for the
other), never let two holders of one name through.

Standard library only: the lease CLI imports this without the daemon's stack.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
from pathlib import Path
from typing import IO, Mapping

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_DIR_ENV = "PSEUDOLIFE_LEASE_LOCK_DIR"
LOCK_PREFIX = "lease-"
LOCK_SUFFIX = ".lock"

# What a non-blocking attempt raises while another holder has the lock:
# EACCES from msvcrt.locking, EWOULDBLOCK/EAGAIN from flock.
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def lock_dir(environ: Mapping[str, str] | None = None) -> Path:
    """``PSEUDOLIFE_LEASE_LOCK_DIR``, else ``~/.pseudolife-mcp/locks``."""
    environ = os.environ if environ is None else environ
    override = environ.get(LOCK_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".pseudolife-mcp" / "locks"


def lock_file_name(name: str) -> str:
    """The lock file name for lease ``name`` (see the module docstring)."""
    safe = _UNSAFE.sub("_", name)
    if safe != name:
        safe += "-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{LOCK_PREFIX}{safe}{LOCK_SUFFIX}"


def _try_lock(handle: IO[bytes]) -> bool:
    """True when taken, False when another holder has it. Any other failure
    (a filesystem without locks, a bad handle) raises: read as busy, it would
    make a waiter wait forever behind nobody."""
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _BUSY_ERRNOS:
            return False
        raise
    return True


def _unlock(handle: IO[bytes]) -> None:
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # closing the handle releases it regardless


class OsLock:
    """One exclusive lock on ``path``, taken without blocking.

    The file (and its directory) is created on the first :meth:`acquire`.
    The handle is not inheritable (PEP 446), so a command started while the
    lock is held does not keep it alive after this process ends.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._handle: IO[bytes] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Take the lock if it is free; True when this object holds it."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 — held until release()
        try:
            taken = _try_lock(handle)
        except BaseException:
            handle.close()
            raise
        if not taken:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            _unlock(handle)
            handle.close()


def probe(path: Path) -> bool | None:
    """Whether a lock file is held right now: True held, False free, None
    when there is no such file. A free lock is taken for an instant and let
    go, so a waiter polling at that moment retries at its next poll; a
    missing file is never created."""
    try:
        handle = open(path, "rb")  # not "a": that would create it
    except FileNotFoundError:
        return None
    try:
        if not _try_lock(handle):
            return True
        _unlock(handle)
        return False
    finally:
        handle.close()
