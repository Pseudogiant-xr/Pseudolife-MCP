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
from mcp_wire import ACCEPT, PROTOCOL, Session, header, request, sse_data, sse_events  # noqa: E402

TOK = {"default": "tok-default-0001", "alice": "tok-alice-0001", "bob": "tok-bob-0001",
       "carol": "tok-carol-0001",
       # Stored principals seeded by mcp_run.py: dave minimal, erin no tier, frank full.
       "dave": "tok-dave-0001", "erin": "tok-erin-0001", "frank": "tok-frank-0001"}
JSON_HDRS = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
# Free text after these prefixes (spec M-free): pydantic and jiter wording.
FREE_PREFIXES = ("Parse error: ", "Validation error: ")
# Bodies not yet served by the Rust daemon: tool -> owning slice. Mirrors
# rust/daemon/divergences.md; delete a row when its body is wired.
UNSERVED = {
    "document_search": "W2-D",
    "document_ingest": "W2-E",
    "memory_dream": "W3-H",
}


BINDING = json.loads((Path(__file__).resolve().parents[1] / "src/mcp/binding.json")
                     .read_text(encoding="utf-8"))["tools"]


def binding_args(name):
    """Each required parameter of the tool at a value its binding accepts."""
    from mcp_binding_sweep import baseline
    return {p["name"]: baseline(p["schema"])
            for p in BINDING[name]["params"] if "default" not in p}


def init_body(version=PROTOCOL, params=None):
    p = params if params is not None else {
        "protocolVersion": version, "capabilities": {},
        "clientInfo": {"name": "mcp", "version": "0.1.0"}, "_meta": {}}
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": p}


def norm(resp):
    """The comparable view of one response."""
    keep = {}
    for k in ("content-type", "allow", "location", "cache-control", "connection",
              "x-accel-buffering"):
        v = header(resp, k)
        if v is not None:
            keep[k] = v
    sid = header(resp, "mcp-session-id")
    # The echoed id must be the request's own; a fresh one is only present.
    keep["mcp-session-id"] = (None if sid is None
                              else "<request>" if sid == resp.get("req_sid") else "<fresh>")
    ctype = header(resp, "content-type") or ""
    if ctype == "application/json; charset=utf-8":
        # The Console gate's `_send_json` (W1-A's contract): values, not bytes.
        return {"status": resp["status"], "headers": keep,
                "body": {"json": json.loads(resp["body"] or b"null")}}
    if ctype.startswith("text/event-stream"):
        body = {"sse": sse_data(resp), "events": sse_events(resp)}
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
    for m, p in [("tools/list", {"cursor": 5}), ("tools/list", {"cursor": "x"}),
                 ("tools/list", {"cursor": None}), ("prompts/list", {"cursor": 1}),
                 ("ping", {"_meta": 5}), ("ping", {"_meta": {}}),
                 ("tools/call", {"name": "memory_toolset", "arguments": {"action": "status"},
                                 "_meta": 5}),
                 ("tools/call", {"name": 5}), ("prompts/get", {}), ("prompts/get", {"name": "x"}),
                 ("resources/read", {}), ("resources/read", {"uri": "x://y"}),
                 ("resources/subscribe", {}), ("resources/subscribe", {"uri": "x://y"}),
                 ("resources/unsubscribe", {"uri": "x://y"}),
                 ("initialize", {"protocolVersion": PROTOCOL, "capabilities": {"roots": 5},
                                 "clientInfo": {"name": "a", "version": "1"}})]:
        out.append(norm(s.call(m, p)))
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
                 {"action": "status", "verbose": True}, {"action": 3}, {"action": None},
                 # SequenceMatcher is not symmetric: difflib scores (candidate, word).
                 {"iain": "status"}, {"cint": 1}]:
        out.append(norm(s.tool("memory_toolset", args)))
    s.close()
    return out


