"""``writer_context`` HTTP header resolution.

The stamping/session id must be STABLE per session. The shim mints
``X-PL-Session`` and rides it on every call; the transport's
``mcp-session-id`` is per-call (fresh connection per call) so it is only a
fallback for older clients.
"""
from __future__ import annotations

import pytest

from pseudolife_memory import writer_context as wc


class _Req:
    def __init__(self, headers):
        self.headers = headers


class _Ctx:
    """Stands in for mcp's RequestContext — only ``.request`` is read."""

    def __init__(self, req):
        self.request = req


def _with_request(headers: dict):
    """Bind fake request headers into the seam the resolver reads. SDK v2
    removed the ambient request_ctx; the daemon binds headers itself via
    writer_context.bind_request_headers, and tests set the same contextvar
    (the returned pair keeps the ``tok = ctxvar.set(value)`` call sites)."""
    from pseudolife_memory import writer_context as wc

    return wc._REQUEST_HEADERS, {k.lower(): v for k, v in headers.items()}


def test_prefers_x_pl_session_over_mcp_session_id():
    ctxvar, ctx = _with_request(
        {"x-pl-writer": "w1", "x-pl-session": "stable-1",
         "mcp-session-id": "per-call-9"})
    tok = ctxvar.set(ctx)
    try:
        assert wc._http_writer_session() == ("w1", "stable-1")
    finally:
        ctxvar.reset(tok)


def test_mcp_session_id_fallback_retired(monkeypatch):
    # Spec 2026-08-25: the transport header names the CONNECTION, not the
    # session (and MCP 2026-07-28 removes it) — ignored unless the one-release
    # legacy hatch is set.
    ctxvar, ctx = _with_request(
        {"x-pl-writer": "w1", "mcp-session-id": "per-call-9"})
    tok = ctxvar.set(ctx)
    monkeypatch.delenv("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION", raising=False)
    try:
        assert wc._http_writer_session() == ("w1", None)
        monkeypatch.setenv("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION", "1")
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


def test_the_singular_token_resolves_default_and_keeps_the_writer_header(monkeypatch):
    # The singular token is the default principal, whose writer stays the
    # client-asserted X-PL-Writer.
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tokA:hermes-box")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "single")
    ctxvar, ctx = _with_request(
        {"authorization": "Bearer single", "x-pl-writer": "w1"})
    tok = ctxvar.set(ctx)
    try:
        writer, _, _ = wc.resolve_writer_detailed("fallback")
        assert writer == "w1"
    finally:
        ctxvar.reset(tok)


def test_an_unknown_bearer_is_refused_not_named_default(monkeypatch):
    # Security review, 2026-10-02: with authentication configured, a bearer
    # that matches nothing is not served as "default" (which would honour
    # its X-PL-Writer); naming refuses instead.
    from pseudolife_memory.principals import PrincipalsUnavailable
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "tokA:hermes-box")
    ctxvar, ctx = _with_request(
        {"authorization": "Bearer other", "x-pl-writer": "w1"})
    tok = ctxvar.set(ctx)
    try:
        with pytest.raises(PrincipalsUnavailable):
            wc.resolve_writer_detailed("fallback")
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


def test_non_ascii_named_bearer_from_mcp_transport_names_principal(monkeypatch):
    # The MCP transport binds Starlette's headers, which decode the bearer
    # as latin-1; the caller authenticated at the gate, so the named
    # principal must survive rather than fall back to "default".
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKENS", "café-token:hermes-box")
    wire = "café-token".encode("utf-8").decode("latin-1")
    binding = wc.bind_request_headers({"authorization": "Bearer " + wire})
    try:
        assert wc.current_principal() == "hermes-box"
    finally:
        wc.unbind_request_headers(binding)
