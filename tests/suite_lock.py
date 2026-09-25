"""One full test suite at a time per machine unless configured for more, and
CPU-only by default.

Measured 2026-09-23 on the maintainer's Windows host: one full CPU suite
commits ~20 GB (the pytest process ~14.9 GB, its spawned test daemons
~4.5 GB) against a ~120-124 GB commit limit that the desktop, WSL and ~75
MCP shims already hold ~100 GB of. Two concurrent suites crossed the limit —
spawned daemons died with "The paging file is too small for this operation
to complete. (os error 1455)", surfacing as MCPError/ConnectError in
test_daemon_http, while the same head passed alone — and three left 7.7 GB
free. Earlier that day a targeted GPU run beside two suites took 143 CUDA
OOMs, because every pytest loads the embedder on CUDA when it can see one.
A board rule (announce SUITE-START/SUITE-END) held for briefed sessions,
but two unbriefed sessions each started a full suite: only the test harness
reaches every session, whatever agent runs it.

So ``tests/conftest.py`` hides CUDA before anything imports torch, and a
full run takes an OS-level exclusive lock shared by every worktree on the
machine — released by the OS when its holder exits or dies, so there is no
pid file to go stale.

Waiters are served first come, first served. On 2026-09-25, with eight full
suites queued, one waited from 13:48 to past 14:57 while later arrivals took
the lock ahead of it: each waiter polled a non-blocking lock, so whoever
polled first after a release won. Now each waiter drops a ticket in
``full-suite.queue/`` beside the lock, named for its arrival time and pid,
and holds an OS lock on that ticket while it waits; only a waiter with no
live ticket ahead of it may try the lock. A waiter that dies loses its
ticket's lock with everything else: later waiters pass the ticket over at
once, and delete it once it is past a grace window. Liveness is that lock,
not the pid: Windows reuses pids quickly, and ``os.kill(pid, 0)`` there
sends CTRL_C_EVENT (which is 0) rather than probing. A waiter stopped while
first in line (a debugger, SIGSTOP, a Windows console mid-selection) keeps
its place, and holds up the queue even while the lock is free; the waiting
notice names its pid. A run from code older than the queue takes no ticket
and races for the lock as before; it still excludes and is excluded, since
the lock file is unchanged.

Configuration:

``PSEUDOLIFE_SUITE_LOCK``
    ``wait`` (default) queues behind the holder and any earlier waiters,
    naming them about once a minute; ``fail`` exits with a usage error
    instead of queueing, even when the lock is free but an earlier waiter
    has not taken it yet; ``off`` skips the
    lock. Unset on GitHub Actions it means ``off``: each hosted job has a
    VM of its own, so there is nothing to contend with, and a lock fault
    there would only turn into a silent hang until the job timeout.
    This file's tests still run both lock backends in CI.
``PSEUDOLIFE_SUITE_SLOTS``, else a ``full-suite.slots`` file in the lock directory
    How many full runs may hold the lock at once; 1 when neither is set.
    The maintainer's Windows host runs 2 since 2026-09-25, after memory was
    trimmed (paging to NVMe accepted). Slot 0 keeps the historical file
    names, so runs from older code share it and never see a second slot;
    waiters take free slots in arrival order, and notices name every holder.
    A lowered count takes effect as the higher slots' holders finish.
``PSEUDOLIFE_SUITE_LOCK_DIR``
    Overrides ``~/.pseudolife-mcp/locks`` (the tests use a temp dir).
``PSEUDOLIFE_TEST_CUDA=1``
    Leaves ``CUDA_VISIBLE_DEVICES`` as the environment has it.

Only the standard library at import time (pytest is imported lazily, where
a session already has it): conftest imports this before torch.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_ENV = "PSEUDOLIFE_SUITE_LOCK"
LOCK_DIR_ENV = "PSEUDOLIFE_SUITE_LOCK_DIR"
SLOTS_ENV = "PSEUDOLIFE_SUITE_SLOTS"
CUDA_OPT_IN_ENV = "PSEUDOLIFE_TEST_CUDA"
MODES = ("wait", "fail", "off")
LOCK_FILE = "full-suite.lock"
HOLDER_FILE = "full-suite.holder.json"
SLOTS_FILE = "full-suite.slots"
QUEUE_DIR = "full-suite.queue"
TICKET_SUFFIX = ".ticket"

# A waiter creates its ticket, then locks it, so for a moment a new ticket
# looks abandoned. A free ticket younger than this is passed over but not
# deleted: deleted then, its owner would lock an unlinked file on POSIX and
# drop out of the queue. Older, its owner is gone, and the next waiter to
# look deletes it. Two adjacent syscalls take microseconds; 30 s only
# delays the tidying up.
TICKET_GRACE_NS = 30 * 10**9

# pytest options under which a session runs no test. (--help needs no entry:
# pytest stops parsing before it sets config.args, so no path covers tests/.)
LISTING_OPTIONS = (
    "collectonly", "markers", "showfixtures", "show_fixtures_per_test",
)

# What a non-blocking lock attempt raises while another process holds it:
# EACCES from msvcrt.locking, EWOULDBLOCK/EAGAIN from flock.
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


def _only_excludes(expression: str) -> bool:
    """Whether a -k/-m expression still selects an item that matches none
    of its names (``not slow``, ``slow or not graph``) — nearly the whole
    suite. Evaluated with pytest's own -k/-m grammar; an expression pytest
    cannot parse is rejected before any test runs, so it narrows."""
    try:
        from _pytest.mark.expression import Expression

        return bool(Expression.compile(expression).evaluate(
            lambda name, **kwargs: False))
    except Exception:  # noqa: BLE001 — any parse error, or a moved private API
        return False


class SuiteLockBusy(RuntimeError):
    """The lock is held and the mode is ``fail``."""


@dataclass
class HeldLock:
    file: IO[bytes]
    directory: Path
    slot: int = 0


def _slot_file(name: str, slot: int) -> str:
    """Slot 0 keeps the historical names (``full-suite.lock``,
    ``full-suite.holder.json``), so runs from older code share it; slot k
    is ``full-suite.k.lock`` and ``full-suite.k.holder.json``."""
    if slot == 0:
        return name
    stem, _, rest = name.partition(".")
    return f"{stem}.{slot}.{rest}"


def hide_cuda(environ) -> bool:
    """Hide every GPU unless the run opted in; True when hidden.

    ``-1``, never the empty string: measured 2026-09-23 on the Windows host
    (torch 2.10+cu126), an empty value set from bash, PowerShell or Python
    left ``torch.cuda.is_available()`` True — the driver reads it as unset —
    while ``device_count()``, which only parses the string, reported 0.
    """
    if environ.get(CUDA_OPT_IN_ENV) == "1":
        return False
    environ["CUDA_VISIBLE_DEVICES"] = "-1"
    return True


def lock_mode(environ) -> str:
    raw = environ.get(LOCK_ENV)
    if raw is None:
        # GITHUB_ACTIONS, not the generic CI flag: agent harnesses export
        # CI=true too, and those sessions are what the lock exists for.
        return "off" if environ.get("GITHUB_ACTIONS") == "true" else "wait"
    mode = raw.strip().lower()
    if mode not in MODES:
        raise ValueError(f"{LOCK_ENV}={raw!r}: expected one of {', '.join(MODES)}")
    return mode


def lock_dir(environ) -> Path:
    override = environ.get(LOCK_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".pseudolife-mcp" / "locks"


def slot_count(environ, directory: Path) -> int:
    """How many full runs may hold the lock at once: ``PSEUDOLIFE_SUITE_SLOTS``,
    else the ``full-suite.slots`` file in the lock directory, else 1."""
    raw, source = environ.get(SLOTS_ENV), SLOTS_ENV
    if raw is None:
        path = directory / SLOTS_FILE
        try:
            raw, source = path.read_text(encoding="utf-8"), str(path)
        except FileNotFoundError:
            return 1
    try:
        count = int(raw.strip())
    except ValueError:
        count = 0
    if count < 1:
        raise ValueError(f"{source}={raw.strip()!r}: expected a whole number, "
                         f"at least 1")
    return count


def is_full_run(args, invocation_dir: Path, tests_root: Path, *,
                keyword: str = "", markexpr: str = "",
                listing_only: bool = False) -> bool:
    """Whether a run covers the whole ``tests/`` tree, or most of it.

    Full: some path argument is ``tests/`` itself or one of its ancestors
    (``pytest`` with no arguments resolves to ``tests`` via testpaths), or
    the named test files are at least half of ``tests/test_*.py`` (a shell
    glob such as ``tests/test_*.py`` names them all). Targeted: a few named
    files or node ids, a ``-k``/``-m`` selection — unless it only excludes
    (``-k "not x"``) — or a run that executes no test (``--collect-only``,
    ``--fixtures``, ``--help``).
    """
    if listing_only:
        return False
    for expression in (keyword, markexpr):
        if expression and not _only_excludes(expression):
            return False
    root = tests_root.resolve()
    named: set[Path] = set()
    for arg in args:
        path_part = str(arg).split("::", 1)[0]
        if not path_part:
            continue
        path = Path(path_part)
        if not path.is_absolute():
            path = Path(invocation_dir) / path
        try:
            path = path.resolve()
        except OSError:
            continue
        if path == root or path in root.parents:
            return True
        # Only real test modules count toward the share: not conftest.py,
        # helpers or a mistyped path.
        if (path.parent == root and fnmatch(path.name, "test_*.py")
                and path.is_file()):
            named.add(path)
    if not named:
        return False
    return 2 * len(named) >= sum(1 for _ in root.glob("test_*.py"))


def _try_lock(handle: IO[bytes]) -> bool:
    """True when taken, False when another process holds it. Any other
    failure (a bad handle, a filesystem without locks) raises: read as
    "busy", it would queue the run forever behind nobody."""
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


@dataclass
class _Ticket:
    path: Path
    file: IO[bytes]
    key: tuple[int, int]  # (arrival ns, pid): the queue order


def _ticket_key(name: str) -> tuple[int, int] | None:
    if not name.endswith(TICKET_SUFFIX):
        return None
    arrived, _, pid = name[: -len(TICKET_SUFFIX)].partition("-")
    try:
        return int(arrived), int(pid)
    except ValueError:
        return None


def _enqueue(queue_dir: Path) -> _Ticket:
    """Create this waiter's ticket and lock it."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    # Wall-clock time, which every process on the machine reads alike.
    # Arrivals within one clock tick (15.6 ms on Windows) go in pid order,
    # and a clock stepped back lets later arrivals sort first: both cost
    # fairness only, never exclusion.
    arrived, pid = time.time_ns(), os.getpid()
    while True:
        path = queue_dir / f"{arrived:020d}-{pid}{TICKET_SUFFIX}"
        try:
            handle = open(path, "xb")  # noqa: SIM115 — held until _leave()
            break
        except FileExistsError:
            arrived += 1  # another thread of this process, same clock tick
    ticket = _Ticket(path, handle, (arrived, pid))
    try:
        # A later arrival testing the new ticket holds its lock for a moment.
        for _ in range(100):
            if _try_lock(handle):
                return ticket
            time.sleep(0.01)
        raise OSError(errno.EAGAIN, f"cannot lock the new queue ticket {path}")
    except BaseException:
        _leave(ticket)
        raise


