"""Enabled-machine turn policy; imported only after the opt-in marker."""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

_held: dict[str, Lease] = {}
_mutex = threading.RLock()


class TurnUnavailable(RuntimeError):
    """Another live process holds the machine's database turn."""


def _backend():
    from . import _database_turn_platform

    return _database_turn_platform


class Lease:
    def __init__(self, stream, *, pid: int, delegated: bool = False):
        self.stream = stream
        self.pid = pid
        self.delegated = delegated
        self.contained = False
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.stream is not None:
            try:
                # A forked child must never truncate its parent's record.
                if self.pid == os.getpid():
                    self.stream.seek(0)
                    self.stream.truncate()
                    self.stream.flush()
                    os.fsync(self.stream.fileno())
            finally:
                self.stream.close()


def _record(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _matching_owner(record, backend):
    if not record or type(record.get("pid")) is not int or record["pid"] <= 0:
        return None
    try:
        identity = backend.identity(record["pid"])
    except OSError:
        return None
    if identity is None:
        return None
    started = (record.get("process_started") if os.name == "nt" else
               record.get("process_start_ticks", record.get("process_started")))
    if (str(started) != identity.started or not isinstance(record.get("owner"), str)
            or record["owner"].casefold() != identity.owner.casefold()):
        return None
    return identity


def _ancestor(owner, backend) -> bool:
    try:
        child = backend.identity(os.getpid())
        requester = child.owner if child is not None else ""
        seen = set()
        while child is not None and child.parent > 0 and child.pid not in seen:
            seen.add(child.pid)
            parent = backend.identity(child.parent)
            if parent is None or int(parent.started) > int(child.started):
                return False  # A reused parent PID is not an ancestor.
            if parent.pid == owner.pid:
                return parent == owner and parent.owner.casefold() == requester.casefold()
            child = parent
    except OSError:
        pass
    return False


def _holder(record, owner) -> str:
    if owner is None or not isinstance(record.get("holder"), str):
        listing = ("Get-Process python,pwsh -ErrorAction SilentlyContinue | "
                   "Select-Object Id,ProcessName,StartTime" if os.name == "nt" else
                   "ps -eo pid,ppid,lstart,comm")
        return ("a live holder whose identity could not be verified; list running harnesses with "
                + listing)
    try:
        acquired = datetime.fromisoformat(record["acquired"])
        age = max(0, int((datetime.now(timezone.utc) - acquired).total_seconds()))
    except (KeyError, TypeError, ValueError):
        age = None
    held = f", held for {age} s" if age is not None else ", acquisition time unavailable"
    return f"{record['holder']} (PID {owner.pid}){held}"


def acquire(path: Path, *, holder: str, wait_seconds: float = 0,
            contain: bool = False) -> Lease:
    with _mutex:
        return _acquire(path, holder=holder, wait_seconds=wait_seconds, contain=contain)


def _after_fork():
    global _held, _mutex
    _mutex = threading.RLock()
    for lease in _held.values():
        lease.close()  # Child copies close without truncating the parent's record.
    _held = {}


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _acquire(path: Path, *, holder: str, wait_seconds: float,
             contain: bool) -> Lease:
    if (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
            or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 86400):
        raise ValueError("database turn wait must be between 0 and 86400 seconds")
    key = str(path.absolute())
    previous = _held.get(key)
    if previous is not None and not previous.closed:
        if previous.pid == os.getpid():
            if contain and not previous.contained:
                _backend().contain_process_tree()
                previous.contained = True
            return previous
        previous.close()
    backend = _backend()
    deadline = time.monotonic() + wait_seconds
    next_notice = 0.0
    while True:
        try:
            stream = backend.try_lock(path)
        except OSError as error:
            raise RuntimeError(f"cannot open the database turn at {path}: {error}; fix the lock-folder access and retry") from None
        if stream is not None:
            lease = Lease(stream, pid=os.getpid())
            try:
                identity = backend.identity(os.getpid())
                if identity is None:
                    raise RuntimeError("cannot verify this database turn's process identity")
                if contain:
                    backend.contain_process_tree()
                    lease.contained = True
                record = {"holder": holder, "pid": identity.pid,
                          "process_started": identity.started, "owner": identity.owner,
                          "acquired": datetime.now(timezone.utc).isoformat()}
                if os.name != "nt":
                    record["process_start_ticks"] = identity.started
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps(record).encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                lease.close()
                raise
            _held[key] = lease
            atexit.register(lease.close)
            return lease
        record = _record(path)
        owner = _matching_owner(record, backend)
        if owner is not None and _ancestor(owner, backend):
            # The OS handle is still held. A flag or a stale record alone
            # cannot grant this reuse; the live owner must be an ancestor.
            return Lease(None, pid=os.getpid(), delegated=True)
        who = _holder(record, owner)
        message = (f"database turn is held by {who}; wait for that run to finish, "
                   "or use --database-turn-wait-seconds with a bounded wait")
        now = time.monotonic()
        if now >= deadline:
            raise TurnUnavailable(message)
        if now >= next_notice:
            print(f"Waiting: {message}", file=sys.stderr, flush=True)
            next_notice = now + 30
        time.sleep(min(0.1, max(0, deadline - now)))
