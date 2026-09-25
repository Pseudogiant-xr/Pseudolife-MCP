import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import pseudolife_memory.episode_cli as ec
from pseudolife_memory.shim import _NoRedirectHandler


def test_daemon_down_is_silent_exit_zero(monkeypatch, capsys):
    # probe_health returning None == daemon down -> do nothing, never raise.
    monkeypatch.setattr(ec, "_daemon_url", lambda: "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(ec, "probe_health", lambda url: None, raising=False)
    ec.run_episode("episode-start", stdin_text='{"session_id":"abc","cwd":"/x"}')
    assert capsys.readouterr().out == ""


def test_parses_session_key_from_stdin(monkeypatch):
    captured = {}

    def fake_post(url, token, path, payload):
        captured["path"] = path
        captured["payload"] = payload

    monkeypatch.setattr(ec, "_daemon_url", lambda: "http://x", raising=False)
    monkeypatch.setattr(ec, "probe_health", lambda url: {"ok": True}, raising=False)
    monkeypatch.setattr(ec, "_post", fake_post, raising=False)
    ec.run_episode("episode-start",
                   stdin_text='{"session_id":"abc","cwd":"/home/u/Proj"}')
    assert captured["path"] == "/api/episode/start"
    assert captured["payload"]["session_key"] == "abc"
    assert "Proj" in captured["payload"]["title"]


# `_post` carries the daemon bearer. Plain urlopen follows a 3xx and copies
# every header except content-length/content-type to the redirect target
# (seen on Python 3.11 and 3.12), so a redirecting daemon URL would hand the
# bearer to another host. It must open through the shim's no-redirect handler.


def test_post_refuses_redirects(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    class _Opener:
        def open(self, req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            seen["timeout"] = timeout
            return _Resp()

    def fake_build_opener(*handlers):
        seen["handlers"] = handlers
        return _Opener()

    def plain_urlopen(*args, **kwargs):
        raise AssertionError("plain urlopen follows redirects with the bearer")

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", plain_urlopen)
    ec._post("http://x", "tok", "/api/episode/start", {"session_key": "abc"})
    assert seen["handlers"] == (_NoRedirectHandler,)
    assert seen["auth"] == "Bearer tok"
    assert seen["timeout"] == 5


def _serve(handler_cls) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_post_does_not_forward_the_bearer_across_a_redirect():
    """End to end over loopback: a daemon URL answering 302 must not get the
    bearer delivered to the Location it names."""
    received = []

    class Target(BaseHTTPRequestHandler):
        def _record(self):
            received.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST = _record

        def log_message(self, *args):
            pass

    target = _serve(Target)
    target_url = f"http://127.0.0.1:{target.server_port}/elsewhere"

    class Redirector(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", target_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    redirector = _serve(Redirector)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            ec._post(f"http://127.0.0.1:{redirector.server_port}", "secret",
                     "/api/episode/start", {"session_key": "abc"})
        exc.value.close()
        assert exc.value.code == 302
        assert received == []
    finally:
        for server in (redirector, target):
            server.shutdown()
            server.server_close()
