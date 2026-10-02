"""Private queue correlation; a mailbox read never consumes a queued notice."""
from __future__ import annotations

from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

from .codex_coordination import _prepare_private_dir, thread_id_from_meta
from .coordination_identity import default_digest_dir, digest_path_for
from .os_lock import _try_lock, _unlock
from .private_state import PrivateStateError, _private_fd, open_private


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

    @contextmanager
    def _locked(self):
        _prepare_private_dir(self.path.parent, self.path.parent)
        fd = open_private(self.path.with_suffix(".bell-lock"), os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, "r+b") as handle:
            if os.fstat(fd).st_size == 0:
                handle.write(b"0")
                handle.flush()
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

    def _current(self):
        from .codex_doorbell import doorbell_text

        record = json.loads(self._read(self.path))
        if (not isinstance(record, dict) or record.get("thread_id") != self.thread_id
                or type(record.get("count")) is not int or record["count"] < 1
                or not isinstance(record.get("nonce"), str)
                or re.fullmatch("[0-9a-f]{32}", record["nonce"]) is None
                or record.get("text") != doorbell_text(record["count"], record["nonce"])):
            raise ValueError("invalid doorbell pending record")
        return record

    def _prompt_seen(self, record):
        try:
            return self._read(self.prompt_seen_path).strip() == record["nonce"]
        except FileNotFoundError:
            return False

    def _mark(self, path, nonce):
        if os.path.lexists(path):
            self._read(path)  # Never tighten or replace a foreign/private-invalid marker.
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".bell-")
        try:
            try:
                _private_fd(fd, Path(temporary), PrivateStateError)
            except BaseException:
                os.close(fd)
                raise
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(nonce + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(OSError):
                os.unlink(temporary)

    def reserve(self, count):
        from .codex_doorbell import doorbell_text

        try:
            with self._locked() as taken:
                if not taken:
                    return None
                if self.path.exists():
                    previous = self._current()
                    if not self._prompt_seen(previous):
                        return None
                    self.path.unlink()
                nonce = uuid.uuid4().hex
                record = {"thread_id": self.thread_id, "nonce": nonce, "count": count,
                          "text": doorbell_text(count, nonce)}
                fd = open_private(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(record, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                return record
        except (OSError, ValueError, PrivateStateError):
            return None  # Missing or malformed proof never authorizes a second queue.

    def resolved(self):
        try:
            with self._locked() as taken:
                return taken and self._prompt_seen(self._current())
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
                # A newer valid reservation required the previous exact hook
                # receipt. A late CLI result must not overwrite its state.
                return True
        except (OSError, ValueError, PrivateStateError):
            return False  # The pending reservation still suppresses duplicates.

    def note_prompt(self, payload):
        if (not isinstance(payload, dict) or payload.get("session_id") != self.thread_id
                or not isinstance(payload.get("prompt"), str)):
            return False
        try:
            with self._locked() as taken:
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
