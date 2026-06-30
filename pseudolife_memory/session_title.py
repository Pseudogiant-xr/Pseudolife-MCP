"""Shared session-episode title derivation.

Torch-free (stdlib only) so both the stdio shim and the episode CLI can import
it without pulling in the heavy service stack. The title is
``"{project} - {YYYY-MM-DD HH:MM}"`` — the minute-resolution stamp keeps
same-day, same-project sessions distinguishable (the old date-only title
collided for every session in a day).
"""
from __future__ import annotations

import os
import time


def git_project_name(cwd: str | None) -> str | None:
    """Nearest git-repo-root basename walking up from ``cwd``; ``None`` when
    ``cwd`` is not inside a repo. Robust to being run from a subdirectory."""
    if not cwd:
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
            name = os.path.basename(norm) or None
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    return f"{name or 'session'} - {stamp}"
