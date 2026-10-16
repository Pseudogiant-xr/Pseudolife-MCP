"""Additive Referer implementation proof, awaiting configured candidate execution."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
import threading

from .test_episode_executable import candidate, invoke  # noqa: F401


@contextmanager
def redirect_http():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(("GET", self.path, list(self.headers.raw_items()), b""))
            self.send_response(302 if self.path == "/health" else 200)
            if self.path == "/health":
                self.send_header("Location", "/redirect-health")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(("POST", self.path, list(self.headers.raw_items()), body))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_health_redirect_generates_no_referer_without_changing_episode_route(candidate, tmp_path):
    for arm, command in [("python", [sys.executable, "-m", "pseudolife_memory.cli"]), ("candidate", candidate)]:
        with redirect_http() as (url, requests):
            result = invoke(command, tmp_path / arm, "episode-end", b'{"session_id":"key"}', url, "fixture-token")
        assert result == {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}
        assert [(method, path) for method, path, _, _ in requests] == [
            ("GET", "/health"), ("GET", "/redirect-health"), ("POST", "/api/episode/end")]
        assert all(not any(name.lower() == "referer" for name, _ in headers)
                   for _, _, headers, _ in requests)
        assert all(not any(name.lower() == "authorization" for name, _ in headers)
                   for _, _, headers, _ in requests[:2])
        assert requests[2][3] == b'{"session_key": "key"}'
        assert [(name.lower(), value) for name, value in requests[2][2]
                if name.lower() == "authorization"] == [("authorization", "Bearer fixture-token")]
