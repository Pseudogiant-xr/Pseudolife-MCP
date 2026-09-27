"""``pseudolife-mcp lease`` — hold a named lease on a shared resource around a command.

A lease is a named, expiring hold on something several agents share: a full
test suite, the GPU, a daemon maintenance window. Its truth is an OS file lock
(:mod:`pseudolife_memory.os_lock`) this process holds while the command runs,
so the OS releases it the moment this process exits or dies. The agent board,
where the daemon has one, only mirrors it: other agents see who holds what
and until when, and queue in arrival order. An install protects its own
resources by putting this around any command, without touching Pseudolife
code; without a daemon it is ``flock`` with a name.

``lease run NAME -- COMMAND`` on the board: register an ephemeral address
(``resumable: false``, retired an hour after its last activity), call
``lease`` until the board answers ``held`` (the board keeps the FIFO queue),
then take the OS lock and run the command, renewing the board lease every
ttl/3. The board comes first and the OS lock second, and nothing holds the OS
lock while it waits on the board, so the two cannot wait on each other. If the
OS lock is held by something the board does not show (a ``--no-board`` run, or
a holder whose board lease lapsed), the board lease is kept renewed while the
OS lock is retried.

Without the board (no token, daemon unreachable, a bearer the board refuses,
coordination off, a daemon without leases, or ``--no-board``) it says once
why, then waits on the OS lock alone: polled, not FIFO. A board that answers
and then keeps failing for ``BOARD_GIVE_UP`` seconds is dropped the same way.
A board failure never stops the command from running under the OS lock, and
the OS lock is what excludes: a renewal that finds the lease lost warns once
and the command keeps running.

``--expect`` is sent on every ``lease`` call, the same value each time: the
board keeps each hold's expect, so repeating it leaves the expected end alone
(a changed value would be logged as a change, so it is never recomputed).

The instance credential lives in this process's memory only: it is never
printed, logged, or passed to the command. The command inherits stdio and the
environment plus ``PSEUDOLIFE_LEASES_HELD`` (comma-separated, appended to any
inherited value). A run whose name is already in that list while its lock is
held is nested inside a run of the same lease, and would wait for its own
parent forever; it exits 64 at once instead. The OS lock's handle is not
inheritable, so a daemon the command leaves behind cannot pin the lock. The
flip side: if this process is killed outright (SIGKILL, TerminateProcess),
the OS drops the lock at once while the command, if it survives, carries on
unguarded.

Ctrl-C, SIGTERM or SIGHUP stop the command before anything is released. A
console Ctrl-C reaches the command too, so it first gets ``KILL_GRACE`` to
clean up on its own; a stop signal is forwarded to it. Then it is interrupted
(POSIX), terminated and killed, each after another grace.

Exit codes: the command's own; 2 usage error; 64 nested inside a run of the
same lease; 71 the lock file is unusable; 75 ``--timeout`` expired before the
lease was held (the command did not run); 126 the command could not be
started; 127 it was not found; 128+N stopped by signal N (130 Ctrl-C, 143
SIGTERM, 129 SIGHUP).

``lease hold NAME --while-pid PID`` is the lease for a process this run did not
start and cannot wrap: a launcher such as ``evals/qwen_server.ps1`` starts a
detached server, then starts a hold that takes the OS lock, mirrors it on the
board (:class:`BoardMirror`) and releases both when PID exits. The order is the
reverse of ``run``: the OS lock first (the process already owns the resource),
the board second, so a board that is down or shows someone else never delays
or stops anything. The same mirror is what ``tests/conftest.py`` puts behind
the full-suite lock. Acquiring and releasing tell the peers the lease
concerns, by board mail: those whose status says ``suite=running``,
``suite=queued`` or ``gpu=``, and those parked with ``park_clear_by`` naming
the lease (the field is read where present). That replaces the hand-written
SUITE-START/SUITE-END notes, which on the night of 2026-09-27 were status
overwrites nobody was sent.

``lease check NAME`` is the launch gate: exit 0 when free, 1 when the OS lock
or the board says held, naming the holder and the expected end. For
``full-suite`` it probes the test suite's own lock (``full-suite.lock`` and its
slots, with the holder record beside it), since that file, not
``lease-full-suite.lock``, is the truth for a full run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path

from pseudolife_memory import os_lock

EXIT_USAGE = 2
EXIT_NESTED = 64  # sysexits EX_USAGE: run inside a run of the same lease
EXIT_SOFTWARE = 70  # sysexits EX_SOFTWARE: `lease check` itself failed
EXIT_LOCK_ERROR = 71  # sysexits EX_OSERR
EXIT_TEMPFAIL = 75  # sysexits EX_TEMPFAIL
EXIT_CANNOT_RUN = 126
EXIT_NOT_FOUND = 127
# A run stopped by signal N exits 128+N, as from a shell: 130 for Ctrl-C.

DEFAULT_URL = "http://127.0.0.1:8765"
HELD_ENV = "PSEUDOLIFE_LEASES_HELD"
LABEL = "lease-run"
HOLD_LABEL = "lease-hold"  # a mirror of a lock this process holds (hold, conftest)
# The test suite's own lock (tests/suite_lock.py); only ever probed here. Its
# file names are that module's: slot 0 keeps the bare names, slot k inserts
# the slot number, and the holder record beside each names pid and worktree.
SUITE_LEASE = "full-suite"
SUITE_LOCK_FILE = "full-suite.lock"
SUITE_HOLDER_FILE = "full-suite.holder.json"
SUITE_LOCK_DIR_ENV = "PSEUDOLIFE_SUITE_LOCK_DIR"
SUITE_MAX_SLOTS = 8
# Whose status marks them as concerned with the suite and GPU leases: the
# CLAUDE.md status convention (``suite=running|queued``, ``gpu=...``).
_CONCERNED_STATUS = re.compile(r"\bsuite=(running|queued)\b|\bgpu=")
# At most this many peers are told per acquire or release: the board allows
# 60 sends a minute per address, and a lease's audience is a handful.
ANNOUNCE_MAX = 20

# Limits of the coordination lease contract, checked here first so a bad
# argument fails the same way with or without a board.
MAX_NAME = 120
MAX_PURPOSE = 240
MAX_SCOPE = 120  # register's project and task fields
DEFAULT_TTL = 120
MIN_TTL, MAX_TTL = 30, 86400
MIN_EXPECT, MAX_EXPECT = 1, 604800
LIST_LIMIT = 50

# Cadences from the lease contract (2026-09-26), not measured tuning: board
# polls every ~5 s while queued, OS-lock retries every ~2 s, a waiting notice
# about once a minute, renewals every ttl/3. The give-up bound is one default
# ttl: a board failing that long has already let this run's lease lapse.
BOARD_POLL = 5.0
LOCK_POLL = 2.0
NOTICE_EVERY = 60.0
BOARD_GIVE_UP = 120.0
# A renewal that failed transiently is retried this soon (or at the normal
# interval, if that is sooner), so two blips cannot outlast the ttl.
TRANSIENT_RETRY = 5.0
REQUEST_TIMEOUT = 10.0
RELEASE_TIMEOUT = 5.0
# An announcement is a courtesy: a slow daemon gets this long per call.
ANNOUNCE_TIMEOUT = 5.0
KILL_GRACE = 10.0
# How often a wait on the command checks for Ctrl-C: an untimed wait is not
# interruptible on Windows.
CHILD_POLL = 0.2
# How often ``hold`` looks whether the process it follows is still there.
PID_POLL = 0.5

_DURATION = re.compile(r"([0-9]+)([smh]?)", re.IGNORECASE)
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}
_CODE = re.compile(r"[a-z0-9_]{1,64}")

# Why a daemon's refusal means no board for this run, by error code.
_REFUSALS = {
    "unauthorized": "the daemon did not accept the bearer token",
    "authentication_required": "the daemon has no bearer authentication, which the board needs",
    "principal_not_allowed": "this bearer's principal is not allowed on the board",
    "coordination_requires_postgres": "the daemon's board needs PostgreSQL",
    "unknown_coordination_action": "the daemon does not support leases (it predates them)",
    "invalid_credential": "the daemon did not accept this run's board address",
    "instance_not_found": "the daemon no longer knows this run's board address",
}


def parse_duration(text: str) -> int:
    """Whole seconds from ``90``, ``90s``, ``20m`` or ``2h``."""
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r} (use 90, 90s, 20m or 2h)")
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def _renew_interval(ttl: float) -> float:
    return ttl / 3


def _exit_status(returncode: int, *, windows: bool = os.name == "nt") -> int:
    """The command's status as this process's exit code: a POSIX death by
    signal N becomes 128+N as in a shell; a Windows status above 2**31 (an
    NTSTATUS such as 0xC000013A) is made signed so ``sys.exit`` hands the
    same 32 bits back."""
    if returncode < 0 and not windows:
        return 128 - returncode
    if windows and returncode > 0x7FFFFFFF:
        return returncode - (1 << 32)
    return returncode


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) in ("Cc", "Cs") for char in text)


def _clean(value, limit: int = MAX_SCOPE) -> str:
    """Daemon-supplied text made safe for a terminal: control and format
    characters become ``?``, and it is cut at ``limit``."""
    text = "".join("?" if unicodedata.category(char).startswith("C") else char
                   for char in str(value))
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _printable(text: str) -> str:
    """Text the board accepts in a register field: no C0/C1 controls or
    surrogates."""
    return "".join("?" if unicodedata.category(char) in ("Cc", "Cs") else char
                   for char in text)


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:  # an int beyond float range
        return None


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600:02d}h"


def _clock(stamp: float) -> str:
    try:
        local = time.localtime(stamp)
    except (OverflowError, OSError, ValueError):
        return "?"
    if time.strftime("%Y-%m-%d", local) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", local)
    return time.strftime("%Y-%m-%d %H:%M", local)


def _describe_holder(holder: dict, now: float) -> str:
    who = _clean(holder.get("label") or str(holder.get("agent_id") or "")[:8] or "an agent")
    if holder.get("principal"):
        who += f" ({_clean(holder['principal'])})"
    acquired = _number(holder.get("acquired_at"))
    if acquired is not None:
        who += f" for {_span(now - acquired)}"
    if holder.get("purpose"):
        who += f', purpose "{_clean(holder["purpose"], MAX_PURPOSE)}"'
    return who


def _expected_text(stamp, now: float, stale: bool = False) -> str:
    stamp = _number(stamp)
    if stamp is None:
        return ""
    text = f", expected end {_clock(stamp)}"
    if stale or stamp < now:
        text += " (stale: past it)"
    return text


def _describe_waiter(entry: dict) -> str:
    who = _clean(entry.get("label") or str(entry.get("agent_id") or "")[:8] or "an agent")
    since = _number(entry.get("enqueued_at"))
    if since is not None:
        who += f" since {_clock(since)}"
    if entry.get("purpose"):
        who += f', purpose "{_clean(entry["purpose"], MAX_PURPOSE)}"'
    return who


def _queued_notice(name: str, reply: dict) -> str:
    now = time.time()
    position, queued = reply.get("position"), reply.get("queued")
    place = (f"position {position} of {queued}"
             if isinstance(position, int) and isinstance(queued, int) else "queued")
    holder = reply.get("holder")
    if isinstance(holder, dict):
        situation = ("held by " + _describe_holder(holder, now)
                     + _expected_text(holder.get("expected_end"), now))
    else:
        situation = "no holder right now; the board is passing it down the queue"
    return f"lease: waiting for {name!r} on the board ({place}); {situation}"


# --- the board -------------------------------------------------------------------

class _Refused(Exception):
    """The board cannot be used for this run; the message says why, and
    ``code`` is the daemon's error code when it gave one."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class _Transient(Exception):
    """Worth retrying: HTTP 429, a 5xx, a full lease queue, or a daemon that
    answered before and does not now."""


