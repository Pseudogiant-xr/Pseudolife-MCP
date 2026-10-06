"""Additive binary nodes, distinct from the unchanged in-process oracle tests."""
import ast
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from .cli_process import snapshot
from .episode_policy import refusal_response
from .harness import isolated_env, run_cli


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def candidate():
    configured = os.environ.get("PSEUDOLIFE_PORT_EPISODE_JSON")
    if configured is None:
        pytest.skip("additive episode executable nodes require PSEUDOLIFE_PORT_EPISODE_JSON")
    command = json.loads(configured)
    assert isinstance(command, list) and command and all(isinstance(value, str) and value for value in command)
    assert Path(command[0]).is_absolute() and Path(command[0]).is_file()
    return command


@contextmanager
def fixture_http(*, health_body=b"{}", health_status=200):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, body):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append(("GET", self.path, list(self.headers.raw_items()), b""))
            self.respond(health_status, health_body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(("POST", self.path, list(self.headers.raw_items()), body))
            self.respond(200, b"{}")

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


def invoke(command, tmp_path, mode, raw, url, token, *, token_file=False):
    home = tmp_path / "home"
    env = isolated_env(home)
    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": url, "PSEUDOLIFE_MCP_NO_SPAWN": "1"})
    if token is not None:
        env["PSEUDOLIFE_MCP_TOKEN"] = token
    if token_file:
        path = home / "fixture-token"
        path.write_text("fixture-file-token", encoding="utf-8")
        env["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(path)
    before = snapshot(home)
    result = run_cli(command, [mode, "--help", "ignored"], cwd=ROOT, env=env, timeout=10, stdin=raw)
    assert snapshot(home) == before
    return result


@pytest.mark.parametrize("mode", ["episode-start", "episode-end"])
@pytest.mark.parametrize("case_id,token", [("token-del", "fixture\x7f"),
    ("token-control", "fixture\x01"), ("token-fold", "fixture\r\n folded"),
    ("bearer-del", "fixture\x7f"), ("bearer-control", "fixture\x01"),
    ("bearer-fold", "fixture\r\n folded"), ("token-invalid-line", "fixture\r\ninvalid")])
def test_candidate_forbidden_bearer_uses_named_refusal_after_health(candidate, tmp_path, mode, case_id, token):
    with fixture_http() as (url, requests):
        result = invoke(candidate, tmp_path, mode, b'{"session_id":"key"}', url, token)
    assert result == refusal_response(case_id, windows=os.name == "nt")
    assert [(verb, path) for verb, path, _, _ in requests] == [("GET", "/health")]


@pytest.mark.parametrize("mode", ["episode-start", "episode-end"])
@pytest.mark.parametrize("raw,health", [(b"{}", b"{}"), (b'{"session_id":"key"}', b"null")])
def test_forbidden_bearer_does_not_move_ahead_of_input_and_health_gates(candidate, tmp_path, mode, raw, health):
    with fixture_http(health_body=health) as (url, requests):
        result = invoke(candidate, tmp_path, mode, raw, url, "fixture\x7f")
    assert result == {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}
    assert [(verb, path) for verb, path, _, _ in requests] == ([] if raw == b"{}" else [("GET", "/health")])


@pytest.mark.parametrize("mode", ["episode-start", "episode-end"])
@pytest.mark.parametrize("token,posted,token_file", [(None, True, False), ("", True, False),
    ("fixture\tvalue", True, False), ("caf\u00e9", True, False), ("fixture\U0001f9e0", False, False),
    (None, True, True), ("", True, True)])
def test_bearer_controls_keep_current_python_boundary(candidate, tmp_path, mode, token, posted, token_file):
    for arm, command in [("python", [sys.executable, "-m", "pseudolife_memory.cli"]), ("candidate", candidate)]:
        with fixture_http() as (url, requests):
            before = time.time()
            result = invoke(command, tmp_path / arm, mode, b'{"session_id":"key"}', url, token, token_file=token_file)
            after = time.time()
        assert result == {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}
        expected = [("GET", "/health")] + ([("POST", "/api/episode/" + mode.removeprefix("episode-"))] if posted else [])
        assert [(verb, path) for verb, path, _, _ in requests] == expected
        if posted:
            body = json.loads(requests[1][3])
            assert body["session_key"] == "key"
            if mode == "episode-start":
                assert after - before < 60
                allowed = {time.strftime("%Y-%m-%d %H:%M", time.localtime(value)) for value in (before, after)}
                assert body["title"] in {"session - " + minute for minute in allowed}
            headers = {name.lower(): value for name, value in requests[1][2]}
            # This checks only the synthetic bearer value, not raw header parity.
            assert (headers.get("authorization") == "Bearer " + token) if token else "authorization" not in headers


def test_shared_help_asset_matches_current_python_usage_literal_at_columns_80(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    tree = ast.parse((ROOT / "pseudolife_memory/cli.py").read_text(encoding="utf-8"))
    usage = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "_USAGE" for target in node.targets))
    assert (ROOT / "rust/shim/src/cli_help.txt").read_bytes() == usage.encode("utf-8")
    assert "episode-start" in usage and "episode-end" in usage
