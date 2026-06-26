"""``pseudolife-mcp briefing`` — print the session-start briefing markdown.

Torch-free client. Connects to an ALREADY-RUNNING daemon (never auto-starts;
session-start must stay fast) and prints the briefing markdown. Prints nothing +
exit 0 when the daemon is down, the bank is cold, or anything goes wrong — a
memory briefing must never break a session.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _extract_markdown(result) -> str:
    """Pull the ``markdown`` field from an mcp ClientSession.call_tool result."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("markdown", "") or ""
    content = getattr(result, "content", None) or []
    texts = [getattr(c, "text", "") for c in content]
    try:
        return (json.loads("".join(texts)) or {}).get("markdown", "") or ""
    except Exception:
        return ""


def _as_hook_json(md: str) -> str:
    """Wrap the briefing markdown as a Claude Code SessionStart hook payload
    (``hookSpecificOutput.additionalContext``). Empty string when there's nothing
    to inject — so the hook adds no context on a cold bank / down daemon."""
    md = (md or "").strip()
    if not md:
        return ""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": md}})


async def _fetch(url: str, token: str | None, max_unsure: int, max_lessons: int) -> str:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with streamablehttp_client(url + "/mcp", headers=headers) as (read, write, _sid):
        async with ClientSession(read, write) as remote:
            await remote.initialize()
            result = await remote.call_tool(
                "memory_briefing",
                {"max_unsure": max_unsure, "max_lessons": max_lessons},
            )
    return _extract_markdown(result)


def run_briefing() -> None:
    import asyncio

    from pseudolife_memory.shim import _daemon_url, probe_health  # torch-free helpers

    ap = argparse.ArgumentParser(prog="pseudolife-mcp briefing")
    ap.add_argument("--max-unsure", type=int, default=3)
    ap.add_argument("--max-lessons", type=int, default=3)
    ap.add_argument("--hook-json", action="store_true",
                    help="emit a Claude Code SessionStart hook payload "
                         "(hookSpecificOutput.additionalContext) instead of raw markdown")
    args, _ = ap.parse_known_args(sys.argv[2:])  # argv[1] == "briefing"

    url = _daemon_url()
    if probe_health(url) is None:
        return  # daemon down -> inject nothing
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    try:
        md = asyncio.run(_fetch(url, token, args.max_unsure, args.max_lessons))
    except Exception:
        return  # never break session start
    md = (md or "").strip()
    if args.hook_json:
        payload = _as_hook_json(md)
        if payload:
            print(payload)
    elif md:
        print(md)
