"""A loopback HTTP daemon with scripted routes that records every request.

Each arm gets a fresh instance from its case's factory, so both arms see
identical response bytes and each arm's requests are recorded separately.
The recorded request is the CLI's output on the wire: method, raw target,
header fields in arrival order and the body.
"""

from __future__ import annotations

import http
import http.server
import json
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable

Response = tuple[int, list[tuple[str, str]], bytes]
TLS_DIR = Path(__file__).resolve().parent.parent / "shim" / "tests" / "fixtures" / "pg_tls"
TLS_CA = TLS_DIR / "ca.pem"
Route = Callable[[dict], Response] | Response


def text(body: str | bytes, status: int = 200,
         content_type: str = "text/plain; charset=utf-8") -> Response:
    data = body.encode("utf-8") if isinstance(body, str) else body
    return status, [("Content-Type", content_type)], data


def json_body(value, status: int = 200) -> Response:
    # The daemon's _send_json: json.dumps(value, default=str), UTF-8.
    return text(json.dumps(value, default=str), status, "application/json")


def redirect(location: str, status: int = 307) -> Response:
    return status, [("Location", location), ("Content-Length", "0")], b""


class Trickle:
    """A response written ``chunk`` bytes at a time, ``delay`` seconds apart:
    the status line, head and body arrive while each gap stays inside the
    client's per-read timeout and the whole takes longer than it."""

    def __init__(self, response: Response, chunk: int, delay: float):
        self.response, self.chunk, self.delay = response, chunk, delay

    def raw(self) -> bytes:
        status, headers, data = self.response
        head = [f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}"]
        head += [f"{k}: {v}" for k, v in headers]
        head += [f"Content-Length: {len(data)}", "Connection: close", "", ""]
        return "\r\n".join(head).encode("latin-1") + data


class FixtureDaemon:
    def __init__(self, routes: dict[str, Route], tls: bool = False):
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
                if isinstance(route, Trickle):
                    raw = route.raw()
                    for start in range(0, len(raw), route.chunk):
                        self.wfile.write(raw[start:start + route.chunk])
                        self.wfile.flush()
                        time.sleep(route.delay)
                    self.close_connection = True
                    return
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
        port = self.server.server_address[1]
        if tls:
            # The disposable pg_tls test certificate: DNS SAN localhost,
            # issued by TLS_CA, which no system trust store holds.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(TLS_DIR / "server.pem", TLS_DIR / "server-key.pem")
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.url = f"https://localhost:{port}"
        else:
            self.url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        if tls:
            self._warm_tls(port)

    def _warm_tls(self, port: int) -> None:
        # One full exchange before any arm runs. On a cold machine the
        # first server-side handshake pages in OpenSSL's server code: after
        # a standby-list purge on the Windows dev VM (2026-10-16) it took
        # 0.28-1.0 s, past the CLI's 0.25 s health probe, so the first arm
        # (always Python) saw a silent daemon; CI's first https case failed
        # that way three times. The recorded request is discarded.
        context = ssl.create_default_context(cafile=str(TLS_CA))
        with socket.create_connection(("127.0.0.1", port), timeout=30) as raw, \
                context.wrap_socket(raw, server_hostname="localhost") as tls:
            tls.sendall(b"GET /fixture-warm-up HTTP/1.1\r\nHost: localhost\r\n\r\n")
            while tls.recv(65536):
                pass
        with self._lock:
            self._requests.clear()

    def requests(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._requests]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def factory(routes: dict[str, Route], tls: bool = False) -> Callable[[], FixtureDaemon]:
    return lambda: FixtureDaemon(routes, tls)
