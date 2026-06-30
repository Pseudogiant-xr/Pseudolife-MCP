"""``writer_context`` HTTP header resolution.

The stamping/session id must be STABLE per session. The shim mints
``X-PL-Session`` and rides it on every call; the transport's
``mcp-session-id`` is per-call (fresh connection per call) so it is only a
fallback for older clients.
"""
from __future__ import annotations

from pseudolife_memory import writer_context as wc


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _Ctx:
    """Stands in for mcp's RequestContext — only ``.request`` is read."""

    def __init__(self, req):
        self.request = req


def _with_request(headers: dict):
    """Bind a fake live MCP request into the contextvar the resolver reads."""
    import mcp.server.lowlevel.server as srv

    return srv.request_ctx, _Ctx(_Req(headers))


def test_prefers_x_pl_session_over_mcp_session_id():
    ctxvar, ctx = _with_request(
        {"x-pl-writer": "w1", "x-pl-session": "stable-1",
         "mcp-session-id": "per-call-9"})
    tok = ctxvar.set(ctx)
    try:
        assert wc._http_writer_session() == ("w1", "stable-1")
    finally:
        ctxvar.reset(tok)


def test_falls_back_to_mcp_session_id():
    ctxvar, ctx = _with_request(
        {"x-pl-writer": "w1", "mcp-session-id": "per-call-9"})
    tok = ctxvar.set(ctx)
    try:
        assert wc._http_writer_session() == ("w1", "per-call-9")
    finally:
        ctxvar.reset(tok)
