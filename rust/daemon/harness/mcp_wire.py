"""Raw streamable-HTTP MCP client for the W2-G harness (standard library only).

Speaks the canonical shape the shims and the Python SDK client send: POST
initialize, POST notifications/initialized, POST requests with the session id
and MCP-Protocol-Version 2025-11-25, then DELETE. Every response keeps its raw
status, headers and body bytes so the harness can diff them exactly.
"""
import http.client
import json

PROTOCOL = "2025-11-25"
ACCEPT = "application/json, text/event-stream"


def request(port, method, body, headers, timeout=120):
    """One HTTP exchange; ``body`` is bytes, a JSON-able value, or None."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    c.putrequest(method, "/mcp", skip_accept_encoding=True)
    pairs = headers.items() if isinstance(headers, dict) else headers
    for k, v in pairs:
        c.putheader(k, v)
    if data is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    raw = r.read()
    hdrs = [(k.lower(), v) for k, v in r.getheaders()]
    c.close()
    return {"status": r.status, "headers": hdrs, "body": raw}


def header(resp, name):
    for k, v in resp["headers"]:
        if k == name:
            return v
    return None


def sse_data(resp):
    """The data payloads of an SSE body, as raw strings, in order."""
    out = []
    for event in resp["body"].decode("utf-8").split("\r\n\r\n"):
        lines = [ln for ln in event.split("\r\n") if ln.startswith("data: ")]
        if lines:
            out.append("\n".join(ln[6:] for ln in lines))
    return out


class Session:
    """One initialized MCP session (legacy handshake era)."""

    def __init__(self, port, token=None, extra=None):
        self.port = port
        self.base = [("Content-Type", "application/json"), ("Accept", ACCEPT)]
        if token:
            self.base.append(("Authorization", f"Bearer {token}"))
        self.base += list((extra or {}).items())
        self.next_id = 0
        self.sid = None
        self.init = request(port, "POST", {
            "jsonrpc": "2.0", "id": self._id(), "method": "initialize",
            "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                       "clientInfo": {"name": "mcp", "version": "0.1.0"}, "_meta": {}}},
            self.base)
        self.sid = header(self.init, "mcp-session-id")
        self.initialized = self.notify("notifications/initialized")

    def _id(self):
        self.next_id += 1
        return self.next_id

    def _headers(self):
        h = list(self.base)
        if self.sid:
            h += [("mcp-session-id", self.sid), ("mcp-protocol-version", PROTOCOL)]
        return h

    def notify(self, method):
        return request(self.port, "POST", {"jsonrpc": "2.0", "method": method}, self._headers())

    def call(self, method, params):
        return request(self.port, "POST", {"jsonrpc": "2.0", "id": self._id(), "method": method,
                                           "params": params}, self._headers())

    def tool(self, name, arguments):
        return self.call("tools/call", {"name": name, "arguments": arguments, "_meta": {}})

    def close(self):
        return request(self.port, "DELETE", None, self._headers())


def json_spans(text, start):
    """Byte-exact spans of the elements of the JSON array opening at ``text[start]``."""
    assert text[start] == "["
    spans, depth, i, in_str, elem = [], 0, start, False, None
    while True:
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            if depth == 1 and elem is None:
                elem = i
        elif ch in "[{":
            depth += 1
            if depth == 2 and elem is None:
                elem = i
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                if elem is not None:
                    spans.append(text[elem:i])
                return spans
        elif ch == "," and depth == 1:
            spans.append(text[elem:i])
            elem = None
        elif depth == 1 and elem is None and not ch.isspace():
            elem = i
        i += 1
