"""The memory daemon — MCP over streamable HTTP (spec §3.1).

One long-lived process owns the bank: every Claude Code session (and any
LAN agent) connects here instead of spawning its own stdio server. Single
writer by construction — the concurrency class of bugs from v0.1 cannot
occur.

Security model:
* ``/health`` is always unauthenticated (cheap liveness probe).
* When ``PSEUDOLIFE_MCP_TOKEN`` is set, every other route requires
  ``Authorization: Bearer <token>``.
* Binding a non-loopback host WITHOUT a token is refused outright —
  the bank never listens on the LAN unauthenticated.
* Postgres itself stays loopback-bound (ops/docker-compose.yml); the
  LAN only ever sees this daemon.
"""

from __future__ import annotations

import json
import logging
import os
import sys

logger = logging.getLogger("pseudolife-mcp.daemon")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _json_response(send, status: int, payload: dict):
    body = json.dumps(payload).encode()

    async def _send():
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    return _send()


class AuthHealthASGI:
    """Pure-ASGI wrapper: /health endpoint + bearer-token gate.

    Implemented against the raw ASGI protocol (not Starlette middleware
    classes) so it has no dependency on Starlette's middleware API
    surface staying stable across mcp-SDK versions.
    """

    def __init__(self, app, token: str | None, health_payload) -> None:
        self.app = app
        self.token = token or None
        self.health_payload = health_payload  # callable -> dict

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/health":
                try:
                    payload = self.health_payload()
                except Exception as exc:  # noqa: BLE001
                    await _json_response(
                        send, 500, {"status": "error", "error": str(exc)})
                    return
                await _json_response(send, 200, payload)
                return
            if self.token is not None:
                headers = {k.decode().lower(): v.decode()
                           for k, v in scope.get("headers", [])}
                expect = f"Bearer {self.token}"
                if headers.get("authorization") != expect:
                    await _json_response(
                        send, 401,
                        {"error": "unauthorized",
                         "hint": "Authorization: Bearer <PSEUDOLIFE_MCP_TOKEN>"})
                    return
        await self.app(scope, receive, send)


def run_daemon(host: str | None = None, port: int | None = None) -> None:
    """Entry point for ``pseudolife-mcp serve``. Blocks until shutdown."""
    import uvicorn

    # Importing mcp_server constructs the FastMCP instance + MemoryService
    # and registers the autosave/warmup/atexit machinery via its helpers.
    from pseudolife_memory import mcp_server

    host = host or os.environ.get("PSEUDOLIFE_MCP_HOST", DEFAULT_HOST)
    port = int(port or os.environ.get("PSEUDOLIFE_MCP_PORT", DEFAULT_PORT))
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None

    if host not in _LOOPBACK and token is None:
        logger.error(
            "Refusing to bind %s without PSEUDOLIFE_MCP_TOKEN — the memory "
            "bank must not listen on the network unauthenticated. Set the "
            "token (and give it to LAN clients) or bind 127.0.0.1.", host,
        )
        sys.exit(2)

    def _health() -> dict:
        svc = mcp_server.service
        return {
            "status": "ok",
            "schema": 8,
            "storage": "postgres" if getattr(svc, "_db_url", None) else "files",
            "auth": token is not None,
        }

    mcp_server.start_background_durability()
    mcp_server.start_dream_sweep()

    app = AuthHealthASGI(
        mcp_server.mcp.streamable_http_app(), token, _health,
    )
    logger.info("daemon: listening on %s:%s (auth=%s, storage=%s)",
                host, port, token is not None, _health()["storage"])
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_daemon()
