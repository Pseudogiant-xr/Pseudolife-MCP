"""The one-line agent-board status the installers and doctor print.

Each case runs a real loopback HTTP server standing in for the daemon's
``/api/hook/coordination-start`` route, so the probe is exercised through
urllib exactly as the installer and doctor call it.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from unittest.mock import AsyncMock

import pytest

from pseudolife_memory.board_status import board_status


class _Daemon:
    def __init__(self, header: str | None, body: bytes = b""):
        self.seen: list[str | None] = []
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's naming
                daemon.seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                if header is not None:
                    self.send_header("X-PL-Board", header)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def test_board_is_on_where_the_daemon_serves_the_checkin():
    with _Daemon("on", b"Pseudolife coordination: check in.\n") as daemon:
        on, line = board_status(daemon.url, "fixture-token")
    assert on is True
    assert line == "on - token present, principal allowed"
    assert daemon.seen == ["Bearer fixture-token"]


@pytest.mark.parametrize("reason,expected", [
    ("disabled", "coordination.enabled"),
    ("unauthorized", "rejected this token"),
    ("authentication_required", "no bearer token configured"),
    ("principal_not_allowed", "coordination.allowed_principals"),
    ("coordination_requires_postgres", "PostgreSQL"),
])
def test_board_off_line_names_the_daemons_reason(reason, expected):
    with _Daemon(f"off; reason={reason}") as daemon:
        on, line = board_status(daemon.url, "fixture-token")
    assert on is False
    assert line.startswith("off - ")
    assert expected in line
    assert "fixture-token" not in line


def test_board_off_without_a_token_never_asks_the_daemon():
    with _Daemon("on", b"check in\n") as daemon:
        on, line = board_status(daemon.url, None)
    assert on is False
    assert line.startswith("off - no bearer token")
    assert daemon.seen == []


def test_board_off_when_the_daemon_is_unreachable():
    with _Daemon("on") as daemon:
        url = daemon.url
    on, line = board_status(url, "fixture-token", timeout=0.5)
    assert (on, line) == (False, "off - daemon unreachable")


def test_board_status_from_a_daemon_without_the_reason_header():
    """A daemon from before the header still answers: text means on, an
    empty body means off with no reason to report."""
    with _Daemon(None, b"check in\n") as daemon:
        assert board_status(daemon.url, "fixture-token")[0] is True
    with _Daemon(None) as daemon:
        on, line = board_status(daemon.url, "fixture-token")
    assert on is False
    assert "update the daemon" in line


def test_doctor_reports_one_board_line(monkeypatch, capsys):
    from pseudolife_memory import doctor_cli, shim

    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: {"status": "ok"})
    monkeypatch.setattr(doctor_cli, "_handshake", AsyncMock(return_value={
        "instructions_present": True, "tool_count": 9, "tools_missing_annotations": []}))
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "fixture-token")
    with _Daemon("off; reason=principal_not_allowed") as daemon:
        monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", daemon.url)
        with pytest.raises(SystemExit):
            doctor_cli.run_doctor()
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["board"].startswith("off - ")
    assert "coordination.allowed_principals" in report["board"]
    assert "fixture-token" not in output
    assert daemon.seen == ["Bearer fixture-token"]


def test_doctor_board_line_survives_an_unreadable_token_file(monkeypatch, capsys, tmp_path):
    from pseudolife_memory import doctor_cli, shim

    monkeypatch.setattr(sys, "argv", ["pseudolife-mcp", "doctor"])
    monkeypatch.setattr(shim, "_require_mcp_sdk_v2", lambda: None)
    monkeypatch.setattr(shim, "probe_health", lambda *a, **kw: None)
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(tmp_path / "missing.token"))
    with pytest.raises(SystemExit):
        doctor_cli.run_doctor()
    report = json.loads(capsys.readouterr().out)
    assert report["board"].startswith("off - ")
    assert "token file" in report["board"]
