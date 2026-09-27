"""The daemon origin and the redirect-refusing opener, standard library only.

The shim re-exports these under the same names. They live apart because
``pseudolife-mcp prompt-hook`` runs on every user turn and needs nothing
else from the shim, whose import pulls in httpx (and rich through it).
Measured 2026-09-27 on the maintainer's Windows host (Python 3.11, stub
daemon on loopback answering a bare cursor, runs of 10 spawns): the hook's
median was 266-275 ms a turn importing the shim (four runs) and 132-136 ms
importing this module instead (three runs). Keep this module free of
third-party imports; tests/test_memory_changes_hook.py guards the hook's
imports.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8765"


def _daemon_url() -> str:
    return _validated_daemon_url(
        os.environ.get("PSEUDOLIFE_MCP_DAEMON_URL", DEFAULT_URL))


def _validated_daemon_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        valid_port = parsed.port
    except (TypeError, ValueError):
        parsed = None
        valid_port = None
    valid = bool(
        parsed is not None
        and parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        and not any(character.isspace() or ord(character) < 0x20
                    for character in value)
    )
    if not valid:
        print("[shim] invalid PSEUDOLIFE_MCP_DAEMON_URL; use an http(s) origin "
              "without credentials, a path, query, or fragment.", file=sys.stderr)
        raise SystemExit(1)
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.rstrip("/"), "", "", ""))


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
