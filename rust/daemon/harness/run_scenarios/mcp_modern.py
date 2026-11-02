"""Stateless MCP protocol routing and results against the pinned Python SDK."""

import copy
import json

VERSION = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"


def register(harness):
    class McpModern(harness.Scenario):
        name = "mcp-modern"
        env = {"PSEUDOLIFE_MCP_TOKEN": harness.T_DEFAULT}
        unreachable_database = True
        settle = False
        snapshot_settle = False
        start_stages = ()

        def cases(self):
            base = [harness.bearer(harness.T_DEFAULT), ("Content-Type", "application/json"),
                    ("Accept", "application/json, text/event-stream"),
                    ("MCP-Protocol-Version", VERSION)]
            rows = []

            def add(label, method="server/discover", params=None, *, change=None, headers=None, verb="POST"):
                params = copy.deepcopy(params or {})
                params.setdefault("_meta", {VERSION_KEY: VERSION, CAPABILITIES_KEY: {}})
                body = {"jsonrpc": "2.0", "id": len(rows) + 1, "method": method, "params": params}
                if change:
                    change(body)
                hs = list(base) + [("Mcp-Method", method)]
                name = params.get("name") if method in ("tools/call", "prompts/get") else params.get("uri")
                if isinstance(name, str):
                    hs.append(("Mcp-Name", name))
                if headers:
                    hs = headers(hs)
                rows.append(harness.case(label, verb, "/mcp", hs, json.dumps(body).encode()))

            add("discover without session")
            add("unknown session header ignored", headers=lambda hs: hs + [("Mcp-Session-Id", "unknown")])
            for method in ("tools/list", "prompts/list", "resources/list", "resources/templates/list",
                           "initialize", "ping", "unknown/method"):
                add(method, method)
            add("tool call", "tools/call", {"name": "memory_toolset", "arguments": {"action": "status"}})
            add("unknown tool", "tools/call", {"name": "not_a_tool", "arguments": {}})
            add("invalid tool arguments", "tools/call", {"name": "memory_search", "arguments": {"top_k": 0}})
            add("unknown prompt", "prompts/get", {"name": "missing"})
            add("unknown resource", "resources/read", {"uri": "memory://missing"})
            add("invalid resource uri", "resources/read", {"uri": "no scheme"})
            add("invalid tools cursor", "tools/list", {"cursor": 1})
            add("untrusted request state", "tools/call", {"name": "memory_toolset", "requestState": "invalid"})
            add("no meta", change=lambda body: body["params"].pop("_meta"))
            add("missing capability envelope", params={"_meta": {VERSION_KEY: VERSION}})
            add("null capabilities", params={"_meta": {VERSION_KEY: VERSION, CAPABILITIES_KEY: None}})
            add("invalid capabilities", params={"_meta": {VERSION_KEY: VERSION, CAPABILITIES_KEY: []}})
            add("version header mismatch", params={"_meta": {VERSION_KEY: "2099-01-01", CAPABILITIES_KEY: {}}})
            add("unsupported version", params={"_meta": {VERSION_KEY: "2099-01-01", CAPABILITIES_KEY: {} }},
                headers=lambda hs: [(k, "2099-01-01" if k.lower() == "mcp-protocol-version" else v) for k, v in hs])
            add("missing method header", headers=lambda hs: [(k, v) for k, v in hs if k != "Mcp-Method"])
            add("duplicate method header", headers=lambda hs: hs + [("Mcp-Method", "server/discover")])
            add("duplicate version header", headers=lambda hs: hs + [("MCP-Protocol-Version", VERSION)])
            add("missing name header", "tools/call", {"name": "memory_toolset"},
                headers=lambda hs: [(k, v) for k, v in hs if k != "Mcp-Name"])
            add("encoded name header", "tools/call", {"name": "memory_toolset"},
                headers=lambda hs: [(k, "=?base64?bWVtb3J5X3Rvb2xzZXQ=?=" if k == "Mcp-Name" else v) for k, v in hs])
            add("modern GET", verb="GET")
            add("modern DELETE", verb="DELETE")
            add("missing SSE accept", headers=lambda hs: [(k, "application/json" if k == "Accept" else v) for k, v in hs])
            add("notification acknowledged", "notifications/cancelled", {"requestId": 1},
                change=lambda body: body.pop("id"))
            add("notification needs no envelope", "notifications/cancelled", {"requestId": 1},
                change=lambda body: (body.pop("id"), body["params"].pop("_meta")))
            for label, value in (("null id", None), ("boolean id", True), ("fractional id", 1.5), ("integer-valued float id", 1.0)):
                add(label, change=lambda body, value=value: body.update(id=value))
            for label, body in (("parse error", b"{"), ("batch", b"[]"), ("posted result", b'{"jsonrpc":"2.0","id":1,"result":{}}')):
                rows.append(harness.case(label, "POST", "/mcp", base, body))
            return rows

    return [McpModern]