def stored(port):
    """A stored principal's row tier sits after the tier map, for its own bearer only."""
    out = []
    for who, extra in [("dave", {}), ("erin", {}), ("frank", {}),
                       ("default", {"X-PL-Writer": "dave"}), ("default", {"X-PL-Writer": "frank"})]:
        s = Session(port, TOK[who], extra)
        out.append(norm(s.call("tools/list", {})))
        for args in [{"action": "status"}, {"action": "collapse"}, {"action": "expand"},
                     {"action": "collapse"}, {"action": "status"}]:
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
                 ("mcp-session-id", s.sid or ""), ("mcp-protocol-version", PROTOCOL)]:
        c.putheader(k, v)
    c.endheaders()
    g = c.getresponse()
    head = {"status": g.status, "headers": [(k.lower(), v) for k, v in g.getheaders()], "body": b""}
    second = request(port, "GET", None, [("Accept", "text/event-stream"),
                                         ("Authorization", f"Bearer {TOK['carol']}"),
                                         ("mcp-session-id", s.sid or "")])
    def next_event(buf):
        # Read until one complete data event (keep-alive comments skipped).
        while not any(e.startswith(b"event:") or b"\r\ndata: " in e or e.startswith(b"data: ")
                      for e in buf.split(b"\r\n\r\n")[:-1]):
            chunk = g.read1(4096)
            if not chunk:
                break
            buf += chunk
        return buf

    s.tool("memory_toolset", {"action": "expand"})
    buf = next_event(b"")
    s.tool("memory_toolset", {"action": "collapse"})
    n_before = buf.count(b"data: ")
    while buf.count(b"data: ") == n_before:
        chunk = g.read1(4096)
        if not chunk:
            break
        buf += chunk
    head["body"] = buf
    s.close()
    c.close()
    return [norm(head) | {"body": {"sse": sse_data(head), "events": sse_events(head)}},
            norm(second)]


def stream_reconnect(port):
    """A client that drops its GET stream can open a new one at once
    (review finding, 2026-10-09: the Rust stream held the slot until its next
    15 s keep-alive, so the reconnect answered 409)."""
    import http.client
    import time
    s = Session(port, TOK["carol"])
    hdrs = [("Accept", "text/event-stream"), ("Authorization", f"Bearer {TOK['carol']}"),
            ("mcp-session-id", s.sid or ""), ("mcp-protocol-version", PROTOCOL)]
    out = []
    for _ in range(3):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.putrequest("GET", "/mcp", skip_accept_encoding=True)
        for k, v in hdrs:
            c.putheader(k, v)
        c.endheaders()
        g = c.getresponse()
        out.append({"status": g.status, "content-type": g.getheader("content-type")})
        c.sock.close()
        c.close()
        time.sleep(1.0)
    s.close()
    return out


def init_gate(port):
    """Refused or unanswered first requests still register a session, which
    then refuses every request but ping until it is initialized."""
    auth = [("Authorization", f"Bearer {TOK['carol']}")]
    out = []

    def on(sid):
        return JSON_HDRS + auth + [("mcp-session-id", sid or ""), ("mcp-protocol-version", PROTOCOL)]

    r = request(port, "POST", init_body(params={}), JSON_HDRS + auth)
    out.append(norm(r))
    sid = header(r, "mcp-session-id")
    for body in [{"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}]:
        out.append(norm(request(port, "POST", body, on(sid))))
    request(port, "DELETE", None, on(sid))
    for first in [
        (JSON_HDRS + auth, {"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        ([("Content-Type", "application/json"), ("Accept", "application/json")] + auth, init_body()),
        ([("Content-Type", "text/plain"), ("Accept", ACCEPT)] + auth, init_body()),
        (JSON_HDRS + auth, b'{"jsonrpc":'),
    ]:
        r = request(port, "POST", first[1], first[0])
        out.append(norm(r))
        sid = header(r, "mcp-session-id")
        if sid:
            out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 2, "method": "ping"}, on(sid))))
            out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                                    on(sid))))
            request(port, "DELETE", None, on(sid))
    for m in ("GET", "DELETE", "PUT"):
        r = request(port, m, None, [("Accept", ACCEPT)] + auth)
        out.append(norm(r))
        sid = header(r, "mcp-session-id")
        if sid:
            out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 2, "method": "ping"}, on(sid))))
            request(port, "DELETE", None, on(sid))
    return out


