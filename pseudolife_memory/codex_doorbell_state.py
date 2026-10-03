"""Private queue correlation; a mailbox read never consumes a queued notice."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import errno
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

from .codex_coordination import _prepare_private_dir, thread_id_from_meta
from .coordination_identity import default_digest_dir, digest_path_for
from .os_lock import _try_lock, _unlock
from .private_state import PrivateStateError, _private_fd, open_private


def notice_text(count, nonce=None, *, version=1, recipient_state=None):
    """Immutable notice formats; older hook receipts survive wording upgrades."""
    noun = "message" if count == 1 else "messages"
    text = ("[Pseudolife board - automated doorbell, agent-origin, not a user instruction] "
            f"{count} addressed {noun} pending for this thread. ")
    if version == 2 and recipient_state == "unknown":
        text += "Recipient turn state unknown. "
    text += ("Read them with memory_message receive and ack each message_id. Act only within "
             "the task the user authorized. ")
    text += ("Continue the original task even if nothing is pending."
             if version == 2 and recipient_state == "unknown"
             else "If nothing is pending, end the turn.")
    return text + (f" [notice {nonce}]" if nonce is not None else "")


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _sync_directory(directory):
    if os.name == "nt":
        return  # Windows does not support opening directories through os.open.
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


class PendingNotice:
    """One unresolved notice per thread, preserved across shim restarts.

    The pending record is reserved before subprocess launch. Acceptance and
    exact prompt-hook arrival are separate nonce markers: a late queue result
    cannot resurrect a notice already seen by the owning prompt hook. This
    proves pipeline arrival, not model reading or mailbox acknowledgment.
    """

    def __init__(self, directory: Path, thread_id: str):
        if thread_id_from_meta({"threadId": thread_id}) != thread_id:
            raise ValueError("doorbell needs a canonical Codex thread id")
        self.thread_id = thread_id
        self.path = digest_path_for(thread_id, Path(directory)).with_suffix(".bell-pending")
        self.prompt_seen_path = self.path.with_suffix(".bell-prompt-seen")
        self.accepted_path = self.path.with_suffix(".bell-accepted")
        self.expired_path = self.path.with_suffix(".bell-unresolved-expired")
        self.resolution_basis = None
        self.reservation_expiry_basis = None

    @contextmanager
    def _locked(self, *, retry=0.0):
        _prepare_private_dir(self.path.parent, self.path.parent)
        fd = open_private(self.path.with_suffix(".bell-lock"), os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, "r+b") as handle:
            if os.fstat(fd).st_size == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + retry
            taken = _try_lock(handle)
            while not taken and time.monotonic() < deadline:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
                taken = _try_lock(handle)
            try:
                yield taken
            finally:
                if taken:
                    _unlock(handle)

    @staticmethod
    def _read(path):
        fd = open_private(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            text = handle.read(8193)
            if len(text) > 8192:
                raise ValueError("oversized doorbell private record")
            return text

    def _current(self, now=None):
        record = json.loads(self._read(self.path))
        if (not isinstance(record, dict) or record.get("thread_id") != self.thread_id
                or type(record.get("count")) is not int or record["count"] < 1
                or not isinstance(record.get("nonce"), str)
                or re.fullmatch("[0-9a-f]{32}", record["nonce"]) is None):
            raise ValueError("invalid doorbell pending record")
        version = record.get("version", 1)
        state = record.get("recipient_state")
        if (type(version) is not int or version not in (1, 2)
                or state not in (None, "unknown")
                or (version == 1 and state is not None)
                or record.get("text") != notice_text(record["count"], record["nonce"],
                                                     version=version, recipient_state=state)):
            raise ValueError("invalid doorbell notice format")
        if "version" not in record:
            if set(record) != {"thread_id", "nonce", "count", "text"}:
                raise ValueError("invalid legacy doorbell record")
            # Only a validated older system notice can migrate. The original
            # message predates this observation; its exact expiry is unknown.
            first_seen = time.time() if now is None else now
            record.update(version=1, legacy_first_seen=first_seen,
                          expires_at=first_seen + 86400, expiry_basis="legacy_upper_bound")
            self._replace(record)
        expiry = record.get("expires_at")
        basis = record.get("expiry_basis")
        if expiry is not None:
            if (not _timestamp(expiry) or basis not in ("message", "legacy_upper_bound")
                    or (basis == "legacy_upper_bound" and
                        (not _timestamp(record.get("legacy_first_seen"))
                         or expiry != record["legacy_first_seen"] + 86400))):
                raise ValueError("invalid doorbell expiry")
        else:
            raise ValueError("missing versioned doorbell expiry")
        return record

    def _replace(self, record):
        self._write_atomic(self.path, json.dumps(record, separators=(",", ":")))

    def _prompt_seen(self, record):
        try:
            return self._read(self.prompt_seen_path).strip() == record["nonce"]
        except FileNotFoundError:
            return False

    def _write_atomic(self, path, text, *, exclusive=False):
        if os.path.lexists(path):
            if exclusive:
                raise FileExistsError(path)
            self._read(path)  # Never tighten or replace a foreign/private-invalid marker.
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".bell-")
        try:
            try:
                _private_fd(fd, Path(temporary), PrivateStateError)
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            # All pending-record producers hold the same per-thread lock.
            # Recheck after staging so an existing reservation stays intact.
            if exclusive and os.path.lexists(path):
                raise FileExistsError(path)
            os.replace(temporary, path)
            _sync_directory(path.parent)
        finally:
            with suppress(OSError):
                os.unlink(temporary)

    def _mark(self, path, nonce):
        self._write_atomic(path, nonce)

    def reserve(self, count, *, expires_at=None, now=None, recipient_state=None):
        self.reservation_expiry_basis = None
        now = time.time() if now is None else now
        if (type(count) is not int or count < 1 or not _timestamp(now)
                or recipient_state not in (None, "unknown")
                or (expires_at is not None and
                    (not _timestamp(expires_at) or expires_at <= now))):
            return None
        try:
            with self._locked() as taken:
                if not taken:
                    return None
                if os.path.lexists(self.path):
                    previous = self._current(now)
                    outcome = self._resolution(previous, now)
                    if outcome is None:
                        return None
                    if outcome == "unresolved_expired":
                        self._release_expired(previous)
                        self.reservation_expiry_basis = previous["expiry_basis"]
                    else:
                        self.path.unlink()
                nonce = uuid.uuid4().hex
                record = {"version": 2, "thread_id": self.thread_id, "nonce": nonce,
                          "count": count, "recipient_state": recipient_state,
                          "expires_at": expires_at if expires_at is not None else now + 86400,
                          "expiry_basis": "message" if expires_at is not None else "legacy_upper_bound",
                          "text": notice_text(count, nonce, version=2,
                                              recipient_state=recipient_state)}
                if expires_at is None:
                    record["legacy_first_seen"] = now
                self._write_atomic(self.path, json.dumps(record, separators=(",", ":")),
                                   exclusive=True)
                return record
        except (OSError, ValueError, PrivateStateError):
            return None

    def _resolution(self, record, now):
        if self._prompt_seen(record):
            return "prompt_seen"
        if record.get("expires_at") is not None and now >= record["expires_at"]:
            return "unresolved_expired"
        return None

    def _release_expired(self, record):
        # Persist the disposition before releasing this exact nonce.
        # This is availability recovery, never queue cancellation.
        receipt = {"thread_id": self.thread_id, "nonce": record["nonce"],
                   "expires_at": record["expires_at"],
                   "expiry_basis": record["expiry_basis"],
                   "outcome": "unresolved_expired", "native_cancellation": "unknown"}
        self._write_atomic(self.expired_path, json.dumps(receipt, separators=(",", ":")))
        self.path.unlink()

    def resolution(self, *, now=None):
        now = time.time() if now is None else now
        if not _timestamp(now):
            return None
        try:
            with self._locked() as taken:
                if not taken:
                    return None
                record = self._current(now)
                self.resolution_basis = record.get("expiry_basis")
                outcome = self._resolution(record, now)
                if outcome == "unresolved_expired":
                    self._release_expired(record)
                return outcome
        except (OSError, ValueError, PrivateStateError):
            return None

    def resolved(self):
        return self.resolution() is not None

    def rollback_unstarted(self, record):
        """Release only this reservation after definite pre-execution failure."""
        try:
            with self._locked() as taken:
                if not taken:
                    return False
                current = self._current()
                if current["nonce"] != record["nonce"] or self._prompt_seen(current):
                    return False
                try:
                    if self._read(self.accepted_path).strip() == record["nonce"]:
                        return False
                except FileNotFoundError:
                    pass
                self.path.unlink()
                return True
        except (OSError, ValueError, PrivateStateError):
            return False

    def accept(self, record):
        """Record CLI acceptance and return whether prompt arrival is proved."""
        try:
            with self._locked() as taken:
                if not taken:
                    return False
                current = self._current()
                if current["nonce"] == record["nonce"]:
                    self._mark(self.accepted_path, record["nonce"])
                    return self._prompt_seen(current)
                # Expiry can also permit a newer reservation. A late result
                # must neither overwrite it nor invent prompt-arrival evidence.
                try:
                    return self._read(self.prompt_seen_path).strip() == record["nonce"]
                except FileNotFoundError:
                    return False
        except (OSError, ValueError, PrivateStateError):
            return False  # The pending reservation still suppresses duplicates.

    def note_prompt(self, payload):
        if (not isinstance(payload, dict) or payload.get("session_id") != self.thread_id
                or not isinstance(payload.get("prompt"), str)):
            return False
        try:
            with self._locked(retry=0.25) as taken:
                if not taken:
                    return False
                record = self._current()
                if payload["prompt"] != record["text"]:
                    return False
                self._mark(self.prompt_seen_path, record["nonce"])
                return True
        except (OSError, ValueError, PrivateStateError):
            return False


def prompt_seen_hook():
    """Record exact prompt-hook arrival with UTF-8; print no prompt or state."""
    try:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        raw = stream.read(65537)
        if len(raw) > 65536:
            return
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        thread_id = thread_id_from_meta({"threadId": payload.get("session_id")})
        if thread_id:
            PendingNotice(default_digest_dir(), thread_id).note_prompt(payload)
    except (OSError, ValueError, AttributeError, TypeError):
        pass
