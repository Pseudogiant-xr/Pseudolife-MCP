"""Shared session-episode title derivation.

Torch-free (stdlib only) so both the stdio shim and the episode CLI can import
it without pulling in the heavy service stack. The title is
``"{project} - {YYYY-MM-DD HH:MM}"`` — the minute-resolution stamp keeps
same-day, same-project sessions distinguishable (the old date-only title
collided for every session in a day).
"""
from __future__ import annotations

import ntpath
import os
import re
import time

# Directories that are never a "project" — a GUI client (e.g. Claude Desktop)
# launches the shim with cwd set to one of these, which must not become a title.
_NON_PROJECT_DIRS = {"system32", "syswow64", "windows", "system", "system64"}

# The daemon's lazy-open fallback title. Anything still matching this at
# session close was never named by an agent/shim and is fair game for the
# derived-title pass.
GENERIC_TITLE_RE = re.compile(r"^session - \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

# Routing sources that carry progress noise rather than project identity —
# they only win the dominant-source vote when nothing else is present.
_NOISE_SOURCES = {"status", "log", "claude", "conversation"}

_SNIPPET_CHARS = 60


def derive_session_title(
    started_at: float, entries: list[tuple[float, str, str]],
) -> str | None:
    """A content title for a generic-named session episode at close time:
    ``"{dominant_source} - {start stamp}: {first-entry snippet}"``.

    ``entries`` is ``[(timestamp, source, text), ...]`` for the episode's
    subtree. The dominant source is an honest project signal because sources
    are stable per project by convention; noise sources (``status``/``log``/
    default client tags) only win when they're all there is. Returns ``None``
    when there are no entries (the caller prunes empty episodes anyway).
    """
    if not entries:
        return None
    counts: dict[str, int] = {}
    for _ts, source, _text in entries:
        s = (source or "").strip() or "session"
        counts[s] = counts.get(s, 0) + 1
    signal = {s: n for s, n in counts.items() if s not in _NOISE_SOURCES}
    pool = signal or counts
    src = max(pool, key=lambda s: pool[s])
    first_text = min(entries, key=lambda e: e[0])[2] or ""
    snippet = " ".join(first_text.split())
    if len(snippet) > _SNIPPET_CHARS:
        cut = snippet.rfind(" ", 0, _SNIPPET_CHARS)
        snippet = snippet[:cut if cut > 20 else _SNIPPET_CHARS].rstrip() + "…"
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(started_at))
    return f"{src} - {stamp}: {snippet}" if snippet else f"{src} - {stamp}"

_WINDOWSY = re.compile(r"^[A-Za-z]:[\\/]|\\")


def _foreign_windows_path(cwd: str) -> bool:
    """A Windows-style path seen on a POSIX host (the Linux daemon handed a
    Windows client's cwd): os.path parses it as ONE relative segment, so
    abspath anchors it under the daemon's own cwd and a git walk can "find"
    the daemon's repo — titling the session after the wrong project."""
    return os.name != "nt" and bool(_WINDOWSY.search(cwd))


def git_project_name(cwd: str | None) -> str | None:
    """Nearest git-repo-root basename walking up from ``cwd``; ``None`` when
    ``cwd`` is not inside a repo. Robust to being run from a subdirectory."""
    if not cwd or _foreign_windows_path(cwd):
        return None
    try:
        path = os.path.abspath(cwd)
    except Exception:  # noqa: BLE001
        return None
    prev = ""
    while path and path != prev:
        if os.path.isdir(os.path.join(path, ".git")):
            return os.path.basename(path) or None
        prev, path = path, os.path.dirname(path)
    return None


def title_from_cwd(cwd: str | None, now: float | None = None) -> str:
    """A stable, human title: the project (git repo root) name when
    discoverable, else the working-dir basename, else ``session`` — followed by
    a ``YYYY-MM-DD HH:MM`` stamp. Never titles a session after the home
    directory (some session starts arrive with ``cwd`` set to home, which
    produced noisy ``<user> - <date>`` titles)."""
    name = git_project_name(cwd)
    if not name and cwd:
        norm = os.path.normpath(cwd)
        try:
            is_home = os.path.abspath(norm) == os.path.abspath(os.path.expanduser("~"))
        except Exception:  # noqa: BLE001
            is_home = False
        if not is_home:
            # ntpath.basename splits on BOTH separators, so a Windows-style
            # cwd still yields "System32" (not the whole path) on POSIX.
            name = ntpath.basename(norm) or None
            if name and name.lower() in _NON_PROJECT_DIRS:
                name = None  # a system dir is not a project
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    return f"{name or 'session'} - {stamp}"