def body_limit(port):
    """The SDK's 4 MiB body limit answers 413 before sessions or routing."""
    auth = [("Authorization", f"Bearer {TOK['carol']}")]
    big = b'{"jsonrpc":"2.0","id":5,"method":"ping","x":"' + b"a" * (4 * 1024 * 1024) + b'"}'
    head = b'{"jsonrpc":"2.0","id":5,"method":"ping","x":"'
    edge = head + b"a" * (4 * 1024 * 1024 - len(head) - 2) + b'"}'  # exactly 4 MiB: accepted
    s = Session(port, TOK["carol"])
    out = [norm(request(port, "POST", big, s._headers())),
           norm(request(port, "POST", edge, s._headers())),
           norm(request(port, "POST", edge + b" ", s._headers())),
           norm(request(port, "POST", big, JSON_HDRS + auth + [("mcp-session-id", "deadbeef" * 4)])),
           norm(request(port, "GET", None, auth + [("Content-Length", "9999999")])),
           norm(request(port, "GET", None, auth + [("Content-Length", " 9999999 ")]))]
    # A malformed Content-Length is refused by the HTTP parser (uvicorn vs
    # hyper) before any application code: declared, not compared.
    s.close()
    return out


def messages(port):
    """jsonrpc_message_adapter's union: request, notification, response, error."""
    s = Session(port, TOK["carol"])
    out = []
    for b in [{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},
              {"jsonrpc": "2.0", "id": 77, "result": 5},
              {"jsonrpc": "2.0", "id": 78, "result": {}},
              {"jsonrpc": "2.0", "id": None, "method": "ping"},
              {"jsonrpc": "2.0", "id": True, "method": "ping"},
              {"jsonrpc": "2.0", "id": 1.5, "method": "ping"},
              {"jsonrpc": "2.0", "id": 2.0, "method": "ping"},
              {"jsonrpc": "2.0", "id": "7", "method": "ping"},
              {"jsonrpc": "2.0", "method": "ping", "params": 5},
              {"jsonrpc": "2.0", "id": 3, "error": {"code": 1, "message": "x"}},
              {"jsonrpc": "2.0", "id": None, "error": {"code": 1, "message": "x"}},
              {"jsonrpc": "2.0", "id": 3, "error": 5},
              {"jsonrpc": "1.0", "id": 3, "method": "ping"}]:
        out.append(norm(request(port, "POST", b, s._headers())))
    s.close()
    return out


def concurrent(port):
    """Eight requests in flight on one session at once."""
    import threading
    s = Session(port, TOK["carol"])
    results = [None] * 8
    bodies = [("tools/list", {}), ("ping", {}), ("tools/call", {"name": "memory_toolset",
                                                               "arguments": {"action": "status"}}),
              ("prompts/list", {})] * 2

    def go(i):
        m, p = bodies[i]
        results[i] = request(port, "POST", {"jsonrpc": "2.0", "id": 100 + i, "method": m,
                                            "params": p}, s._headers())

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.close()
    return [norm(r) for r in results]


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
                            + [("mcp-session-id", s.sid or "")])))
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
                       ("/mcp//", {"Authorization": f"Bearer {TOK['carol']}"}),
                       ("/mcp///?a=1", {"Authorization": f"Bearer {TOK['carol']}"}),
                       ("/mcp/x/", {"Authorization": f"Bearer {TOK['carol']}"}),
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
    try:
        return _stub_tools(port)
    except Exception as exc:  # noqa: BLE001
        return {"<exception>": {"exception": f"{type(exc).__name__}: {exc}"}}


