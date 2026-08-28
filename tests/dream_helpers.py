"""Test doubles shared by the dream-path suites.

The stub extractor existed in seven near-identical copies whose signatures
had drifted apart; this is the widest of them. ``known_facts`` is optional
because ``service_dream`` only passes it when the known-facts window is on,
so a stub accepting it works on both call paths.

Also home to the stub OpenAI-compatible chat server used by the
``OpenAICompatExtractor`` tests, which ``test_span_gate`` previously reached
for by importing another test module.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import threading


class StubExtractor:
    """Returns a fixed claim list regardless of input (drives dream_run)."""

    def __init__(self, claims):
        self._claims = claims

    def extract(self, texts, vocab, known_facts=None):
        return [dict(c) for c in self._claims]


# ── stub OpenAI-compatible server (no PG, no embedder) ───────────────────

class StubHandler(http.server.BaseHTTPRequestHandler):
    responder = None  # (status, body_str) callable, set per subclass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        status, body = type(self).responder()
        data = body.encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


@contextlib.contextmanager
def stub_server(responder):
    handler = type("H", (StubHandler,), {"responder": staticmethod(responder)})
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def chat_payload(claims):
    return json.dumps({"choices": [{"message": {
        "content": json.dumps({"claims": claims})}}]})


def chat_relations_payload(relations):
    return json.dumps({"choices": [{"message": {
        "content": json.dumps({"relations": relations})}}]})
