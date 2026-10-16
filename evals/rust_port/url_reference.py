"""Disposable Python reference adapter proving the external candidate URL lane."""
from contextlib import contextmanager, ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import threading
import urllib.error
import urllib.request

from .harness import _NoRedirect, strict_json_loads


@contextmanager
def reference_adapter(root):
    from evals.rust_baseline.daemon import launched_daemon, private_directory
    from evals.rust_baseline.transport import TOKEN
    from evals.memory_policy_daemon import _server_check, check_database
    from .full_bank import private_home_overrides
    nonce = secrets.token_hex(32)
    stack, lock = ExitStack(), threading.Lock()
    state = {"bank_selected": False, "bank_released": False, "server_stopped": False}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args): pass
        def respond(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
            self.close_connection = True
        def handle_request(self):
            if self.path.startswith("/_rust_port/") and self.headers.get("Authorization") != "Bearer " + TOKEN:
                self.respond(401, {"error": "unauthorized"})
                return
            if self.path == "/_rust_port/ready":
                self.respond(200, {"nonce": nonce, "disposable": True})
                return
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            with lock:
                if self.path == "/_rust_port/disposable-bank":
                    body = strict_json_loads(raw)
                    if state["bank_selected"]:
                        self.respond(409, {"error": "already bound"})
                        return
                    name = check_database(body["database_url"])
                    _server_check(body["database_url"])
                    fingerprint = hashlib.sha256(name.encode()).hexdigest()
                    if body.get("bank_sha256") != fingerprint or body.get("disposable") is not True or body.get("token") != TOKEN:
                        self.respond(400, {"error": "binding mismatch"})
                        return
                    private = stack.enter_context(private_directory())
                    _, url, cleanup = stack.enter_context(launched_daemon(body["database_url"], private,
                        env_extra=private_home_overrides(private), source_root=root))
                    state.update(bank_selected=True, url=url, binding_nonce=body["nonce"], daemon_cleanup=cleanup)
                    self.respond(200, {"nonce": body["nonce"], "bank_sha256": fingerprint, "disposable": True})
                    return
                if self.path == "/_rust_port/disposable-bank/release":
                    body = strict_json_loads(raw)
                    if body.get("nonce") != state.get("binding_nonce"):
                        self.respond(403, {"error": "binding mismatch"})
                        return
                    stack.close()
                    state["bank_released"] = True
                    self.respond(200, {"nonce": body["nonce"], "released": True})
                    return
                if not state["bank_selected"] or state["bank_released"]:
                    self.respond(503, {"error": "no disposable bank bound"})
                    return
                headers = {k: v for k, v in self.headers.items()
                           if k.lower() not in {"host", "content-length", "connection"}}
                request = urllib.request.Request(state["url"] + self.path, data=raw or None,
                                                method=self.command, headers=headers)
                try:
                    response = opener.open(request, timeout=120)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    result = response.read()
                    self.send_response_only(response.status)
                    for key, value in response.headers.items():
                        if key.lower() not in {"content-length", "transfer-encoding", "connection"}:
                            self.send_header(key, value)
                    self.send_header("Content-Length", str(len(result)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(result)
                    self.close_connection = True
        do_GET = do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", nonce, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        stack.close()
        if thread.is_alive():
            raise RuntimeError("reference URL adapter did not stop")
        state["server_stopped"] = True
        # Private routing origin/nonces are never receipt fields.
        for key in ("url", "binding_nonce"):
            state.pop(key, None)
