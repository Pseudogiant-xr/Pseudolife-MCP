"""Synchronous client for the Pseudolife memory daemon.

MCP over streamable HTTP for the tools, plain REST for episode lifecycle. The
plugin never imports ``pseudolife_memory``; this is an ordinary external client.

Two deliberate choices, both learned from the stdio shim:

* **Connect per call.** The daemon (and Docker's loopback proxy, and uvicorn's
  keep-alive) reaps idle connections, and the MCP client has no reconnect — a
  long-lived session hangs on a dead stream after the first idle gap. Under the
  stateless protocol a connection is nothing but the HTTP exchange anyway.
* **Headers on the http client, not the transport helper.** SDK v2 moved them
  there; ``X-PL-Session`` is what makes ``memory_outcome(used_ids=)`` able to
  credit this episode's searches, so it has to ride every request.

Each call runs its own event loop on the calling thread and restores whatever
loop that thread had, so it composes with the harness's per-worker loop setup.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from . import telemetry
from .session import extract_served_ids

DEFAULT_TIMEOUT = 60.0


class PLMemoryClient:
    """One instance per episode. Not shared; not global."""

    def __init__(
        self,
        url: str,
        session_uid: str,
        writer: str = "taubench",
        token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        read_only: bool = False,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.session_uid = session_uid
        self.writer = writer
        self.token = token or None
        self.timeout = timeout
        # A frozen store takes no writes of ANY kind, episode rows included.
        # The guard lives here rather than at each call site because the agent
        # opens and closes the episode on paths that never reach the
        # reflection's own read-only check.
        self.read_only = bool(read_only)

    # ── headers ──────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        headers = {"X-PL-Session": self.session_uid}
        if self.writer:
            headers["X-PL-Writer"] = self.writer
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # ── MCP ──────────────────────────────────────────────────────────────────
    def call_tool(self, name: str, args: dict) -> dict:
        """Call one daemon tool and return its parsed JSON result.

        Records telemetry for every call — this is the single instrumentation
        point for memory traffic. Errors come back as ``{"error": ...}`` rather
        than raising: a memory failure must degrade the run, never end it.
        """
        start = time.perf_counter()
        ok = True
        try:
            result = _run(self._call_tool_async(name, args))
        except Exception as exc:  # noqa: BLE001
            ok = False
            result = {"error": f"{type(exc).__name__}: {exc}"}
        latency_ms = (time.perf_counter() - start) * 1000.0
        served = extract_served_ids(result)
        telemetry.record_call(
            tool=name,
            args_digest=_digest(args),
            ok=ok and "error" not in result,
            latency_ms=latency_ms,
            result_len=len(json.dumps(result, default=str)),
            served_ids=served if served else None,
        )
        return result

    async def _call_tool_async(self, name: str, args: dict) -> dict:
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import (
            create_mcp_http_client,
            streamable_http_client,
        )

        async with create_mcp_http_client(headers=self._headers()) as http:
            async with streamable_http_client(self.url + "/mcp", http_client=http) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, args)
        return _parse_result(result)

    # ── REST episode lifecycle ───────────────────────────────────────────────
    def episode_start(self, title: str) -> bool:
        if self.read_only:
            return False
        return self._post("/api/episode/start", {
            "session_key": self.session_uid, "title": title,
        })

    def episode_end(self) -> bool:
        if self.read_only:
            return False
        return self._post("/api/episode/end", {"session_key": self.session_uid})

    def _post(self, path: str, payload: dict) -> bool:
        """Best-effort: episode bookkeeping must never break or slow a run."""
        try:
            import httpx

            headers = {"content-type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            response = httpx.post(
                self.url + path, json=payload, headers=headers, timeout=10.0
            )
            return response.status_code < 400
        except Exception:  # noqa: BLE001
            return False


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_result(result: Any) -> dict:
    """The tool result as a dict.

    Structured content first (v2 returns it when the tool declares an output
    schema), then the first text block, which the daemon fills with JSON.
    """
    structured = getattr(result, "structured_content", None) or getattr(
        result, "structuredContent", None
    )
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {}


def _digest(args: Optional[dict]) -> dict:
    """A small, low-cardinality summary of the arguments, never the payload."""
    args = args or {}
    out: dict = {}
    if "query" in args:
        out["query"] = str(args["query"])[:200]
    if "top_k" in args:
        out["top_k"] = args["top_k"]
    if "tags" in args and isinstance(args["tags"], list):
        out["n_tags"] = len(args["tags"])
    if "sources" in args and isinstance(args["sources"], list):
        out["sources"] = args["sources"]
    if "text" in args:
        out["text_chars"] = len(str(args["text"]))
    if "detail" in args:
        out["detail_chars"] = len(str(args["detail"]))
    if "outcome" in args:
        out["outcome"] = args["outcome"]
    if "about" in args:
        out["about"] = str(args["about"])[:120]
    if "used_ids" in args and isinstance(args["used_ids"], list):
        out["n_used_ids"] = len(args["used_ids"])
    if "action" in args:
        out["action"] = args["action"]
    return out


def _run(coro):
    """Run one coroutine on a fresh loop, restoring the thread's previous loop.

    The harness's batch worker installs an event loop per thread and cleans it
    up later; closing or nulling it out from under that would break the cleanup,
    so the previous loop is put back verbatim.
    """
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:  # noqa: BLE001 - no loop set for this thread
        previous = None
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(previous)
