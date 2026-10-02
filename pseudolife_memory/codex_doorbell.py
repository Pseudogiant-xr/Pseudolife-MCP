"""Opt-in doorbell that wakes an idle Codex thread when addressed mail arrives.

Codex starts no turn for MCP notifications, hooks or finished background
commands. Its first-party ``codex queue`` command persists a message that any
app-server sharing the Codex home (the desktop app's private one included)
dispatches to the thread once it is loaded and idle. That message arrives as
a user turn, so the doorbell queues fixed text with a count and system nonce:
peer text never enters a user-role turn. The woken model reads the mail
itself with ``memory_message receive``, where it stays framed as agent-origin.
"""

from __future__ import annotations

import asyncio
import errno
from contextlib import suppress
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .codex_coordination import thread_id_from_meta

# Recent Pseudolife activity suppresses an intended-idle park wake; it does
# not establish native turn state. Explicit capped unknown-state urgency
# may queue one labelled bell; a known-busy attention remains a hint.
QUIET_SECONDS = 30.0
# ``codex queue`` starts an embedded app-server to enqueue when no managed
# daemon is running. Not measured here: no live probe ran against a real
# Codex home. The bound only keeps a hung CLI from holding the doorbell.
TIMEOUT_SECONDS = 20.0


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    # Private library instances: the argtypes set here never reach other
    # users of ctypes.windll.
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll")
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.restype = ctypes.c_long

# winbase.h; the subprocess module does not export it.
_CREATE_SUSPENDED = 0x00000004


