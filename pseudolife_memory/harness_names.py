"""The name a harness already shows for a session, read from its own files.

The coordination board shows each address by name. A harness that titles
its sessions keeps that title in a file of its own, and the shim's adapter
sends it with its next heartbeat when it changed (``name_source:
"harness"``):

* **Claude Code** writes the shown title into the session transcript,
  ``<config>/projects/<cwd-encoded>/<session_id>.jsonl`` (``config`` is
  ``CLAUDE_CONFIG_DIR``, else ``~/.claude``), as ``custom-title`` lines
  (the Desktop app's titles and renames), ``agent-name`` lines (the name the
  session goes by in the Desktop app's session list) and ``ai-title`` lines
  (the CLI's generated title). Each repeats through the session and the
  latest of a kind wins; a custom title beats an agent name, which beats a
  generated title (all three re-checked on Claude Code 2.1.287, 2026-10-05).
* **Codex** appends ``{"id", "thread_name", "updated_at"}`` lines to
  ``<CODEX_HOME or ~/.codex>/session_index.jsonl``; the latest line per
  thread id wins.

Transcripts are large (23-53 MB measured 2026-10-02 on the maintainer's
host, a ``custom-title`` roughly every 30 lines and the last one within ~30
lines of the end; one 45 MB transcript held no title at all). So a reader
reads the tail first, which settles the common case at once, then works
forward from a remembered offset and back through the older part in steps
of at most ``max_read`` bytes per poll, and reads nothing when the file's
size and mtime have not changed. A shrunk or replaced file is read afresh.
Every error reads as ``None``: a name is decoration, never a reason to
fail a heartbeat.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path

# The board's bound on a name (``MAX_NAME`` in storage/coordination.py).
MAX_NAME = 120
# Per-poll read budget and the first look at the end of a transcript. A
# heartbeat is ~20 s, so a 45 MB transcript with no title is fully read in
# ~45 polls (~15 min) without any one heartbeat reading more than this.
MAX_READ_BYTES = 1024 * 1024
TAIL_BYTES = 256 * 1024
# A title line is a few hundred bytes. A line longer than this is skipped
# unparsed (tool output lines run to megabytes).
MAX_TITLE_LINE = 64 * 1024
# How often a transcript that is not there yet is looked for again.
GLOB_INTERVAL = 60.0

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Transcript line type -> (kind, field). ``_KINDS`` is their precedence.
_LINE_TYPES = {"custom-title": ("custom", "customTitle"),
               "agent-name": ("agent", "agentName"),
               "ai-title": ("ai", "aiTitle")}
_KINDS = ("custom", "agent", "ai")
_MARKERS = tuple(t.encode() for t in _LINE_TYPES)


def clean_name(value) -> str | None:
    """One line of at most MAX_NAME characters: whitespace and every
    control or format character collapse to single spaces. ``None`` for
    anything that is not a string or is blank."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join("".join(" " if unicodedata.category(c)[0] == "C" else c
                               for c in value).split())
    return cleaned[:MAX_NAME] or None


def _claude_config_dir() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _scan(data: bytes, skip: bool, handle, *, max_line: int) -> tuple[int, bool]:
    """Feed every complete line of ``data`` to ``handle``. Returns the bytes
    consumed (through the last newline, or all of a line too long to keep)
    and whether the next chunk starts inside a line being skipped."""
    pos = 0
    if skip:
        newline = data.find(b"\n")
        if newline < 0:
            return len(data), True
        pos, skip = newline + 1, False
    last = data.rfind(b"\n")
    if last >= pos:
        for line in data[pos:last + 1].split(b"\n"):
            if line and len(line) <= max_line:
                handle(line)
        pos = last + 1
    if len(data) - pos >= max_line:
        # Already longer than any title line: drop it and its remainder.
        return len(data), True
    return pos, skip


def _signature(st) -> tuple:
    return (st.st_size, st.st_mtime_ns, getattr(st, "st_ino", 0))


