"""``pseudolife-mcp briefing`` — print the session-start briefing markdown.

Torch-free and dependency-light: stdlib ``urllib`` only, no MCP handshake. Hits
the already-running daemon's REST ``/api/briefing`` (never auto-starts one;
session-start must stay fast). With ``--hook-json`` it reads
``/api/hook/session-start`` instead — the same compact memory core plus
bounded briefing the plugin hook serves — so a hook wired by the installer
(Claude Code without the plugin, older Codex hooks) starts with the core too.
Prints nothing + exit 0 when the daemon is down, the bank is cold, or anything
goes wrong — a memory briefing must never break a session. ``--coordination``
prints the agent-board check-in from ``/api/hook/coordination-start``
instead, which the daemon serves only where this bearer can use the board.

``pseudolife-mcp prompt-hook`` is the per-turn UserPromptSubmit hook for the
same installs: the plugin's memory-change note (see :func:`run_prompt_hook`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
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
    ``initialize`` handshake — so it's fast enough for a per-session hook. A
    redirect is refused rather than followed, since urllib would carry the
    bearer to its target."""
    from pseudolife_memory.shim import _NoRedirectHandler
    qs = urllib.parse.urlencode({"max_unsure": max_unsure, "max_lessons": max_lessons,
                                 "max_world": max_world})
    req = urllib.request.Request(f"{url}/api/briefing?{qs}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    with opener.open(req, timeout=5) as r:
        data = json.loads(r.read().decode("utf-8"))
    return (data or {}).get("markdown", "") or ""


def _fetch_session_start(url: str, token: str | None) -> str:
    """GET ``/api/hook/session-start`` — the plugin hook's plain-text context:
    the memory core, then (when authorized) the bounded briefing, already
    within the hook's size budget. No ``session_id`` is sent, so this path
    registers no episode. A redirect is refused rather than followed, since
    urllib would carry the bearer to its target."""
    from pseudolife_memory.shim import _NoRedirectHandler
    req = urllib.request.Request(f"{url}/api/hook/session-start")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    with opener.open(req, timeout=5) as r:
        return r.read().decode("utf-8")


def _fetch_checkin(url: str, token: str | None) -> str:
    """GET ``/api/hook/coordination-start``: the agent-board check-in, or
    empty where this bearer cannot use the board (off, unauthenticated, or
    an unlisted principal). A redirect is refused rather than followed, since
    urllib would carry the bearer to its target; two seconds keeps the hook
    inside its five-second budget."""
    from pseudolife_memory.shim import _NoRedirectHandler
    req = urllib.request.Request(f"{url}/api/hook/coordination-start")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    with opener.open(req, timeout=2) as r:
        return r.read().decode("utf-8")


def run_briefing() -> None:
    from pseudolife_memory.shim import _daemon_url, probe_health  # torch-free helpers

    ap = argparse.ArgumentParser(prog="pseudolife-mcp briefing")
    ap.add_argument("--max-unsure", type=int, default=3,
                    help="cap surprises AND questions at this many EACH (default 3 of each)")
    ap.add_argument("--max-lessons", type=int, default=3)
    ap.add_argument("--max-world", type=int, default=3)
    ap.add_argument("--hook-json", action="store_true",
                    help="emit a Claude Code/Codex SessionStart hook payload "
                         "(hookSpecificOutput.additionalContext) carrying the "
                         "memory core and the bounded briefing the plugin hook "
                         "serves; the --max-* caps do not apply")
    ap.add_argument("--coordination", action="store_true",
                    help="print the agent-board check-in instead, only where this "
                         "bearer can use the board")
    args, _ = ap.parse_known_args(sys.argv[2:])  # argv[1] == "briefing"

    if args.coordination:
        # Unset means on by default; any other value that is not a yes is
        # this client's opt-out, as for the shim and the plugin hooks.
        setting = os.environ.get("PSEUDOLIFE_AGENT_COORDINATION", "").strip().lower()
        if setting and setting not in {"1", "true", "yes", "on"}:
            return
    url = _daemon_url()
    if probe_health(url) is None:
        return  # daemon down -> inject nothing
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    try:
        if args.coordination:
            md = _fetch_checkin(url, token)
        elif args.hook_json:
            md = _fetch_session_start(url, token)
        else:
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


# The plugin hooks' shapes (session-start.sh memory-changes, lifecycle.ps1):
# a session id safe to put in a query and a file name, and the daemon's
# cursor, which the hook stores verbatim and sends back as ``since``.
_SESSION_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_CURSOR = re.compile(r"[0-9.]{1,22}")
_MARK_MAX_AGE_S = 30 * 86400


def _fetch_memory_changes(url: str, token: str | None, session_id: str,
                          since: str) -> str:
    """GET ``/api/hook/memory-changes``: the next cursor on line 1, then the
    note when memory changed. One attempt, two seconds: the turn waits on
    it. A redirect is refused rather than followed, since urllib would
    carry the bearer to its target."""
    from pseudolife_memory.shim import _NoRedirectHandler
    query = {"session_id": session_id}
    if since:
        query["since"] = since
    req = urllib.request.Request(
        f"{url}/api/hook/memory-changes?{urllib.parse.urlencode(query)}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    with opener.open(req, timeout=2) as r:
        return r.read().decode("utf-8")


def _prompt_hook(raw: bytes) -> None:
    """Print this turn's note, if any, then advance the cursor. A turn that
    prints nothing because the request failed keeps the cursor, so the next
    turn asks for the same window."""
    try:
        # Hook payloads are UTF-8 JSON; the console code page is not.
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return
    from pseudolife_memory.credentials import CredentialProvider
    from pseudolife_memory.shim import _daemon_url

    mark_dir = os.environ.get("PSEUDOLIFE_DIGEST_DIR") or os.path.join(
        os.path.expanduser("~"), ".pseudolife-mcp", "digests")
    mark = os.path.join(
        mark_dir, hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".mark")
    since = ""
    if os.path.isfile(mark) and not os.path.islink(mark):
        try:
            with open(mark, encoding="ascii") as f:
                since = f.readline().strip()
        except (OSError, ValueError):
            since = ""
        if not _CURSOR.fullmatch(since):
            since = ""
    token = CredentialProvider.from_environment().snapshot().token
    body = _fetch_memory_changes(_daemon_url(), token, session_id, since)
    cursor, _, note = body.partition("\n")
    cursor = cursor.rstrip("\r")
    if not _CURSOR.fullmatch(cursor):
        return
    note = note.rstrip("\r\n")
    os.makedirs(mark_dir, mode=0o700, exist_ok=True)
    if os.path.islink(mark):
        return
    if not os.path.exists(mark):
        # A session's first note: marks of sessions gone a month go too.
        cutoff = time.time() - _MARK_MAX_AGE_S
        for name in os.listdir(mark_dir):
            old = os.path.join(mark_dir, name)
            try:
                if (name.endswith(".mark") and os.path.isfile(old)
                        and not os.path.islink(old) and os.path.getmtime(old) < cutoff):
                    os.remove(old)
            except OSError:
                pass
    # Open the cursor for writing before printing: one that cannot be saved
    # would repeat the same note every turn, so a refusal stays silent.
    fd = os.open(mark, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
        if note:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": note}}), flush=True)
        f.truncate(0)
        f.write(cursor + "\n")


def run_prompt_hook() -> None:
    """``pseudolife-mcp prompt-hook``: the plugin's per-turn memory-change
    note for installs without the plugin (the Claude Code settings.json and
    Codex hooks.json hooks ``ops/install-hook.*`` write). Reads the hook
    payload on stdin and prints a UserPromptSubmit hook payload only when
    new lessons or other sessions' status notes landed since this session's
    last note. The cursor is the plugin hooks' own file,
    ``<PSEUDOLIFE_DIGEST_DIR or ~/.pseudolife-mcp/digests>/<sha256(session
    id)>.mark``. Silent, exit 0, on every failure: a memory hook must never
    break a turn, and the SessionStart briefing reports a down daemon or a
    bad credential."""
    try:
        _prompt_hook(sys.stdin.buffer.read())
    except BaseException:  # noqa: BLE001 — SystemExit from a bad URL included
        pass