def _leave(ticket: _Ticket) -> None:
    _unlock(ticket.file)
    ticket.file.close()
    try:
        ticket.path.unlink()
    except OSError:
        # Windows refuses while a waiter has it open to test it; unlocked
        # now, it is deleted by a later look once past its grace window.
        pass


def _still_held(path: Path, *, young: bool) -> bool:
    """Whether a ticket's owner still holds it. A free one past its grace
    window is deleted on the way."""
    try:
        handle = open(path, "rb")  # not "a": that would recreate a deleted one
    except OSError:
        return False  # deleted, or being deleted
    try:
        if not _try_lock(handle):
            return True
        _unlock(handle)
    finally:
        handle.close()
    if not young:
        try:
            path.unlink()
        except OSError:
            pass  # another waiter has it open to test it; a later look deletes it
    return False


def _queued_ahead(ticket: _Ticket) -> list[int]:
    """The pids of the live waiters that arrived before ``ticket``, oldest
    first."""
    now = time.time_ns()
    earlier = sorted((key, name) for name in os.listdir(ticket.path.parent)
                     if (key := _ticket_key(name)) is not None and key < ticket.key)
    return [key[1] for key, name in earlier
            if _still_held(ticket.path.parent / name,
                           young=now - key[0] < TICKET_GRACE_NS)]


