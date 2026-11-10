"""Database identifiers allocated by one differential harness process."""

from __future__ import annotations

import hashlib
import os
import re
import secrets


# Set by the forced runner (rust/daemon/harness/mirror/forced.py) to one
# attempt's token: it starts every suffix this process allocates, so a sweep
# scoped to that attempt drops its databases and no other worker's (--jobs).
# The rest of the suffix stays random, so two processes never share a name.
ATTEMPT_ENV = "PL_HARNESS_ATTEMPT"
_ATTEMPT = re.compile(r"[0-9a-f]{8}")


class RunNames:
    def __init__(self):
        self._pid = os.getpid()
        attempt = os.environ.get(ATTEMPT_ENV, "")
        lead = attempt if _ATTEMPT.fullmatch(attempt) else secrets.token_hex(4)
        self._suffix = lead + secrets.token_hex(4)
        self.labels: dict[str, str] = {}

    def require_process(self) -> None:
        """Inherited identifiers and fixture caches cannot be reused after fork."""
        if self._pid != os.getpid():
            raise RuntimeError("harness database state cannot be used after fork: "
                               "start a fresh harness process")

    def name(self, label: str, *, max_length: int) -> str:
        self.require_process()
        for allocated, original in self.labels.items():
            if original == label:
                return allocated
        stem = label
        room = max_length - len(self._suffix) - 1
        if len(stem) > room:
            digest = hashlib.sha256(label.encode()).hexdigest()[:6]
            stem = stem[:room - 7] + "_" + digest
        allocated = stem + "_" + self._suffix
        if allocated in self.labels:
            raise ValueError("disposable database label collision")
        self.labels[allocated] = label
        return allocated

    def owns(self, name: str) -> bool:
        self.require_process()
        return name in self.labels

    def require_owned(self, name: str) -> None:
        if not self.owns(name):
            raise ValueError(f"database is not owned by this harness run: {name!r}")

    def normalize(self, text: str) -> str:
        self.require_process()
        if not self.labels:
            return text
        pattern = r"(?<!\w)(?:" + "|".join(
            re.escape(name) for name in sorted(self.labels, key=len, reverse=True)) + r")(?!\w)"
        return re.sub(pattern, lambda match: self.labels[match[0]], text)
