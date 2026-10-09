"""Differential harness for the MCP surface (spec.md, "MCP surface (W2-G)").

usage:
  python mcp_compare.py compare <python-port> <rust-port> <out.json>
  python mcp_compare.py record  <python-port> <golden.json>
  python mcp_compare.py golden  <golden.json> <rust-port> <out.json>

Both daemons run on disposable banks with the same environment:
  PSEUDOLIFE_MCP_TOKEN=tok-default-0001
  PSEUDOLIFE_MCP_TOKENS=tok-alice-0001:alice,tok-bob-0001:bob,tok-carol-0001:carol
  PSEUDOLIFE_MCP_TIER_MAP=alice:minimal,bob:core,writer-m:minimal
  PSEUDOLIFE_MCP_TOOLSET=core
and PSEUDOLIFE_WRITER_ID unset. Add --tokenless when both run with no token
at all (the DNS-rebinding cases run only then).

Every case is a sequence of HTTP exchanges replayed identically against each
side. A response is compared by status, the headers clients read
(content-type, allow, location, and whether mcp-session-id is present), and
its body bytes: SSE bodies by their data payloads in order (keep-alive
comments dropped), other bodies exactly. Declared-free wording (parse and
validation error text) is compared by its fixed prefix only. Tools whose
bodies have not landed are checked against the declared stub on the Rust
side (rust/daemon/divergences.md). Exits 1 on any diff.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_wire import ACCEPT, PROTOCOL, Session, header, request, sse_data  # noqa: E402

TOK = {"default": "tok-default-0001", "alice": "tok-alice-0001", "bob": "tok-bob-0001",
       "carol": "tok-carol-0001"}
JSON_HDRS = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
# Free text after these prefixes (spec M-free): pydantic and jiter wording.
FREE_PREFIXES = ("Parse error: ", "Validation error: ")
# Bodies not yet served by the Rust daemon: tool -> owning slice. Mirrors
# rust/daemon/divergences.md; delete a row when its body is wired.
UNSERVED = {
    "memory_agents": "W2-F", "memory_message": "W2-F",
    "memory_search": "W2-D", "memory_recall": "W2-D", "memory_world_search": "W2-D",
    "memory_lesson_search": "W2-D", "memory_get": "W2-D", "memory_recent": "W2-D",
    "memory_history": "W2-D", "memory_stats": "W2-D", "memory_graph": "W2-D",
    "memory_fact_get": "W2-D", "document_search": "W2-D", "memory_episode_summary": "W2-D",
    "memory_consolidation_candidates": "W2-D",
    "memory_store": "W2-E", "memory_fact_set": "W2-E", "memory_set_add": "W2-E",
    "memory_set_remove": "W2-E", "memory_fact_resolve": "W2-E", "memory_world_set": "W2-E",
    "memory_outcome": "W2-E", "memory_episode_start": "W2-E", "memory_episode_end": "W2-E",
    "memory_session_title": "W2-E", "memory_supersede": "W2-E", "memory_reinstate": "W2-E",
    "memory_reinforce": "W2-E", "memory_forget": "W2-E", "document_ingest": "W2-E",
    "memory_dream": "W3-H", "memory_graph_review": "W3-H", "memory_consolidate": "W3-H",
    "memory_graph_relate": "W3-H", "memory_graph_unrelate": "W3-H", "memory_alias": "W3-H",
    "memory_relation_define": "W3-H",
}


def init_body(version=PROTOCOL, params=None):
    p = params if params is not None else {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "mcp", "version": "0.1.0"}, "_meta": {}}
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": p}


def norm(resp):
    """The comparable view of one response."""
    keep = {}
    for k in ("content-type", "allow", "location"):
        v = header(resp, k)
        if v is not None:
            keep[k] = v
    keep["mcp-session-id"] = header(resp, "mcp-session-id") is not None
    ctype = header(resp, "content-type") or ""
    if ctype.startswith("text/event-stream"):
        body = {"sse": sse_data(resp)}
    else:
        text = resp["body"].decode("utf-8", "replace")
        try:
            obj = json.loads(text) if text else None
        except ValueError:
            obj = None
        err = obj.get("error") if isinstance(obj, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        if isinstance(msg, str) and msg.startswith(FREE_PREFIXES):
            obj["error"]["message"] = msg.split(": ", 1)[0] + ": <free>"
            text = json.dumps(obj, separators=(",", ":"))
        body = {"raw": text}
    return {"status": resp["status"], "headers": keep, "body": body}


# ── cases ───────────────────────────────────────────────────────────────────
# A case is a function (port) -> list of normalized responses. It must leave
# the daemon's tier overrides as it found them, so reruns compare equal.

def handshake(port):
    s = Session(port, TOK["default"])
    out = [norm(s.init), norm(s.initialized), norm(s.call("ping", {}))]
    out.append(norm(s.close()))
    return out


def lists(port):
    out = []
    for who, extra in [("default", {}), ("alice", {}), ("bob", {}), ("carol", {}),
                       ("default", {"X-PL-Writer": "writer-m"}),
                       ("default", {"X-PL-Writer": " Writer-M "}),
                       ("default", {"X-PL-Writer": "WRITER-M"}),
                       ("bob", {"X-PL-Writer": "writer-m"}),
                       ("default", {"X-PL-Writer": ""})]:
        s = Session(port, TOK[who], extra)
        out.append(norm(s.call("tools/list", {"_meta": {}})))
        out.append(norm(s.call("tools/list", {})))
        s.close()
    return out


def protocol_versions(port):
    out = []
    for v in ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28", "2030-01-01", "x"]:
        r = request(port, "POST", init_body(v), JSON_HDRS + [("Authorization", f"Bearer {TOK['bob']}")])
        out.append(norm(r))
        sid = header(r, "mcp-session-id")
        if sid:
            request(port, "DELETE", None, JSON_HDRS + [("Authorization", f"Bearer {TOK['bob']}"),
                                                       ("mcp-session-id", sid)])
    for params in [{}, {"protocolVersion": PROTOCOL}, {"protocolVersion": PROTOCOL, "capabilities": {},
                                                       "clientInfo": {"name": "x"}}]:
        out.append(norm(request(port, "POST", init_body(params=params),
                                JSON_HDRS + [("Authorization", f"Bearer {TOK['bob']}")])))
    return out


def methods(port):
    s = Session(port, TOK["carol"])
    out = [norm(s.call(m, {})) for m in ["prompts/list", "resources/list", "resources/templates/list",
                                         "nosuch/method", "subscriptions/listen"]]
    out.append(norm(s.call("initialize", init_body()["params"])))
    out.append(norm(s.notify("notifications/cancelled")))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 77, "result": {}}, s._headers())))
    s.close()
    return out


def calls(port):
    s = Session(port, TOK["carol"])
    out = [norm(s.tool("no_such_tool", {})),
           norm(s.call("tools/call", {"arguments": {}})),
           norm(s.call("tools/call", {"name": "memory_toolset", "arguments": [1]})),
           norm(s.call("tools/call", {"name": "memory_toolset"}))]
    s.close()
    return out


def toolset(port):
    out = []
    for who, extra in [("carol", {}), ("alice", {}), ("bob", {}),
                       ("default", {"X-PL-Writer": "writer-x"})]:
        s = Session(port, TOK[who], extra)
        for args in [{"action": "status"}, {"action": "expand"}, {"action": "status"},
                     {"action": "expand"}, {"action": "expand"}]:
            out.append(norm(s.tool("memory_toolset", args)))
        out.append(norm(s.call("tools/list", {})))
        # A new session sees the principal's override.
        s2 = Session(port, TOK[who], extra)
        out.append(norm(s2.call("tools/list", {})))
        s2.close()
        for _ in range(3):
            out.append(norm(s.tool("memory_toolset", {"action": "collapse"})))
        out.append(norm(s.tool("memory_toolset", {"action": "status"})))
        s.close()
    s = Session(port, TOK["carol"])
    for args in [{"action": "bogus"}, {}, {"actoin": "status"}, {"actoin": "x", "zzz": 1},
                 {"action": "status", "verbose": True}, {"action": 3}, {"action": None}]:
        out.append(norm(s.tool("memory_toolset", args)))
    s.close()
    return out


def list_changed_stream(port):
    """expand pushes notifications/tools/list_changed to the session's GET stream."""
    import http.client
    s = Session(port, TOK["carol"])
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("GET", "/mcp", skip_accept_encoding=True)
    for k, v in [("Accept", "text/event-stream"), ("Authorization", f"Bearer {TOK['carol']}"),
                 ("mcp-session-id", s.sid), ("mcp-protocol-version", PROTOCOL)]:
        c.putheader(k, v)
    c.endheaders()
    g = c.getresponse()
    head = {"status": g.status, "headers": [(k.lower(), v) for k, v in g.getheaders()], "body": b""}
    second = request(port, "GET", None, [("Accept", "text/event-stream"),
                                         ("Authorization", f"Bearer {TOK['carol']}"),
                                         ("mcp-session-id", s.sid)])
    s.tool("memory_toolset", {"action": "expand"})
    buf = b""
    while b"\r\n\r\n" not in buf.lstrip(b": ping"):
        chunk = g.read1(4096)
        if not chunk:
            break
        buf += chunk
        if b"data: " in buf and buf.endswith(b"\r\n\r\n"):
            break
    head["body"] = buf
    s.tool("memory_toolset", {"action": "collapse"})
    s.close()
    c.close()
    return [norm(head) | {"body": {"sse": sse_data(head)}}, norm(second)]


