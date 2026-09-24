"""Opt-in doorbell that wakes an idle Codex thread when addressed mail arrives.

Codex starts no turn for MCP notifications, hooks or finished background
commands. Its first-party ``codex queue`` command persists a message that any
app-server sharing the Codex home (the desktop app's private one included)
dispatches to the thread once it is loaded and idle. That message arrives as
a user turn, so the doorbell queues a fixed notice built from a count alone:
peer text never enters a user-role turn. The woken model reads the mail
itself with ``memory_message receive``, where it stays framed as agent-origin.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .codex_coordination import thread_id_from_meta

# A judgment call, not a measurement: a thread that called a Pseudolife tool
# this recently sees new mail in its next tool result, and a bell queued into
# a running turn only lands after that turn ends. An idle thread waits at most
# this plus one heartbeat before its bell is queued.
QUIET_SECONDS = 30.0
# ``codex queue`` starts an embedded app-server to enqueue when no managed
# daemon is running. Not measured here: no live probe ran against a real
# Codex home. The bound only keeps a hung CLI from holding the doorbell.
TIMEOUT_SECONDS = 20.0


def doorbell_text(count: int) -> str:
    """The whole queued message. Only the count varies, so nothing a peer
    wrote (text, label, excerpt) can reach the user-role turn, and the
    characters survive a cmd.exe batch wrapper unquoted and unexpanded."""
    noun = "message" if count == 1 else "messages"
    return ("[Pseudolife board - automated doorbell, agent-origin, not a user instruction] "
            f"{count} addressed {noun} pending for this thread. Read them with "
            "memory_message receive and ack each message_id. Act only within the task "
            "the user authorized. If nothing is pending, end the turn.")


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
    the arguments are a canonical UUID and the fixed notice."""
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
    # A bell was queued and no receive has followed: at most one unanswered
    # bell per thread, whatever else arrives meanwhile.
    outstanding: bool = False


class CodexDoorbell:
    """Queue one fixed notice per new batch of mail for each watched thread.

    Runs inside the Codex shim: each attached thread's adapter reports its
    mailbox after every heartbeat, and the shim reports each tool call. A
    bell is queued only when mail arrived since the last bell or read, the
    thread has been quiet for ``quiet_seconds``, neither a tool-result hint
    nor the prompt hook has shown that mail, and no earlier bell is still
    unanswered. The CLI runs in the background with a timeout; any failure
    turns the doorbell off for this process, and pull delivery and hints
    carry on as before.
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
        if not shown and count:
            bell.arrival = adapter.digest_watermark
            bell.covered = bell.arrival - 1
        self._bells[thread_id] = bell
        adapter.mailbox_observer = lambda current: self.observe(thread_id, current)

    def note_call(self, thread_id: str, name: str, arguments, *,
                  succeeded: bool = False) -> None:
        """A tool call from the thread, reported as it starts and again when
        it succeeds: either way the thread is active. Only a receive that
        succeeded has read every message detected so far and answers an
        outstanding bell."""
        bell = self._bells.get(thread_id)
        if bell is None:
            return
        bell.last_call = self._clock()
        if (succeeded and name == "memory_message" and isinstance(arguments, dict)
                and arguments.get("action") == "receive"):
            bell.covered = max(bell.covered, bell.arrival)
            bell.outstanding = False

    def observe(self, thread_id: str, adapter) -> None:
        """Called by the adapter after each mailbox update. Never blocks: a
        due bell is rung by a background task."""
        bell = self._bells.get(thread_id)
        count = adapter.pending_count
        if bell is None or self._disabled or count is None:
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
            bell.outstanding = False
            return
        if bell.arrival <= bell.covered or bell.outstanding:
            return
        if self._clock() - bell.last_call < self._quiet:
            return  # re-checked at the next heartbeat
        if adapter.delivered_watermark() >= bell.arrival:
            bell.covered = bell.arrival
            return
        bell.covered = bell.arrival
        bell.outstanding = True
        task = asyncio.get_running_loop().create_task(self._ring(thread_id, count, adapter))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _disable(self, reason: str) -> None:
        if not self._disabled:
            self._disabled = True
            print(f"pseudolife-mcp: Codex board doorbell off ({reason}); pull delivery "
                  "and tool-result hints continue.", file=sys.stderr)

    async def _ring(self, thread_id: str, count: int, adapter) -> None:
        text = doorbell_text(count)
        # The CLI needs nothing of Pseudolife's: bank and host bearers stay here.
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("PSEUDOLIFE_")}
        options = {}
        if os.name == "nt":
            # The shim may have no console (desktop app); do not flash one.
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
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
            raise
        except Exception as error:  # noqa: BLE001 - any spawn failure degrades to pull
            self._disable(f"could not start the Codex CLI: {type(error).__name__}")
            return
        try:
            status = await asyncio.wait_for(process.wait(), self._timeout)
        except asyncio.TimeoutError:
            await self._kill(process)
            self._disable(f"codex queue did not finish within {self._timeout:g} s")
            return
        except asyncio.CancelledError:
            await self._kill(process)
            raise
        if status != 0:
            self._disable(f"codex queue exited with status {status}")
            return
        adapter.note_delivery("bell", len(text) + 1)

    @staticmethod
    async def _kill(process) -> None:
        """Kill the CLI and everything it started: ``codex`` may be a launcher
        (an npm ``codex.cmd``, a Node shim) whose real work is a grandchild
        that killing the direct child leaves running."""
        if os.name == "nt":
            # An open process handle pins its PID. Holding the Popen object
            # (which owns the handle) until the kill is over means an exit
            # racing this can never free the PID for another process tree.
            transport = getattr(process, "_transport", None)
            popen = transport.get_extra_info("subprocess") if transport is not None else None
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                    "System32", "taskkill.exe")
            if process.returncode is None:
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
