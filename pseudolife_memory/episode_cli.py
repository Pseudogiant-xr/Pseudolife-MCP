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


def _git_project_name(cwd: str | None) -> str | None:
    """Walk up from ``cwd`` to the nearest git repo root and return its
    directory name — the *project* the session is in (robust to running from
    a subdirectory). ``None`` when ``cwd`` is not inside a repo."""
    if not cwd:
        return None
    try:
        path = os.path.abspath(cwd)
    except Exception:
        return None
    prev = ""
    while path and path != prev:
        if os.path.isdir(os.path.join(path, ".git")):
            return os.path.basename(path) or None
        prev, path = path, os.path.dirname(path)
    return None


def _title_from_cwd(cwd: str | None) -> str:
    """A stable, human title for a session episode: the project (git repo
    root) name when discoverable, else the working-dir basename, else
    ``session``. Never titles a session after the home directory — some
    SessionStart fires arrive with ``cwd`` set to home, which produced noisy
    ``<user> - <date>`` titles."""
    name = _git_project_name(cwd)
    if not name and cwd:
        norm = os.path.normpath(cwd)
        try:
            is_home = os.path.abspath(norm) == os.path.abspath(os.path.expanduser("~"))
        except Exception:
            is_home = False
        if not is_home:
            name = os.path.basename(norm) or None
    return f"{name or 'session'} - {time.strftime('%Y-%m-%d')}"


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
