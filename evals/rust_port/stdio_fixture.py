"""Storage-free HTTP fixture mined from the previous stdio protocol instrument.

It observes call dispatch before stdin EOF. Fixture shutdown never completes a
hanging call until the shim has exited and its output has been retained.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from pseudolife_memory import __version__


class HangingFixture:
    def __init__(self, fault=None):
        self.fault = fault
        self.token = "synthetic-fixture-before"
        self.records = []
        self.sessions = {}
        self.errors = []
        self.condition = threading.Condition()
        self.stopping = threading.Event()
        self.call_count = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def reply(self, body, status=200, session=None):
                payload = json.dumps(body, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                self.wfile.write(payload)
                self.wfile.flush()

            def do_GET(self):
                self.reply({"status": "ok", "auth": owner.fault == "rotation", "version": __version__}
                           if self.path == "/health" else {},
                           404 if owner.fault == "degraded" else 200 if self.path == "/health" else 405)

            def do_DELETE(self):
                session = self.headers.get("Mcp-Session-Id")
                with owner.condition:
                    if session in owner.sessions:
                        owner.sessions[session]["deleted"] = True
                        owner.condition.notify_all()
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
                    self.reply({}, 404)
                    return
                request = json.loads(raw)
                method = request["method"]
                if owner.fault == "down":
                    self.close_connection = True
                    return
                if owner.fault == "rotation" and self.headers.get("Authorization") != "Bearer " + owner.token:
                    self.reply({"error": "Unauthorized"}, 401)
                    return
                session = self.headers.get("Mcp-Session-Id")
                with owner.condition:
                    if method == "initialize":
                        session = "fixture-" + str(len(owner.sessions) + 1)
                        owner.sessions[session] = {"initialized": False, "deleted": False, "calls": []}
                    owner.records.append({"request": request, "session": session})
                    if method == "notifications/initialized":
                        owner.sessions[session]["initialized"] = True
                    elif method == "tools/call":
                        if not owner.sessions[session]["initialized"] or owner.sessions[session]["calls"]:
                            raise RuntimeError("upstream initialization or fresh-session contract failed")
                        owner.sessions[session]["calls"].append(request["params"]["name"])
                        owner.call_count += 1
                        owner.condition.notify_all()
                if "id" not in request:
                    self.reply({}, 202)
                elif method == "initialize":
                    self.reply({"jsonrpc": "2.0", "id": request["id"], "result": {
                        "protocolVersion": request["params"]["protocolVersion"],
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "fixture-upstream", "version": "1"},
                        "instructions": "Disposable hanging-call fixture."}}, session=session)
                elif method == "tools/call":
                    if owner.fault in {"401", "503"}:
                        self.reply({"error": "synthetic refusal"}, int(owner.fault))
                        return
                    if owner.fault in {"rotation", "degraded", "healthy"}:
                        self.reply({"jsonrpc": "2.0", "id": request["id"], "result": {
                            "content": [{"type": "text", "text": "synthetic live answer"}], "isError": False}})
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.flush()
                    while not owner.stopping.wait(.01):
                        with owner.condition:
                            if owner.sessions[session]["deleted"]:
                                break
                    self.close_connection = True
                elif method == "tools/list" and owner.fault is not None:
                    self.reply({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": [{
                        "name": "fixture_tool", "description": "Synthetic fixture tool.",
                        "inputSchema": {"type": "object"}}]}})
                else:
                    raise RuntimeError("unexpected fixture operation")

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = False
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=.02))
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.http.server_port}"

    def wait_calls(self, count):
        with self.condition:
            if not self.condition.wait_for(lambda: self.call_count >= count, timeout=10):
                raise RuntimeError("EOF call did not reach fixture")

    def close(self):
        # Snapshot BEFORE fixture cleanup: cancellation may leave an upstream
        # HTTP session un-deleted in the pinned Python SDK, an oracle limitation.
        self.automatic_sessions = deepcopy(self.sessions)
        self.stopping.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("hanging HTTP fixture did not stop")
        return {"server_stopped": True, "automatic_sessions": self.automatic_sessions,
                "fixture_errors": self.errors, "dispatched_calls": self.call_count,
                "upstream_requests": self.records}
