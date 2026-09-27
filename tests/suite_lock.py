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

A queued run must not outlive a change to the code it already holds. By the
time it waits, it has imported conftest.py and what that imports at load:
this module, the test helpers, and some package code (tests/fake_embedder.py
brings in pseudolife_memory/utils/config.py). pytest imports everything else
at collection, after the lock. On 2026-09-25 a run queued from 14:59 to
17:46, its session merged master at ~15:50 (new CoordinationConfig
defaults), and the run tested the old defaults against the new test files:
9 failures, each passing alone. So a full run fingerprints what it has
already read from the tree before it queues: the modules it imported, the
ini file pytest read, and files conftest names (ops/.env, read for the
bench URL). It compares them on every poll, the last time just before it
takes the lock, and on a change it leaves the queue with a usage error
asking for a rerun: within seconds of the change, not when its turn comes,
and without ever holding the lock. Reloading instead is unsound: other
modules keep references to the old objects. Modules not yet imported are
read at collection, after the lock. What goes unseen: a change in the
first second of startup, between a module's import and the fingerprint
(0.7 s measured, most of it the mcp import and plugin setup). xdist workers
start after the controller holds the lock (in pytest_sessionstart) and
import from disk then, so the controller's check covers them; an xdist run
refuses all the same, since its controller's hooks would still run the old
code. Taking the lock before those imports cannot close the gap on its
own: pytest sets ``config.args``, which decide whether a run is full, only
after importing conftest.py, and this module must be imported to take the
lock at all.

The board mirrors the lock. On the night of 2026-09-27 two full suites and
three GPU cells ran while the agent board's lease list stayed empty, and the
SUITE-START/SUITE-END notes the rule asked for were status overwrites that no
peer was sent. A full run now puts a board lease named ``full-suite`` behind
its OS lock (``pseudolife_memory.lease_cli.BoardMirror``): while it queues it
is a board waiter, once it holds the lock it holds the lease, renewed in the
background, with the run's pid and worktree as its purpose and the expected
end from the median of the last EXPECT_SAMPLE recorded run times (or
DEFAULT_EXPECT_SECONDS); the peers whose status says ``suite=running``,
``suite=queued`` or ``gpu=``, or who are parked until the lease clears, are
sent one notice on acquire and one on release. The OS lock stays the truth:
a board that is unreachable, refuses, or shows another holder costs one
line on stderr and never delays or stops the run; ``PSEUDOLIFE_SUITE_LOCK=off``
(CI) takes neither. conftest snapshots the bearer and daemon URL at import
(``board_environment``), before tests/client_environment.py strips them.

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
    Measured 2026-09-25 on the maintainer's Windows host: two slots ran each
    suite in ~50 min instead of ~17, with load-timeout failures, so it stays
    on 1. Slot 0 keeps the historical file
    names, so runs from older code share it and never see a second slot;
    waiters take free slots in arrival order, and notices name every holder.
    Queued runs re-read the count at every poll, so a change reaches the
    backlog at once; lowering it never stops a run that already holds a
    higher slot. A whole number from 1 to 8; the file may carry a BOM.
``PSEUDOLIFE_SUITE_LOCK_DIR``
    Overrides ``~/.pseudolife-mcp/locks`` (the tests use a temp dir).
``PSEUDOLIFE_TEST_CUDA=1``
    Leaves ``CUDA_VISIBLE_DEVICES`` as the environment has it.

Only the standard library at import time (pytest is imported lazily, where
a session already has it): conftest imports this before torch.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import IO, Callable

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
MAX_SLOTS = 8  # a sanity bound: each slot is a ~20 GB full suite
QUEUE_DIR = "full-suite.queue"
TICKET_SUFFIX = ".ticket"
# How long each full run held the lock, one JSON line per run, newest last;
# the board's expected end is the median of the last EXPECT_SAMPLE. Passing
# suites measured 16:40 to 17:26 on the maintainer's host (2026-09-24/25);
# the default leaves room for a loaded one until five runs are on record.
DURATIONS_FILE = "full-suite.durations.jsonl"
DURATIONS_KEEP = 50
EXPECT_SAMPLE = 5
DEFAULT_EXPECT_SECONDS = 25 * 60
# What the board mirror reads from the environment conftest imports in:
# the bearer, its file form, the daemon URL and the project for the notices.
BOARD_ENVIRONMENT = ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE",
                     "PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_AGENT_PROJECT")
