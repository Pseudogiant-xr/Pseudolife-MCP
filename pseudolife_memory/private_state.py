"""Open a private, owner-only regular file: the one primitive every state
and credential file shares.

Standard library only, on purpose. ``ops/setup-codex-hooks.py`` runs under
whatever ``python3`` the installer finds, and a client-only machine has a
bare interpreter: importing this through the coordination adapter pulled in
anyio and httpx, which failed the Codex step of the 2026-09-28 client-only
dogfood with a swallowed ModuleNotFoundError. The adapter re-exports
``_open_state`` with its own error type; scripts import ``open_private``
from here.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat


class PrivateStateError(RuntimeError):
    """A file that must be private and owned by the current user is not."""


def _private_fd(fd: int, path: Path, error: type[Exception]) -> None:
    if os.name != "nt":
        os.fchmod(fd, 0o600)
        return
    from .credentials import CredentialError, _secure_windows_file

    try:
        _secure_windows_file(path)
    except CredentialError as exc:
        raise error("cannot secure adapter state") from exc


def open_private(path: Path, flags: int,
                 error: type[Exception] = PrivateStateError) -> int:
    """``os.open`` ``path`` with ``flags`` and prove it is a private regular
    file owned by the caller; raise ``error`` otherwise. A file created here
    is secured (mode 0600 or an owner-only ACL); an existing file is only
    checked, since tightening it cannot prove who wrote it before."""
    if path.is_symlink():
        raise error("adapter state must be a private regular file")
    open_flags = flags | getattr(os, "O_NOFOLLOW", 0)
    created = False
    if flags & os.O_CREAT:
        try:
            fd = os.open(path, open_flags | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            if flags & os.O_EXCL:
                raise
            fd = os.open(path, open_flags & ~os.O_CREAT, 0o600)
    else:
        fd = os.open(path, open_flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise error("adapter state must be a private regular file")
        # Only newly created files may be secured here. Tightening an existing
        # record's ACL cannot prove who created or previously modified it.
        if created:
            _private_fd(fd, path, error)
        if os.name == "nt":
            from .credentials import _windows_owner_only

            private = _windows_owner_only(fd)
        else:
            private = info.st_uid == os.geteuid() and not (info.st_mode & 0o077)
        if not private:
            raise error("adapter state must be private and owned by the current user")
        return fd
    except BaseException:
        os.close(fd)
        raise
