"""Writer/session attribution seam (v0.4 T4).

A single chokepoint for "who wrote this version". Resolution order:

1. An explicit override set via :func:`set_writer_context` — for tests,
   direct API callers, and future in-process agents.
2. The live MCP request. When the service runs inside the daemon, the MCP
   SDK binds the originating Starlette request (headers and all) to a
   contextvar *inside the handler's task* (``request_ctx`` in
   ``mcp.server.lowlevel.server``). That is the same task the tool runs
   in, so the ``X-PL-Writer`` header set by the shim survives the
   streamable-HTTP session-task boundary — the integration risk the plan
   flagged. ``session_id`` reuses the transport's ``mcp-session-id``
   header, which is stable per connection.
3. The process default (``PSEUDOLIFE_WRITER_ID`` env, or ``"unknown"``),
   supplied by the caller.

The MCP read is best-effort and fully isolated to this module: file-mode
and direct API use never import or touch the SDK.
"""
from __future__ import annotations

import contextvars
import os
from functools import lru_cache

# (writer_id, session_id) override; (None, None) means "not set".
_WRITER_CTX: contextvars.ContextVar[tuple[str | None, str | None]] = (
    contextvars.ContextVar("pl_writer_ctx", default=(None, None))
)


def set_writer_context(writer_id: str | None,
                       session_id: str | None = None):
    """Bind an explicit ``(writer_id, session_id)`` for the current context.

    Returns the contextvars token — pass it to :func:`reset_writer_context`
    (or use the value with ``_WRITER_CTX.reset``) to restore.
    """
    return _WRITER_CTX.set((writer_id, session_id))


def reset_writer_context(token) -> None:
    _WRITER_CTX.reset(token)


def _http_writer_session() -> tuple[str | None, str | None]:
    """Best-effort ``(writer_id, session_id)`` from the live MCP request.

    Compat shim over :func:`_http_writer_session_detailed` — prefer that
    (this merge loses the header-vs-transport distinction)."""
    w, hs, ts = _http_writer_session_detailed()
    return (w, hs or ts)


def _http_request_headers():
    """Best-effort headers of the live MCP request, or ``None`` outside one.
    Isolates the SDK read (and its v1 ``request_ctx`` dependency) to one
    place — the v2 port swaps this body for ``ctx.headers``."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        req = getattr(request_ctx.get(), "request", None)
        return None if req is None else req.headers
    except Exception:  # noqa: BLE001  (LookupError when unset; ImportError; ...)
        return None


def _http_writer_session_detailed() -> tuple[str | None, str | None, str | None]:
    """Best-effort ``(writer_id, header_session, transport_session)`` from the
    live MCP request. ``header_session`` is the integrator-asserted
    ``X-PL-Session`` (identity tier 1); ``transport_session`` is the
    transport's ``mcp-session-id`` — per-CONNECTION in multiplexing clients,
    and REMOVED from the MCP spec in the 2026-07-28 revision (SEP-2567), so
    it is a legacy fallback only."""
    headers = _http_request_headers()
    if headers is None:
        return (None, None, None)
    return (headers.get("x-pl-writer"), headers.get("x-pl-session"),
            headers.get("mcp-session-id"))


@lru_cache(maxsize=8)
def _parsed_token_map(raw: str) -> dict[str, str]:
    from pseudolife_memory.principals import parse_token_map

    return parse_token_map(raw)


def current_principal() -> str:
    """Principal NAME for the live request's bearer (spec 2026-08-10).

    Naming, not authentication — the transport gate already rejected unknown
    tokens, so an unmatched bearer here (the singular token, or open loopback
    mode) is simply the default principal. Fail-open: any error resolves to
    ``"default"``; identity resolution must never fail a request."""
    from pseudolife_memory.principals import DEFAULT_PRINCIPAL

    try:
        headers = _http_request_headers()
        auth = headers.get("authorization") if headers is not None else None
        raw = os.environ.get("PSEUDOLIFE_MCP_TOKENS")
        if not auth or not raw:
            return DEFAULT_PRINCIPAL
        scheme, _, presented = auth.partition(" ")
        if scheme.lower() != "bearer":
            return DEFAULT_PRINCIPAL
        return _parsed_token_map(raw).get(presented.strip(), DEFAULT_PRINCIPAL)
    except Exception:  # noqa: BLE001
        return DEFAULT_PRINCIPAL


def resolve_writer_detailed(
        default_writer: str) -> tuple[str, str | None, str | None]:
    """``(writer_id, header_session, transport_session)`` for this request.
    An explicit override binds its session into the HEADER slot — overrides
    are the strongest assertion we have. A NAMED principal from the token
    map is the writer (the credential outranks the client-asserted
    ``X-PL-Writer``); the default principal keeps the legacy header/env
    path, so single-token installs behave exactly as before."""
    from pseudolife_memory.principals import DEFAULT_PRINCIPAL

    w, s = _WRITER_CTX.get()
    if w is not None:
        return (w, s, None)
    hw, hs, ts = _http_writer_session_detailed()
    principal = current_principal()
    if principal != DEFAULT_PRINCIPAL:
        return (principal, hs, ts)
    return (hw or default_writer, hs, ts)


def resolve_writer(default_writer: str) -> tuple[str, str | None]:
    """Compat wrapper: ``(writer_id, session)`` with the pre-contract merge
    (header wins over transport). Prefer ``resolve_writer_detailed``."""
    w, hs, ts = resolve_writer_detailed(default_writer)
    return (w, hs or ts)