def _daemon_url(value: str) -> str | None:
    """An http(s) origin, as ``shim._validated_daemon_url`` accepts it, or
    None. Replicated rather than imported: the shim's version exits the
    process, and a bad URL here only means no board."""
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port  # noqa: B018 — raises ValueError for a malformed port
    except (TypeError, ValueError):
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(char.isspace() or ord(char) < 0x20 for char in value)):
        return None
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.rstrip("/"), "", "", ""))


def _refusal_text(status: int, code: str | None) -> str:
    if code in _REFUSALS:
        return _REFUSALS[code]
    if status in (401, 403):
        return "the daemon refused the bearer token"
    if status in (404, 405):
        return "the daemon has no coordination API"
    return "the daemon did not accept the request"


def _decode(response) -> dict:
    status = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = None
    code = payload.get("error") if isinstance(payload, dict) else None
    if not (isinstance(code, str) and _CODE.fullmatch(code)):
        code = None  # only a well-formed code is ever repeated to the user
    if status == 200:
        if not isinstance(payload, dict):
            raise _Refused("the daemon's reply was not a JSON object")
        if payload.get("enabled") is False:
            raise _Refused("coordination is disabled on the daemon")
        return payload
    detail = f"HTTP {status}" + (f" {code}" if code else "")
    if status == 429 or status >= 500 or code == "lease_queue_full":
        raise _Transient(detail)
    raise _Refused(f"{_refusal_text(status, code)} ({detail})", code)


class _Board:
    """The coordination REST actions a lease needs. Calls are serialized, so
    the renewal thread and the main thread never interleave on the wire."""

    def __init__(self, url: str, token: str, transport=None):
        import httpx  # the mcp SDK's HTTP client; not needed without a board

        self._httpx = httpx
        self.url = url
        self._client = httpx.Client(
            base_url=url, transport=transport, follow_redirects=False,
            timeout=REQUEST_TIMEOUT, headers={"Authorization": f"Bearer {token}"})
        self._lock = threading.Lock()
        self._agent_id: str | None = None
        self._credential: str | None = None
        self._answered = False

    @property
    def registered(self) -> bool:
        return self._agent_id is not None

    def close(self) -> None:
        self._client.close()

    def _post(self, action: str, body: dict, *, instance: bool = True,
              timeout: float = REQUEST_TIMEOUT) -> dict:
        headers = ({"X-PL-Agent": self._agent_id, "X-PL-Agent-Key": self._credential}
                   if instance else {})
        with self._lock:
            try:
                response = self._client.post(f"/api/coordination/{action}", json=body,
                                             headers=headers, timeout=timeout)
            except self._httpx.RequestError as exc:
                failure = f"the daemon at {self.url} is unreachable ({type(exc).__name__})"
                if self._answered:
                    raise _Transient(failure) from None
                raise _Refused(failure) from None
            self._answered = True
        return _decode(response)

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    def forget(self) -> None:
        """Drop this run's address, so the next call registers a new one."""
        self._agent_id = self._credential = None

    def register(self, *, task: str, project: str, label: str = LABEL,
                 status: str = "") -> None:
        reply = self._post("register", {
            "label": label, "project": project, "task": task, "status": status,
            "capabilities": {"resumable": False}, "wake_enabled": False,
        }, instance=False)
        agent_id, credential = reply.get("agent_id"), reply.get("credential")
        if not (isinstance(agent_id, str) and agent_id
                and isinstance(credential, str) and credential):
            raise _Refused("the daemon's registration reply carried no address")
        self._agent_id, self._credential = agent_id, credential

    def agents(self, *, timeout: float = ANNOUNCE_TIMEOUT) -> list[dict]:
        """The peers the board lists for this address (bounded page)."""
        reply = self._post("agents", {"limit": LIST_LIMIT}, timeout=timeout)
        agents = reply.get("agents")
        if not isinstance(agents, list):
            raise _Refused("the daemon's agents reply was not understood")
        return [agent for agent in agents
                if isinstance(agent, dict) and isinstance(agent.get("agent_id"), str)]

    def send(self, to: str, text: str, request_id: str, *,
             timeout: float = ANNOUNCE_TIMEOUT) -> None:
        self._post("send", {"to": to, "text": text, "request_id": request_id},
                   timeout=timeout)

    def lease(self, name: str, ttl: int, *, expect: int | None = None,
              purpose: str | None = None) -> dict:
        body: dict = {"name": name, "ttl": ttl}
        if expect is not None:
            body["expect"] = expect
        if purpose:
            body["purpose"] = purpose
        reply = self._post("lease", body)
        if reply.get("state") not in ("held", "queued"):
            raise _Refused("the daemon's lease reply was not understood")
        return reply

    def release_quietly(self, name: str) -> None:
        """Release or leave the queue, best effort: a lease this address
        neither holds nor waits for (``lease_not_held``) is already gone."""
        if not self.registered:
            return
        try:
            self._post("release", {"name": name}, timeout=RELEASE_TIMEOUT)
        except Exception:  # noqa: BLE001 — cleanup must not replace the exit code
            pass

    def leases(self, name: str | None = None) -> tuple[list[dict], bool]:
        reply = self._post("leases", {"name": name} if name else {"limit": LIST_LIMIT},
                           instance=False)
        leases = reply.get("leases")
        if not isinstance(leases, list):
            raise _Refused("the daemon's leases reply was not understood")
        return ([lease for lease in leases
                 if isinstance(lease, dict) and isinstance(lease.get("name"), str)],
                reply.get("truncated") is True)