def transport(port):
    auth = [("Authorization", f"Bearer {TOK['carol']}")]
    out = []
    s = Session(port, TOK["carol"])
    h = s._headers()
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                            [("Content-Type", "application/json"), ("Accept", "application/json")]
                            + h[2:])))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                            [("Content-Type", "text/event-stream, */*")] + h[1:])))
    out.append(norm(request(port, "POST", b'{"jsonrpc":"2.0","id":9,"method":"ping"}',
                            [("Content-Type", "text/plain")] + h[1:])))
    out.append(norm(request(port, "POST", b'{"jsonrpc":"2.0","id":9,"method":"ping"}',
                            [("Content-Type", "application/json-seq")] + h[1:])))
    out.append(norm(request(port, "POST", b'{"jsonrpc":"2.0","id":9,"method":"ping"}',
                            [("Content-Type", "application/json; charset=utf-8")] + h[1:])))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                            [("Content-Type", "application/json"), ("Accept", "*/*")] + h[2:])))
    out.append(norm(request(port, "POST", b'{"jsonrpc":', h)))
    out.append(norm(request(port, "POST", {"id": 9, "method": "ping"}, h)))
    out.append(norm(request(port, "POST", [{"jsonrpc": "2.0", "id": 9, "method": "ping"}], h)))
    out.append(norm(request(port, "PUT", b"{}", h)))
    out.append(norm(request(port, "GET", None, [("Accept", "application/json")] + auth
                            + [("mcp-session-id", s.sid)])))
    out.append(norm(s.close()))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, h)))
    out.append(norm(request(port, "DELETE", None, h)))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                            JSON_HDRS + auth)))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                            JSON_HDRS + auth + [("mcp-session-id", "deadbeef" * 4)])))
    out.append(norm(request(port, "DELETE", None, JSON_HDRS + auth)))
    out.append(norm(request(port, "GET", None, [("Accept", "text/event-stream")] + auth)))
    out.append(norm(request(port, "PATCH", b"{}", JSON_HDRS + auth)))
    out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                            JSON_HDRS + auth + [("mcp-protocol-version", "1999-01-01")])))
    out.append(norm(request(port, "POST", init_body(), [("Content-Type", "application/json"),
                                                        ("Accept", "text/event-stream")] + auth)))
    return out


