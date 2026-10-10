"""Database identifiers allocated by one differential harness process."""

from __future__ import annotations

import hashlib
import os
import re
import secrets


class RunNames:
    def __init__(self):
        self._pid = os.getpid()
        self._suffix = secrets.token_hex(8)
        self.labels: dict[str, str] = {}

    def _after_fork(self) -> None:
        if self._pid != os.getpid():
            self.__init__()

    def name(self, label: str, *, max_length: int) -> str:
        self._after_fork()
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
        self._after_fork()
        return name in self.labels

    def require_owned(self, name: str) -> None:
        if not self.owns(name):
            raise ValueError(f"database is not owned by this harness run: {name!r}")

    def normalize(self, text: str) -> str:
        self._after_fork()
        if not self.labels:
            return text
        pattern = r"(?<!\w)(?:" + "|".join(
            re.escape(name) for name in sorted(self.labels, key=len, reverse=True)) + r")(?!\w)"
        return re.sub(pattern, lambda match: self.labels[match[0]], text)