def _connect(no_board: bool, transport, environ=None) -> tuple[_Board | None, str | None]:
    """The board, or None and why there is none. ``environ`` is where the
    daemon URL and the bearer are read (the process environment by default;
    the suite's mirror passes a snapshot taken before its tests scrub it)."""
    if no_board:
        return None, "--no-board was given"
    environ = os.environ if environ is None else environ
    url = _daemon_url(environ.get("PSEUDOLIFE_MCP_DAEMON_URL", DEFAULT_URL))
    if url is None:
        return None, ("PSEUDOLIFE_MCP_DAEMON_URL is not an http(s) origin "
                      "(scheme, host and optional port only)")
    from pseudolife_memory.credentials import CredentialError, CredentialProvider

    try:
        if "PSEUDOLIFE_MCP_TOKEN_FILE" in environ:
            provider = CredentialProvider(path=environ["PSEUDOLIFE_MCP_TOKEN_FILE"])
        else:
            provider = CredentialProvider(token=environ.get("PSEUDOLIFE_MCP_TOKEN") or None)
        token = provider.snapshot().token
    except CredentialError as exc:
        return None, f"the bearer credential is unusable ({exc})"
    if not token:
        return None, "no bearer token (set PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE)"
    return _Board(url, token, transport), None


class _Renewer:
    """Renews a held board lease every ttl/3 on a background thread. A
    transient failure is retried soon and quietly; a lost lease warns once.
    After ``queued`` it keeps renewing, since that same call is what takes
    the lease back when the board grants it again; after a refusal it stops."""

    def __init__(self, board: _Board, name: str, ttl: int, expect: int | None,
                 purpose: str | None):
        self._board, self._name, self._ttl = board, name, ttl
        self._expect, self._purpose = expect, purpose
        self._interval = _renew_interval(ttl)
        self._stop = threading.Event()
        self._warned = False
        self._thread = threading.Thread(target=self._loop, name="lease-renew", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=REQUEST_TIMEOUT + 1)

    def _loop(self) -> None:
        delay = self._interval
        while not self._stop.wait(delay):
            delay = self._renew()

    def _renew(self) -> float:
        """One renewal; returns the delay until the next. It repeats the
        run's expect and purpose unchanged, so a re-grant carries them too."""
        try:
            reply = self._board.lease(self._name, self._ttl, expect=self._expect,
                                      purpose=self._purpose)
        except _Transient:
            return min(TRANSIENT_RETRY, self._interval)
        except _Refused as exc:
            self._lost(str(exc))
            self._stop.set()  # asking again with the same identity will not help
            return self._interval
        except Exception as exc:  # noqa: BLE001 — never die with a traceback here
            self._lost(f"renewal failed: {type(exc).__name__}")
            self._stop.set()
            return self._interval
        if reply["state"] != "held":
            self._lost("it has this run queued behind another holder")
        return self._interval

    def _lost(self, why: str) -> None:
        if not self._warned:
            self._warned = True
            _say(f"lease: warning: the board no longer shows this run holding "
                 f"{self._name!r} ({why}); the command keeps running under the local "
                 f"lock, which is what excludes other runs")


# --- lease run ---------------------------------------------------------------------

class _TimedOut(Exception):
    """``--timeout`` expired before the lease was held."""


