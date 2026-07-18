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
    """Best-effort ``(writer_id, session_id)`` from the live MCP request, or
    ``(None, None)`` when not inside a daemon HTTP request.

    DEPRECATED: Use :func:`resolve_writer_detailed` or :func:`resolve_writer`
    instead. This function exists only for backward compatibility with
    :mod:`mcp_server` and tests."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        req = getattr(request_ctx.get(), "request", None)
        if req is None:
            return (None, None)
        headers = req.headers
        # Prefer the shim's stable per-session id; the transport's
        # ``mcp-session-id`` is stable per session for a direct-HTTP client
        # (persistent connection) and per-call only for the reconnecting shim.
        return (
            headers.get("x-pl-writer"),
            headers.get("x-pl-session") or headers.get("mcp-session-id"),
        )
    except Exception:  # noqa: BLE001  (LookupError when unset; ImportError; ...)
        return (None, None)


def _http_writer_session_detailed() -> tuple[str | None, str | None, str | None]:
    """Best-effort ``(writer_id, header_session, transport_session)`` from the
    live MCP request. ``header_session`` is the integrator-asserted
    ``X-PL-Session`` (identity tier 1); ``transport_session`` is the
    transport's ``mcp-session-id`` — per-CONNECTION in multiplexing clients,
    and REMOVED from the MCP spec in the 2026-07-28 revision (SEP-2567), so
    it is a legacy fallback only."""
    try:
        from mcp.server.lowlevel.server import request_ctx

        req = getattr(request_ctx.get(), "request", None)
        if req is None:
            return (None, None, None)
        headers = req.headers
        return (headers.get("x-pl-writer"), headers.get("x-pl-session"),
                headers.get("mcp-session-id"))
    except Exception:  # noqa: BLE001  (LookupError when unset; ImportError; ...)
        return (None, None, None)


def resolve_writer_detailed(
        default_writer: str) -> tuple[str, str | None, str | None]:
    """``(writer_id, header_session, transport_session)`` for this request.
    An explicit override binds its session into the HEADER slot — overrides
    are the strongest assertion we have."""
    w, s = _WRITER_CTX.get()
    if w is not None:
        return (w, s, None)
    hw, hs, ts = _http_writer_session_detailed()
    return (hw or default_writer, hs, ts)


def resolve_writer(default_writer: str) -> tuple[str, str | None]:
    """Compat wrapper: ``(writer_id, session)`` with the pre-contract merge
    (header wins over transport). Prefer ``resolve_writer_detailed``."""
    w, hs, ts = resolve_writer_detailed(default_writer)
    return (w, hs or ts)