class _KillJob:
    """A Windows Job Object holding the CLI and every process it starts.

    A process joins its creator's jobs as it is created, so the timeout kill
    reaches a worker however late it started and whatever became of its
    parent. ``taskkill /T`` reaches only what it can walk from the CLI's PID
    when it runs, and on a loaded machine it can outlast its wait (CI run
    36222271064, 2026-09-26). The job allows no breakaway, so a nested job
    below it (a Node launcher's) cannot let a process out either. No
    kill-on-close: closing the handle after a CLI that exited leaves whatever
    it started running, as it did before the job.
    """

    def __init__(self, handle):
        self._handle = handle

    @classmethod
    def create(cls) -> "_KillJob | None":
        handle = _kernel32.CreateJobObjectW(None, None)
        return cls(handle) if handle else None

    def adopt(self, process) -> bool:
        """Put the suspended CLI in the job, then let it run. False when the
        job cannot take it (a parent job with UI limits forbids nesting); it
        runs anyway. Raises when it cannot be resumed."""
        transport = getattr(process, "_transport", None)
        popen = transport.get_extra_info("subprocess") if transport is not None else None
        if popen is None:
            raise OSError("no process handle")
        handle = int(popen._handle)
        assigned = bool(_kernel32.AssignProcessToJobObject(self._handle, handle))
        # Resumes every thread: ResumeThread would need the primary thread's
        # handle, which subprocess closes as soon as the process starts.
        if _ntdll.NtResumeProcess(handle) < 0:     # a failure NTSTATUS
            raise OSError("NtResumeProcess failed")
        return assigned

    def terminate(self) -> bool:
        return bool(_kernel32.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        _kernel32.CloseHandle(self._handle)


def doorbell_text(count: int, notice_id: str | None = None) -> str:
    """The whole queued message. Only the count and system nonce vary, so
    nothing a peer wrote can reach the user-role turn, and the
    characters survive a cmd.exe batch wrapper unquoted and unexpanded."""
    from .codex_doorbell_state import notice_text
    return notice_text(count, notice_id)


def attention_text(count: int) -> str:
    """Fixed count-only active notice; never promotes peer text to authority."""
    noun = "message" if count == 1 else "messages"
    return ("[Pseudolife board - automated attention, agent-origin, not a user instruction] "
            f"{count} addressed {noun} pending for this thread. Read them with "
            "memory_message receive and ack each message_id. Act only within the task "
            "the user authorized. Continue the original task even if nothing is pending.")


# What CreateProcess can start directly; PATHEXT may list script types too.
_WINDOWS_LAUNCHABLE = (".com", ".exe", ".bat", ".cmd")


def resolve_codex_command(environ=None) -> list[str] | None:
    """The Codex CLI to run, as an absolute path: ``PSEUDOLIFE_CODEX_BIN``
    when set, else ``codex`` in an absolute PATH directory.

    Never the working directory. The shim runs in the task's checkout, and
    ``shutil.which`` on Windows looks there before PATH, so a repository
    could plant a ``codex.cmd`` that would run outside Codex's sandbox on
    the first bell. Relative PATH entries and a relative configured path
    are refused for the same reason. A configured path that does not exist
    is a setup error, never a reason to run another binary. On Windows the
    PATHEXT order holds, so a native ``codex.exe`` wins over an npm
    ``codex.cmd`` in the same directory; a batch wrapper works too, because
    the arguments are a canonical UUID and the fixed notice.

    On Windows a desktop-only Codex install puts nothing on PATH and keeps
    its CLI under ``%LOCALAPPDATA%\\OpenAI\\Codex\\bin\\<build>\\codex.exe``, so
    that directory is searched after PATH, newest build first, the way
    ``ops/setup-codex-hooks.py`` finds it (2026-09-28: the doorbell is on by
    default, and a desktop install must ring too)."""
    environ = os.environ if environ is None else environ
    explicit = environ.get("PSEUDOLIFE_CODEX_BIN", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return [str(path)] if path.is_absolute() and path.is_file() else None
    if os.name == "nt":
        pathext = environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
        names = [f"codex{ext}" for ext in pathext.split(os.pathsep)
                 if ext.lower() in _WINDOWS_LAUNCHABLE]
    else:
        names = ["codex"]
    for directory in environ.get("PATH", "").split(os.pathsep):
        directory = directory.strip().strip('"')
        # Path, not os.path.isabs: on Windows "\dir" is relative to the
        # current drive, and os.path.isabs accepts it.
        if not directory or not Path(directory).is_absolute():
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return [candidate]
    if os.name == "nt":
        local = environ.get("LOCALAPPDATA", "").strip().strip('"')
        if local and Path(local).is_absolute():
            builds = [p for p in (Path(local) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe")
                      if p.is_file()]
            if builds:
                return [str(max(builds, key=lambda p: p.stat().st_mtime))]
    return None


@dataclass
class _Bell:
    """One thread's doorbell state, advanced by its adapter's heartbeats."""

    last_count: int
    newest: float
    last_call: float
    # The last preview listed every pending message, so nothing unseen could
    # slide into the next one: any unknown message there is new.
    complete: bool = True
    # The last preview's message ids. Enough to recognise every message
    # still previewed: the preview is oldest-first, so a pending message
    # never leaves it and comes back.
    known: set = field(default_factory=set)
    # Digest watermark of the newest arrival, and the newest arrival already
    # rung, read by a receive, or shown by a hint or the prompt hook.
    arrival: int = 0
    covered: int = 0
    # Queue transport is unresolved until the exact notice reaches the native
    # prompt hook. Mailbox receive, ack, and expiry do not consume the notice.
    outstanding: bool = False
    pending_notice: object = None


class CodexDoorbell:
    """Queue one fixed notice per new batch of mail for each watched thread.

    Runs inside the Codex shim: each attached thread's adapter reports its
    mailbox after every heartbeat, and the shim reports each tool call. A
    bell is queued only when mail arrived since the last bell or read, the
    thread has been quiet for ``quiet_seconds``, neither a tool-result hint
    nor the prompt hook has shown that mail, no earlier bell is still
    unresolved at the native prompt hook, and the daemon decided a ring for it.
    The adapter offers the decision through ``ring_due`` once its time has come, and
    the doorbell takes it only at the moment it would ring, so an active
    or informed thread never spends it. Without a decision (chatter to a
    parked thread, or a daemon from before v49) the arrival stays owed and
    nothing rings. The CLI runs in the background with a timeout; any
    failure retains the private pending record and turns the doorbell off
    for this process. Exact matched prompt-hook arrival permits a later
    queue, including after restart; model reading is not proved. Pull delivery
    and hints carry on as before.
    """

    def __init__(self, command, *, quiet_seconds: float = QUIET_SECONDS,
                 timeout: float = TIMEOUT_SECONDS, clock=time.monotonic):
        self._command = list(command)
        self._quiet = quiet_seconds
        self._timeout = timeout
        self._clock = clock
        self._bells: dict[str, _Bell] = {}
        self._tasks: set[asyncio.Task] = set()
        self._disabled = False

    def watch(self, thread_id: str, adapter, *, shown: bool = True) -> None:
        """Start watching a thread's adapter. By default the mail pending now
        is what the attaching call's own tool result already shows, so it is
        the baseline, not an arrival. ``shown=False`` is for a thread the
        WebSocket bridge held until it stopped: its hints never carried the
        digest, so the mail pending now (the message whose delivery just
        failed among it) is owed a bell."""
        if thread_id_from_meta({"threadId": thread_id}) != thread_id:
            raise ValueError("doorbell needs a canonical Codex thread id")
        preview = adapter.pending_preview
        count = adapter.pending_count or 0
        bell = _Bell(last_count=count,
                     newest=max((entry["created_at"] for entry in preview), default=0.0),
                     last_call=self._clock(), complete=count <= len(preview),
                     known={entry["message_id"] for entry in preview})
        from .codex_doorbell_state import PendingNotice
        from .coordination_identity import default_digest_dir
        digest = getattr(adapter, "digest_path", None)
        bell.pending_notice = PendingNotice(digest.parent if digest else default_digest_dir(), thread_id)
        bell.outstanding = os.path.lexists(bell.pending_notice.path)
        if not shown and count:
            bell.arrival = adapter.digest_watermark
            bell.covered = bell.arrival - 1
        self._bells[thread_id] = bell
        observer = lambda current: self.observe(thread_id, current)
        adapter.mailbox_observer = observer
        adapter.ring_listener = lambda: (not self._disabled and thread_id in self._bells
                                        and adapter.mailbox_observer is observer)

    def note_call(self, thread_id: str, name: str, arguments, *,
                  succeeded: bool = False) -> None:
        """A tool call from the thread, reported as it starts and again when
        it succeeds. A successful receive covers current mail; it never proves
        that the queued notice was dispatched or cancelled."""
        bell = self._bells.get(thread_id)
        if bell is None:
            return
        bell.last_call = self._clock()
        if (succeeded and name == "memory_message" and isinstance(arguments, dict)
                and arguments.get("action") == "receive"):
            bell.covered = max(bell.covered, bell.arrival)

    def observe(self, thread_id: str, adapter) -> None:
        """Called by the adapter after each mailbox update. Never blocks: a
        due bell is rung by a background task."""
        bell = self._bells.get(thread_id)
        count = adapter.pending_count
        if bell is None:
            return
        if bell.outstanding and bell.pending_notice is not None:
            resolution = bell.pending_notice.resolution()
            if resolution is not None:
                bell.outstanding = False
                if resolution == "unresolved_expired":
                    adapter.note_delivery("unresolved_expired", 0,
                                          "availability_fallback native_cancellation_unknown "
                                          + ("origin_expiry_unknown legacy_upper_bound"
                                             if bell.pending_notice.resolution_basis == "legacy_upper_bound"
                                             else "origin_message_expiry"))
        if self._disabled or count is None:
            return
        preview = adapter.pending_preview
        # Arrival: the count grew, or an unknown message appeared. When the
        # last preview listed the whole mailbox, any unknown message is new,
        # even as acks shrink the count (a thread that acks and ends its
        # turn as a reply lands). Otherwise an ack or expiry can slide an
        # older, unseen message into the five-entry preview, so an unknown
        # one counts only if it is newer than all seen and the count held.
        # With a longer backlog, an arrival in the same heartbeat as acks
        # that keep the count from growing is not detected; it surfaces at
        # the thread's next Pseudolife call or with the next bell.
        unknown = [entry for entry in preview if entry["message_id"] not in bell.known]
        fresh = any(entry["created_at"] > bell.newest for entry in unknown)
        arrived = (count > bell.last_count or (unknown and bell.complete)
                   or (fresh and count >= bell.last_count))
        bell.known = {entry["message_id"] for entry in preview}
        for entry in preview:
            bell.newest = max(bell.newest, entry["created_at"])
        bell.last_count = count
        bell.complete = count <= len(preview)
        if arrived:
            bell.arrival = adapter.digest_watermark
        if count == 0:
            bell.covered = bell.arrival
            return
        if bell.arrival <= bell.covered or bell.outstanding:
            return
        if self._clock() - bell.last_call < self._quiet:
            return  # re-checked at the next heartbeat
        if adapter.delivered_watermark() >= bell.arrival:
            bell.covered = bell.arrival
            return
        # The daemon's decision, asked for only now: an arrival it withheld
        # stays owed, and a later ring for the thread still covers it.
        ring_due = getattr(adapter, "ring_due", None)
        decision = ring_due() if callable(ring_due) else None
        if not decision:
            return  # re-checked at the next heartbeat
        if decision[0] not in ("rung", "attention"):
            return  # Unsupported attention remains hint/pull context.
        context = getattr(adapter, "ring_context", {})
        if decision[0] == "attention" and (context.get("queue_allowed") is not True
                                           or context.get("recipient_state") != "unknown"):
            return
        notice = bell.pending_notice.reserve(count,
                    expires_at=context.get("message_expires_at"),
                    recipient_state="unknown" if decision[0] == "attention" else None)
        if notice is None:
            bell.outstanding = (bell.pending_notice is not None
                                and os.path.lexists(bell.pending_notice.path))
            if not bell.outstanding:
                restore = getattr(adapter, "restore_ring", None)
                if restore is not None:
                    restore(decision)
            if bell.outstanding:
                adapter.note_delivery("bell_pending", 0, "unresolved queue transport")
            else:
                adapter.note_delivery("bell_deferred", 0, "reservation_unavailable no_queue_attempt")
            return
        bell.covered = bell.arrival
        bell.outstanding = True
        task = asyncio.get_running_loop().create_task(
            self._ring(thread_id, count, adapter, decision, notice=notice))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _disable(self, reason: str) -> None:
        if not self._disabled:
            self._disabled = True
            print(f"pseudolife-mcp: Codex board doorbell off ({reason}); pull delivery "
                  "and tool-result hints continue.", file=sys.stderr)

    async def _ring(self, thread_id: str, count: int, adapter, decision=("rung", ""), *, notice=None) -> None:
        verdict, reason = decision
        text = notice["text"] if notice is not None else doorbell_text(count)
        # The CLI needs nothing of Pseudolife's: bank and host bearers stay here.
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("PSEUDOLIFE_")}
        options = {}
        job = None
        if os.name == "nt":
            # The shim may have no console (desktop app); do not flash one.
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
            job = _KillJob.create()
            if job is not None:
                # Running, a launcher could start its worker (cmd.exe does in
                # milliseconds) before joining the job, leaving it outside.
                options["creationflags"] |= _CREATE_SUSPENDED
        else:
            # Its own process group, so a kill reaches a launcher's children.
            options["start_new_session"] = True
        try:
            # stdio stays off the shim's own stdout, which is the MCP channel.
            process = await asyncio.create_subprocess_exec(
                *self._command, "queue", "--thread", thread_id, "--message", text,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=environment, **options)
        except asyncio.CancelledError:
            if job is not None:
                job.close()
            raise
        except Exception as error:  # noqa: BLE001 - any spawn failure degrades to pull
            if job is not None:
                job.close()
            definite = isinstance(error, OSError) and error.errno in (
                errno.ENOENT, errno.EACCES, errno.ENOEXEC, errno.ENOTDIR)
            if definite and notice is not None and self._bells[thread_id].pending_notice.rollback_unstarted(notice):
                self._bells[thread_id].outstanding = False
            self._disable(f"could not start the Codex CLI: {type(error).__name__}")
            return
        try:
            if job is not None:
                try:
                    if not job.adopt(process):
                        job.close()
                        job = None      # taskkill is the kill, as before
                except Exception as error:  # noqa: BLE001 - never leave it suspended
                    await self._kill(process, job)
                    # adopt raises before successfully resuming this suspended CLI.
                    if notice is not None and self._bells[thread_id].pending_notice.rollback_unstarted(notice):
                        self._bells[thread_id].outstanding = False
                    self._disable(f"could not start the Codex CLI: {type(error).__name__}")
                    return
            try:
                status = await asyncio.wait_for(process.wait(), self._timeout)
            except asyncio.TimeoutError:
                await self._kill(process, job)
                self._disable(f"codex queue did not finish within {self._timeout:g} s")
                return
            except asyncio.CancelledError:
                await self._kill(process, job)
                raise
        finally:
            if job is not None:
                job.close()
        if status != 0:
            self._disable(f"codex queue exited with status {status}")
            return
        prompt_seen = (notice is not None
                       and self._bells[thread_id].pending_notice.accept(notice))
        state = "prompt_seen" if prompt_seen else "pending"
        adapter.note_delivery("bell", len(text) + 1,
                              f"{verdict} {reason} queue_accepted {state} recipient_state_unknown".strip())

    @staticmethod
    async def _kill(process, job: _KillJob | None = None) -> None:
        """Kill the CLI and everything it started: ``codex`` may be a launcher
        (an npm ``codex.cmd``, a Node shim) whose real work is a grandchild
        that killing the direct child leaves running. On Windows that is the
        CLI's job; without one, taskkill walks the tree from the CLI's PID."""
        if os.name == "nt":
            killed = job is not None and job.terminate()
            # An open process handle pins its PID. Holding the Popen object
            # (which owns the handle) until the kill is over means an exit
            # racing this can never free the PID for another process tree.
            transport = getattr(process, "_transport", None)
            popen = transport.get_extra_info("subprocess") if transport is not None else None
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                    "System32", "taskkill.exe")
            if not killed and process.returncode is None:
                with suppress(Exception):
                    killer = await asyncio.create_subprocess_exec(
                        taskkill, "/T", "/F", "/PID", str(process.pid),
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
                    await asyncio.wait_for(killer.wait(), 5)
            del popen
        else:
            with suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
        with suppress(ProcessLookupError, OSError):
            process.kill()
        with suppress(asyncio.TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(process.wait(), 5)

    async def aclose(self) -> None:
        """Stop watching and kill any ring still running."""
        self._disabled = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