def _stub_tools(port):
    # Arguments that bind, so an unserved body answers its stub.
    s = Session(port, TOK["carol"])
    s.tool("memory_toolset", {"action": "expand"})
    out = {name: norm(s.tool(name, binding_args(name))) for name in UNSERVED}
    s.tool("memory_toolset", {"action": "collapse"})
    s.close()
    return out


def rebinding(port):
    """Tokenless installs keep the SDK's loopback Host/Origin allowlist."""
    out = []
    ping = {"jsonrpc": "2.0", "id": 9, "method": "ping"}
    for host, origin in [(f"127.0.0.1:{port}", None), ("localhost:1", None), ("[::1]:5", None),
                         ("127.0.0.1", None), ("evil.example:80", None), ("localhost", None),
                         ("127.0.0.1.evil:1", None),
                         (f"127.0.0.1:{port}", "http://localhost:3000"),
                         (f"127.0.0.1:{port}", "http://evil.example"),
                         (f"127.0.0.1:{port}", "https://127.0.0.1:1"),
                         (f"127.0.0.1:{port}", "http://127.0.0.1"),
                         (f"127.0.0.1:{port}", "")]:
        extra = [("Host", host)] + ([("Origin", origin)] if origin is not None else [])
        r = request(port, "POST", init_body(), JSON_HDRS + extra)
        out.append(norm(r))
        sid = header(r, "mcp-session-id")
        if r["status"] == 200 and sid:
            out.append(norm(request(port, "POST", ping, JSON_HDRS + extra
                                    + [("mcp-session-id", sid), ("mcp-protocol-version", PROTOCOL)])))
            request(port, "DELETE", None, [("Host", f"127.0.0.1:{port}"), ("mcp-session-id", sid)])
    good = [("Host", f"127.0.0.1:{port}")]
    r = request(port, "POST", init_body(), JSON_HDRS + good)
    sid = header(r, "mcp-session-id")
    request(port, "DELETE", None, good + [("mcp-session-id", sid)])
    for host in (f"127.0.0.1:{port}", "evil.example:1"):
        out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                JSON_HDRS + [("Host", host), ("mcp-session-id", sid)])))
        out.append(norm(request(port, "POST", {"jsonrpc": "2.0", "id": 9, "method": "ping"},
                                JSON_HDRS + [("Host", host), ("mcp-session-id", "deadbeef" * 4)])))
    out.append(norm(request(port, "GET", None, [("Host", "evil.example:1"),
                                               ("Accept", "text/event-stream")])))
    out.append(norm(request(port, "DELETE", None, [("Host", "evil.example:1")])))
    out.append(norm(request(port, "POST", b"x", [("Host", "evil.example:1"),
                                                 ("Content-Type", "text/plain")])))
    return out


def open_lists(port):
    """With no token every caller is the default principal; X-PL-Writer keys the tier."""
    out = []
    for extra in [{}, {"X-PL-Writer": "writer-m"}, {"Authorization": "Bearer anything"}]:
        s = Session(port, None, extra)
        out.append(norm(s.call("tools/list", {})))
        out.append(norm(s.tool("memory_toolset", {"action": "status"})))
        s.close()
    return out


def config_lists(port):
    """PSEUDOLIFE_WRITER_ID keys the default principal's tier; the default tier
    and tier map parse leniently (mcp_run.py --config2)."""
    out = []
    for who, extra in [("default", {}), ("default", {"X-PL-Writer": "writer-x"}),
                       ("alice", {}), ("bob", {}), ("carol", {})]:
        s = Session(port, TOK[who], extra)
        out.append(norm(s.call("tools/list", {})))
        out.append(norm(s.tool("memory_toolset", {"action": "status"})))
        s.close()
    return out


