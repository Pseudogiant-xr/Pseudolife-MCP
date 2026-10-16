"""Storage-free startup and held-response fixture for public stdio captures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from pseudolife_memory import __version__


class ScenarioFixture:
    def __init__(self, health=None, *, together=False):
        self.health = health or {"status": "ok", "auth": False, "version": __version__}
        self.condition = threading.Condition()
        self.release = {name: threading.Event() for name in ("A", "B")}
        if together:
            self.release["B"] = self.release["A"]
        self.records, self.errors, self.sessions, self.arrivals = [], [], {}, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, body, status=200, session=None):
                raw = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()

            def do_GET(self):
                owner.record("GET", self.path)
                self.reply(owner.health if self.path == "/health" else {},
                           200 if self.path == "/health" else 405)

            def do_DELETE(self):
                session = self.headers.get("Mcp-Session-Id")
                with owner.condition:
                    owner.record("DELETE", self.path, session=session)
                    if session in owner.sessions:
                        owner.sessions[session]["deleted"] = True
                self.reply({})

            def do_POST(self):
                try:
                    self.handle_post()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as error:
                    owner.errors.append(type(error).__name__)
                    self.reply({}, 500)

            def handle_post(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path != "/mcp":
                    owner.record("POST", self.path)
                    self.reply({})
                    return
                request = json.loads(raw)
                method = request["method"]
                session = self.headers.get("Mcp-Session-Id")
                with owner.condition:
                    if method == "initialize":
                        session = "fixture-" + str(len(owner.sessions) + 1)
                        owner.sessions[session] = {"initialized": False, "deleted": False, "calls": []}
                    elif method == "notifications/initialized":
                        owner.sessions[session]["initialized"] = True
                    elif method == "tools/call":
                        name = request["params"]["name"]
                        state = owner.sessions[session]
                        if not state["initialized"] or state["calls"]:
                            raise RuntimeError("fresh initialized upstream session required")
                        state["calls"].append(name)
                        owner.arrivals.append(name)
                        owner.condition.notify_all()
                    owner.record("POST", self.path, request=request, session=session)
                if "id" not in request:
                    self.reply({}, 202)
                elif method == "initialize":
                    self.reply({"jsonrpc": "2.0", "id": request["id"], "result": {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "baseline-fixture", "version": "1"},
                        "instructions": "Synthetic startup guidance."}}, session=session)
                elif method == "tools/list":
                    self.reply({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": [{
                        "name": name, "description": "Synthetic held response.",
                        "inputSchema": {"type": "object"}} for name in ("A", "B")]}})
                elif method == "tools/call":
                    if not owner.release[name].wait(12):
                        raise RuntimeError("held response was not released")
                    self.reply({"jsonrpc": "2.0", "id": request["id"], "result": {
                        "content": [{"type": "text", "text": "answer-" + name}], "isError": False}})
                    owner.record("response-written", "/mcp", call=name)
                else:
                    raise RuntimeError("unexpected fixture request")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.02))
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def record(self, method, path, **extra):
        with self.condition:
            self.records.append({"method": method, "path": path, **extra})

    def wait_arrival(self, name):
        with self.condition:
            if not self.condition.wait_for(lambda: name in self.arrivals, timeout=5):
                raise RuntimeError("call did not arrive while responses were held: " + name)

    def close(self):
        for event in self.release.values():
            event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("scenario fixture did not stop")
        return {"server_stopped": True, "handler_threads_stopped": True,
                "upstream_requests": self.records, "sessions": self.sessions,
                "fixture_errors": self.errors, "call_arrivals": self.arrivals}
