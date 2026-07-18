"""``pseudolife-mcp briefing`` — print the session-start briefing markdown.

Torch-free and dependency-light: stdlib ``urllib`` only, no MCP handshake. Hits
the already-running daemon's REST ``/api/briefing`` (never auto-starts one;
session-start must stay fast). Prints nothing + exit 0 when the daemon is down,
the bank is cold, or anything goes wrong — a memory briefing must never break a
session.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request


def _as_hook_json(md: str) -> str:
    """Wrap the briefing markdown as a SessionStart hook payload
    (``hookSpecificOutput.additionalContext``). Empty string when there's nothing
    to inject — so the hook adds no context on a cold bank / down daemon."""
    md = (md or "").strip()
    if not md:
        return ""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": md}})


def _fetch_markdown(url: str, token: str | None, max_unsure: int, max_lessons: int,
                    max_world: int = 3) -> str:
    """GET ``/api/briefing`` and return its ``markdown`` field. Plain HTTP — no MCP
    ``initialize`` handshake — so it's fast enough for a per-session hook."""
    qs = urllib.parse.urlencode({"max_unsure": max_unsure, "max_lessons": max_lessons,
                                 "max_world": max_world})
    req = urllib.request.Request(f"{url}/api/briefing?{qs}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data or {}).get("markdown", "") or ""


def run_briefing() -> None:
    from pseudolife_memory.shim import _daemon_url, probe_health  # torch-free helpers

    ap = argparse.ArgumentParser(prog="pseudolife-mcp briefing")
    ap.add_argument("--max-unsure", type=int, default=3,
                    help="cap surprises AND questions at this many EACH (default 3 of each)")
    ap.add_argument("--max-lessons", type=int, default=3)
    ap.add_argument("--max-world", type=int, default=3)
    ap.add_argument("--hook-json", action="store_true",
                    help="emit a Claude Code/Codex SessionStart hook payload "
                         "(hookSpecificOutput.additionalContext) instead of raw markdown")
    args, _ = ap.parse_known_args(sys.argv[2:])  # argv[1] == "briefing"

    url = _daemon_url()
    if probe_health(url) is None:
        return  # daemon down -> inject nothing
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    try:
        md = _fetch_markdown(url, token, args.max_unsure, args.max_lessons, args.max_world)
    except Exception:
        return  # never break session start
    md = (md or "").strip()
    if args.hook_json:
        payload = _as_hook_json(md)
        if payload:
            print(payload)
    elif md:
        print(md)
