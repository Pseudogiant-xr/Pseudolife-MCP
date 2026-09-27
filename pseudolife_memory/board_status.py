"""One line saying whether the agent board is on for a bearer, and why not.

The installers' final ladder and ``pseudolife-mcp doctor`` print it. The
answer is the daemon's own: ``GET /api/hook/coordination-start`` serves the
check-in only where this bearer can use the board, and its ``X-PL-Board``
header names the gate that refused it. Standard library only, so the
installers can run it from a checkout before the shim is installed.
"""
from __future__ import annotations

import re
import urllib.request

from pseudolife_memory.daemon_url import _NoRedirectHandler

_REASONS = {
    "disabled": "coordination is disabled in the daemon's config.yaml "
                "(coordination.enabled)",
    "authentication_required": "the daemon has no bearer token configured; set "
                               "PSEUDOLIFE_MCP_TOKEN in ops/.env and redeploy",
    "unauthorized": "the daemon rejected this token; re-run the installer so the "
                    "client token file matches ops/.env",
    "principal_not_allowed": "this token's principal is not in "
                             "coordination.allowed_principals",
    "coordination_requires_postgres": "the board needs the PostgreSQL bank",
}


def board_status(url: str, token: str | None, *, timeout: float = 2.0) -> tuple[bool, str]:
    """``(on, line)`` for ``token`` against the daemon at ``url``.

    The line never carries the token or the daemon's transport errors."""
    if not token:
        return False, ("off - no bearer token, so the daemon has no principal to "
                       "admit (open-loopback install)")
    request = urllib.request.Request(url.rstrip("/") + "/api/hook/coordination-start")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            header = (response.headers.get("X-PL-Board") or "").strip()
            served = bool(response.read().strip())
    except Exception:  # noqa: BLE001 - any failure means no board, reported plainly
        return False, "off - daemon unreachable"
    if header == "on" or (not header and served):
        return True, "on - token present, principal allowed"
    reason = header.partition("reason=")[2].strip()
    if reason in _REASONS:
        return False, f"off - {_REASONS[reason]}"
    if re.fullmatch(r"[a-z_]{1,64}", reason):  # never echo arbitrary header text
        return False, f"off - the daemon refused the board ({reason})"
    return False, ("off - the daemon does not serve the board to this token; "
                   "update the daemon to see why")
