"""Find an external program on PATH without ever looking in the current directory.

``shutil.which`` on Windows (CPython 3.11) searches the current directory
before PATH, and on every OS it joins a relative or empty PATH entry onto
the current directory. A command run from a checkout or a download folder
would then start a ``tailscale.cmd`` or ``git.exe`` planted there instead of
the real program. :func:`find_program` only ever returns a file inside an
absolute PATH directory:

- ``PATH`` is split on ``os.pathsep``; each entry has surrounding whitespace
  and then double quotes removed (Windows lets an entry be quoted). An
  entry that is empty or not absolute (``.``, ``bin``, ``C:bin`` and
  ``\\bin`` on Windows) is skipped. An unset ``PATH`` searches nothing.
- On Windows the names tried are ``name`` with each ``PATHEXT`` extension
  added, in ``PATHEXT``'s order and spelling (``.COM;.EXE;.BAT;.CMD`` when
  it is unset or empty), keeping only those ``CreateProcess`` can start:
  ``.com``, ``.exe``, ``.bat`` and ``.cmd``. A ``name`` that already ends
  in one of those four is tried as it is and nothing is added.
- A name that already holds a path separator is not searched for.
- A candidate counts when it is a regular file (after links) and, outside
  Windows, executable for this user.

Standard library only: ``expose_cli`` and the shim runtime import it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
# What CreateProcess can start directly; PATHEXT may list script types too.
WINDOWS_LAUNCHABLE = (".com", ".exe", ".bat", ".cmd")


def path_directories(environ: Mapping[str, str] | None = None) -> list[str]:
    """The absolute directories of ``PATH``, in order (see the module doc)."""
    environ = os.environ if environ is None else environ
    found = []
    for entry in (environ.get("PATH") or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        # Path, not os.path.isabs: on Windows "\dir" is relative to the
        # current drive, and os.path.isabs accepts it.
        if entry and Path(entry).is_absolute():
            found.append(entry)
    return found


def program_names(name: str, environ: Mapping[str, str] | None = None) -> list[str]:
    """The file names ``name`` may have on this OS."""
    if os.name != "nt":
        return [name]
    # A name that already carries an extension CreateProcess starts
    # (``python.exe``) is that one file, as ``shutil.which`` reads it.
    if name.lower().endswith(WINDOWS_LAUNCHABLE):
        return [name]
    environ = os.environ if environ is None else environ
    pathext = environ.get("PATHEXT") or DEFAULT_PATHEXT
    return [f"{name}{ext}" for ext in pathext.split(os.pathsep)
            if ext.lower() in WINDOWS_LAUNCHABLE]


def is_program(path: str | os.PathLike) -> bool:
    """A regular file this user may run."""
    return os.path.isfile(path) and (os.name == "nt" or os.access(path, os.X_OK))


def find_program(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """``name`` in the first absolute ``PATH`` directory holding it, as
    ``os.path.join(directory, file name)``; ``None`` when there is none.
    A ``name`` holding a path separator is a path the caller chose (a
    configured ``PSEUDOLIFE_DOCKER``): it is returned as given when it is a
    program, and no directory is searched."""
    if not name:
        return None
    if os.sep in name or (os.altsep and os.altsep in name):
        return name if is_program(name) else None
    names = program_names(name, environ)
    for directory in path_directories(environ):
        for file_name in names:
            candidate = os.path.join(directory, file_name)
            if is_program(candidate):
                return candidate
    return None