class ClaudeSessionTitle:
    """The title Claude Code shows for ``session_id``: the latest
    ``customTitle``, else the latest ``agentName``, else the latest
    ``aiTitle``, else ``None``."""

    def __init__(self, session_id, *, config_dir=None, max_read=MAX_READ_BYTES,
                 tail=TAIL_BYTES, glob_interval=GLOB_INTERVAL, clock=time.monotonic):
        self.session_id = (session_id if isinstance(session_id, str)
                           and _SESSION_ID.fullmatch(session_id) else None)
        self._config = Path(config_dir) if config_dir is not None else None
        self.max_read = max_read
        self.tail = min(tail, max_read)
        self._max_line = min(MAX_TITLE_LINE, max_read // 2)
        self._glob_interval = glob_interval
        self._clock = clock
        self._path = None
        self._looked_at = None
        self._lock = threading.Lock()
        self.bytes_read = 0
        self._reset()

    def _reset(self):
        self._signature = None
        self._started = False
        self._offset = 0           # forward position, at a line start unless skipping
        self._skip = False
        self._back = None          # [position, end] of the older part still unread
        self._back_skip = False
        self._newer = {}           # titles from the tail and later
        self._older = {}           # titles from before the tail (backfill)

    def poll(self) -> str | None:
        with self._lock:
            self.bytes_read = 0
            try:
                return self._poll()
            except Exception:  # noqa: BLE001 - a name never fails its caller
                return None

    def _current(self):
        return next((titles[kind] for kind in _KINDS
                     for titles in (self._newer, self._older) if kind in titles), None)

    def _locate(self):
        if self.session_id is None:
            return None
        if self._path is not None:
            if self._path.is_file():
                return self._path
            self._path = None
            self._reset()
        now = self._clock()
        if self._looked_at is not None and now - self._looked_at < self._glob_interval:
            return None
        self._looked_at = now
        config = self._config if self._config is not None else _claude_config_dir()
        found = [p for p in (config / "projects").glob(f"*/{self.session_id}.jsonl")
                 if p.is_file()]
        if not found:
            return None
        self._path = max(found, key=lambda p: p.stat().st_mtime_ns)
        return self._path

    def _sink(self, titles):
        def handle(line: bytes) -> None:
            if not any(marker in line for marker in _MARKERS):
                return
            try:
                record = json.loads(line)
            except ValueError:
                return
            if not isinstance(record, dict):
                return
            kind = _LINE_TYPES.get(record.get("type"))
            if kind is None:
                return
            title = clean_name(record.get(kind[1]))
            if title is not None:
                titles[kind[0]] = title
        return handle

    def _poll(self):
        path = self._locate()
        if path is None:
            return None
        st = path.stat()
        signature = _signature(st)
        if signature == self._signature and self._back is None:
            return self._current()
        size = st.st_size
        if (self._signature is not None and signature[2] != self._signature[2]) or size < self._offset:
            self._reset()
        budget = self.max_read
        with open(path, "rb") as stream:
            if not self._started:
                self._started = True
                if size > self.tail:
                    start = size - self.tail
                    stream.seek(start)
                    data = stream.read(self.tail)
                    self.bytes_read += len(data)
                    budget -= len(data)
                    first = data.find(b"\n")
                    last = data.rfind(b"\n")
                    if first >= 0:
                        _scan(data[first + 1:last + 1], False, self._sink(self._newer),
                              max_line=self._max_line)
                        self._offset = start + last + 1
                    else:
                        self._offset, self._skip = start + len(data), True
                    if "custom" not in self._newer:
                        # Older titles matter only while no custom title is
                        # known: a custom title in the tail is the newest one.
                        self._back = start + first + 1 if first >= 0 else start
                        self._back_skip = first < 0
            if size > self._offset and budget > 0:
                stream.seek(self._offset)
                data = stream.read(min(budget, size - self._offset))
                self.bytes_read += len(data)
                budget -= len(data)
                consumed, self._skip = _scan(data, self._skip, self._sink(self._newer),
                                             max_line=self._max_line)
                caught_up = self._offset + len(data) >= size
                self._offset += consumed
            else:
                caught_up = True
            if "custom" in self._newer:
                self._back = None
            if self._back is not None and budget >= self._max_line:
                self._backfill(stream, budget)
        self._signature = signature if caught_up else None
        return self._current()

    def _backfill(self, stream, budget):
        """Read the part before the tail backwards, one chunk per poll, so
        the first title found of each kind is the newest of the older part
        (no name churn), and stop once a custom title is known."""
        end = self._back
        start = max(0, end - budget)
        stream.seek(start)
        data = stream.read(end - start)
        self.bytes_read += len(data)
        if self._back_skip:
            # The chunk ends inside a line too long to read whole.
            cut = data.rfind(b"\n")
            if cut < 0:
                self._back = start if start > 0 else None
                return
            data, self._back_skip = data[:cut + 1], False
            if start > 0 and data.find(b"\n") == cut:
                # The one line ending here began before this chunk and may
                # be short (a title): read it next poll from its own end.
                self._back = start + cut + 1
                return
        if start > 0:
            first = data.find(b"\n")
            if first < 0 or first == len(data) - 1:
                # No whole line in the chunk: the one ending here started
                # before it and is too long to be a title. Step over it.
                self._back, self._back_skip = start, True
                return
            data, end = data[first + 1:], start + first + 1
        else:
            end = 0
        found = {}
        _scan(data, False, self._sink(found), max_line=self._max_line)
        for kind, title in found.items():
            self._older.setdefault(kind, title)
        self._back = None if end == 0 or "custom" in self._newer or "custom" in self._older else end


class CodexThreadNames:
    """Codex thread names from ``session_index.jsonl``, one reader shared by
    every thread a shim serves (the registry's adapters call it from worker
    threads, so it locks)."""

    def __init__(self, *, codex_home=None, max_read=MAX_READ_BYTES, max_entries=4096):
        self._home = Path(codex_home) if codex_home is not None else None
        self.max_read = max_read
        self._max_line = min(MAX_TITLE_LINE, max_read // 2)
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self.bytes_read = 0
        self._reset()

    def _reset(self):
        self._signature = None
        self._offset = 0
        self._skip = False
        self._names: dict[str, str] = {}

    def name(self, thread_id) -> str | None:
        if not isinstance(thread_id, str):
            return None
        with self._lock:
            self.bytes_read = 0
            try:
                self._refresh()
            except Exception:  # noqa: BLE001 - a name never fails its caller
                return None
            return self._names.get(thread_id.lower())

    def _handle(self, line: bytes) -> None:
        if b"thread_name" not in line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            return
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            return
        name = clean_name(record.get("thread_name"))
        if name is None:
            return
        key = record["id"].lower()
        self._names.pop(key, None)
        self._names[key] = name
        if len(self._names) > self._max_entries:
            self._names.pop(next(iter(self._names)))

    def _refresh(self):
        home = self._home if self._home is not None else _codex_home()
        path = home / "session_index.jsonl"
        try:
            st = path.stat()
        except FileNotFoundError:
            self._reset()
            return
        signature = _signature(st)
        if signature == self._signature:
            return
        if (self._signature is not None and signature[2] != self._signature[2]) or st.st_size < self._offset:
            self._reset()
        with open(path, "rb") as stream:
            stream.seek(self._offset)
            data = stream.read(min(self.max_read, st.st_size - self._offset))
        self.bytes_read = len(data)
        consumed, self._skip = _scan(data, self._skip, self._handle, max_line=self._max_line)
        caught_up = self._offset + len(data) >= st.st_size
        self._offset += consumed
        self._signature = signature if caught_up else None
