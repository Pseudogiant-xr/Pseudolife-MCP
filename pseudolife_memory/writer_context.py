"""Writer/session attribution seam (v0.4 T4; reworked for SDK v2 2026-08-25).

A single chokepoint for "who wrote this version". Resolution order:

1. An explicit override set via :func:`set_writer_context` — for tests,
   direct API callers, and future in-process agents.
2. A NAMED principal from the request's bearer token
   (``PSEUDOLIFE_MCP_TOKENS``, spec 2026-08-10) — the credential outranks
   the client-asserted header below.
3. The live MCP request's headers. SDK v2 removed the ambient
   ``request_ctx`` contextvar, so the daemon binds each request's headers
   itself at its dispatch wrap (``mcp_server._wire_transport_tiering`` →
   :func:`bind_request_headers`); ``anyio.to_thread`` copies the context,
   so worker-thread tool bodies resolve the same binding. ``X-PL-Writer``
   attributes the writer; ``X-PL-Session`` (shim-asserted) is the session.
   The transport's ``mcp-session-id`` is RETIRED — it named the
   connection, not the session, and MCP 2026-07-28 removes it; the
   ``PSEUDOLIFE_LEGACY_TRANSPORT_SESSION`` hatch restores it for one
   release. Session identity for hook-registered clients rides the
   episode handle passed as a tool argument instead (spec 2026-08-25).
4. The process default (``PSEUDOLIFE_WRITER_ID`` env, or ``"unknown"``),
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


# Headers of the live MCP request, bound by the daemon's transport wrap
# (mcp_server._wire_transport_tiering) around every tools/call and
# tools/list dispatch. SDK v2 removed the ambient request_ctx contextvar,
# so the daemon asserts the headers itself at its one dispatch seam;
# anyio.to_thread copies the context, so worker-thread tool bodies resolve
# the same binding.
_REQUEST_HEADERS: contextvars.ContextVar = contextvars.ContextVar(
    "pl_request_headers", default=None)


def bind_request_headers(headers):
    """Bind the live request's headers for the current context; returns the
    token for ``unbind_request_headers``. ``None`` clears (binds nothing)."""
    return _REQUEST_HEADERS.set(headers)


def unbind_request_headers(token) -> None:
    _REQUEST_HEADERS.reset(token)


def _http_request_headers():
    """Best-effort headers of the live MCP request, or ``None`` outside one.
    Single seam for request-header reads: the daemon-bound contextvar first
    (SDK v2 path), then the v1 SDK's ambient ``request_ctx`` (absent under
    v2 — the import fails harmlessly)."""
    bound = _REQUEST_HEADERS.get()
    if bound is not None:
        return bound
    try:
        from mcp.server.lowlevel.server import request_ctx

        req = getattr(request_ctx.get(), "request", None)
        return None if req is None else req.headers
    except Exception:  # noqa: BLE001  (LookupError when unset; ImportError; ...)
        return None


_legacy_transport_warned = False


def _legacy_transport_session_enabled() -> bool:
    """The ``mcp-session-id`` fallback is RETIRED (spec 2026-08-25): the
    header names the connection, not the session, and the 2026-07-28 MCP
    revision (SEP-2567) removes it from the protocol entirely.
    ``PSEUDOLIFE_LEGACY_TRANSPORT_SESSION=1`` restores it for one release
    as a rollback hatch; first use logs a warning."""
    global _legacy_transport_warned
    # Repo env-flag convention (daemon.py, web/routes.py): explicit truthy
    # values only — "0"/"false" must mean OFF, not "set, therefore on".
    raw = os.environ.get("PSEUDOLIFE_LEGACY_TRANSPORT_SESSION", "")
    if raw.strip().lower() not in ("1", "true", "yes", "on"):
        return False
    if not _legacy_transport_warned:
        _legacy_transport_warned = True
        import logging

        logging.getLogger("pseudolife-mcp").warning(
            "PSEUDOLIFE_LEGACY_TRANSPORT_SESSION set — the retired "
            "mcp-session-id transport fallback (per-connection, removed by "
            "MCP 2026-07-28) is active; migrate callers to episode handles")
    return True


def _http_writer_session_detailed() -> tuple[str | None, str | None, str | None]:
    """Best-effort ``(writer_id, header_session, transport_session)`` from the
    live MCP request. ``header_session`` is the integrator-asserted
    ``X-PL-Session`` (identity tier 1); ``transport_session`` is the
    transport's ``mcp-session-id`` — per-CONNECTION in multiplexing clients,
    REMOVED from the MCP spec in the 2026-07-28 revision (SEP-2567), and
    retired here (always ``None`` unless the legacy escape hatch is set)."""
    headers = _http_request_headers()
    if headers is None:
        return (None, None, None)
    transport = (headers.get("mcp-session-id")
                 if _legacy_transport_session_enabled() else None)
    return (headers.get("x-pl-writer"), headers.get("x-pl-session"), transport)


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
