"""Raw streamable-HTTP MCP client for the W2-G harness (standard library only).

Speaks the canonical shape the shims and the Python SDK client send: POST
initialize, POST notifications/initialized, POST requests with the session id
and MCP-Protocol-Version 2025-11-25, then DELETE. Every response keeps its raw
status, headers and body bytes so the harness can diff them exactly.
"""
import http.client
import json
import math
import os
import socket
import struct
import threading

PROTOCOL = "2025-11-25"
ACCEPT = "application/json, text/event-stream"

# Windows discards data it has received but not yet handed to a receive once
# the peer's reset arrives. A blocking receive is posted in the kernel and
# takes the data on arrival; a CPython socket timeout waits in select() and
# only then calls a non-blocking recv, so a reset landing in between still
# loses the response: 22 of 3000 refused 4 MiB bodies with a timeout against
# 0 of 3000 blocking on a Windows 11 host (2026-10-18). There the exchange
# runs blocking, with the same timeout enforced by the kernel. Other hosts
# keep received data readable after a reset.
KERNEL_TIMEOUTS = os.name == "nt"


def request(port, method, body, headers, timeout=120, *, connection=None):
    """One exchange, closing the supplied or newly created connection afterward."""
    c = connection if connection is not None else http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    pairs = list(headers.items() if isinstance(headers, dict) else headers)
    try:
        c.putrequest(method, "/mcp", skip_accept_encoding=True,
                     skip_host=any(k.lower() == "host" for k, _ in pairs))
        for k, v in pairs:
            c.putheader(k, v)
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        if data:
            r, raw, send_exc = send_while_reading(c, method, data)
            send_error = None if send_exc is None else f"{type(send_exc).__name__}: {send_exc}"
        else:
            c.endheaders(data)
            r = c.getresponse()
            raw = r.read()
            send_error = None
        hdrs = [(k.lower(), v) for k, v in r.getheaders()]
    finally:
        c.close()
    req_sid = next((v for k, v in pairs if k.lower() == "mcp-session-id"), None)
    out = {"status": r.status, "headers": hdrs, "body": raw, "req_sid": req_sid}
    if send_error is not None:
        out["send_error"] = send_error
    return out


def send_while_reading(c, method, data, *, encode_chunked=False):
    """Read the response on a helper thread that is waiting before the request is sent.

    The Rust daemon refuses an MCP body over its 4 MiB budget after reading
    the budget, then closes with the overshoot unread, so its stack resets
    the connection (divergences.md, MCP-REFUSAL-CLEANUP). Read only after the
    send, body_limit's 413 was lost to WinError 10054 in about 1 of 100 calls
    on an idle Windows 11 host (2026-10-18). The send starts once the reader
    signals that it is about to read, and the daemon reads 4 MiB before it
    answers, which narrows the race without closing it: hosted run
    39253550659 still lost two refusals decided after 4 MiB or 256 KiB on the
    reader's first read (2026-10-20, #830). A response split across segments
    with the reset right behind can likewise lose its tail. Either raises,
    never passes. A complete response is returned even if the send
    was cut off, and the send's exception is returned beside it (``request``
    records it as ``send_error``); without one the
    read's error is raised, so a daemon that closes without a complete
    answer still fails. ``run.py`` sends its admission bodies the same way
    (``data`` an iterable of chunks with ``encode_chunked``).
    """
    if c.sock is None:
        c.connect()
    if KERNEL_TIMEOUTS:
        _kernel_timeouts(c.sock)
    r = c.response_class(c.sock, method=method)
    reading = threading.Event()
    got, failed = [], []

    def read():
        reading.set()
        try:
            r.begin()
            got.append(r.read())
        except BaseException as exc:
            failed.append(exc)

    reader = threading.Thread(target=read, name="mcp_wire-read", daemon=True)
    reader.start()
    reading.wait()
    try:
        c.endheaders(data, encode_chunked=encode_chunked)
        send_exc = None
    except Exception as exc:
        send_exc = exc
    reader.join()
    if failed:
        r.close()
        raise failed[0]
    return r, got[0], send_exc


def _kernel_timeouts(sock):
    """Make ``sock`` blocking, with its timeout kept as Winsock's DWORD milliseconds (0: none)."""
    timeout = sock.gettimeout()
    ms = 0 if timeout is None else max(1, math.ceil(timeout * 1000))
    sock.settimeout(None)
    for option in (socket.SO_RCVTIMEO, socket.SO_SNDTIMEO):
        sock.setsockopt(socket.SOL_SOCKET, option, struct.pack("I", ms))


def header(resp, name):
    for k, v in resp["headers"]:
        if k == name:
            return v
    return None


def sse_events(resp):
    """The SSE body's events as raw text blocks, keep-alive comments dropped."""
    out = []
    for event in resp["body"].decode("utf-8").split("\r\n\r\n"):
        lines = [ln for ln in event.split("\r\n") if ln and not ln.startswith(":")]
        if lines:
            out.append("\r\n".join(lines))
    return out


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