def _describe_wait(records: list[dict | None], ahead: list[int]) -> str:
    """What this run is queued behind: every slot's holder, and earlier
    waiters. ``records`` has one entry per slot."""
    if not ahead:  # every slot was tried, and every one is held
        return "held by " + " and ".join(map(describe_holder, records))
    plural = "s" if len(ahead) > 1 else ""
    queued = (f"{len(ahead)} earlier arrival{plural} queued ahead "
              f"(pid{plural} {', '.join(map(str, ahead))}")
    present = [record for record in records if record]
    if len(present) < len(records):
        # A slot is free, or between holders. A first waiter that stays first
        # is stopped (a debugger, SIGSTOP, a Windows console mid-selection)
        # and keeps its place until it resumes or ends.
        queued += (f"; if pid {ahead[0]} never takes the lock, it is paused or "
                   f"hung: resume or end it")
    if present:
        return ("held by " + " and ".join(map(describe_holder, present))
                + f", with {queued})")
    return f"with no recorded holder, {queued})"


def read_holder(directory: Path, slot: int = 0) -> dict | None:
    path = directory / _slot_file(HOLDER_FILE, slot)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def describe_holder(record: dict | None) -> str:
    if not record:
        return "a process that left no holder record"
    since = "?"
    try:
        since = datetime.fromisoformat(record["started"]).strftime("%H:%M")
    except (KeyError, TypeError, ValueError):
        pass
    return (f"{record.get('worktree', '?')} (pid {record.get('pid', '?')}) "
            f"since {since}")