def paths(port):
    import http.client
    out = []
    for path, hdrs in [("/mcp/", {"Authorization": f"Bearer {TOK['carol']}"}),
                       ("/mcp/x", {"Authorization": f"Bearer {TOK['carol']}"}),
                       ("/mcp", {}),
                       ("/mcp", {"Authorization": "Bearer nope"})]:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", path, body=json.dumps(init_body()),
                  headers={"Content-Type": "application/json", "Accept": ACCEPT, **hdrs})
        r = c.getresponse()
        resp = {"status": r.status, "headers": [(k.lower(), v) for k, v in r.getheaders()],
                "body": r.read()}
        c.close()
        n = norm(resp)
        if "location" in n["headers"]:
            n["headers"]["location"] = n["headers"]["location"].replace(str(port), "<port>")
        out.append(n)
    return out


def stub_tools(port):
    """One call per tool, to compare or (unserved on Rust) to check the stub."""
    s = Session(port, TOK["carol"])
    s.tool("memory_toolset", {"action": "expand"})
    out = {name: norm(s.tool(name, {})) for name in UNSERVED}
    s.tool("memory_toolset", {"action": "collapse"})
    s.close()
    return out


CASES = [handshake, lists, protocol_versions, methods, calls, toolset, list_changed_stream,
         transport, paths]


