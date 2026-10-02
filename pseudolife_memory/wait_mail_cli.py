"""``pseudolife-mcp wait-mail`` — block until the daemon rings, print the mail.

MCP gives a server no way to start a turn in an idle session; only the host
can, for instance when a background command exits (Claude Code's Bash tool
with ``run_in_background``) or a hook asks it to. This is that command. It
reads the coordination digest file the shim's adapter rewrites on its
heartbeat, the ``<key>.ring`` marker the adapter writes beside it when the
daemon decides a ring, and the ``.seen`` delivery marker it shares with the
prompt hooks and the tool-result hint. No daemon connection, token or
network is involved, and nothing heavy is imported. While waiting, a short
local listener lease lets the authenticated adapter report a live wake path
(the daemon decides a ring only for a recipient with one); the waiter renews
it each poll and removes it on exit.

Regular mail never wakes, only a ring does (maintainer decision 2026-10-02),
so it fires on the Stop hook's rule (plugin/hooks/stop-wake.sh): the ring
marker is a ``rung`` ring past ``.seen``, the digest's watermark is past
``.seen`` too and the digest lists pending mail. Plain mail the daemon
decided no ring for keeps it waiting; that mail is still pending at the
session's next turn. On firing it prints the digest body verbatim (the body
already frames the mail as agent-origin, not user authority), advances
``.seen`` to the digest's watermark so nothing repeats it, and exits 0.
Re-arming with that mail still unacknowledged waits for the next ring
instead of firing again. A ring that arrived between one wake and the
re-arm fires at once. A baseline taken at arm time would absorb it, which
the 2026-09-23 coordination trial observed.

Exit codes: 0 a ring (the mail on stdout); 3 timeout, re-arm to keep
waiting; 2 nothing to wait on (no session id, no readable digest file, bad
arguments, or a stdout that cannot take the mail). Diagnostics go to stderr
only.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

from pseudolife_memory.coordination_identity import resolve_digest_path
from pseudolife_memory.wake_liveness import WaitListener

EXIT_MAIL = 0
EXIT_SETUP = 2
EXIT_TIMEOUT = 3

# The board-wait.sh script this replaces waited 4 h per arm through the
# 2026-09-23 trial; each timeout costs the session one re-arm turn.
DEFAULT_TIMEOUT = 4 * 3600.0
# A day bounds a waiter its session has forgotten.
MAX_TIMEOUT = 86400.0
# The adapter rewrites the digest at most once per 20 s heartbeat. A 2 s stat
# poll adds at most 2 s to that, and the file is opened only when it changed.
DEFAULT_INTERVAL = 2.0
MIN_INTERVAL = 0.01
MAX_INTERVAL = 60.0

# The adapter names every digest by a SHA-256 key. Holding --digest to that
# shape keeps the command's writes (the .seen marker, a ledger line) inside
# coordination digest directories even when an allow rule approves any
# arguments.
_DIGEST_NAME = re.compile(r"[0-9a-f]{64}\.txt")

# The ring marker as coordination_adapter._write_ring writes it and the Stop
# hook's ring_past_seen reads it: line 1 the digest watermark the ring is
# for, at most 12 digits once CR, LF and spaces are dropped; line 2, one
# trailing CR dropped, the daemon's decision and reason in [-A-Za-z0-9_ ],
# and only a rung decision rings.
_RING_AT = re.compile(rb"[0-9]{1,12}")
_RING_REASON = re.compile(rb"rung [-A-Za-z0-9_ ]*")


class _DigestGone(Exception):
    """The digest file vanished mid-wait: its shim exited or restarted."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pseudolife-mcp wait-mail",
        description="Wait until the daemon rings this session for addressed mail (plain "
                    "mail never wakes it), print the mail, exit. "
                    "Exit 0: a ring (the mail on stdout); 3: timeout; 2: nothing to wait on.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--session-id", help="host session id keying the digest, e.g. a Codex "
                        "thread id (default: CLAUDE_CODE_SESSION_ID)")
    source.add_argument("--digest", type=Path,
                        help="coordination digest file (<64 hex digits>.txt) to watch instead")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"seconds to wait, at most {MAX_TIMEOUT:g} (default {DEFAULT_TIMEOUT:g})")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help=f"seconds between checks, {MIN_INTERVAL:g} to {MAX_INTERVAL:g} "
                             f"(default {DEFAULT_INTERVAL:g})")
    return parser


