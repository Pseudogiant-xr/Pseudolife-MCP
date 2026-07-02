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

import logging
import os
import sys

logger = logging.getLogger("pseudolife-mcp.daemon")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


# (AuthHealthASGI and its _json_response helper were removed in the
# 2026-07-02 zombie sweep: run_daemon has served the composed Console app
# from web/api.py — which owns /health and the token gate — since the
# Cortex Console landed, leaving this wrapper dead code.)


def run_daemon(host: str | None = None, port: int | None = None) -> None:
    """Entry point for ``pseudolife-mcp serve``. Blocks until shutdown."""
    import uvicorn

    # Importing mcp_server constructs the FastMCP instance + MemoryService
    # and registers the autosave/warmup/atexit machinery via its helpers.
    from pseudolife_memory import mcp_server

    host = host or os.environ.get("PSEUDOLIFE_MCP_HOST", DEFAULT_HOST)
    port = int(port or os.environ.get("PSEUDOLIFE_MCP_PORT", DEFAULT_PORT))
    token = os.environ.get("PSEUDOLIFE_MCP_TOKEN") or None
    trust_bind = os.environ.get("PSEUDOLIFE_MCP_TRUST_BIND", "").lower() in (
        "1", "true", "yes", "on",
    )

    if host not in _LOOPBACK and token is None:
        # Inside a container the process MUST bind 0.0.0.0, but the real
        # network boundary is the Docker port publish (compose binds it to
        # 127.0.0.1). PSEUDOLIFE_MCP_TRUST_BIND is the operator's explicit
        # assertion that exposure is enforced outside this process, so the
        # 0.0.0.0 bind here is not a LAN exposure. The host-daemon default
        # (no flag) still refuses — see ops/docker-compose.yml.
        if trust_bind:
            logger.warning(
                "Binding %s without a token because PSEUDOLIFE_MCP_TRUST_BIND "
                "is set — relying on an external network boundary (e.g. a "
                "container port published only to 127.0.0.1). Do NOT set this "
                "when running the daemon directly on a host.", host,
            )
        else:
            logger.error(
                "Refusing to bind %s without PSEUDOLIFE_MCP_TOKEN — the memory "
                "bank must not listen on the network unauthenticated. Set the "
                "token (and give it to LAN clients), bind 127.0.0.1, or set "
                "PSEUDOLIFE_MCP_TRUST_BIND=1 if the boundary is external "
                "(containerised, loopback-published).", host,
            )
            sys.exit(2)

    def _health() -> dict:
        from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

        svc = mcp_server.service
        payload = {
            "status": "ok",
            "schema": SCHEMA_META_VERSION,
            "storage": "postgres" if getattr(svc, "_db_url", None) else "files",
            "auth": token is not None,
            # Durable-save failures since start (see service.PersistenceError);
            # >0 means writes succeeded in memory but a snapshot did not persist.
            "persist_errors": getattr(svc, "_persist_errors", 0),
        }
        # Honest DB liveness (2026-07-02 review fix): /health used to say
        # "ok" while a restarted Postgres had every memory tool failing.
        # ping() uses a dedicated short-lived connection so the probe can't
        # interleave with the shared connection another thread is using.
        # Before the first tool call storage is lazily unbuilt — that is
        # still "ok" (nothing to probe yet).
        storage = getattr(svc, "_storage", None)
        if storage is not None and hasattr(storage, "ping"):
            try:
                storage.ping()
                payload["db"] = "ok"
            except Exception as exc:  # noqa: BLE001 — surface, don't raise
                payload["status"] = "degraded"
                payload["db"] = f"error: {exc}"
        return payload

    mcp_server.start_background_durability()
    mcp_server.start_dream_sweep()
    mcp_server.start_session_reaper()

    # Compose the Cortex Console (static SPA at /ui + REST at /api) in front of
    # the MCP app. /health and the static shell stay open; /api joins /mcp
    # behind the bearer-token gate. See pseudolife_memory/web/.
    from pseudolife_memory.web import build_console_app

    app = build_console_app(
        mcp_server.mcp.streamable_http_app(), token, _health, mcp_server.service,
    )
    logger.info("daemon: listening on %s:%s (auth=%s, storage=%s) — console at /ui/",
                host, port, token is not None, _health()["storage"])
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_daemon()
