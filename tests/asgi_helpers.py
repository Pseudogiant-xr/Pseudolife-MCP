"""One ASGI driver for the console app's HTTP-level tests.

``tests/test_web.py`` and ``tests/test_session_identity.py`` each carried
their own copy of the same ~20-line scope/receive/send harness — three in
total, differing only in whether they set a query string and how much of the
response they handed back. The driver lives here instead:

* ``call`` — the shape almost every call site wants: ``(status, body)``;
* ``call_with_headers`` — the same run, plus the response headers as a dict,
  for the tests that assert on ``content-type``;
* ``stub_mcp`` — the 501 stand-in both files mount where the real MCP app
  goes (it was byte-identical in both).

Plain callables rather than fixtures, so importing them keeps each test file
honest about what it drives.
"""
from __future__ import annotations

import asyncio


async def stub_mcp(scope, receive, send):
    """Stand-in for the mounted MCP app. The console app only needs something
    ASGI-shaped to delegate ``/mcp`` to; these tests never exercise it."""
    await send({"type": "http.response.start", "status": 501, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def call_with_headers(app, method, path, headers=None, body=b"", query=""):
    """Drive ``app`` through exactly one request.

    ``headers`` is the raw ASGI list of ``(bytes, bytes)`` pairs; the returned
    response headers are a dict of the same. Returns
    ``(status, headers, body)``.
    """
    async def run():
        scope = {"type": "http", "method": method, "path": path,
                 "query_string": query.encode(), "headers": headers or []}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        out = {"status": None, "headers": [], "body": bytearray()}

        async def send(m):
            if m["type"] == "http.response.start":
                out["status"] = m["status"]
                out["headers"] = m.get("headers", [])
            elif m["type"] == "http.response.body":
                out["body"].extend(m.get("body", b""))

        await app(scope, receive, send)
        return out["status"], dict(out["headers"]), bytes(out["body"])

    return asyncio.run(run())


def call(app, method, path, headers=None, body=b"", query=""):
    """``call_with_headers`` without the response headers: ``(status, body)``."""
    status, _, out = call_with_headers(app, method, path, headers=headers,
                                       body=body, query=query)
    return status, out
