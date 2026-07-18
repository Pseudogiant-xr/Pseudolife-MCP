"""ASGI plumbing for the Cortex Console.

Composes, in front of the FastMCP streamable app, three additional surfaces:

* ``/health``        — open liveness probe (unchanged behaviour).
* ``/`` , ``/ui/*``  — the static SPA shell (open; it's just code, no data).
* ``/api/*``         — JSON over ``MemoryService``, behind the same bearer-token
                       gate as ``/mcp``.

Everything else (``/mcp`` and any MCP sub-path) is forwarded to the wrapped MCP
app untouched. Implemented against the raw ASGI protocol (no Starlette
middleware classes) to match :mod:`pseudolife_memory.daemon` and avoid coupling
to the MCP SDK's internal middleware surface.

Sync ``service`` calls are dispatched to a threadpool so a slow retrieval can't
stall the event loop (and the concurrent ``/mcp`` traffic).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from pseudolife_memory.web.routes import ConsoleRoutes
from pseudolife_memory.web.session_hook import hook_session_end, hook_session_start

logger = logging.getLogger("pseudolife-mcp.web")

STATIC_DIR = Path(__file__).parent / "static"
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")


async def _send_json(send, status: int, payload: Any) -> None:
    body = json.dumps(payload, default=str).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store")],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_bytes(send, status: int, body: bytes, content_type: str,
                      cache: str = "no-cache") -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type.encode()),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", cache.encode())],
    })
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body"):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


def _parse_query(scope) -> dict[str, str]:
    raw = scope.get("query_string", b"").decode("utf-8", "replace")
    out: dict[str, str] = {}
    if not raw:
        return out
    from urllib.parse import parse_qsl
    for k, v in parse_qsl(raw, keep_blank_values=True):
        out[k] = v  # last value wins; handlers that want lists split on ","
    return out


def _serve_static(rel_path: str) -> tuple[int, bytes, str]:
    """Resolve ``/ui/<rel_path>`` under STATIC_DIR with traversal protection."""
    rel = rel_path.strip("/")
    if rel in ("", "ui", "ui/"):
        rel = "index.html"
    elif rel.startswith("ui/"):
        rel = rel[3:]
    target = (STATIC_DIR / rel).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return 403, b"forbidden", "text/plain"
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        # SPA fallback: unknown sub-route -> index.html (hash router handles it).
        index = STATIC_DIR / "index.html"
        if index.is_file():
            return 200, index.read_bytes(), "text/html; charset=utf-8"
        return 404, b"not found", "text/plain"
    ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in (
            "application/javascript", "image/svg+xml", "application/json"):
        ctype += "; charset=utf-8" if "charset" not in ctype else ""
    return 200, target.read_bytes(), ctype


def build_console_app(
    mcp_app: Callable,
    token: str | None,
    health_payload: Callable[[], dict],
    service: Any,
) -> Callable:
    """Return the composed ASGI app. ``mcp_app`` is forwarded for non-console
    paths; ``health_payload`` powers ``/health``; ``service`` backs ``/api``."""
    token = token or None
    routes = ConsoleRoutes(service)

    def _authorized(scope) -> bool:
        if token is None:
            return True
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        return headers.get("authorization") == f"Bearer {token}"

    def _hdr(scope, name: bytes) -> str | None:
        for k, v in scope.get("headers", []):
            if k.lower() == name:
                return v.decode("latin-1")
        return None

    def _host_part(value: str) -> str:
        """Lower-cased host from a Host header or an Origin URL —
        strips scheme, port, and IPv6 brackets."""
        if "://" in value:
            value = urlsplit(value).netloc or value
        value = value.strip().lower()
        if value.startswith("["):
            return value.partition("]")[0].lstrip("[")
        if value.count(":") == 1:
            return value.rsplit(":", 1)[0]
        return value

    def _browser_gate(scope) -> str | None:
        """Tokenless /api is loopback-only (2026-07-02 review H2). Browsers
        always send Host — and Origin on cross-site requests — so a foreign
        Origin is CSRF and a foreign Host is DNS rebinding; both are
        rejected. Non-browser clients (curl, scripts, tests) send neither
        and pass. With a token set, Authorization already proves intent (it
        cannot be attached cross-origin without a failing preflight), so
        remote/LAN hosts stay legitimate."""
        if token is not None:
            return None
        origin = _hdr(scope, b"origin")
        if origin is not None and _host_part(origin) not in _LOOPBACK:
            return "forbidden_origin"
        host = _hdr(scope, b"host")
        if host is not None and _host_part(host) not in _LOOPBACK:
            return "forbidden_host"
        return None

    async def app(scope, receive, send):
        if scope["type"] != "http":
            await mcp_app(scope, receive, send)
            return

        path = scope.get("path", "") or "/"
        method = scope.get("method", "GET").upper()

        # 1) open liveness probe. A degraded payload (e.g. DB unreachable)
        # surfaces as 503 so orchestration/healthchecks can actually see it.
        if path == "/health":
            try:
                payload = health_payload()
                ok = payload.get("status", "ok") == "ok"
                await _send_json(send, 200 if ok else 503, payload)
            except Exception as exc:  # noqa: BLE001
                await _send_json(send, 500, {"status": "error", "error": str(exc)})
            return

        # 2) root -> console
        if path == "/":
            await send({"type": "http.response.start", "status": 307,
                        "headers": [(b"location", b"/ui/")]})
            await send({"type": "http.response.body", "body": b""})
            return

        # 3) static SPA shell (open)
        if path == "/ui" or path.startswith("/ui/"):
            try:
                status, body, ctype = await asyncio.get_running_loop().run_in_executor(
                    None, _serve_static, path)
                # Fonts/images are immutable; markup, styles and scripts must
                # never be cached so updates are picked up without a hard reload.
                cache = ("max-age=86400" if ctype.startswith(("font/", "image/"))
                         else "no-store")
                await _send_bytes(send, status, body, ctype, cache)
            except Exception as exc:  # noqa: BLE001
                logger.warning("static serve error for %s: %s", path, exc)
                await _send_bytes(send, 500, b"static error", "text/plain")
            return

        # 4) plugin SessionStart hook context (plain text, 200 always). The
        # instructions half is public repo content, so an unauthorized
        # request (token set, no bearer) still gets it — but never the
        # briefing, which is memory content. Loopback gating as for /api.
        # An optional ?session_id= (+ ?source=) registers the session's
        # episode and active-session pointer (identity tier 3) and prepends
        # the episode-handle advertisement line — but only when authorized:
        # unauthorized-with-token callers must not be able to mint/hijack the
        # active-session pointer just by hitting this endpoint, so session_id
        # and source are dropped entirely (byte-identical to the plain
        # instructions-only response) rather than merely gated on output.
        if path == "/api/hook/session-start":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "GET":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            authorized = _authorized(scope)
            params = _parse_query(scope)
            session_id = params.get("session_id") if authorized else None
            source = params.get("source") if authorized else None
            text = await asyncio.get_running_loop().run_in_executor(
                None, hook_session_start, service, session_id, source,
                authorized)
            await _send_bytes(send, 200, text.encode("utf-8"),
                              "text/plain; charset=utf-8", "no-store")
            return

        # 4b) plugin SessionEnd hook: closes the session's episode and clears
        # the active-session pointer (only if still owned). Fail-open, always
        # 200 — mirrors session-start's contract. Unlike session-start there
        # is no unauthorized-safe subset of this call (it's pure mutation),
        # so a configured token is enforced outright, same shape as /api's
        # 401.
        if path == "/api/hook/session-end":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {"error": denied})
                return
            if method != "POST":
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            if not _authorized(scope):
                await _send_json(send, 401, {
                    "error": "unauthorized",
                    "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
                return
            raw = await _read_body(receive)
            body: dict = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    body = {}
            if not isinstance(body, dict):
                body = {}
            result = await asyncio.get_running_loop().run_in_executor(
                None, hook_session_end, service, body.get("session_id"))
            await _send_json(send, 200, result)
            return

        # 5) console REST API (token-gated like /mcp)
        if path.startswith("/api/") or path == "/api":
            denied = _browser_gate(scope)
            if denied:
                await _send_json(send, 403, {
                    "error": denied,
                    "hint": "tokenless /api serves loopback browsers only; "
                            "set PSEUDOLIFE_MCP_TOKEN for remote access"})
                return
            if not _authorized(scope):
                await _send_json(send, 401, {
                    "error": "unauthorized",
                    "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
                return
            if method not in ("GET", "POST"):
                await _send_json(send, 405, {"error": "method_not_allowed"})
                return
            params = _parse_query(scope)
            body: dict = {}
            if method == "POST":
                raw = await _read_body(receive)
                if raw:
                    # A cross-site form/fetch can send text/plain or
                    # urlencoded WITHOUT a CORS preflight; application/json
                    # cannot. 415 here forces the preflight (which fails —
                    # no CORS headers are served). Bodyless POSTs are
                    # covered by the Origin gate above.
                    ctype = (_hdr(scope, b"content-type") or "")
                    if ctype.split(";")[0].strip().lower() != "application/json":
                        await _send_json(send, 415, {
                            "error": "content_type_must_be_application_json"})
                        return
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        await _send_json(send, 400, {"error": "invalid_json"})
                        return
                    if not isinstance(body, dict):
                        await _send_json(send, 400, {"error": "body_must_be_object"})
                        return
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    None, routes.dispatch, method, path, params, body)
                await _send_json(send, 200, result)
            except KeyError:
                # unknown path, or a wrong-verb hit on a known path
                status = 405 if routes.has(path) else 404
                await _send_json(send, status, {
                    "error": "not_found" if status == 404 else "method_not_allowed",
                    "path": path})
            except ValueError as exc:
                await _send_json(send, 400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                logger.exception("api handler error: %s %s", method, path)
                await _send_json(send, 500, {"error": str(exc)})
            return

        # 6) everything else -> the MCP app (token gate preserved)
        if not _authorized(scope):
            await _send_json(send, 401, {
                "error": "unauthorized",
                "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
            return
        await mcp_app(scope, receive, send)

    return app
