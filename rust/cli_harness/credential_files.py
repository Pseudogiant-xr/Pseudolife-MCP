"""Credential fixture files and verdicts for the transport rows, package-free.

The rows seed token files as the client's own writer leaves them and record,
per file an arm leaves, the verdicts the frozen client gave: whether a file
is owner-only on Windows, and whether the shim would read a token file
(``client_config.check_token_file``). The checks are the frozen client's own
code, copied verbatim into ``_credential_checks``; this module only names
the calls the rows make.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import _credential_checks as checks

# Direct and isolated CLI launches put only <repo>/rust on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from rust.harness_support.private_fixture import write_owner_only
finally:
    sys.path.pop(0)

# client_config.BOM: a UTF-8 byte-order mark the shim would send as the
# start of the bearer.
BOM = "﻿"


def write_token_file(path: Path, token: str) -> None:
    """Create or replace ``path`` holding ``token`` as an owner-only file,
    the way the client's token writer (``credentials._write_token_file``)
    leaves one: the token checked first, parents created with the default
    mode, the file 0600 or owner-only on Windows, replaced atomically."""
    data = token.encode("utf-8")
    checks._decode_token(data)
    write_owner_only(Path(os.path.abspath(Path(path).expanduser())), data)


def windows_owner_only(path: Path) -> bool:
    """The frozen client's Windows owner-only test of ``path``."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return checks._windows_owner_only(fd)
    finally:
        os.close(fd)


def token_file_status(path: Path) -> str:
    """``ready`` or ``failed``: ``client_config.check_token_file``'s status,
    which reads the file as the shim does (``CredentialProvider.snapshot``)
    and refuses a token that starts with a byte-order mark."""
    try:
        token, _ = checks._open_token(Path(os.path.abspath(Path(path).expanduser())))
    except checks.CredentialError:
        return "failed"
    return "failed" if token.startswith(BOM) else "ready"
