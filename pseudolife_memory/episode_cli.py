"""``pseudolife-mcp episode-start`` / ``episode-end`` — hook-driven episode
lifecycle. Torch-free (stdlib ``urllib`` only); hits the already-running daemon
and NEVER auto-starts one. Prints nothing and returns on any error / cold daemon
so a SessionStart / SessionEnd hook can never break or slow a session.

Reads Claude Code's hook stdin JSON for ``session_id`` (→ ``session_key``, the
idempotency key) and ``cwd`` (→ a human episode title).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

from pseudolife_memory.shim import _daemon_url, probe_health  # torch-free


def _read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _title_from_cwd(cwd: str | None) -> str:
    base = os.path.basename(os.path.normpath(cwd)) if cwd else "session"
    return f"{base or 'session'} - {time.strftime('%Y-%m-%d')}"


def _post(url: str, token: str | None, path: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{url}{path}", data=data, method="POST")
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        r.read()


def run_episode(mode: str, stdin_text: str | None = None) -> None:
    """``mode`` is ``episode-start`` or ``episode-end``. ``stdin_text`` is for
    tests; production reads real stdin."""
    if stdin_text is not None:
        try:
            payload_in = json.loads(stdin_text)
        except Exception:
            payload_in = {}
    else:
        payload_in = _read_stdin()

    session_key = str(payload_in.get("session_id") or "") or None
    if session_key is None:
        return  # no key -> nothing safe to do; stay silent

    url = _daemon_url()
    if probe_health(url) is None:
        return  # daemon down -> inject nothing
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None

    try:
        if mode == "episode-start":
            _post(url, token, "/api/episode/start", {
                "session_key": session_key,
                "title": _title_from_cwd(payload_in.get("cwd")),
            })
        elif mode == "episode-end":
            _post(url, token, "/api/episode/end", {"session_key": session_key})
    except Exception:
        return  # never break session start/stop
