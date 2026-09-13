"""Codex opt-in preserves configuration and verifies access without registering."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "codex_coordination_setup", ROOT / "ops/setup-codex-coordination.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class Client:
    def __init__(self, home):
        self.calls = []
        self.config = {"config": {"mcp_servers": {"pseudolife-memory": {
            "command": "python", "args": ["-m", "pseudolife_memory.cli"],
            "env": {"PSEUDOLIFE_MCP_TOKEN": "private-fixture", "UNRELATED": "keep"}}}},
            "layers": [{"name": {"type": "user", "file": str(home / "config.toml")},
                        "version": "version-a"}]}

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "config/batchWrite":
            env = self.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
            for edit in params["edits"]:
                env[edit["keyPath"].split('.')[-1].strip('"')] = edit["value"]
        return self.config if method == "config/read" else {}


def test_enable_uses_scoped_cas_and_private_backup(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("# user's unrelated config\n")
    client = Client(tmp_path)
    monkeypatch.setattr(setup, "probe", lambda url, token: token == "private-fixture")
    result = setup.configure(client, tmp_path, tmp_path, "enable")
    assert result["status"] == "enabled"
    assert "private-fixture" not in json.dumps(result)
    method, request = next(call for call in client.calls if call[0] == "config/batchWrite")
    assert method == "config/batchWrite"
    assert request["expectedVersion"] == "version-a"
    assert all(e["keyPath"].startswith('"mcp_servers"."pseudolife-memory"."env".')
               for e in request["edits"])
    values = {e["keyPath"].split('.')[-1].strip('"'): e["value"] for e in request["edits"]}
    assert values["PSEUDOLIFE_AGENT_COORDINATION"] == "1"
    assert values["PSEUDOLIFE_WRITER_ID"] == "codex"
    assert values["PSEUDOLIFE_AGENT_WAKE"] == "0"
    assert values["PSEUDOLIFE_AGENT_STATE_DIR"] == str(tmp_path / "pseudolife" / "agents")
    assert "PSEUDOLIFE_MCP_TOKEN" not in values
    assert Path(result["backup"]).read_text() == path.read_text()


@pytest.mark.parametrize("problem", ["token", "state", "transport", "daemon", "version"])
def test_enable_refuses_unsafe_or_unready_config_without_write(tmp_path, monkeypatch, problem):
    client = Client(tmp_path)
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    monkeypatch.setattr(setup, "probe", lambda *args: problem != "daemon")
    if problem == "token":
        server["env"].pop("PSEUDOLIFE_MCP_TOKEN")
        monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "not-forwarded-to-mcp")
    elif problem == "state":
        server["env"]["PSEUDOLIFE_AGENT_STATE"] = "shared.json"
    elif problem == "transport":
        server["url"] = "http://fixture.invalid/mcp"
    elif problem == "version":
        client.config["layers"] = []
    with pytest.raises(setup.SetupError):
        setup.configure(client, tmp_path, tmp_path, "enable")
    assert all(method != "config/batchWrite" for method, _ in client.calls)


def test_check_never_writes_and_disable_needs_no_daemon(tmp_path, monkeypatch):
    client = Client(tmp_path)
    monkeypatch.setattr(setup, "probe", lambda *args: False)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "needs-configuration"
    assert len(client.calls) == 1
    result = setup.configure(client, tmp_path, tmp_path, "disable")
    assert result["status"] == "disabled"
    edits = next(params["edits"] for method, params in client.calls if method == "config/batchWrite")
    assert len(edits) == 1
    assert edits[0]["value"] == "0"


def test_already_enabled_configuration_is_idempotent(tmp_path, monkeypatch):
    client = Client(tmp_path)
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"].update(setup.ENABLED_ENV)
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_AGENT_STATE_DIR"] = str(tmp_path / "agents")
    monkeypatch.setattr(setup, "probe", lambda *args: True)
    result = setup.configure(client, tmp_path, tmp_path, "enable")
    assert result["status"] == "enabled"
    assert len(client.calls) == 1


def test_check_requires_codex_writer_and_enabled_server(tmp_path, monkeypatch):
    client = Client(tmp_path)
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    server["env"]["PSEUDOLIFE_AGENT_COORDINATION"] = "1"
    monkeypatch.setattr(setup, "probe", lambda *args: True)
    assert setup.configure(client, tmp_path, tmp_path, "check")["status"] == "needs-configuration"
    server["env"]["PSEUDOLIFE_WRITER_ID"] = "codex"
    server["enabled"] = False
    assert setup.configure(client, tmp_path, tmp_path, "check")["status"] == "needs-configuration"


def test_enable_verifies_effective_values_after_write(tmp_path, monkeypatch):
    class Overridden(Client):
        def rpc(self, method, params):
            result = super().rpc(method, params)
            if method == "config/batchWrite":
                self.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_WRITER_ID"] = "other"
            return result

    monkeypatch.setattr(setup, "probe", lambda *args: True)
    with pytest.raises(setup.SetupError, match="effective"):
        setup.configure(Overridden(tmp_path), tmp_path, tmp_path, "enable")


def test_coordination_route_exposes_authenticated_readiness_boundary():
    from pseudolife_memory.web.api import build_console_app
    from pseudolife_memory.web.fixtures import FixtureService
    from tests.asgi_helpers import call, stub_mcp

    service = FixtureService()
    service.config.coordination.enabled = True
    service.config.coordination.allowed_principals = ["agent-user"]
    app = build_console_app(stub_mcp, None, lambda: {}, service, token_map={
        "fixture-secret": "agent-user", "denied-secret": "denied-user"})

    def headers(token):
        return [(b"authorization", ("Bearer " + token).encode()),
                (b"content-type", b"application/json")]
    status, body = call(app, "POST", "/api/coordination/agents", body=b"{}", headers=[
        (b"authorization", b"Bearer fixture-secret"),
        (b"content-type", b"application/json")])
    assert status == 401
    assert json.loads(body) == {"error": "instance_authentication_required"}
    status, body = call(app, "POST", "/api/coordination/agents", body=b"{}",
                        headers=headers("bad-secret"))
    assert status == 401 and json.loads(body)["error"] == "unauthorized"
    status, body = call(app, "POST", "/api/coordination/agents", body=b"{}",
                        headers=headers("denied-secret"))
    assert status == 403 and json.loads(body)["error"] == "principal_not_allowed"


def test_probe_uses_authenticated_read_only_gate(monkeypatch):
    from urllib.error import HTTPError
    import io
    seen = []

    def respond(request, timeout):
        seen.append(request)
        raise HTTPError(request.full_url, 401, "Unauthorized", {},
                        io.BytesIO(b'{"error":"instance_authentication_required"}'))

    monkeypatch.setattr(setup, "urlopen", respond)
    assert setup.probe("http://127.0.0.1:8765", "fixture-token")
    assert seen[0].full_url.endswith("/api/coordination/agents")
    assert seen[0].data == b"{}"
    assert seen[0].get_header("Authorization") == "Bearer fixture-token"
    assert not setup.probe("http://user:password@127.0.0.1:8765", "fixture-token")


@pytest.mark.parametrize(("status", "body"), [
    (400, b'{"error":"instance_authentication_required"}'),
    (401, b'{"error":"authentication_required"}'),
    (401, b'{"error":"unauthorized"}'),
    (403, b'{"error":"principal_not_allowed"}'),
    (401, b'not-json'),
])
def test_probe_rejects_other_status_and_error_pairs(monkeypatch, status, body):
    from urllib.error import HTTPError
    import io

    def respond(request, timeout):
        raise HTTPError(request.full_url, status, "fixture", {}, io.BytesIO(body))

    monkeypatch.setattr(setup, "urlopen", respond)
    assert not setup.probe("http://127.0.0.1:8765", "fixture-token")


def test_real_codex_config_writer_preserves_other_settings(tmp_path, monkeypatch):
    """Exercise actual TOML/CAS semantics without a model turn or a real bank."""
    import os
    import shutil
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    binary = shutil.which("codex")
    if not binary:
        pytest.skip("Codex CLI unavailable")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"instance_authentication_required"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.toml"
    config.write_text('# Preserved user comment\nmodel = "fixture-model"\n'
                      '[mcp_servers.unrelated]\ncommand = "fixture-command"\n'
                      '[mcp_servers.pseudolife-memory]\ncommand = "python"\n'
                      'args = ["-m", "pseudolife_memory.cli"]\n'
                      '[mcp_servers.pseudolife-memory.env]\nUNRELATED = "keep"\n'
                      'PSEUDOLIFE_MCP_TOKEN = "private-fixture"\n'
                      f'PSEUDOLIFE_MCP_DAEMON_URL = "http://127.0.0.1:{server.server_port}"\n')
    # No thread/start is needed: configuration writes cannot run project hooks.
    try:
        with setup.hooks.codex(binary, home, tmp_path) as client:
            result = setup.configure(client, home, tmp_path, "enable")
            assert result["status"] == "enabled"
            readback = client.rpc("config/read", {"includeLayers": True, "cwd": str(tmp_path)})
            actual = readback["config"]
            assert actual["model"] == "fixture-model"
            assert actual["mcp_servers"]["unrelated"]["command"] == "fixture-command"
            env = actual["mcp_servers"]["pseudolife-memory"]["env"]
            assert env["UNRELATED"] == "keep"
            assert all(env[key] == value for key, value in setup.ENABLED_ENV.items())
            assert env["PSEUDOLIFE_AGENT_STATE_DIR"] == str(home / "pseudolife" / "agents")
        assert config.read_text().startswith("# Preserved user comment")
        assert seen == ["/api/coordination/agents"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
