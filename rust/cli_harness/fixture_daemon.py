"""A loopback HTTP daemon with scripted routes that records every request.

Each arm gets a fresh instance from its case's factory, so both arms see
identical response bytes and each arm's requests are recorded separately.
The recorded request is the CLI's output on the wire: method, raw target,
header fields in arrival order and the body.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import Callable

Response = tuple[int, list[tuple[str, str]], bytes]
Route = Callable[[dict], Response] | Response

# Fields compared by default. The rest (Accept, Accept-Encoding, Connection)
# are transport choices covered by recorded dispositions; see
# normalize.hook-transport-headers.
COMPARED_HEADERS = ("authorization", "user-agent", "host", "content-type", "content-length")


def text(body: str | bytes, status: int = 200,
         content_type: str = "text/plain; charset=utf-8") -> Response:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return status, [("Content-Type", content_type)], data


def json_body(value, status: int = 200) -> Response:
    # The daemon's _send_json: json.dumps(value, default=str), UTF-8.
    return text(json.dumps(value, default=str), status, "application/json")


def redirect(location: str, status: int = 307) -> Response:
    return status, [("Location", location), ("Content-Length", "0")], b""


class FixtureDaemon:
    def __init__(self, routes: dict[str, Route]):
        self.routes = routes
        self._requests: list[dict] = []
        self._lock = threading.Lock()
        daemon = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # quiet
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                request = {"method": self.command, "target": self.path,
                           "headers": [[k, v] for k, v in self.headers.items()],
                           "body": body.decode("utf-8", "backslashreplace")}
                with daemon._lock:
                    daemon._requests.append(request)
                path = self.path.split("?", 1)[0]
                route = daemon.routes.get(path)
                if route is None:
                    status, headers, data = text("not found", 404)
                elif callable(route):
                    status, headers, data = route(request)
                else:
                    status, headers, data = route
                self.send_response(status)
                names = {k.lower() for k, _ in headers}
                for key, value in headers:
                    self.send_header(key, value)
                if "content-length" not in names:
                    self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def requests(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def factory(routes: dict[str, Route]) -> Callable[[], FixtureDaemon]:
    return lambda: FixtureDaemon(routes)