SUITE_LEASE = "full-suite"
# How long a run that leaves the queue without the lock (refused, stale
# tree, Ctrl-C) waits for the board to drop its place.
ABORT_BUDGET = 5.0

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
    worktree: str = ""
    taken_at: float = 0.0  # time.monotonic() at acquisition
    mirror: object | None = None  # the board's view of this lock, if any


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
            # utf-8-sig: Windows PowerShell 5.1's Set-Content -Encoding UTF8
            # writes a byte-order mark.
            raw, source = path.read_text(encoding="utf-8-sig"), str(path)
        except FileNotFoundError:
            return 1
        except UnicodeDecodeError:
            raise ValueError(f"{path}: not UTF-8 text (PowerShell 5.1's `>` "
                             f"writes UTF-16; use Set-Content -Encoding ascii)") from None
    text = raw.strip()
    if not (text.isascii() and text.isdigit() and 1 <= int(text) <= MAX_SLOTS):
        raise ValueError(f"{source}={text!r}: expected a whole number from 1 "
                         f"to {MAX_SLOTS}")
    return int(text)


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


def _take_a_slot(directory: Path, handles: dict[int, IO[bytes]],
                 count: int) -> int | None:
    """Lock the first free slot of ``count``, opening handles as needed."""
    for index in range(count):
        if index not in handles:
            handles[index] = open(directory / _slot_file(LOCK_FILE, index), "a+b")  # noqa: SIM115
        if _try_lock(handles[index]):
            return index
    return None


def _mirror_call(mirror, method: str, out: IO[str], *args) -> None:
    """One call on the board mirror; whatever it raises is one line, never
    the run's problem. The mirror does its board traffic on its own thread,
    so ``waiting`` and ``hold`` return at once and ``release`` within its
    budget."""
    try:
        getattr(mirror, method)(*args)
    except Exception as exc:  # noqa: BLE001 — the board never stops a run
        print(f"full-suite lock: the board mirror failed on {method} "
              f"({type(exc).__name__}); the run continues", file=out, flush=True)


def acquire(directory: Path, mode: str, *, worktree: str,
            slots: int | Callable[[], int] = 1,
            poll: float = 2.0, notice_every: float = 60.0,
            out: IO[str] | None = None,
            check: Callable[[], None] | None = None,
            mirror=None) -> HeldLock:
    """Take one of ``slots`` lock slots in arrival order, waiting (``wait``)
    or raising :class:`SuiteLockBusy` (``fail``) while every slot is held
    or another process queued first. A callable ``slots`` is read again on
    every poll, so a changed count reaches runs already queued. ``check``
    runs on every poll, just before each try for the lock; whatever it
    raises abandons the wait. ``mirror`` (see :func:`board_mirror`) is told
    ``waiting()`` on every poll spent queued and ``hold()`` once the lock is
    taken; :func:`release` tells it ``release()``."""
    count = slots() if callable(slots) else slots
    if count < 1:
        raise ValueError(f"slots={count}: at least one is needed")
    out = out or sys.stderr
    directory.mkdir(parents=True, exist_ok=True)
    handles: dict[int, IO[bytes]] = {}
    slot = None
    waited_from = next_notice = None
    try:
        ticket = _enqueue(directory / QUEUE_DIR)
        try:
            while True:
                if callable(slots):
                    try:
                        count = slots()
                    except (OSError, ValueError):
                        pass  # a bad edit mid-queue: keep the last good count
                ahead = _queued_ahead(ticket)
                if check is not None:
                    check()  # every poll, the last time just before the lock
                if not ahead:
                    slot = _take_a_slot(directory, handles, count)
                    if slot is not None:
                        break
                situation = _describe_wait(
                    [read_holder(directory, index) for index in range(count)], ahead)
                if mode == "fail":
                    raise SuiteLockBusy(
                        f"full-suite lock {situation}; {LOCK_ENV}=fail refuses "
                        f"to queue (unset it to wait, or run a targeted subset)")
                now = time.monotonic()
                hint = ""
                if waited_from is None:
                    waited_from = next_notice = now
                    hint = (f" (lock directory {directory}; {LOCK_ENV}=fail exits "
                            f"instead, =off skips it)")
                if now >= next_notice:
                    print(f"waiting for the full-suite lock {situation}{hint}",
                          file=out, flush=True)
                    next_notice = now + notice_every
                if mirror is not None:
                    _mirror_call(mirror, "waiting", out)
                time.sleep(poll)
        finally:
            _leave(ticket)  # taken, refused or interrupted: out of the queue
    except BaseException:  # refused, a lock error, or Ctrl-C while queued
        for handle in handles.values():
            handle.close()
        if mirror is not None:
            # Out of the board's queue too, or give back an early grant:
            # otherwise `lease check` shows a run that is not there.
            _mirror_call(mirror, "release", out, ABORT_BUDGET)
        raise
    for index, handle in handles.items():
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
        which = f" (slot {slot + 1} of {count})" if count > 1 else ""
        print(f"full-suite lock acquired{which} after waiting "
              f"{minutes}m{seconds:02d}s", file=out, flush=True)
    held = HeldLock(handles[slot], directory, slot, worktree=str(worktree),
                    taken_at=time.monotonic(), mirror=mirror)
    if mirror is not None:
        _mirror_call(mirror, "hold", out)
    return held


