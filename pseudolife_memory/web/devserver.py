"""Fixture-backed dev server for Cortex Console UI development.

Serves the real console ASGI app (static shell + ``/api`` routes) against the
:class:`~pseudolife_memory.web.fixtures.FixtureService` — no Postgres, no torch,
no live bank. This is the harness used to QA the frontend in a browser.

    python -m pseudolife_memory.web.devserver           # http://127.0.0.1:8770/ui/
    python -m pseudolife_memory.web.devserver --port 9000

It exercises the genuine fetch/render code paths, so what looks right here looks
right against the live daemon (same routes, same JSON shapes).
"""

from __future__ import annotations

import argparse


async def _stub_mcp_app(scope, receive, send):
    """Stand-in for the FastMCP app — the dev server has no MCP transport."""
    body = b'{"error":"mcp_not_available_in_devserver"}'
    await send({"type": "http.response.start", "status": 501,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def build_dev_app(token: str | None = None):
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService

    service = FixtureService()

    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    def _health() -> dict:
        return {"status": "ok", "schema": SCHEMA_META_VERSION,
                "storage": "postgres (fixture)", "auth": token is not None,
                "persist_errors": 0, "mode": "devserver"}

    return build_console_app(_stub_mcp_app, token, _health, service)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cortex Console dev server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--token", default=None,
                        help="Optional bearer token to exercise the auth gate.")
    args = parser.parse_args()

    import uvicorn

    app = build_dev_app(token=args.token)
    print(f"Cortex Console (fixtures) -> http://{args.host}:{args.port}/ui/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