def _read_digest(path: Path) -> tuple[int | None, bytes]:
    """(watermark, body) from one read; the adapter replaces the file atomically."""
    head, _, body = path.read_bytes().partition(b"\n")
    try:
        return int(head), body
    except ValueError:
        return None, b""


def _read_ring(path: Path) -> tuple[int, str] | None:
    """(watermark, "rung <reason>") from the ring marker, or None when it
    holds no rung ring. Anything but a regular file (a symlink included)
    with a watermark and a rung decision is no ring, as in the Stop hook. An
    OSError other than a missing file propagates: a sharing violation
    mid-replace on Windows, looked at again next poll."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    head, _, rest = raw.partition(b"\n")
    reason = rest.partition(b"\n")[0]
    head = re.sub(rb"[\r ]", b"", head)
    if reason.endswith(b"\r"):
        reason = reason[:-1]
    if not _RING_AT.fullmatch(head) or not _RING_REASON.fullmatch(reason):
        return None
    return int(head), reason.decode("ascii")


def _signature(path: Path):
    """What changes on every replace of ``path``; None while it is absent.
    A file that cannot be inspected for now never matches, so it is looked
    at again next poll."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return object()
    return info.st_ino, info.st_mtime_ns, info.st_size


def _read_seen(path: Path) -> int:
    try:
        return int(path.read_bytes().strip() or 0)
    except (OSError, ValueError):
        return 0


def _mark_seen(path: Path, watermark: int) -> None:
    """Advance the shared delivery marker. Never lower it: a hook or the
    tool-result hint may already be past this watermark. A reader holding the
    file open can refuse the replace on Windows, so it is retried briefly."""
    for attempt in range(3):
        if _read_seen(path) >= watermark:
            return
        temp = None
        try:
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                handle.write(f"{watermark}\n")
            os.replace(temp, path)
            return
        except OSError:
            if temp is not None:
                with suppress(OSError):
                    os.unlink(temp)
            if attempt == 2:
                raise
            time.sleep(0.05)


def _ledger(digest: Path, watermark: int, size: int, ring: str) -> None:
    """One line per delivery, beside the adapter's and the prompt hooks';
    like the Stop hook's ``wait`` line, the sixth column is the ring."""
    with suppress(OSError):
        with open(digest.parent / "ledger.log", "a", encoding="utf-8") as handle:
            handle.write(f"{int(time.time())}\twait\t{digest.stem[:8]}\t{watermark}\t{size}"
                         f"\t{ring}\n")


def _wait(digest: Path, timeout: float, interval: float) -> tuple[int, bytes, str] | None:
    """Poll until the daemon rings for unshown mail: ``(watermark, body,
    ring)``, or None on timeout."""
    seen = digest.with_suffix(".seen")
    deadline = time.monotonic() + timeout
    listener = WaitListener(digest, timeout)
    try:
        return _wait_listener(digest, seen, deadline, interval, listener)
    finally:
        listener.close()


