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
from pathlib import Path

from pseudolife_memory import os_lock

EXIT_USAGE = 2
EXIT_NESTED = 64  # sysexits EX_USAGE: run inside a run of the same lease
EXIT_LOCK_ERROR = 71  # sysexits EX_OSERR
EXIT_TEMPFAIL = 75  # sysexits EX_TEMPFAIL
EXIT_CANNOT_RUN = 126
EXIT_NOT_FOUND = 127
# A run stopped by signal N exits 128+N, as from a shell: 130 for Ctrl-C.

DEFAULT_URL = "http://127.0.0.1:8765"
HELD_ENV = "PSEUDOLIFE_LEASES_HELD"
LABEL = "lease-run"
# The test suite's own lock (tests/suite_lock.py); only ever probed here.
SUITE_LOCK_FILE = "full-suite.lock"

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
KILL_GRACE = 10.0
# How often a wait on the command checks for Ctrl-C: an untimed wait is not
# interruptible on Windows.
CHILD_POLL = 0.2

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
    """The board cannot be used for this run; the message says why."""


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
    raise _Refused(f"{_refusal_text(status, code)} ({detail})")


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

    def register(self, *, task: str, project: str) -> None:
        reply = self._post("register", {
            "label": LABEL, "project": project, "task": task, "status": "",
            "capabilities": {"resumable": False}, "wake_enabled": False,
        }, instance=False)
        agent_id, credential = reply.get("agent_id"), reply.get("credential")
        if not (isinstance(agent_id, str) and agent_id
                and isinstance(credential, str) and credential):
            raise _Refused("the daemon's registration reply carried no address")
        self._agent_id, self._credential = agent_id, credential

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


def _connect(no_board: bool, transport) -> tuple[_Board | None, str | None]:
    """The board, or None and why there is none."""
    if no_board:
        return None, "--no-board was given"
    url = _daemon_url(os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", DEFAULT_URL))
    if url is None:
        return None, ("PSEUDOLIFE_MCP_DAEMON_URL is not an http(s) origin "
                      "(scheme, host and optional port only)")
    from pseudolife_memory.credentials import CredentialError, CredentialProvider

    try:
        token = CredentialProvider.from_environment().snapshot().token
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


def _project() -> str:
    return _printable(os.environ.get("PSEUDOLIFE_AGENT_PROJECT", ""))[:MAX_SCOPE]


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
    actions = parser.add_subparsers(dest="action", metavar="{run,list,break}")
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
    return parser, run, listing, breaking


def main(argv: list[str] | None = None, *, transport=None) -> int:
    """Entry point for ``pseudolife-mcp lease``; ``argv`` excludes the mode
    (default ``sys.argv[2:]``). ``transport`` is an httpx transport for the
    board (tests pass a mock)."""
    argv = sys.argv[2:] if argv is None else list(argv)
    command = None
    if "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    parser, run, listing, breaking = _parsers()
    try:
        args = parser.parse_args(argv)
        if args.action is None:
            parser.print_usage(sys.stderr)
            parser.exit(EXIT_USAGE, "pseudolife-mcp lease: choose run, list or break "
                                    "(--help explains each)\n")
        if args.action == "run" and not command:
            run.error("put the command to run after --, as in: "
                      "pseudolife-mcp lease run gpu -- python train.py")
        if args.action == "list" and command is not None:
            listing.error("list takes no command")
        if args.action == "break" and command is not None:
            breaking.error("break takes no command")
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_USAGE
    if args.action == "list":
        return _list(args, transport)
    if args.action == "break":
        return _break(args)
    return _run(args, command, transport)