class _CannotRun(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class _LockError(Exception):
    """The lock file cannot be created or locked at all."""


class _Signalled(BaseException):
    """SIGTERM or SIGHUP arrived. A BaseException, like KeyboardInterrupt,
    so no ``except Exception`` on the way swallows it."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# Signals that stop a run the way Ctrl-C does: `kill`, `timeout`, a service or
# CI stop, a closed terminal. SIGTERM is handled on Windows too, where only
# this process can raise it.
_STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP")
                      if hasattr(signal, name))


def _install_stop_handlers(state: dict) -> dict:
    """Raise _Signalled on a stop signal, unless ``state["cleaning"]``: the
    bounded cleanup is not cut short by a second one. Returns the previous
    handlers; empty off the main thread, where Python cannot set handlers."""
    if threading.current_thread() is not threading.main_thread():
        return {}

    def stop(signum, frame):
        if not state["cleaning"]:
            raise _Signalled(signum)

    previous = {}
    for signum in _STOP_SIGNALS:
        try:
            previous[signum] = signal.signal(signum, stop)
        except (OSError, ValueError):
            pass
    return previous


def _restore_handlers(previous: dict) -> None:
    for signum, handler in previous.items():
        # None: a handler not set from Python, which cannot be put back.
        signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def _pause(poll: float, deadline: float | None) -> None:
    if deadline is None:
        time.sleep(poll)
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _TimedOut
    time.sleep(min(poll, remaining))


def _task(name: str, command: list[str]) -> str:
    program = _printable(os.path.basename(command[0]) or command[0])
    return f"{name}: {program}"[:MAX_SCOPE]


def _project(environ=None) -> str:
    environ = os.environ if environ is None else environ
    return _printable(environ.get("PSEUDOLIFE_AGENT_PROJECT", ""))[:MAX_SCOPE]


def _wait_for_board(board: _Board, args, command: list[str],
                    deadline: float | None) -> dict:
    """Poll ``lease`` until the board answers ``held``. Raises _Refused when
    the board cannot be used, or has failed for BOARD_GIVE_UP seconds."""
    failing_since = None
    next_notice = time.monotonic()
    while True:
        try:
            if not board.registered:
                board.register(task=_task(args.name, command), project=_project())
            reply = board.lease(args.name, args.ttl, expect=args.expect,
                                purpose=args.purpose)
        except _Transient as exc:
            now = time.monotonic()
            if failing_since is None:
                failing_since = now
            elif now - failing_since >= BOARD_GIVE_UP:
                raise _Refused(f"the board kept failing for {_span(now - failing_since)} "
                               f"({exc})") from None
        else:
            failing_since = None
            if reply["state"] == "held":
                return reply
            now = time.monotonic()
            if now >= next_notice:
                _say(_queued_notice(args.name, reply))
                next_notice = now + NOTICE_EVERY
        _pause(BOARD_POLL, deadline)


def _try_lock(lock: os_lock.OsLock) -> bool:
    try:
        return lock.acquire()
    except OSError as exc:
        raise _LockError(f"lease: cannot use the lock file {lock.path}: {exc}") from None


def _wait_for_lock(lock: os_lock.OsLock, name: str, deadline: float | None, *,
                   board_holds: bool) -> None:
    """Take the OS lock, polling while another process holds it."""
    if _try_lock(lock):
        return
    next_notice = time.monotonic()
    while True:
        now = time.monotonic()
        if now >= next_notice:
            if board_holds:
                _say(f"lease: the board granted {name!r}, but its local lock ({lock.path}) is "
                     f"held by a process the board does not show (a --no-board run, or a "
                     f"holder whose board lease lapsed); waiting for it")
            else:
                _say(f"lease: waiting for the local lock on {name!r} ({lock.path}), "
                     f"which another process holds")
            next_notice = now + NOTICE_EVERY
        _pause(LOCK_POLL, deadline)
        if _try_lock(lock):
            return


def _spawn(command: list[str], env: dict[str, str]) -> subprocess.Popen:
    if os.name == "nt":
        # CreateProcess looks only for .exe on PATH; resolve the way a shell
        # does (PATHEXT: .cmd, .bat) so `-- npm test` works as typed.
        found = shutil.which(command[0])
        if found:
            command = [found, *command[1:]]
    return subprocess.Popen(command, env=env)


def _start(command: list[str], name: str) -> subprocess.Popen:
    env = dict(os.environ)
    held = env.get(HELD_ENV)
    env[HELD_ENV] = f"{held},{name}" if held else name
    try:
        return _spawn(command, env)
    except FileNotFoundError:
        raise _CannotRun(EXIT_NOT_FOUND, f"lease: command not found: {command[0]}") from None
    except OSError as exc:
        raise _CannotRun(EXIT_CANNOT_RUN,
                         f"lease: cannot run {command[0]}: {exc.strerror or exc}") from None


def _wait_up_to(child: subprocess.Popen, seconds: float | None) -> int:
    end = None if seconds is None else time.monotonic() + seconds
    while True:
        step = CHILD_POLL if end is None else max(0.0, min(CHILD_POLL, end - time.monotonic()))
        try:
            return child.wait(timeout=step)
        except subprocess.TimeoutExpired:
            if end is not None and time.monotonic() >= end:
                raise


def _stop_child(child: subprocess.Popen, signum: int) -> None:
    """Stop the command after this process got ``signum``.

    A console Ctrl-C reached the command as well (one console on Windows, one
    foreground process group on POSIX), so it first gets KILL_GRACE to finish
    the cleanup that Ctrl-C started. Only then is SIGINT sent (POSIX), for an
    interrupt aimed at this process alone (``kill -INT``); sent at once, it
    would be a second interrupt to a command already cleaning up, which is
    what cuts a Python program's teardown short. SIGTERM or SIGHUP, aimed at
    this process, are forwarded at once. Then terminate, then kill, each after
    another KILL_GRACE. Asked again while waiting, it kills at once."""
    if signum == signal.SIGINT:
        steps = [None] + ([signal.SIGINT] if os.name != "nt" else [])
    else:
        steps = [signum]
    if signal.SIGTERM not in steps:
        steps.append(signal.SIGTERM)  # Popen.terminate(): TerminateProcess on Windows
    try:
        for step in steps:
            if child.poll() is not None:
                return
            if step is not None:
                child.send_signal(step)
            try:
                _wait_up_to(child, KILL_GRACE)
                return
            except subprocess.TimeoutExpired:
                pass
    except (KeyboardInterrupt, _Signalled):
        pass  # asked again: no more waiting
    if child.poll() is None:
        child.kill()
    child.wait()


def _nested(name: str, lock: os_lock.OsLock) -> bool:
    """Whether this run sits inside a run of the same lease, which it would
    wait for forever: its name is in PSEUDOLIFE_LEASES_HELD (whole names
    only) and the lock is held right now. A process an earlier run started
    keeps that variable after the run ends; the lock tells the two apart."""
    held = os.environ.get(HELD_ENV, "")
    if f",{name}," not in f",{held},":
        return False
    try:
        return os_lock.probe(lock.path) is True
    except OSError:
        return False


def _run(args, command: list[str], transport) -> int:
    name = args.name
    lock = os_lock.OsLock(os_lock.lock_dir() / os_lock.lock_file_name(name))
    if _nested(name, lock):
        _say(f"lease: {name!r} is held by an enclosing lease run "
             f"({HELD_ENV}={_clean(os.environ.get(HELD_ENV, ''), MAX_PURPOSE)}); a nested "
             f"run of the same lease would wait for itself forever. Run the command "
             f"directly, or use another name. If this process was not started by "
             f"that run, unset {HELD_ENV}.")
        return EXIT_NESTED
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    board = renewer = child = None
    state = {"cleaning": False}
    previous = _install_stop_handlers(state)
    try:
        skipped = None
        try:
            board, skipped = _connect(args.no_board, transport)
            if board is not None:
                _wait_for_board(board, args, command, deadline)
                started = _Renewer(board, name, args.ttl, args.expect, args.purpose)
                started.start()
                renewer = started
        except _TimedOut:
            raise
        except _Refused as exc:
            skipped = str(exc)
        except Exception as exc:  # noqa: BLE001 — a board failure never stops the command
            skipped = f"the board failed unexpectedly ({type(exc).__name__})"
        if renewer is None:
            if board is not None:
                board.release_quietly(name)  # give up any place in the queue now
            _say(f"lease: board skipped: {skipped}; waiting on the local lock for "
                 f"{name!r} alone (not FIFO)")
        _wait_for_lock(lock, name, deadline, board_holds=renewer is not None)
        child = _start(command, name)
        return _exit_status(_wait_up_to(child, None))
    except _TimedOut:
        _say(f"lease: gave up waiting for {name!r} after {_span(args.timeout)} "
             f"(--timeout); the command did not run")
        return EXIT_TEMPFAIL
    except _CannotRun as exc:
        _say(str(exc))
        return exc.code
    except _LockError as exc:
        _say(str(exc))
        return EXIT_LOCK_ERROR
    except (KeyboardInterrupt, _Signalled) as stop:
        signum = stop.signum if isinstance(stop, _Signalled) else signal.SIGINT
        if child is not None:
            _stop_child(child, signum)
        what = ("interrupted" if signum == signal.SIGINT
                else f"stopped by {signal.Signals(signum).name}")
        _say(f"lease: {what}; releasing {name!r}")
        return 128 + signum
    finally:
        state["cleaning"] = True
        # The OS lock first: the work is over, and a renewal stuck on a slow
        # daemon (joined for up to REQUEST_TIMEOUT) must not hold it longer.
        lock.release()
        if renewer is not None:
            renewer.stop()
        if board is not None:
            board.release_quietly(name)
            board.close()
        _restore_handlers(previous)


# --- the mirror of a lock this process holds --------------------------------------

def _clear_by(agent: dict):
    """A peer's ``park_clear_by`` (a sibling change's field), flat or under
    ``park``, as a list of names; empty when it has none."""
    clear_by = agent.get("park_clear_by")
    if clear_by is None and isinstance(agent.get("park"), dict):
        clear_by = agent["park"].get("clear_by")
    if isinstance(clear_by, str):
        return [clear_by]
    if isinstance(clear_by, (list, tuple)):
        return [item for item in clear_by if isinstance(item, str)]
    return []


def _concerned(agent: dict, name: str, project: str) -> bool:
    """Whether a listed peer is told about ``name``. In ``project`` when one
    is known, compared without case (the board holds this repo as
    Pseudolife-MCP, PseudoLife-MCP and pseudolife-mcp). Then either parked
    until this lease clears (``park_clear_by``), attached or detached, since
    mail waits for a parked session; or live (attached, or registered
    without an adapter) and working around the suite or the GPU by its
    status (``suite=running``, ``suite=queued``, ``gpu=``). Both leases go
    to both groups on purpose: a GPU server beside a full suite is the
    contention (2026-09-23: 143 CUDA OOMs). Revoked addresses never."""
    lifecycle = agent.get("lifecycle")
    if lifecycle == "revoked":
        return False
    if project and str(agent.get("project") or "").lower() != project.lower():
        return False
    if name in _clear_by(agent):
        return True
    if lifecycle not in (None, "attached", "registered"):
        return False
    return bool(_CONCERNED_STATUS.search(str(agent.get("status") or "")))


class _Environment(dict):
    """The board's environment (bearer, daemon URL, project), whose repr
    names its keys only: a traceback shown with locals must not print the
    bearer."""

    def __repr__(self) -> str:
        return f"<board environment: {', '.join(sorted(self))}>"

    __str__ = __repr__


# How long the release side may take (the notice to peers, then freeing the
# board lease), and the acquire side's notice. Both run on the mirror's own
# thread after the OS lock has moved, so they bound only how long a board
# record can trail the lock, never the work: a mirror that runs out of time
# leaves its board lease to lapse at its ttl.
RELEASE_BUDGET = 20.0
ABORT_BUDGET = 5.0
ANNOUNCE_BUDGET = 30.0


class BoardMirror:
    """The board's view of an OS lock this process holds or waits for.

    Used by ``lease hold`` and by the test suite's lock (tests/suite_lock.py):
    :meth:`waiting` while the OS lock is held elsewhere, so the holder sees a
    waiter; :meth:`hold` once the OS lock is taken, which takes or keeps the
    board lease, renews it every ttl/3 and tells the peers; :meth:`release`
    after the OS lock is freed, which tells them again and frees the board
    lease. All board traffic runs on one background thread: :meth:`waiting`
    and :meth:`hold` return at once, and :meth:`release` waits at most its
    budget. The OS lock is the truth throughout; every board failure ends in
    one line on stderr and the work going on. A board that grants the lease
    before the OS lock does is given it back (the board mirrors the lock; it
    must not name a queued run as the holder); one that shows another holder
    while this process has the OS lock is reported once and left to catch up.
    """

    def __init__(self, name: str, *, purpose: str = "", expect: int | None = None,
                 ttl: int = DEFAULT_TTL, worktree: str = "", pid: int | None = None,
                 environ=None, transport=None, no_board: bool = False):
        self.name = name
        self.purpose = purpose[:MAX_PURPOSE]
        self.expect = expect
        self.ttl = ttl
        # Its name only, never its path: a path names the OS user, and board
        # rows reach audit exports (orchestrator review, 2026-09-28).
        self.worktree = Path(worktree or os.getcwd()).name or str(worktree)
        self.pid = os.getpid() if pid is None else pid
        self._environ = _Environment(os.environ if environ is None else environ)
        self._transport = transport
        self._no_board = no_board
        self._board: _Board | None = None
        self._state_lock = threading.Lock()
        self._wake = threading.Event()
        self._state: str | None = None  # waiting, holding, released
        self._thread: threading.Thread | None = None
        self._deadline = 0.0  # the release side's, set by release()
        self._failing_since: float | None = None
        self._granted_early = False  # handed back; no more asking until held
        self._warned_lost = False
        self._held_since: float | None = None

    def waiting(self) -> None:
        """While this process waits for the OS lock: queue on the board (the
        worker asks every BOARD_POLL seconds)."""
        self._set("waiting")

    def hold(self) -> None:
        """Once this process holds the OS lock: take or keep the board lease,
        renew it, and tell the peers, all on the worker thread."""
        self._held_since = time.time()
        self._set("holding")

    def release(self, budget: float = RELEASE_BUDGET) -> None:
        """Tell the peers (if they were told of the hold), free the board
        lease, close; waits at most ``budget`` seconds for the worker."""
        with self._state_lock:
            thread = self._thread
            self._state = "released"
            self._deadline = time.monotonic() + budget
        self._wake.set()
        if thread is not None:
            thread.join(budget + 1)

    def _set(self, state: str) -> None:
        with self._state_lock:
            if self._state == "released":
                return
            self._state = state
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._work, name=f"lease-mirror-{self.name}", daemon=True)
                self._thread.start()
        self._wake.set()

    def _current(self) -> str | None:
        with self._state_lock:
            return self._state

    # -- the worker --

    def _work(self) -> None:
        announced = False
        try:
            self._connect()
            next_call = 0.0
            holding = False
            while self._board is not None:
                state = self._current()
                if state == "released":
                    break
                now = time.monotonic()
                if state == "holding" and not holding:
                    holding = True
                    reply = self._lease()
                    if reply is not None and reply["state"] != "held":
                        self._not_held(reply)
                    if self._board is not None and self._current() == "holding":
                        self._announce("acquired", time.monotonic() + ANNOUNCE_BUDGET)
                        announced = True
                    next_call = time.monotonic() + _renew_interval(self.ttl)
                elif state == "waiting" and self._granted_early:
                    next_call = float("inf")  # handed back: wait for hold()
                elif now >= next_call:
                    reply = self._lease()
                    interval = BOARD_POLL if state == "waiting" else _renew_interval(self.ttl)
                    if reply is None:
                        interval = min(TRANSIENT_RETRY, interval)
                    elif state == "waiting" and reply["state"] == "held":
                        self._early_grant()
                        continue
                    elif state == "holding" and reply["state"] != "held":
                        self._lost()
                    next_call = time.monotonic() + interval
                pause = next_call - time.monotonic()
                self._wake.wait(None if pause == float("inf") else max(0.0, pause))
                self._wake.clear()
            board = self._board
            if board is not None:
                if announced:
                    self._announce("released", self._deadline)
                if time.monotonic() < self._deadline:
                    board.release_quietly(self.name)
        except Exception as exc:  # noqa: BLE001 — the board never stops the work
            _say(f"lease: the board mirror of {self.name!r} stopped "
                 f"({type(exc).__name__}); the local lock is unaffected")
        finally:
            if self._board is not None:
                self._board.close()
                self._board = None

    def _connect(self) -> None:
        try:
            self._board, skipped = _connect(self._no_board, self._transport, self._environ)
        except Exception as exc:  # noqa: BLE001
            self._board, skipped = None, f"the board failed unexpectedly ({type(exc).__name__})"
        if self._board is None:
            self._skip(skipped or "no board")

    def _skip(self, why: str) -> None:
        if self._board is not None:
            self._board.close()
            self._board = None
        _say(f"lease: board skipped: {why}; {self.name!r} is held by the local lock alone")

    # The daemon forgot this run's address: a run queued behind holders the
    # board does not show makes no board calls after handing back an early
    # grant, and the daemon retires an ephemeral address an hour after its
    # last activity. Seen live 2026-09-28 06:17, after a 135-minute wait.
    _FORGOTTEN = frozenset({"instance_not_found", "invalid_credential"})

    def _lease(self, *, retry: bool = True) -> dict | None:
        """One ``lease`` call, registering first; None when the board is
        unusable (dropped, with one line) or failing for the moment. An
        address the daemon no longer knows is registered again, once."""
        board = self._board
        if board is None:
            return None
        try:
            if not board.registered:
                board.register(
                    task=f"{self.name}: pid {self.pid}"[:MAX_SCOPE],
                    project=_project(self._environ), label=HOLD_LABEL,
                    status=_printable(f"lease:{self.name} pid {self.pid} {self.worktree}")[:240])
            reply = board.lease(self.name, self.ttl, expect=self.expect, purpose=self.purpose)
        except _Transient as exc:
            now = time.monotonic()
            if self._failing_since is None:
                self._failing_since = now
            elif now - self._failing_since >= BOARD_GIVE_UP:
                self._skip(f"the board kept failing for {_span(now - self._failing_since)} ({exc})")
            return None
        except _Refused as exc:
            if retry and exc.code in self._FORGOTTEN and board.registered:
                board.forget()
                return self._lease(retry=False)
            self._skip(str(exc))
            return None
        self._failing_since = None
        return reply

    def _early_grant(self) -> None:
        """The board granted the lease while the OS lock is still held by a
        run the board does not show (older code, no bearer, lock off). Kept,
        the board would name a queued run as the holder: give it back, and
        ask no more until this process holds the lock (asking again would
        only be granted again, churning the audit log every poll)."""
        self._granted_early = True
        if self._board is not None:
            self._board.release_quietly(self.name)
        _say(f"lease: the board had no holder for {self.name!r} while its local lock is "
             f"held by a run the board does not show; not claiming it until this process "
             f"holds the lock")

    def _not_held(self, reply: dict) -> None:
        holder = reply.get("holder")
        now = time.time()
        who = (_describe_holder(holder, now) + _expected_text(holder.get("expected_end"), now)
               if isinstance(holder, dict) else "nobody the board names")
        _say(f"lease: the board shows {self.name!r} held by {who}; this process holds the "
             f"OS lock, which is the truth, and carries on; the board lease follows when "
             f"that hold frees or expires")
        self._warned_lost = True

    def _lost(self) -> None:
        if not self._warned_lost:
            self._warned_lost = True
            _say(f"lease: warning: the board no longer shows this process holding "
                 f"{self.name!r}; the local lock, which is what excludes, is still held")

    def _text(self, event: str) -> str:
        head = f"LEASE {self.name} {event}: pid {self.pid}, worktree {self.worktree}"
        if event == "acquired":
            if self.expect is not None and self._held_since is not None:
                tail = (f", expected end {_clock(self._held_since + self.expect)} "
                        f"(in {_span(self.expect)})")
            else:
                tail = ", no expected end given"
        else:
            held = 0.0 if self._held_since is None else time.time() - self._held_since
            tail = f", held {_span(held)}"
        return (f"{head}{tail}. The OS lock is the truth; `pseudolife-mcp lease check "
                f"{self.name}` shows it. Automatic notice, no reply needed.")

    def _announce(self, event: str, deadline: float) -> None:
        """Mail the peers the lease concerns, until ``deadline``; best effort.
        An acquire notice stops early if the hold ends meanwhile."""
        board = self._board
        if board is None or not board.registered:
            return

        def left() -> float:
            return deadline - time.monotonic()

        if left() <= 0:
            return
        try:
            peers = board.agents(timeout=min(ANNOUNCE_TIMEOUT, left()))
        except Exception as exc:  # noqa: BLE001 — a courtesy, never a failure
            _say(f"lease: could not list the peers to tell about {self.name!r} "
                 f"({_clean(str(exc), MAX_PURPOSE)})")
            return
        project = _project(self._environ)
        recipients = [agent["agent_id"] for agent in peers
                      if agent["agent_id"] != board.agent_id
                      and _concerned(agent, self.name, project)]
        text = self._text(event)
        stamp = int(self._held_since or time.time())
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.name)[:40]
        failed = []
        for to in recipients[:ANNOUNCE_MAX]:
            if left() <= 0 or (event == "acquired" and self._current() == "released"):
                break
            request_id = f"lease-{event}-{safe}-{self.pid}-{stamp}-{to[:8]}"[:120]
            try:
                board.send(to, text, request_id, timeout=min(ANNOUNCE_TIMEOUT, left()))
            except Exception as exc:  # noqa: BLE001 — the rest may still get through
                failed.append(f"{_clean(to[:8])} ({_clean(str(exc), 80)})")
        if failed:
            _say(f"lease: could not tell {len(failed)} peer(s) about {self.name!r}: "
                 + "; ".join(failed))


# --- lease hold ----------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` still runs. Windows opens the process and asks whether
    it has signalled (``os.kill(pid, 0)`` there sends CTRL_C_EVENT); a
    process this user cannot open is taken as alive. POSIX probes with signal
    0, and reads a zombie's state on Linux, since a parent that has not
    reaped its child leaves the pid answering."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        synchronize, query_limited = 0x00100000, 0x00001000
        handle = kernel32.OpenProcess(synchronize | query_limited, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as stat:
            fields = stat.read().rsplit(")", 1)
        return not (len(fields) == 2 and fields[1].split()[:1] == ["Z"])
    except OSError:
        return True  # no procfs: a zombie counts as alive


def _wait_for_pid(pid: int) -> None:
    while _pid_alive(pid):
        time.sleep(PID_POLL)


def _hold(args, transport) -> int:
    name = args.name
    lock = os_lock.OsLock(os_lock.lock_dir() / os_lock.lock_file_name(name))
    if not _pid_alive(args.while_pid):
        _say(f"lease: pid {args.while_pid} is already gone; nothing to hold {name!r} for")
        return 0
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    mirror = None
    state = {"cleaning": False}
    previous = _install_stop_handlers(state)
    try:
        _wait_for_lock(lock, name, deadline, board_holds=False)
        mirror = BoardMirror(name, purpose=args.purpose or "", expect=args.expect,
                             ttl=args.ttl, worktree=args.worktree, pid=args.while_pid,
                             transport=transport, no_board=args.no_board)
        mirror.hold()
        _wait_for_pid(args.while_pid)
        return 0
    except _TimedOut:
        _say(f"lease: gave up waiting for {name!r} after {_span(args.timeout)} "
             f"(--timeout); nothing is held")
        return EXIT_TEMPFAIL
    except _LockError as exc:
        _say(str(exc))
        return EXIT_LOCK_ERROR
    except (KeyboardInterrupt, _Signalled) as stop:
        signum = stop.signum if isinstance(stop, _Signalled) else signal.SIGINT
        what = ("interrupted" if signum == signal.SIGINT
                else f"stopped by {signal.Signals(signum).name}")
        _say(f"lease: {what}; releasing {name!r} (pid {args.while_pid} is left running)")
        return 128 + signum
    finally:
        state["cleaning"] = True
        # The OS lock first, as in `run`: the board only hears after, on the
        # mirror's thread, within its budget.
        lock.release()
        if mirror is not None:
            try:
                mirror.release()
            except Exception:  # noqa: BLE001 — the lock is already released
                pass
        _restore_handlers(previous)


# --- lease check ---------------------------------------------------------------------

def _suite_lock_dir() -> Path:
    override = os.environ.get(SUITE_LOCK_DIR_ENV)
    return Path(override) if override else os_lock.lock_dir()


def _local_state(name: str) -> dict:
    """The OS lock behind ``name`` right now: ``state`` held, free, or None
    when there is no lock file; for the test suite's lock, the holder record
    (pid, worktree, started) of the held slot."""
    if name != SUITE_LEASE:
        path = os_lock.lock_dir() / os_lock.lock_file_name(name)
        try:
            held = os_lock.probe(path)
        except OSError:
            held = None
        return {"file": path.name, "state": None if held is None else ("held" if held else "free")}
    directory = _suite_lock_dir()
    seen = None
    for slot in range(SUITE_MAX_SLOTS):
        stem, _, rest = SUITE_LOCK_FILE.partition(".")
        lock_name = SUITE_LOCK_FILE if slot == 0 else f"{stem}.{slot}.{rest}"
        try:
            held = os_lock.probe(directory / lock_name)
        except OSError:
            held = None
        if held is None:
            if slot == 0:
                seen = {"file": lock_name, "state": None}
            continue
        if not held:
            seen = seen or {"file": lock_name, "state": "free"}
            continue
        record: dict = {"file": lock_name, "state": "held"}
        stem, _, rest = SUITE_HOLDER_FILE.partition(".")
        holder_name = SUITE_HOLDER_FILE if slot == 0 else f"{stem}.{slot}.{rest}"
        try:
            holder = json.loads((directory / holder_name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            holder = None
        if isinstance(holder, dict):
            for key in ("pid", "worktree", "started"):
                if key in holder:
                    record[key] = holder[key]
        return record
    return seen or {"file": SUITE_LOCK_FILE, "state": None}


def _local_text(local: dict) -> str:
    state = local["state"]
    if state is None:
        return "absent"
    if state != "held":
        return state
    if "pid" not in local and "worktree" not in local:
        return "held"
    since = ""
    started = local.get("started")
    if isinstance(started, str):
        try:
            since = ", since " + datetime.fromisoformat(started).strftime("%H:%M")
        except ValueError:
            pass
    return (f"held (pid {_clean(local.get('pid', '?'))}, worktree "
            f"{_clean(local.get('worktree', '?'), MAX_PURPOSE)}{since})")


def _check(args, transport) -> int:
    """Exit 1 when held, 0 when free, EXIT_SOFTWARE when the check itself
    failed: a gate that reads 1 as held must never refuse on a crash."""
    try:
        return _check_lease(args, transport)
    except Exception as exc:  # noqa: BLE001
        _say(f"lease: check of {args.name!r} failed ({type(exc).__name__}: "
             f"{_clean(str(exc), MAX_PURPOSE)}); neither held nor free is known")
        return EXIT_SOFTWARE


def _check_lease(args, transport) -> int:
    local = _local_state(args.name)
    board: dict = {"available": False, "reason": None, "holder": None, "expected_end": None,
                   "stale": False, "queued": 0, "queue": []}
    try:
        client, reason = _connect(False, transport)
    except Exception as exc:  # noqa: BLE001
        client, reason = None, f"the board failed unexpectedly ({type(exc).__name__})"
    if client is not None:
        try:
            leases, _truncated = client.leases(args.name)
        except (_Refused, _Transient) as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001
            reason = f"the board failed unexpectedly ({type(exc).__name__})"
        else:
            board["available"] = True
            for lease in leases:
                if lease["name"] == args.name and isinstance(lease.get("holder"), dict):
                    board.update(holder=lease["holder"], expected_end=lease.get("expected_end"),
                                 stale=lease.get("stale") is True,
                                 queued=lease.get("queued") or 0,
                                 queue=lease.get("queue") or [])
        finally:
            client.close()
    board["reason"] = reason
    # The OS lock is the truth wherever it exists: a board holder beside a
    # free local lock is a record that outlived its process (a hold killed
    # outright) and lapses at its ttl. Only a lease with no lock file here,
    # a session-held claim or a name never taken on this machine, is the
    # board's to decide.
    if local["state"] is None:
        held = board["holder"] is not None
    else:
        held = local["state"] == "held"
    board_stale = board["holder"] is not None and local["state"] == "free"
    if args.json:
        print(json.dumps({"name": args.name, "held": held, "local": local, "board": board},
                         indent=2))
        return 1 if held else 0
    now = time.time()
    lines = [f"lease {_clean(args.name)}: {'held' if held else 'free'}",
             f"  local lock ({local['file']}): {_local_text(local)}"]
    if board["available"]:
        if board["holder"] is not None:
            lines.append("  board: held by " + _describe_holder(board["holder"], now)
                         + _expected_text(board["expected_end"], now, board["stale"])
                         + ("; stale: the local lock is free, so this record outlived its "
                            "holder and lapses at its ttl" if board_stale else ""))
            if board["queued"]:
                lines.append(f"  board queue ({board['queued']}): " + "; ".join(
                    _describe_waiter(entry) for entry in board["queue"]
                    if isinstance(entry, dict)))
        else:
            lines.append("  board: free")
    else:
        lines.append(f"  board unavailable: {reason}")
    print("\n".join(lines))
    return 1 if held else 0


# --- lease list --------------------------------------------------------------------

def _local_locks(directory: Path, name: str | None) -> dict[str, str]:
    """``lease-*.lock`` files in ``directory`` (or only ``name``'s), each
    ``held`` or ``free`` right now."""
    try:
        files = sorted(os.listdir(directory))
    except OSError:
        return {}
    wanted = os_lock.lock_file_name(name) if name else None
    states = {}
    for file in files:
        if not (file.startswith(os_lock.LOCK_PREFIX) and file.endswith(os_lock.LOCK_SUFFIX)):
            continue
        if wanted is not None and file != wanted:
            continue
        try:
            held = os_lock.probe(directory / file)
        except OSError:
            continue
        if held is not None:
            states[file] = "held" if held else "free"
    return states


def _lease_lines(lease: dict, now: float) -> list[str]:
    name = _clean(lease["name"])
    holder = lease.get("holder")
    if isinstance(holder, dict):
        head = (f"lease {name}: held by {_describe_holder(holder, now)}"
                + _expected_text(lease.get("expected_end"), now, lease.get("stale") is True))
    else:
        head = f"lease {name}: free"
    lines = [head]
    queue = lease.get("queue")
    queue = [entry for entry in queue if isinstance(entry, dict)] if isinstance(queue, list) else []
    queued = lease.get("queued") if isinstance(lease.get("queued"), int) else len(queue)
    if queued:
        waiting = "; ".join(_describe_waiter(entry) for entry in queue)
        lines.append(f"  queue ({queued}): {waiting}" if waiting else f"  queue ({queued})")
    lines.append(f"  local lock: {lease['local_lock'] or 'absent'}")
    return lines


def _list(args, transport) -> int:
    directory = os_lock.lock_dir()
    local = _local_locks(directory, args.name)
    suite = None
    if not args.name:
        try:
            state = os_lock.probe(directory / SUITE_LOCK_FILE)
        except OSError:
            state = None
        suite = None if state is None else ("held" if state else "free")
    try:
        board, reason = _connect(False, transport)
    except Exception as exc:  # noqa: BLE001 — the local state is still worth showing
        board, reason = None, f"the board failed unexpectedly ({type(exc).__name__})"
    url = board.url if board is not None else None
    leases, truncated = [], False
    if board is not None:
        try:
            leases, truncated = board.leases(args.name)
        except (_Refused, _Transient) as exc:
            reason = str(exc)
        except Exception as exc:  # noqa: BLE001
            reason = f"the board failed unexpectedly ({type(exc).__name__})"
        finally:
            board.close()
    available = reason is None
    for lease in leases:
        lease["local_lock"] = local.get(os_lock.lock_file_name(lease["name"]))
    if args.json:
        print(json.dumps({
            "board": {"url": url, "available": available, "reason": reason,
                      "truncated": truncated, "leases": leases},
            "lock_dir": str(directory),
            "local": [{"file": file, "state": state} for file, state in local.items()],
            "test_suite_lock": suite,
        }, indent=2))
        return 0
    now = time.time()
    lines = []
    if available:
        lines.append(f"board: {url}")
        if not leases:
            lines.append("  no leases on the board")
        for lease in leases:
            lines.extend(_lease_lines(lease, now))
        if truncated:
            lines.append(f"  (the board listed only the first {len(leases)})")
    else:
        lines.append(f"board unavailable: {reason}; showing local locks only")
    on_board = {os_lock.lock_file_name(lease["name"]) for lease in leases}
    for file, state in local.items():
        if file not in on_board:
            lines.append(f"local lock {file}: {state}"
                         + (" (not on the board)" if available else ""))
    if not local:
        lines.append(f"no local lease locks in {directory}")
    if suite is not None:
        lines.append(f"test-suite lock: {suite}")
    print("\n".join(lines))
    return 0


# --- operator break ----------------------------------------------------------------

def _break(args) -> int:
    """Free a held lease through the bank itself (operator only). Prints the
    store's answer as JSON; 1 when no bank is found or the break failed."""
    from pseudolife_memory.backup_cli import _default_data_dir
    from pseudolife_memory.transfer_cli import _resolve_dsn
    dsn, own_instance = _resolve_dsn(_default_data_dir(os.environ))
    try:
        if not dsn:
            _say("no bank found: set PSEUDOLIFE_MCP_DATABASE_URL to the bank's database URL, "
                 "or run where the lite tier's data dir holds one")
            return 1
        from pseudolife_memory.storage.coordination import (
            CoordinationConnection, CoordinationError, CoordinationStore)
        try:
            storage = CoordinationConnection(dsn)
        except Exception as exc:  # noqa: BLE001 — a DSN in a driver message stays unprinted
            _say(f"cannot open the bank ({type(exc).__name__})")
            return 1
        try:
            result = CoordinationStore(storage).break_lease(args.name)
        except CoordinationError as exc:
            _say(f"break refused: {exc.code}")
            return 1
        except Exception as exc:  # noqa: BLE001
            _say(f"break failed ({type(exc).__name__}); nothing was changed")
            return 1
        finally:
            storage.close()
        print(json.dumps(result))
        return 0
    finally:
        if own_instance is not None:
            own_instance.stop()


# --- arguments ---------------------------------------------------------------------

def _seconds(low: int, high: int | None):
    def duration(text: str) -> int:
        try:
            value = parse_duration(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
        if value < low or (high is not None and value > high):
            bounds = (f"from {low} to {high} seconds" if high is not None
                      else f"at least {low} seconds")
            raise argparse.ArgumentTypeError(f"{text!r} is out of range: {bounds}")
        return value
    return duration


def _lease_name(text: str) -> str:
    if not text.strip() or len(text) > MAX_NAME or _has_control(text):
        raise argparse.ArgumentTypeError(
            f"a lease name is 1 to {MAX_NAME} characters, not blank, without control "
            f"characters")
    return text


def _purpose(text: str) -> str:
    if len(text) > MAX_PURPOSE or _has_control(text):
        raise argparse.ArgumentTypeError(
            f"a purpose is at most {MAX_PURPOSE} characters, without control characters")
    return text


def _pid(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a process id: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("a process id is a positive whole number")
    return value


_HOLD_EPILOG = """\
The hold takes the OS lock first (the process already owns the resource), then
mirrors it on the board, tells the peers the lease concerns (status suite=running,
suite=queued or gpu=, or parked until this lease clears), and releases both when
PID exits. It never stops PID. A lock another process holds is waited for, polled
every 2 s; --timeout 0 gives up at once.

exit codes: 0 PID ended (or was already gone); 2 usage error; 71 lock file
unusable; 75 --timeout expired before the lock was taken; 128+N stopped by
signal N (130 Ctrl-C), releasing the lease and leaving PID running.
"""


_RUN_EPILOG = """\
DURATION is whole seconds or minutes or hours: 90, 90s, 20m, 2h.

The lease is an OS file lock in ~/.pseudolife-mcp/locks (PSEUDOLIFE_LEASE_LOCK_DIR
overrides it), released by the OS when this process exits or dies. With a
bearer token (PSEUDOLIFE_MCP_TOKEN or PSEUDOLIFE_MCP_TOKEN_FILE) and a daemon
whose board is on, the board mirrors it: FIFO queue, holder, expected end.
Otherwise, or with --no-board, the local lock alone decides (not FIFO). The
command sees PSEUDOLIFE_LEASES_HELD=<names>.

A stop (Ctrl-C, SIGTERM, SIGHUP) stops the command before releasing: it gets
10 s to clean up on its own, then is interrupted, terminated and killed.

exit codes: the command's own; 2 usage error; 64 nested inside a run of the
same lease; 71 lock file unusable; 75 --timeout expired (the command did not
run); 126 cannot start the command; 127 command not found; 128+N stopped by
signal N (130 Ctrl-C).
"""


def _parsers():
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp lease",
        description="Hold a named lease on a shared resource (a test suite, the GPU, a "
                    "maintenance window) around a command. The lease is an OS file lock; "
                    "the agent board, where the daemon has one, mirrors it so other "
                    "agents see the holder, queue in order and see the expected end.")
    actions = parser.add_subparsers(dest="action", metavar="{run,hold,check,list,break}")
    run = actions.add_parser(
        "run", help="run a command while holding a lease",
        usage="pseudolife-mcp lease run NAME [--expect DURATION] [--ttl SECONDS] "
              "[--purpose TEXT] [--no-board] [--timeout DURATION] -- COMMAND [ARGS...]",
        description="Wait for the lease NAME, run COMMAND while holding it, release it.",
        epilog=_RUN_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    run.add_argument("name", type=_lease_name, metavar="NAME",
                     help=f"the lease name, 1 to {MAX_NAME} characters")
    run.add_argument("--expect", type=_seconds(MIN_EXPECT, MAX_EXPECT), metavar="DURATION",
                     help="how long the command should take; the board shows the "
                          "expected end, and 'stale' once it has passed")
    run.add_argument("--ttl", type=_seconds(MIN_TTL, MAX_TTL), default=DEFAULT_TTL,
                     metavar="SECONDS",
                     help=f"how long the board keeps the lease without a renewal, "
                          f"{MIN_TTL} to {MAX_TTL} (default {DEFAULT_TTL}); renewed "
                          f"every ttl/3")
    run.add_argument("--purpose", type=_purpose, metavar="TEXT",
                     help=f"what it is for, shown on the board (at most {MAX_PURPOSE} "
                          f"characters)")
    run.add_argument("--no-board", action="store_true",
                     help="skip the agent board and wait on the local lock alone")
    run.add_argument("--timeout", type=_seconds(0, None), metavar="DURATION",
                     help="give up waiting after DURATION: exit 75 without running")
    hold = actions.add_parser(
        "hold", help="hold a lease for as long as another process runs",
        usage="pseudolife-mcp lease hold NAME --while-pid PID [--expect DURATION] "
              "[--ttl SECONDS] [--purpose TEXT] [--worktree PATH] [--no-board] "
              "[--timeout DURATION]",
        description="Take the lease NAME for a process this command did not start (a "
                    "detached bench server, say): hold its OS lock, mirror it on the board, "
                    "and release both when PID exits.",
        epilog=_HOLD_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    hold.add_argument("name", type=_lease_name, metavar="NAME",
                      help=f"the lease name, 1 to {MAX_NAME} characters")
    hold.add_argument("--while-pid", type=_pid, required=True, metavar="PID",
                      help="the process the lease lasts for")
    hold.add_argument("--expect", type=_seconds(MIN_EXPECT, MAX_EXPECT), metavar="DURATION",
                      help="how long PID should run; the board shows the expected end")
    hold.add_argument("--ttl", type=_seconds(MIN_TTL, MAX_TTL), default=DEFAULT_TTL,
                      metavar="SECONDS",
                      help=f"how long the board keeps the lease without a renewal, "
                           f"{MIN_TTL} to {MAX_TTL} (default {DEFAULT_TTL}); renewed "
                           f"every ttl/3")
    hold.add_argument("--purpose", type=_purpose, metavar="TEXT",
                      help=f"what it is for, shown on the board (at most {MAX_PURPOSE} "
                           f"characters)")
    hold.add_argument("--worktree", default="", metavar="PATH",
                      help="the checkout whose name (never its path) the notices give "
                           "(default: the current directory)")
    hold.add_argument("--no-board", action="store_true",
                      help="skip the agent board and hold the local lock alone")
    hold.add_argument("--timeout", type=_seconds(0, None), metavar="DURATION",
                      help="give up waiting for a held lock after DURATION: exit 75")
    check = actions.add_parser(
        "check", help="exit 0 when a lease is free, 1 when it is held",
        description="The launch gate: print the lease's holder (OS lock and board) and "
                    "its expected end, and exit 0 when it is free, 1 when it is held, or "
                    "70 when the check itself failed. The local lock decides wherever its "
                    "file exists; a board holder beside a free lock is shown as stale. For "
                    "full-suite the test suite's own lock is probed, with its holder "
                    "record.")
    check.add_argument("name", type=_lease_name, metavar="NAME", help="the lease to check")
    check.add_argument("--json", action="store_true", help="print one JSON report")
    listing = actions.add_parser(
        "list", help="show the board's leases and the local lock files",
        description="Show the board's leases (holder, purpose, age, expected end, queue) "
                    "beside the local lock files, each probed held or free, and the "
                    "test suite's own lock. Without a board, local state only.")
    listing.add_argument("name", nargs="?", type=_lease_name, metavar="NAME",
                         help="show only this lease")
    listing.add_argument("--json", action="store_true", help="print one JSON report")
    breaking = actions.add_parser(
        "break", help="(operator) free a lease whatever its holder says",
        description="Operator only: free the lease NAME on the board, whatever its holder "
                    "says, and grant it to the next waiter. It opens the bank directly, "
                    "the way board-audit and export find it (PSEUDOLIFE_MCP_DATABASE_URL, "
                    "else the lite tier's data dir), never through an agent's credential, "
                    "and the audit log records the break with the operator as its actor. "
                    "It frees the board's record only: a process still holding the local "
                    "lock keeps it until it exits.")
    breaking.add_argument("name", type=_lease_name, metavar="NAME", help="the lease to free")
    return parser, run, {"hold": hold, "check": check, "list": listing, "break": breaking}


def main(argv: list[str] | None = None, *, transport=None) -> int:
    """Entry point for ``pseudolife-mcp lease``; ``argv`` excludes the mode
    (default ``sys.argv[2:]``). ``transport`` is an httpx transport for the
    board (tests pass a mock)."""
    argv = sys.argv[2:] if argv is None else list(argv)
    command = None
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    parser, run, others = _parsers()
    try:
        args = parser.parse_args(argv)
        if args.action is None:
            parser.print_usage(sys.stderr)
            parser.exit(EXIT_USAGE, "pseudolife-mcp lease: choose run, hold, check, list or "
                                    "break (--help explains each)\n")
        if args.action == "run" and not command:
            run.error("put the command to run after --, as in: "
                      "pseudolife-mcp lease run gpu -- python train.py")
        if args.action in others and command is not None:
            others[args.action].error(f"{args.action} takes no command")
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    if args.action == "hold":
        return _hold(args, transport)
    if args.action == "check":
        return _check(args, transport)
    if args.action == "list":
        return _list(args, transport)
    if args.action == "break":
        return _break(args)
    return _run(args, command, transport)