def release(held: HeldLock, *, record: bool = True) -> None:
    """Free the lock, then tell the board. ``record`` times the run for the
    next one's expected end: conftest passes False for a run that did not
    run its tests (interrupted, a collection or usage error), so aborts do
    not drag the median down."""
    if record and held.taken_at:
        try:
            record_duration(held.directory, time.monotonic() - held.taken_at,
                            worktree=held.worktree)
        except (OSError, ValueError):
            pass  # a record for the next run's estimate; the lock is what counts
    # Drop the record before unlocking, so it never describes a successor.
    holder = read_holder(held.directory, held.slot)
    if holder is not None and holder.get("pid") == os.getpid():
        try:
            (held.directory / _slot_file(HOLDER_FILE, held.slot)).unlink()
        except OSError:
            pass  # a waiter is reading it; the next holder overwrites it
    _unlock(held.file)
    held.file.close()
    # The board last, as `lease run` does: a slow daemon must never keep the
    # lock from the next run. The mirror waits at most its release budget.
    if held.mirror is not None:
        _mirror_call(held.mirror, "release", sys.stderr)


def record_duration(directory: Path, seconds: float, *, worktree: str) -> None:
    """Append how long a run held the lock, keeping the last DURATIONS_KEEP."""
    path = directory / DURATIONS_FILE
    line = json.dumps({"seconds": round(seconds, 1), "worktree": worktree,
                       "ended": datetime.now().astimezone().isoformat(timespec="seconds")})
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    lines = [*lines, line][-DURATIONS_KEEP:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def expected_seconds(directory: Path) -> int:
    """How long the next full run should take: the median of the last
    EXPECT_SAMPLE recorded runs, else DEFAULT_EXPECT_SECONDS."""
    try:
        lines = (directory / DURATIONS_FILE).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):  # ValueError: not UTF-8
        return DEFAULT_EXPECT_SECONDS
    seconds = []
    for line in lines:
        try:
            value = json.loads(line).get("seconds")
        except (ValueError, AttributeError):
            continue
        # Bounded as well as finite: json reads Infinity, NaN and 1e400.
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 < value <= 7 * 86400):
            seconds.append(float(value))
    recent = sorted(seconds[-EXPECT_SAMPLE:])
    if not recent:
        return DEFAULT_EXPECT_SECONDS
    middle = len(recent) // 2
    median = recent[middle] if len(recent) % 2 else (recent[middle - 1] + recent[middle]) / 2
    return max(1, int(round(median)))


class _BoardEnvironment(dict):
    """A dict whose repr names its keys only: it carries the bearer, and a
    traceback shown with locals (pytest -l) must not print it."""

    def __repr__(self) -> str:
        return f"<board environment: {', '.join(sorted(self))}>"

    __str__ = __repr__


def board_environment(environ) -> dict[str, str]:
    """The environment the board mirror needs, copied before the suite's
    client isolation strips it (tests/client_environment.py)."""
    return _BoardEnvironment(
        {name: environ[name] for name in BOARD_ENVIRONMENT if name in environ})