def stub_expected(name):
    payload = {"error": "not_implemented",
               "message": f"not_implemented: {name} is not yet served by the Rust daemon"}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    data = json.dumps({"content": [{"text": text, "type": "text"}], "isError": True,
                       "structuredContent": payload}, separators=(",", ":"), ensure_ascii=False)
    return data


def run_all(port):
    return {c.__name__: c(port) for c in CASES}


def diff_case(name, a, b, diffs):
    if len(a) != len(b):
        diffs.append(f"{name}: {len(a)} vs {len(b)} responses")
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            diffs.append(f"{name}[{i}]: python={json.dumps(x)[:600]}\n          rust  ={json.dumps(y)[:600]}")


def check_stubs(py_stub, rust_stub, diffs, served):
    for name, resp in rust_stub.items():
        if name in UNSERVED:
            data = resp["body"].get("sse", [None])[0]
            want_suffix = stub_expected(name)
            if resp["status"] != 200 or data is None or not data.endswith(f'"result":{want_suffix}}}'):
                diffs.append(f"stub {name}: {json.dumps(resp)[:400]}")
        elif py_stub is not None and py_stub.get(name) != resp:
            diffs.append(f"served {name}: python={json.dumps(py_stub.get(name))[:400]} rust={json.dumps(resp)[:400]}")
        served.append(name)


def main():
    mode = sys.argv[1]
    if mode == "record":
        port, out = int(sys.argv[2]), sys.argv[3]
        data = run_all(port)
        data["stub_tools"] = stub_tools(port)
        Path(out).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"recorded {sum(len(v) for v in data.values())} responses to {out}")
        return 0
    if mode == "compare":
        py, rs, out = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
        a = run_all(py)
        b = run_all(rs)
        a_stub, b_stub = None, stub_tools(rs)
    elif mode == "golden":
        a = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        a_stub = a.pop("stub_tools", None)
        rs, out = int(sys.argv[3]), sys.argv[4]
        b = run_all(rs)
        b_stub = stub_tools(rs)
    else:
        print(__doc__)
        return 2
    diffs = []
    for c in CASES:
        diff_case(c.__name__, a[c.__name__], b[c.__name__], diffs)
    served = []
    check_stubs(a_stub, b_stub, diffs, served)
    total = sum(len(v) for v in a.values()) + len(b_stub)
    summary = {"responses": total, "cases": len(CASES) + 1, "diffs": len(diffs),
               "unserved_checked": sorted(n for n in served if n in UNSERVED)}
    Path(out).write_text(json.dumps({"summary": summary, "diffs": diffs}, indent=1) + "\n",
                         encoding="utf-8")
    for d in diffs:
        print("DIFF", d)
    print(json.dumps(summary))
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
