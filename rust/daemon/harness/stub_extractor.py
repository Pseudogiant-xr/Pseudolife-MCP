"""A stub OpenAI-style chat-completions endpoint for the dream extractor.

Both daemons' dream extractor clients POST ``{base_url}/chat/completions``,
probe ``GET {base without /v1}/health`` and read ``GET {base_url}/models``.
This stub answers those routes from a declared table of replies and records
every request it receives, so the differential harness can compare the two
daemons' request sequences byte for byte.

Routes (primary and fallback):

* ``GET /health``, ``GET /fallback/health``: 200 ``{"status": "ok"}``, or
  the int status in ``spec["health"]`` (``spec["fallback_health"]`` for the
  fallback), so a scenario can make an endpoint unhealthy; a list of ints
  answers successive probes in turn, its last entry repeating, so an
  endpoint can go down and come back;
* ``GET /v1/models``, ``GET /fallback/v1/models``: the served model id;
* ``POST /v1/chat/completions``, ``POST /fallback/v1/chat/completions``:
  a table lookup, by the SHA-256 of the exact body bytes first, then by
  ``(kind, user message content)``. An entry may answer ``status`` with a
  raw ``body`` instead of a reply, a ``reason`` phrase for the status line
  and extra response ``headers`` (a redirect's ``Location``);
* the merge judge's second and third opinion endpoints, ``/second/v1/...``
  and ``/third/v1/...`` (chat and models): an entry naming
  ``"endpoint": "second"`` / ``"third"`` answers only there, and an entry
  naming none only on the primary and fallback bases; each endpoint
  replies under its own served model (``second_reply_model``,
  ``third_reply_model``, else ``reply_model``, unless the entry names a
  ``model``);
* anything else: 404.

A ``judge_merges`` entry may answer by pair instead of by user content:
``"verdicts": {"Alpha|Beta": {"verdict": ..., "confidence": ...,
"relation": ..., "note": ...}}`` builds the reply from the request's
numbered proposals, listing a verdict for each proposal whose FROM and INTO
displays (either order) name a key, and none for the rest; a key naming a
list of verdicts lists each of them under that proposal's number (a reply
repeating an id). An entry with
``"times": N`` answers N requests per caller (the request's
``Authorization`` digest), then lets the next matching entry answer, so an
endpoint can fail once and recover.

One stub may serve both daemons of a differential run: each side then
sends its own bearer key, and ``drain(auth=...)`` / ``count(auth=...)``
read that side's records alone.

A request no table entry answers gets HTTP 500 and is flagged unanswered.
Each record keeps the request's ``Authorization`` header as a digest
(``auth_digest``), so a run can check which key reached which endpoint
without the key appearing in any record. Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KIND_PREFIXES = (
    ("You consolidate numbered notes", "claims"),
    ("You extract EVENTS", "events"),
    ("You extract durable RELATIONSHIPS", "relations"),
    # the dream's post-consolidation stages
    ("You review the stored record of one work session", "outcomes"),
    ("You consolidate an agent's work-outcome signals", "lessons"),
    ("You turn ONE outcome signal", "rules"),
    ("You write a factual narrative digest", "digest"),
    # the deep dream's merge judge
    ("You judge MERGE PROPOSALS", "judge_merges"),
)
CHAT_PATHS = {"/v1/chat/completions": "primary", "/fallback/v1/chat/completions": "fallback",
              "/second/v1/chat/completions": "second", "/third/v1/chat/completions": "third"}
MODEL_PATHS = {"/v1/models": "primary", "/fallback/v1/models": "fallback",
               "/second/v1/models": "second", "/third/v1/models": "third"}
JUDGE_ENDPOINTS = ("second", "third")
PROPOSAL_HEAD = re.compile(r"^\[(\d+)\] FROM '(.*?)' \(degree ", re.M)
PROPOSAL_INTO = re.compile(r"^    INTO '(.*?)' \(degree ", re.M)
HEALTH_PATHS = {"/health": "primary", "/fallback/health": "fallback"}


def auth_digest(value: str | None) -> str | None:
    """A recorded ``Authorization`` header: the first 16 hex digits of its
    SHA-256, never the key itself (records reach logs and evidence files)."""
    if value is None:
        return None
    return "sha256:" + hashlib.sha256(value.encode("latin-1", "replace")).hexdigest()[:16]


def request_kind(system: str | None) -> str:
    """The prompt kind, from the system message's opening words."""
    for prefix, kind in KIND_PREFIXES:
        if isinstance(system, str) and system.startswith(prefix):
            return kind
    return "other"


