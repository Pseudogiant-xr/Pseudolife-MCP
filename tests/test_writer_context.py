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


# ---------------------------------------------------------------------------
# Principal precedence (spec 2026-08-10): a named principal from the token
# map IS the writer; the default principal keeps the legacy X-PL-Writer path.
# ---------------------------------------------------------------------------


def test_named_principal_overrides_x_pl_writer(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tokA:hermes-box")
    ctxvar, ctx = _with_request(
        {"authorization": "Bearer tokA", "x-pl-writer": "spoofed",
         "x-pl-session": "s1"})
    tok = ctxvar.set(ctx)
    try:
        assert wc.resolve_writer_detailed("fallback") == (
            "hermes-box", "s1", None)
    finally:
        ctxvar.reset(tok)


def test_default_principal_keeps_x_pl_writer(monkeypatch):
    # Singular-token installs: bearer is not in the (empty) map, so the
    # legacy writer path is untouched.
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKENS", raising=False)
    ctxvar, ctx = _with_request(
        {"authorization": "Bearer single-tok", "x-pl-writer": "w1",
         "x-pl-session": "s1"})
    tok = ctxvar.set(ctx)
    try:
        assert wc.resolve_writer_detailed("fallback") == ("w1", "s1", None)
    finally:
        ctxvar.reset(tok)


def test_unknown_bearer_resolves_default_not_named(monkeypatch):
    # Naming is not authentication: the gate already rejected bad tokens, so
    # an unmatched bearer here (e.g. the singular token) is just "default".
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tokA:hermes-box")
    ctxvar, ctx = _with_request(
        {"authorization": "Bearer other", "x-pl-writer": "w1"})
    tok = ctxvar.set(ctx)
    try:
        writer, _, _ = wc.resolve_writer_detailed("fallback")
        assert writer == "w1"
    finally:
        ctxvar.reset(tok)


def test_explicit_override_beats_named_principal(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tokA:hermes-box")
    ctxvar, ctx = _with_request({"authorization": "Bearer tokA"})
    tok = ctxvar.set(ctx)
    ov = wc.set_writer_context("explicit-w", "explicit-s")
    try:
        assert wc.resolve_writer_detailed("fallback") == (
            "explicit-w", "explicit-s", None)
    finally:
        wc.reset_writer_context(ov)
        ctxvar.reset(tok)


def test_current_principal_fail_open_outside_request(monkeypatch):
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tokA:hermes-box")
    # No live request bound at all -> default, never an exception.
    assert wc.current_principal() == "default"
