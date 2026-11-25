"""Client credential fixtures for the installer tests, free of the frozen Python package.

``write_token_file`` seeds a token file as the client's token writer leaves
one, and ``read_token`` reads one back as the client reads its configured
file, refusing a file that is not private or holds no valid token: both are
the package-free ones of the transport rows (``rust/cli_harness/credential_files.py``,
whose checks are the frozen client's own code). ``_is_loopback_url`` and
``normalize_pairing_code`` are the frozen client's rules, copied, which the
native helper stand-in (``tests/native_helper_standin.py``) answers the
installers' ``is-loopback`` and ``pairing-code-valid`` steps with; the tests
compare the installers' answers with these.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import urllib.parse

from rust.cli_harness import _credential_checks as _checks
from rust.cli_harness.credential_files import write_token_file

__all__ = ["write_token_file", "read_token", "_is_loopback_url", "normalize_pairing_code"]


def read_token(path: str | Path) -> str:
    """The bearer token the client reads from ``path`` (as
    ``CredentialProvider(path=path).snapshot().token``); raises when the file
    is missing, not a private regular file, or holds no valid token."""
    token, _generation = _checks._open_token(Path(os.path.abspath(Path(path).expanduser())))
    return token


# The frozen client's daemon_url._is_loopback_url, copied.
def _is_loopback_url(url: str) -> bool:
    """True when ``url`` (already validated) names this machine: ``localhost``
    or a loopback address. Anything else is a daemon on ANOTHER machine (a
    tailnet or LAN address, a reverse-proxy name), which no process started
    here could ever be."""
    hostname = urllib.parse.urlsplit(url).hostname or ""
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return address.is_loopback or bool(mapped and mapped.is_loopback)


# The frozen client's principals.normalize_pairing_code and its constants, copied.
PAIRING_CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PAIRING_CODE_LENGTH = 12
_CODE_LOOKALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})


def normalize_pairing_code(text) -> str | None:
    """The 12-character canonical form of a typed pairing code, or ``None``
    when it is not one. Case, dashes and surrounding whitespace are ignored,
    and ``O`` reads as ``0``, ``I`` and ``L`` as ``1``."""
    if not isinstance(text, str):
        return None
    code = text.strip().upper().replace("-", "").translate(_CODE_LOOKALIKES)
    if len(code) != PAIRING_CODE_LENGTH or any(c not in PAIRING_CODE_ALPHABET for c in code):
        return None
    return code
