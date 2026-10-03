"""Small loopback MCP client and disposable startup fixture."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import queue
import subprocess
import threading
import time

from evals.rust_port.processes import owned_process

TOKEN = "synthetic-baseline-fixture"
PROTOCOL = "2026-07-28"


class Stdio:
    def __init__(self, command, env, cwd):
        self._ownership = owned_process(command, env=env, cwd=cwd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.process = self._ownership.__enter__()
        self._closing = False
        self.messages = queue.Queue()
        self.errors = []
        self.counter = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        try:
            self.reader.start()
            self.error_reader.start()
        except BaseException:
            self._closing = True
            self._ownership.__exit__(None, None, None)
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except ValueError:
            if not self._closing:
                raise
        finally:
            self.messages.put(None)

    def _read_errors(self):
        try:
            for line in self.process.stderr:
                self.errors.append(line.decode("utf-8"))
        except ValueError:
            if not self._closing:
                raise

    def request(self, method, params=None, notification=False, timeout=60):
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notification:
            message["id"] = self.counter
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.process.stdin.flush()
        if notification:
            return None
        deadline = time.monotonic() + timeout
        while True:
            response = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if response is None:
                raise RuntimeError("stdio process closed before response")
            if response.get("id") == message["id"]:
                if "error" in response:
                    raise RuntimeError("stdio JSON-RPC error")
                return response["result"]

    def initialize(self):
        response = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                "clientInfo": {"name": "baseline-fixture", "version": "1"}})
        self.request("notifications/initialized", notification=True)
        return response

    def close(self):
        forced = False
        try:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                forced = True
        finally:
            # EOF only stops the root. Reclaim its owned tree even after a normal
            # root exit, before checking readers or issuing a cleanup receipt.
            self._closing = True
            self._ownership.__exit__(None, None, None)
        self.reader.join(timeout=3)
        self.error_reader.join(timeout=3)
        owned = self.process.owned_cleanup
        return {"exit_code": self.process.returncode, "forced": forced,
                **owned, "cleanup_confirmed": owned["process_stopped"] and owned["subtree_stopped"]
                and not self.reader.is_alive() and not self.error_reader.is_alive(),
                "stderr_present": bool(self.errors)}


@contextmanager
def shim_fixture():
    observations = {"authorized": True, "initialize_requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, payload, status=200):
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == "/health":
                from pseudolife_memory import __version__
                self.reply({"status": "ok", "version": __version__, "auth_required": True})
            else:
                self.reply({}, 405)

        def do_DELETE(self):
            self.reply({}, 405)

        def do_POST(self):
            observations["authorized"] &= self.headers.get("Authorization") == "Bearer " + TOKEN
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/episode/end":
                self.reply({"ended": True})
                return
            if "id" not in payload:
                self.reply({}, 202)
                return
            if payload["method"] == "initialize":
                observations["initialize_requests"] += 1
                result = {"protocolVersion": payload["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "baseline-fixture", "version": "1"}}
            elif payload["method"] == "tools/list":
                result = {"tools": []}
            else:
                self.reply({}, 400)
                return
            self.reply({"jsonrpc": "2.0", "id": payload["id"], "result": result})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", observations
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("fixture failed to stop")