def parse_chat(body: bytes) -> tuple[str, str | None] | None:
    """``(kind, user content)`` of a chat-completions body, or None."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return None
    system = user = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system" and system is None:
            system = message.get("content")
        elif message.get("role") == "user" and user is None:
            user = message.get("content")
    return request_kind(system), user


class StubExtractor:
    """One stub endpoint on 127.0.0.1, serving a primary and a fallback base."""

    def __init__(self, spec: dict | None, name: str):
        self.spec = dict(spec or {})
        self.name = name
        self.replies = list(self.spec.get("replies", []))
        self._records: list[dict] = []
        self._health_served: dict[str, int] = {}
        self._times_served: dict[tuple[int, str | None], int] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.origin = self.base_url = self.fallback_base_url = None

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> "StubExtractor":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002 - silence logging
                pass

            def do_GET(self):
                stub._handle(self, "GET")

            def do_POST(self):
                stub._handle(self, "POST")

            def do_PUT(self):
                stub._handle(self, "PUT")

            def do_DELETE(self):
                stub._handle(self, "DELETE")

            def do_PATCH(self):
                stub._handle(self, "PATCH")

            def do_HEAD(self):
                stub._handle(self, "HEAD")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        port = server.server_address[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.base_url = f"{self.origin}/v1"
        self.fallback_base_url = f"{self.origin}/fallback/v1"
        self._thread = threading.Thread(target=server.serve_forever, name=f"stub-{self.name}",
                                        daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    # ── records ────────────────────────────────────────────────────────────
    def drain(self, auth: str | None = None) -> list[dict]:
        """The requests recorded since the last drain, oldest first; with
        ``auth`` (an ``auth_digest``), that caller's alone, the rest kept."""
        with self._lock:
            if auth is None:
                out, self._records = self._records, []
            else:
                out = [r for r in self._records if r["authorization"] == auth]
                self._records = [r for r in self._records if r["authorization"] != auth]
        return out

    def count(self, auth: str | None = None) -> int:
        """How many requests are recorded (for ``auth`` alone), undrained."""
        with self._lock:
            return sum(1 for r in self._records if auth is None or r["authorization"] == auth)

    # ── handling ───────────────────────────────────────────────────────────
    def _read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while handler.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                chunks.append(handler.rfile.read(size))
                handler.rfile.readline()
            return b"".join(chunks)
        length = int(handler.headers.get("Content-Length") or 0)
        return handler.rfile.read(length) if length else b""

    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        body = b"" if method in ("GET", "HEAD") else self._read_body(handler)
        path = handler.path
        route = path.split("?", 1)[0]
        status, payload, answered, extra = self._route(
            method, route, body, auth_digest(handler.headers.get("Authorization")))
        with self._lock:
            self._records.append({
                "method": method, "path": path,
                "body": None if method == "GET" else body.decode("latin-1"),
                "sha256": None if method == "GET" else hashlib.sha256(body).hexdigest(),
                "authorization": auth_digest(handler.headers.get("Authorization")),
                "answered": answered,
            })
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        handler.send_response(status, extra.get("reason"))
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        for name, value in (extra.get("headers") or {}).items():
            handler.send_header(name, value)
        handler.end_headers()
        if method != "HEAD":
            handler.wfile.write(raw)

    def _health(self, key: str) -> int | None:
        """The status a health probe gets: ``spec[key]`` as an int, or the
        next of a list of ints (the last one repeating), else None (200)."""
        override = self.spec.get(key)
        if isinstance(override, list) and override:
            with self._lock:
                served = self._health_served.get(key, 0)
                self._health_served[key] = served + 1
            override = override[min(served, len(override) - 1)]
        if isinstance(override, int) and not isinstance(override, bool):
            return override
        return None

    def _route(self, method: str, route: str, body: bytes,
               auth: str | None = None) -> tuple[int, str, bool, dict]:
        if method == "GET" and route in HEALTH_PATHS:
            key = "health" if HEALTH_PATHS[route] == "primary" else "fallback_health"
            override = self._health(key)
            if override is not None and override != 200:
                return override, json.dumps({"status": "unhealthy"}), True, {}
            return 200, json.dumps({"status": "ok"}), True, {}
        if method == "GET" and route in MODEL_PATHS:
            served = self.spec.get("served_model", "stub-served-model")
            if MODEL_PATHS[route] == "fallback":
                served = self.spec.get("fallback_served_model", served)
            elif MODEL_PATHS[route] in JUDGE_ENDPOINTS:
                served = self.spec.get(f"{MODEL_PATHS[route]}_reply_model", served)
            return 200, json.dumps({"object": "list", "data": [{"id": served}]}), True, {}
        if method == "POST" and route in CHAT_PATHS:
            endpoint = CHAT_PATHS[route]
            entry = self.lookup(body, endpoint, auth)
            if entry is None:
                return 500, json.dumps({"error": "stub_extractor: no table entry"}), False, {}
            extra = {"reason": entry.get("reason"), "headers": entry.get("headers")}
            if "status" in entry:
                return int(entry["status"]), entry.get("body", ""), True, extra
            content = entry.get("content")
            if "verdicts" in entry:
                content = judge_reply(entry["verdicts"], body)
            default_model = self.spec.get("reply_model", "stub-model")
            if endpoint in JUDGE_ENDPOINTS:
                default_model = self.spec.get(f"{endpoint}_reply_model", default_model)
            return 200, json.dumps({
                "choices": [{"message": {"content": content}}],
                "model": entry.get("model", default_model),
            }), True, extra
        return 404, json.dumps({"error": f"stub_extractor: no route {method} {route}"}), True, {}

    def lookup(self, body: bytes, endpoint: str = "primary",
               auth: str | None = None) -> dict | None:
        """The table entry for a chat body on ``endpoint``: by SHA-256,
        then by (kind, user), then (an entry naming no ``user``) by kind.
        An entry whose ``times`` this caller used up is passed over."""
        digest = hashlib.sha256(body).hexdigest()
        parsed = parse_chat(body)
        kind, user = parsed if parsed is not None else (None, None)
        where = endpoint if endpoint in JUDGE_ENDPOINTS else "primary"

        def serves(entry) -> bool:
            return entry.get("endpoint", "primary") == where and self._take(entry, auth)

        for entry in self.replies:
            if entry.get("sha256") == digest and serves(entry):
                return entry
        if parsed is None:
            return None
        for entry in self.replies:
            if ("sha256" not in entry and "verdicts" not in entry
                    and entry.get("kind") == kind and entry.get("user") == user
                    and serves(entry)):
                return entry
        for entry in self.replies:
            if ("sha256" not in entry and "user" not in entry
                    and entry.get("kind") == kind and serves(entry)):
                return entry
        return None

    def _take(self, entry: dict, auth: str | None) -> bool:
        """Count one answer against ``entry``'s ``times`` for this caller."""
        times = entry.get("times")
        if times is None:
            return True
        key = (id(entry), auth)
        with self._lock:
            used = self._times_served.get(key, 0)
            if used >= int(times):
                return False
            self._times_served[key] = used + 1
        return True


def judge_reply(verdicts: dict, body: bytes) -> str:
    """A ``judge_merges`` reply built from the request's proposals: each
    proposal whose ``FROM|INTO`` (either order) names a key gets that
    verdict (each of a list of them), under its number."""
    parsed = parse_chat(body)
    user = parsed[1] if parsed and isinstance(parsed[1], str) else ""
    out = []
    for block in re.split(r"\n\n(?=\[\d+\] FROM )", user):
        head = PROPOSAL_HEAD.search(block)
        into = PROPOSAL_INTO.search(block)
        if not head or not into:
            continue
        a, b = head.group(2), into.group(1)
        v = verdicts.get(f"{a}|{b}") or verdicts.get(f"{b}|{a}")
        for one in (v if isinstance(v, list) else [] if v is None else [v]):
            out.append({"id": int(head.group(1)), **one})
    return json.dumps({"verdicts": out})
