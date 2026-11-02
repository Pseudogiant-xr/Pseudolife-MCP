"""Stateless MCP protocol routing and results against the pinned Python SDK."""

import copy
import http.client
import json

from mcp_wire import Session

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
            encoded = json.dumps({"jsonrpc": "2.0", "id": "東京", "method": "server/discover",
                                  "params": {"_meta": {VERSION_KEY: VERSION, CAPABILITIES_KEY: {}}}}, ensure_ascii=False)
            discover_headers = base + [("Mcp-Method", "server/discover")]
            for encoding in ("utf-8-sig", "utf-16", "utf-16-be", "utf-32", "utf-32-be"):
                rows.append(harness.case(f"JSON bytes {encoding}", "POST", "/mcp", discover_headers, encoded.encode(encoding)))
            for label, value in (("oversized integer", "1" * 4301), ("nonfinite float", "Infinity")):
                body = encoded.replace('"東京"', value).encode()
                rows.append(harness.case(label, "POST", "/mcp", discover_headers, body))
            return rows

    class McpModernSubscriptions(McpModern):
        name = "mcp-modern-subscriptions"
        golden_replay = True

        def timeline(self, procs, holders):
            answers = [{}, {}]
            for side, proc in procs.items():
                connection = http.client.HTTPConnection("127.0.0.1", proc.port, timeout=30)
                legacy = None
                response = None
                try:
                    body = {"jsonrpc": "2.0", "id": "subscription-probe", "method": "subscriptions/listen",
                            "params": {"notifications": {"toolsListChanged": True,
                                                         "resourcesListChanged": False,
                                                         "resourceSubscriptions": ["memory://one"]},
                                       "_meta": {VERSION_KEY: VERSION, CAPABILITIES_KEY: {}}}}
                    connection.request("POST", "/mcp", json.dumps(body), {
                        "Authorization": f"Bearer {harness.T_DEFAULT}", "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": VERSION,
                        "Mcp-Method": "subscriptions/listen"})
                    response = connection.getresponse()
                    assert response.status == 200
                    assert response.getheader("mcp-session-id") is None
                    assert response.getheader("content-type").split(";")[0] == "text/event-stream"

                    def event():
                        data = []
                        while True:
                            line = response.readline()
                            assert line, f"{side}: subscription ended before its event"
                            if line.startswith(b"data: "):
                                data.append(line[6:].strip())
                            if not line.strip() and data:
                                return json.loads(b"\n".join(data))

                    answers[0][side] = {"status": 200, "headers": {}, "json": event()}
                    # A change through the handshake era reaches stateless subscribers.
                    legacy = Session(proc.port, harness.T_DEFAULT)
                    expanded = legacy.tool("memory_toolset", {"action": "expand"})
                    assert expanded["status"] == 200
                    answers[1][side] = {"status": 200, "headers": {}, "json": event()}
                finally:
                    if response is not None:
                        response.close()
                    connection.close()
                    if legacy is not None:
                        legacy.close()
            for label, values in zip(("honored subscription filter", "legacy change reaches modern subscriber"), answers):
                row = harness.case(label, "POST", "/mcp")
                row["_answers"] = (values.get("python"), values["rust"])
                yield row

    return [McpModern, McpModernSubscriptions]