def board_mirror(directory: Path, worktree, board_environ, *, transport=None):
    """The board's view of this run's lock, or None when the package that
    talks to the board cannot be imported. The lease is ``full-suite``; its
    purpose names the run, and its expected end comes from the recorded run
    times in ``directory``. Nothing is contacted until the lock is waited
    for or taken."""
    try:
        from pseudolife_memory.lease_cli import BoardMirror
    except ImportError as exc:
        print(f"full-suite lock: no board mirror ({exc}); the run continues",
              file=sys.stderr, flush=True)
        return None
    worktree = Path(worktree)
    return BoardMirror(
        SUITE_LEASE, purpose=f"pytest pid {os.getpid()} in {worktree.name}",
        expect=expected_seconds(directory), worktree=str(worktree),
        environ=board_environ, transport=transport)


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None  # absent, or unreadable: no content to compare


def imported_sources(root: Path) -> dict[Path, str | None]:
    """The source file of every module this process has imported from
    ``root``, with a digest of its content now. An interpreter environment
    inside ``root`` (a .venv in the checkout) is left out: it is not the
    tree's code."""
    root = root.resolve()
    interpreter = {prefix for prefix in (Path(p).resolve() for p in (
        sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix))
        if root in prefix.parents}
    sources: dict[Path, str | None] = {}
    for module in list(sys.modules.values()):
        try:
            # vars(), not getattr(): a lazy module's __getattr__ must not run.
            file = vars(module).get("__file__")
        except TypeError:
            continue  # not a module object
        if not isinstance(file, str):
            continue  # built-in, or a namespace package
        try:
            path = Path(file).resolve()
        except (OSError, ValueError):
            continue
        if root in path.parents and not any(
                prefix in path.parents for prefix in interpreter):
            sources[path] = _digest(path)
    return sources


def changed_sources(sources: dict[Path, str | None], root: Path) -> list[str]:
    """The files of ``sources`` whose content differs now, sorted, relative
    to ``root`` where they lie under it; one that went or came is marked."""
    root = root.resolve()
    changed = []
    for path, digest in sorted(sources.items()):
        now = _digest(path)
        if now != digest:
            name = (path.relative_to(root).as_posix() if root in path.parents
                    else str(path))
            if now is None:
                name += " (deleted)"
            elif digest is None:
                name += " (created)"
            changed.append(name)
    return changed


class TreeChanged(RuntimeError):
    """Files a queued run already read changed on disk while it waited."""


def take_for_session(config, environ, tests_root: Path,
                     read_files=(), mirror=None) -> HeldLock | None:
    """The conftest entry point: the held lock, or None when this session
    does not take it (targeted run, ``off``, or an xdist worker). A usage
    error, without ever taking the lock, when tree code this process
    imported, its ini file, or one of ``read_files`` (files conftest read
    at import) changed on disk while it queued. ``mirror`` is the board's
    view of the lock (:func:`board_mirror`), or a callable that builds it,
    called only for a run that takes the lock, before the fingerprint, so
    the modules it imports are fingerprinted with the rest."""
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
    if callable(mirror):
        mirror = mirror()
    # What this process already runs or read from the tree, fingerprinted
    # before it can wait: the module docstring has the 2026-09-25 run that
    # went stale. pytest read the ini file before conftest was imported.
    worktree = tests_root.parent
    sources = imported_sources(worktree)
    inipath = getattr(config, "inipath", None)
    for path in ([inipath] if inipath else []) + list(read_files):
        sources[Path(path).resolve()] = _digest(Path(path))

    def unchanged() -> None:
        changed = changed_sources(sources, worktree)
        if changed:
            raise TreeChanged(
                f"the tree changed while this run was queued; rerun it. It "
                f"read these before queueing and they differ on disk now, so "
                f"it would test their old contents against the new files: "
                f"{', '.join(changed)}")

    try:
        slot_count(environ, directory)  # a bad count fails before queueing
        return acquire(directory, mode, worktree=str(worktree),
                       slots=lambda: slot_count(environ, directory),
                       check=unchanged, mirror=mirror)
    except (SuiteLockBusy, TreeChanged, ValueError) as exc:
        raise pytest.UsageError(str(exc)) from None  # busy (fail), stale, bad count
    except OSError as exc:
        raise pytest.UsageError(
            f"cannot take the full-suite lock in {directory}: {exc} "
            f"({LOCK_ENV}=off skips it)") from None