def tool_binding(port):
    """Binding precedes every body, served or not (binding.rs): an unknown
    name for each tool, then, for a tool with required parameters, none."""
    s = Session(port, TOK["carol"])
    out = []
    for name, table in BINDING.items():
        out.append(norm(s.tool(name, {"zzz": 1})))
        if any("default" not in p for p in table["params"]):
            out.append(norm(s.tool(name, {})))
    # The transport refuses an integer literal past 4,300 characters as a
    # parse error before binding (json.dumps cannot write one).
    raw = (b'{"jsonrpc":"2.0","id":99,"method":"tools/call","params":{"name":"memory_get",'
           b'"arguments":{"entry_id":' + b"1" * 4301 + b"}}}")
    out.append(norm(request(port, "POST", raw, s._headers())))
    s.close()
    return out


def _board_context(port, token):
    """The bearer's own board context (bank id, principal) on this daemon."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        c.request("POST", "/api/coordination/context", body=b"{}",
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {token}"})
        r = c.getresponse()
        body = r.read()
    finally:
        c.close()
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def bound_identity(port):
    """X-PL-Bank / X-PL-Principal must name the bearer's own board context
    before the MCP app sees the request (web/api.py). Each daemon is asked
    with its own bank id, which differs between the two banks. The default
    principal is on the board's default allow list; carol is not, and the
    last calls show that refusal too."""
    ctx = _board_context(port, TOK["default"])
    bank = ctx.get("bank_id") if isinstance(ctx.get("bank_id"), str) else "no-bank"
    principal = ctx.get("principal") if isinstance(ctx.get("principal"), str) else "default"
    bearer = [("Authorization", f"Bearer {TOK['default']}")]
    out = []
    for extra in [
        [], [("X-PL-Bank", bank)], [("X-PL-Principal", principal)],
        [("X-PL-Bank", ""), ("X-PL-Principal", principal)],
        [("X-PL-Bank", bank), ("X-PL-Principal", principal)],
        [("X-PL-Bank", "0" * 64), ("X-PL-Principal", principal)],
        [("X-PL-Bank", bank), ("X-PL-Principal", "mallory")],
        [("X-PL-Bank", bank), ("X-PL-Principal", "%63arol")],
        [("X-PL-Bank", bank), ("X-PL-Principal", "%ZZ")],
        [("X-PL-Bank", bank), ("X-PL-Principal", "%0A")],
        [("X-PL-Bank", bank), ("X-PL-Principal", "%FF")],
        [("X-PL-Bank", bank), ("X-PL-Principal", "a" * 257)],
        # A repeated header: the last value is the one checked.
        [("X-PL-Bank", bank), ("X-PL-Principal", "mallory"), ("X-PL-Principal", principal)],
        [("X-PL-Bank", bank), ("X-PL-Principal", principal), ("X-PL-Principal", "mallory")],
    ]:
        out.append(norm(request(port, "POST", init_body(), JSON_HDRS + bearer + extra)))
    # A live session's later request is checked too, and a matching binding
    # still reaches the tools.
    s = Session(port, TOK["default"], {"X-PL-Bank": bank, "X-PL-Principal": principal})
    out.append(norm(s.init))
    out.append(norm(s.call("tools/list", {})))
    s.base = [h for h in s.base if h[0] != "X-PL-Principal"] + [("X-PL-Principal", "mallory")]
    out.append(norm(s.call("tools/list", {})))
    out.append(norm(s.close()))
    out.append(norm(request(port, "POST", init_body(), JSON_HDRS + [
        ("Authorization", f"Bearer {TOK['carol']}"), ("X-PL-Bank", bank),
        ("X-PL-Principal", "carol")])))
    return out


CASES = [handshake, lists, protocol_versions, methods, calls, toolset, stored, list_changed_stream,
         stream_reconnect, init_gate, body_limit, messages, concurrent, transport, paths,
         tool_binding, bound_identity]
BASE_CASES = tuple(CASES)
CASE_RESPONSE_COUNTS = {
    'handshake': 4, 'lists': 18, 'protocol_versions': 10, 'methods': 24,
    'calls': 4, 'toolset': 53, 'stored': 30, 'list_changed_stream': 2,
    'stream_reconnect': 3, 'init_gate': 21, 'body_limit': 6,
    'messages': 13, 'concurrent': 8, 'transport': 21, 'paths': 7, 'tool_binding': 71,
    'bound_identity': 19,
    'rebinding': 24, 'open_lists': 6, 'config_lists': 10,
}
if "--tokenless" in sys.argv:
    CASES = [rebinding, open_lists]
if "--config2" in sys.argv:
    CASES = [config_lists]


def stub_expected(name):
    payload = {"error": "not_implemented",
               "message": f"not_implemented: {name} is not yet served by the Rust daemon"}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    data = json.dumps({"content": [{"text": text, "type": "text"}], "isError": True,
                       "structuredContent": payload}, separators=(",", ":"), ensure_ascii=False)
    return data


def run_case(case, port):
    """A case that raises is recorded as one response-shaped failure, so a
    broken side shows as a diff rather than a harness crash."""
    try:
        return case(port)
    except Exception as exc:  # noqa: BLE001
        return [{"exception": f"{type(exc).__name__}: {exc}"}]


def run_all(port):
    return {c.__name__: run_case(c, port) for c in CASES}


def ntools(resp):
    """Tool count in a tools/list response, or None."""
    try:
        return len(json.loads(resp["body"]["sse"][0])["result"]["tools"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def sanity(data, diffs):
    """Checks on the oracle's own answers that each case exercised the path
    it is meant to (a case both sides answer 401 would compare equal)."""
    want = {
        # lists: default, alice, bob, carol, writer-m x3, bob+writer-m, empty writer (two lists each)
        "lists": [24, 24, 10, 10, 24, 24, 24, 24, 10, 10, 10, 10, 10, 10, 24, 24, 24, 24],
    }
    if "stored" in data:
        st = data["stored"]
        # dave, erin, frank, then the default principal with X-PL-Writer naming
        # dave (dave's own expand/collapse left an override in the shared
        # writer-id bucket: minimal) and frank (no override, and a stored
        # tier applies to its own bearer only: the default, core).
        want_stored = [10, 24, 38, 10, 24]
        got = [ntools(st[i * 6]) for i in range(5)]
        if got != want_stored:
            diffs.append(f"sanity stored: tool counts {got} != {want_stored}")
    for case, counts in want.items():
        if case in data:
            got = [ntools(r) for r in data[case]]
            if got != counts:
                diffs.append(f"sanity {case}: tool counts {got} != {counts}")
    if "config_lists" in data:
        got = [ntools(r) for r in data["config_lists"][::2]]
        # default via PSEUDOLIFE_WRITER_ID=writer-m, writer-x (bogus default:
        # full), alice, bob (the later valid entry), carol (full)
        if got != [10, 38, 10, 24, 38]:
            diffs.append(f"sanity config_lists: tool counts {got}")
    if "bound_identity" in data:
        # The oracle took each path: no binding, refusals, matches (including
        # the last of a repeated header) and an unadmitted principal.
        got = [r["status"] for r in data["bound_identity"]]
        want_bound = [200, 409, 409, 409, 200, 409, 409, 409, 409, 409, 409, 409, 200, 409,
                      200, 200, 409, 409, 403]
        if got != want_bound:
            diffs.append(f"sanity bound_identity: statuses {got} != {want_bound}")
    if "rebinding" in data:
        statuses = sorted({r["status"] for r in data["rebinding"]})
        # 400: a POST's Content-Type is checked before Host; 404: an unknown
        # session id is answered before the Host check.
        if statuses != [200, 400, 403, 404, 421]:
            diffs.append(f"sanity rebinding: statuses {statuses}")


def diff_case(name, a, b, diffs):
    if len(a) != len(b):
        diffs.append(f"{name}: {len(a)} vs {len(b)} responses")
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            diffs.append(f"{name}[{i}]: python={json.dumps(x)[:600]}\n          rust  ={json.dumps(y)[:600]}")


def check_stubs(py_stub, rust_stub, diffs, served):
    for name, resp in rust_stub.items():
        if "exception" in resp:
            diffs.append(f"stub calls raised: {resp['exception']}")
        elif name in UNSERVED:
            data = (resp["body"].get("sse") or [None])[0]
            try:
                rid = json.loads(data)["id"]
            except (TypeError, ValueError, KeyError):
                rid = None
            want = '{"jsonrpc":"2.0","id":%s,"result":%s}' % (json.dumps(rid), stub_expected(name))
            if resp["status"] != 200 or not isinstance(rid, int) or data != want:
                diffs.append(f"stub {name}: {json.dumps(resp)[:400]}")
        elif py_stub is not None and py_stub.get(name) != resp:
            diffs.append(f"served {name}: python={json.dumps(py_stub.get(name))[:400]} rust={json.dumps(resp)[:400]}")
        served.append(name)


def recording_errors(data, cases=None):
    """Require a complete, failure-free oracle corpus before saving goldens."""
    names = {case.__name__ for case in (CASES if cases is None else cases)}
    side_config = names in ({'config_lists'}, {'rebinding', 'open_lists'})
    expected = names if side_config else names | {'stub_tools'}
    if not isinstance(data, dict):
        return ['record must be an object']
    errors = []
    if missing := expected - data.keys():
        errors.append('missing cases: ' + ', '.join(sorted(missing)))
    if extra := data.keys() - expected:
        errors.append('unexpected cases: ' + ', '.join(sorted(extra)))
    for name in sorted(names & data.keys()):
        responses = data[name]
        if not isinstance(responses, list) or not responses:
            errors.append(f'{name}: missing response sequence')
            continue
        if len(responses) != CASE_RESPONSE_COUNTS[name]:
            errors.append(f'{name}: {len(responses)} responses, expected {CASE_RESPONSE_COUNTS[name]}')
        for index, response in enumerate(responses):
            if not isinstance(response, dict) or 'exception' in response or 'timeout' in response:
                errors.append(f'{name}[{index}]: oracle case failed')
    if not side_config:
        stubs = data.get('stub_tools')
        if not isinstance(stubs, dict) or set(stubs) != set(UNSERVED):
            errors.append('stub_tools: incomplete tool corpus')
        elif any(not isinstance(response, dict) or 'exception' in response or 'timeout' in response
                 for response in stubs.values()):
            errors.append('stub_tools: oracle call failed')
    total = sum(len(value) for value in data.values() if isinstance(value, (list, dict)))
    expected_total = 10 if names == {'config_lists'} else 30 if side_config else 317
    if total != expected_total:
        errors.append(f'corpus: {total} responses, expected {expected_total}')
    if not errors:
        try:
            sanity(data, errors)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            errors.append(f'oracle sanity check failed: {type(exc).__name__}')
    return errors


def main():
    mode = sys.argv[1]
    if mode == "record":
        port, out = int(sys.argv[2]), sys.argv[3]
        data = run_all(port)
        if "--tokenless" not in sys.argv and "--config2" not in sys.argv:
            data["stub_tools"] = stub_tools(port)
        if errors := recording_errors(data):
            print('MCP recording rejected: ' + '; '.join(errors), file=sys.stderr)
            return 2
        Path(out).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"recorded {sum(len(v) for v in data.values())} responses to {out}")
        return 0
    if mode == "compare":
        py, rs, out = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
        a = run_all(py)
        b = run_all(rs)
        side_configs = "--tokenless" in sys.argv or "--config2" in sys.argv
        a_stub, b_stub = None, ({} if side_configs else stub_tools(rs))
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
    sanity(a, diffs)
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