def acquire(directory: Path, mode: str, *, worktree: str, slots: int = 1,
            poll: float = 2.0, notice_every: float = 60.0,
            out: IO[str] | None = None) -> HeldLock:
    """Take one of ``slots`` lock slots in arrival order, waiting (``wait``)
    or raising :class:`SuiteLockBusy` (``fail``) while every slot is held
    or another process queued first."""
    if slots < 1:
        raise ValueError(f"slots={slots}: at least one is needed")
    out = out or sys.stderr
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_FILE
    handles: list[IO[bytes]] = []
    slot = None
    waited_from = next_notice = None
    try:
        for index in range(slots):
            handles.append(open(directory / _slot_file(LOCK_FILE, index), "a+b"))  # noqa: SIM115
        ticket = _enqueue(directory / QUEUE_DIR)
        try:
            while True:
                ahead = _queued_ahead(ticket)
                if not ahead:
                    slot = next((index for index, handle in enumerate(handles)
                                 if _try_lock(handle)), None)
                    if slot is not None:
                        break
                situation = _describe_wait(
                    [read_holder(directory, index) for index in range(slots)], ahead)
                if mode == "fail":
                    raise SuiteLockBusy(
                        f"full-suite lock {situation}; {LOCK_ENV}=fail refuses "
                        f"to queue (unset it to wait, or run a targeted subset)")
                now = time.monotonic()
                hint = ""
                if waited_from is None:
                    waited_from = next_notice = now
                    hint = f" ({lock_path}; {LOCK_ENV}=fail exits instead, =off skips it)"
                if now >= next_notice:
                    print(f"waiting for the full-suite lock {situation}{hint}",
                          file=out, flush=True)
                    next_notice = now + notice_every
                time.sleep(poll)
        finally:
            _leave(ticket)  # taken, refused or interrupted: out of the queue
    except BaseException:  # refused, a lock error, or Ctrl-C while queued
        for handle in handles:
            handle.close()
        raise
    for index, handle in enumerate(handles):
        if index != slot:
            handle.close()
    record = {
        "pid": os.getpid(),
        "worktree": str(worktree),
        "started": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        (directory / _slot_file(HOLDER_FILE, slot)).write_text(
            json.dumps(record), encoding="utf-8")
    except OSError:
        pass  # the record is a courtesy to waiters; the lock is what counts
    if waited_from is not None:
        minutes, seconds = divmod(int(time.monotonic() - waited_from), 60)
        which = f" (slot {slot + 1} of {slots})" if slots > 1 else ""
        print(f"full-suite lock acquired{which} after waiting "
              f"{minutes}m{seconds:02d}s", file=out, flush=True)
    return HeldLock(handles[slot], directory, slot)


def release(held: HeldLock) -> None:
    # Drop the record before unlocking, so it never describes a successor.
    record = read_holder(held.directory, held.slot)
    if record is not None and record.get("pid") == os.getpid():
        try:
            (held.directory / _slot_file(HOLDER_FILE, held.slot)).unlink()
        except OSError:
            pass  # a waiter is reading it; the next holder overwrites it
    _unlock(held.file)
    held.file.close()


def take_for_session(config, environ, tests_root: Path) -> HeldLock | None:
    """The conftest entry point: the held lock, or None when this session
    does not take it (targeted run, ``off``, or an xdist worker)."""
    import pytest

    if hasattr(config, "workerinput"):
        # An xdist worker: its controller already holds the lock, and a
        # worker queued behind its own controller would never start.
        return None
    try:
        mode = lock_mode(environ)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from None
    if mode == "off":
        return None
    option = config.option
    if not is_full_run(
            config.args, config.invocation_params.dir, tests_root,
            keyword=getattr(option, "keyword", "") or "",
            markexpr=getattr(option, "markexpr", "") or "",
            listing_only=any(getattr(option, name, False)
                             for name in LISTING_OPTIONS)):
        return None
    directory = lock_dir(environ)
    try:
        slots = slot_count(environ, directory)
        return acquire(directory, mode, worktree=str(tests_root.parent),
                       slots=slots)
    except (SuiteLockBusy, ValueError) as exc:  # busy in fail mode, or a bad count
        raise pytest.UsageError(str(exc)) from None
    except OSError as exc:
        raise pytest.UsageError(
            f"cannot take the full-suite lock in {directory}: {exc} "
            f"({LOCK_ENV}=off skips it)") from None
