"""One line saying whether the agent board is on for a bearer, and why not.

The installers' final ladder and ``pseudolife-mcp doctor`` print it. The
answer is the daemon's own: ``GET /api/hook/coordination-start`` serves the
check-in only where this bearer can use the board, and its ``X-PL-Board``
header names the gate that refused it. Standard library only, so the
installers can run it from a checkout before the shim is installed.
"""
from __future__ import annotations

import urllib.error
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
                             "coordination.allowed_principals; list it there, or invite "
                             "this machine with `pseudolife-mcp invite <machine>` on the "
                             "daemon host",
    "coordination_requires_postgres": "the board needs the PostgreSQL bank",
    "principals_unavailable": "the daemon cannot check invited machines' tokens right now (its "
                              "database is not answering); try again shortly",
}


def board_status(url: str, token: str | None, *, timeout: float = 2.0) -> tuple[bool, str]:
    """``(on, line)`` for ``token`` against the daemon at ``url``.

    The line never carries the token or the daemon's transport errors."""
    result = board_probe(url, token, timeout=timeout)
    return result["state"] == "on", result["line"]


def board_probe(url: str, token: str | None, *, timeout: float = 2.0) -> dict:
    """Read-only admission probe with a bounded, credential-free state code."""
    if not token:
        return {"state": "missing_bearer", "line":
                "off - no bearer token, so the daemon has no principal to admit (open-loopback install)"}
    request = urllib.request.Request(url.rstrip("/") + "/api/hook/coordination-start")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout) as response:
            header = (response.headers.get("X-PL-Board") or "").strip()
            served = bool(response.read(65536).strip())
    except urllib.error.HTTPError as exc:
        state = "unauthorized" if exc.code == 401 else "unsupported_capability" if exc.code in (404, 405) else "transport_refused"
        return {"state": state, "line": f"off - board probe refused (HTTP {exc.code})"}
    except Exception:  # noqa: BLE001 - any failure means no board, reported plainly
        return {"state": "unreachable", "line": "off - daemon unreachable"}
    if header == "on" or (not header and served):
        return {"state": "on", "line": "on - token present, principal allowed"}
    reason = header.partition("reason=")[2].strip()
    if reason in _REASONS:
        return {"state": reason, "line": f"off - {_REASONS[reason]}"}
    if reason:
        return {"state": "refused", "line": "off - the daemon refused the board (unrecognized reason)"}
    return {"state": "unsupported_capability", "line":
            "off - the daemon does not serve the board to this token; update the daemon to see why"}