def _wait_listener(digest, seen, deadline, interval, listener):
    ring = digest.with_suffix(".ring")
    last = None
    while True:
        listener.renew()
        try:
            info = os.stat(digest)
        except FileNotFoundError:
            raise _DigestGone from None
        except OSError:
            info = None  # e.g. a sharing violation mid-replace on Windows; look again next poll
        if info is not None:
            # A replace makes a new file, so the signature moves on every
            # rewrite of the digest or the ring marker.
            signature = ((info.st_ino, info.st_mtime_ns, info.st_size), _signature(ring))
            if signature != last:
                last = signature
                try:
                    watermark, body = _read_digest(digest)
                    rung = _read_ring(ring)
                except FileNotFoundError:
                    raise _DigestGone from None
                except OSError:
                    last = None  # read again next poll
                else:
                    if watermark is not None and body.strip() and rung is not None:
                        shown = _read_seen(seen)
                        if watermark > shown and rung[0] > shown:
                            return watermark, body, rung[1]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(interval, remaining))


def run_wait_mail(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        if not 0 < args.timeout <= MAX_TIMEOUT:
            parser.error(f"--timeout must be more than 0 and at most {MAX_TIMEOUT:g} seconds")
        if not MIN_INTERVAL <= args.interval <= MAX_INTERVAL:
            parser.error(f"--interval must be from {MIN_INTERVAL:g} to {MAX_INTERVAL:g} seconds")
        if args.digest is not None and not _DIGEST_NAME.fullmatch(args.digest.name):
            parser.error("--digest must name a coordination digest file (<64 hex digits>.txt)")
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_SETUP

    if args.digest is not None:
        digest = args.digest.expanduser()
    else:
        session = (args.session_id if args.session_id is not None
                   else os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        digest = resolve_digest_path(session)
        if digest is None:
            print("wait-mail: no session id to key the digest. Run it from a Claude Code "
                  "session (which sets CLAUDE_CODE_SESSION_ID), or pass --session-id (a Codex "
                  "thread id) or --digest PATH.", file=sys.stderr)
            return EXIT_SETUP
    try:
        # os.stat, not Path.is_file: which OSErrors is_file swallows changed in 3.13.
        present = stat.S_ISREG(os.stat(digest).st_mode)
    except FileNotFoundError:
        present = False
    except OSError as error:
        print(f"wait-mail: cannot read the digest file at {digest} ({error}).", file=sys.stderr)
        return EXIT_SETUP
    if not present:
        print(f"wait-mail: no digest file at {digest}. The shim writes it when its coordination "
              "adapter attaches: at shim start in Claude Code (if it is missing there, "
              "coordination did not attach for this session; the shim's stderr says why), on a "
              "thread's first memory_* call in Codex. Then re-arm. Also check that "
              "PSEUDOLIFE_DIGEST_DIR is the same for the MCP server and this shell.",
              file=sys.stderr)
        return EXIT_SETUP

    started = time.monotonic()
    try:
        found = _wait(digest, args.timeout, args.interval)
    except _DigestGone:
        print("wait-mail: the digest file disappeared (the shim exited or restarted). "
              "Re-arm once the shim is back.", file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        return 130
    if found is None:
        print(f"wait-mail: no ring in {args.timeout:g} s (plain mail does not end the wait); "
              "re-arm to keep waiting.", file=sys.stderr)
        return EXIT_TIMEOUT

    watermark, body, ring = found
    print(f"wait-mail: the daemon rang for addressed mail at {time.strftime('%H:%M:%S')} ({ring}, "
          f"watermark {watermark}, {time.monotonic() - started:.0f} s after arming):",
          file=sys.stderr, flush=True)
    try:
        sys.stdout.flush()
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.flush()
    except (OSError, ValueError) as error:
        # Not shown, so not marked: the next waiter or prompt hook still has it.
        print(f"wait-mail: could not write the mail to stdout ({error}); left it unshown.",
              file=sys.stderr)
        return EXIT_SETUP
    try:
        _mark_seen(digest.with_suffix(".seen"), watermark)
    except OSError as error:
        print(f"wait-mail: could not advance the .seen marker ({error}); the prompt hook or "
              "tool-result hint may show this mail again.", file=sys.stderr)
    _ledger(digest, watermark, len(body.decode("utf-8", "replace")), ring)
    return EXIT_MAIL
