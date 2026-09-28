"""Codex opt-in preserves configuration and verifies access without registering."""
import importlib.util
import hmac
import json
from pathlib import Path
import sys

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
        self.config["layers"][0]["config"] = json.loads(json.dumps(
            self.config["config"]))

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "config/batchWrite":
            server_path = setup.hooks.dotted("mcp_servers", "pseudolife-memory")
            env_path = server_path + "." + setup.hooks.dotted("env")
            for edit in params["edits"]:
                key_path = edit["keyPath"]
                key = key_path.split('.')[-1].strip('"')
                for config in (self.config["config"], self.config["layers"][0]["config"]):
                    server = config["mcp_servers"]["pseudolife-memory"]
                    if key_path == server_path:
                        config["mcp_servers"]["pseudolife-memory"] = json.loads(
                            json.dumps(edit["value"]))
                    elif key_path == env_path:
                        server["env"] = json.loads(json.dumps(edit["value"]))
                    elif key_path.startswith(env_path + "."):
                        server["env"][key] = edit["value"]
                    elif key_path.startswith(server_path + "."):
                        server[key] = edit["value"]
        return self.config if method == "config/read" else {}


def test_enable_uses_scoped_cas_and_private_backup(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("# user's unrelated config\n")
    client = Client(tmp_path)
    monkeypatch.setattr(setup, "probe", lambda url, token:
                        "ready" if token == "private-fixture" else "unavailable")
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
    assert "PSEUDOLIFE_AGENT_WAKE" not in values  # absent already means pull-only
    assert values["PSEUDOLIFE_AGENT_STATE_DIR"] == str(tmp_path / "pseudolife" / "agents")
    assert "PSEUDOLIFE_MCP_TOKEN" not in values
    assert Path(result["backup"]).read_text() == path.read_text()


def test_credential_bootstrap_migrates_literal_with_scoped_cas(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    config = tmp_path / "config.toml"
    config.write_text("# user's unrelated config\n")
    client = Client(tmp_path)
    result = setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    assert result["credential_file_configured"]
    assert result["migrated_literal"]
    assert "private-fixture" not in json.dumps(result)
    env = client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
    assert env["UNRELATED"] == "keep"
    assert "PSEUDOLIFE_MCP_TOKEN" not in env
    token_file = Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"])
    assert token_file == tmp_path / "pseudolife" / "token"
    assert setup.hooks.CredentialProvider(path=token_file).snapshot().token == "private-fixture"
    edit = next(params for method, params in client.calls if method == "config/batchWrite")
    assert edit["expectedVersion"] == "version-a"
    assert edit["edits"][0]["keyPath"] == setup.hooks.dotted(
        "mcp_servers", "pseudolife-memory", "env")
    assert Path(result["backup"]).read_text() == config.read_text()
    connection = json.loads((tmp_path / "pseudolife" / "connection.json").read_text())
    assert connection == setup.hooks._connection_values(
        "http://127.0.0.1:8765", token_file)
    assert "private-fixture" not in json.dumps(connection)


def test_credential_bootstrap_reuses_valid_file_on_rerun(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("# config\n")
    client = Client(tmp_path)
    first = setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    token_file = Path(client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"][
        "PSEUDOLIFE_MCP_TOKEN_FILE"])
    identity = token_file.stat().st_ino
    client.calls.clear()
    second = setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    assert first["credential_file_configured"] and second["credential_file_configured"]
    assert token_file.stat().st_ino == identity
    assert all(method != "config/batchWrite" for method, _ in client.calls)


def test_configured_bad_credential_file_never_falls_back_to_literal(tmp_path, monkeypatch):
    client = Client(tmp_path)
    env = client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
    env["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(tmp_path / "missing")
    client.config["layers"][0]["config"]["mcp_servers"][
        "pseudolife-memory"]["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(tmp_path / "missing")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN", "environment-fallback")
    with pytest.raises(setup.SetupError, match="credential file"):
        setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    assert all(method != "config/batchWrite" for method, _ in client.calls)


def test_server_literal_precedes_unforwarded_ambient_file(tmp_path, monkeypatch):
    ambient = tmp_path / "ambient"
    setup.hooks._write_token_file(ambient, "ambient-token")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(ambient))
    client = Client(tmp_path)
    result = setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    configured = Path(result["credential_file_path"])
    assert configured == tmp_path / "pseudolife" / "token"
    assert setup.hooks.CredentialProvider(path=configured).snapshot().token == "private-fixture"
    assert result["daemon_url"] == "http://127.0.0.1:8765"


def test_existing_server_ignores_unforwarded_ambient_url(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9999")
    client = Client(tmp_path)
    result = setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    assert result["daemon_url"] == "http://127.0.0.1:8765"


@pytest.mark.parametrize("installer_token", [None, "installer-fixture"])
def test_existing_server_preserves_explicitly_forwarded_credential_source(
        tmp_path, monkeypatch, installer_token):
    ambient = tmp_path / "forwarded-token"
    setup.hooks._write_token_file(ambient, "forwarded-fixture")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(ambient))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9876")
    client = Client(tmp_path)
    for config in (client.config["config"], client.config["layers"][0]["config"]):
        server = config["mcp_servers"]["pseudolife-memory"]
        server["env"].pop("PSEUDOLIFE_MCP_TOKEN")
        server["env_vars"] = ["PSEUDOLIFE_MCP_TOKEN_FILE",
                              "PSEUDOLIFE_MCP_DAEMON_URL"]

    installer_connection = (("http://127.0.0.1:9876", installer_token)
                            if installer_token else None)
    result = setup.hooks.configure_credential_file(
        client, tmp_path, tmp_path, installer_connection=installer_connection)

    assert result["credential_file_configured"]
    assert Path(result["credential_file_path"]) == ambient
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    assert server["env_vars"] == ["PSEUDOLIFE_MCP_TOKEN_FILE",
                                  "PSEUDOLIFE_MCP_DAEMON_URL"]
    assert server["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"] == str(ambient)


def test_installer_credential_repairs_matching_existing_tokenless_server(
        tmp_path, monkeypatch):
    client = Client(tmp_path)
    for config in (client.config["config"], client.config["layers"][0]["config"]):
        env = config["mcp_servers"]["pseudolife-memory"]["env"]
        env.pop("PSEUDOLIFE_MCP_TOKEN")
        env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:8765"
    monkeypatch.setattr(setup.hooks, "installer_credential_valid",
                        lambda url, token: url == "http://127.0.0.1:8765"
                        and token == "installer-fixture")
    result = setup.hooks.configure_credential_file(
        client, tmp_path, tmp_path,
        installer_connection=("http://127.0.0.1:8765", "installer-fixture"))
    configured = Path(result["credential_file_path"])
    assert result["credential_file_configured"]
    assert setup.hooks.CredentialProvider(path=configured).snapshot().token == "installer-fixture"
    assert client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"][
        "PSEUDOLIFE_MCP_TOKEN_FILE"] == str(configured)


def test_fresh_installer_connection_precedes_conflicting_ambient_pair(
        tmp_path, monkeypatch):
    ambient = tmp_path / "ambient-token"
    setup.hooks._write_token_file(ambient, "ambient-fixture")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(ambient))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9876")
    client = Client(tmp_path)
    client.config["config"]["mcp_servers"] = {}
    client.config["layers"][0]["config"]["mcp_servers"] = {}
    monkeypatch.setattr(
        setup.hooks, "installer_credential_valid",
        lambda url, token: url == "http://127.0.0.1:8765"
        and token == "installer-fixture")
    result = setup.hooks.configure_credential_file(
        client, tmp_path, tmp_path,
        installer_connection=("http://127.0.0.1:8765", "installer-fixture"))
    configured = Path(result["credential_file_path"])
    assert result["daemon_url"] == "http://127.0.0.1:8765"
    assert configured != ambient
    assert setup.hooks.CredentialProvider(path=configured).snapshot().token == "installer-fixture"


def test_fresh_installer_tokenless_connection_records_selected_origin(
        tmp_path, monkeypatch):
    ambient = tmp_path / "ambient-token"
    setup.hooks._write_token_file(ambient, "ambient-fixture")
    monkeypatch.setenv("PSEUDOLIFE_MCP_TOKEN_FILE", str(ambient))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:9876")
    client = Client(tmp_path)
    client.config["config"]["mcp_servers"] = {}
    client.config["layers"][0]["config"]["mcp_servers"] = {}

    result = setup.hooks.configure_credential_file(
        client, tmp_path, tmp_path,
        installer_connection=("http://127.0.0.1:8765", None))

    assert result["credential_file_configured"] is False
    assert result["connection_configured"] is True
    assert result["daemon_url"] == "http://127.0.0.1:8765"
    connection = json.loads((tmp_path / "pseudolife" / "connection.json").read_text())
    assert connection == setup.hooks._connection_values("http://127.0.0.1:8765")
    assert connection["token_file"] == ""


def test_installer_credential_never_overrides_project_credential(tmp_path, monkeypatch):
    client = Client(tmp_path)
    user_env = client.config["layers"][0]["config"]["mcp_servers"][
        "pseudolife-memory"]["env"]
    user_env.pop("PSEUDOLIFE_MCP_TOKEN")
    effective_env = client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
    effective_env["PSEUDOLIFE_MCP_TOKEN"] = "project-fixture"
    monkeypatch.setattr(setup.hooks, "installer_credential_valid",
                        lambda *args: pytest.fail("project credential must retain precedence"))
    with pytest.raises(setup.SetupError, match="another Codex configuration layer"):
        setup.hooks.configure_credential_file(
            client, tmp_path, tmp_path,
            installer_connection=("http://127.0.0.1:8765", "installer-fixture"))
    assert all(method != "config/batchWrite" for method, _ in client.calls)
    assert not (tmp_path / "pseudolife").exists()


@pytest.mark.parametrize("installer_url,accepted,message", [
    ("http://127.0.0.1:9876", True, "does not match"),
    ("http://127.0.0.1:8765", False, "was not accepted"),
])
def test_installer_credential_requires_matching_authenticated_origin(
        tmp_path, monkeypatch, installer_url, accepted, message):
    client = Client(tmp_path)
    for config in (client.config["config"], client.config["layers"][0]["config"]):
        config["mcp_servers"]["pseudolife-memory"]["env"].pop(
            "PSEUDOLIFE_MCP_TOKEN")
    calls = []
    monkeypatch.setattr(
        setup.hooks, "installer_credential_valid",
        lambda url, token: calls.append((url, token == "installer-fixture")) or accepted)
    with pytest.raises(setup.SetupError, match=message):
        setup.hooks.configure_credential_file(
            client, tmp_path, tmp_path,
            installer_connection=(installer_url, "installer-fixture"))
    assert calls == ([] if installer_url.endswith(":9876") else [
        ("http://127.0.0.1:8765", True)])
    assert all(method != "config/batchWrite" for method, _ in client.calls)
    assert not (tmp_path / "pseudolife").exists()


def test_credential_migration_never_flattens_project_or_managed_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text("# user config\n")
    user_server = {"command": "python", "args": ["-m", "pseudolife_memory.cli"],
                   "env": {"PSEUDOLIFE_MCP_TOKEN": "user-token", "USER_KEEP": "yes"}}
    effective_server = {**user_server, "env": {
        **user_server["env"], "MANAGED_ONLY": "managed", "PROJECT_ONLY": "project"}}
    state = {"user": user_server["env"].copy()}

    class LayeredClient:
        def __init__(self):
            self.writes = []
        def rpc(self, method, params):
            if method == "config/batchWrite":
                self.writes.append(params)
                state["user"] = params["edits"][0]["value"]
                return {}
            effective = {**effective_server, "env": {
                "MANAGED_ONLY": "managed", "PROJECT_ONLY": "project", **state["user"]}}
            return {"config": {"mcp_servers": {"pseudolife-memory": effective}},
                    "layers": [{"name": {"type": "user", "file": str(config_path),
                                         "profile": None},
                                "version": "layer-version",
                                "config": {"mcp_servers": {
                                    "pseudolife-memory": {**user_server,
                                                         "env": state["user"]}}}}]}

    client = LayeredClient()
    setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    written = client.writes[0]["edits"][0]["value"]
    assert written["USER_KEEP"] == "yes"
    assert "MANAGED_ONLY" not in written and "PROJECT_ONLY" not in written


def test_tokenless_credential_bootstrap_is_valid(tmp_path, monkeypatch):
    client = Client(tmp_path)
    client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"].pop(
        "PSEUDOLIFE_MCP_TOKEN")
    client.config["layers"][0]["config"]["mcp_servers"][
        "pseudolife-memory"]["env"].pop("PSEUDOLIFE_MCP_TOKEN")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    result = setup.hooks.configure_credential_file(client, tmp_path, tmp_path)
    assert result == {"credential_file_configured": False,
                      "credential_file_path": "", "daemon_url": None,
                      "connection_configured": False,
                      "migrated_literal": False, "backup": None,
                      "connection_backup": None}
    assert all(method != "config/batchWrite" for method, _ in client.calls)


def test_runtime_defaults_fill_only_absent_values_with_scoped_cas(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("# user's unrelated config\n")
    client = Client(tmp_path)
    for config in (client.config["config"], client.config["layers"][0]["config"]):
        server = config["mcp_servers"]["pseudolife-memory"]
        server["startup_timeout_sec"] = 17
        server["unrelated"] = "keep"

    result = setup.configure_runtime_defaults(client, tmp_path, tmp_path)

    assert result["status"] == "ready"
    assert result["runtime_defaults"] == "configured"
    request = next(params for method, params in client.calls
                   if method == "config/batchWrite")
    assert request["filePath"] == str(config_path.resolve())
    assert request["expectedVersion"] == "version-a"
    assert len(request["edits"]) == 1
    edit = request["edits"][0]
    assert edit["keyPath"] == setup.hooks.dotted(
        "mcp_servers", "pseudolife-memory")
    assert edit["mergeStrategy"] == "replace"
    assert edit["value"]["startup_timeout_sec"] == 17
    assert edit["value"]["tool_timeout_sec"] == 240
    assert edit["value"]["required"] is True
    assert edit["value"]["unrelated"] == "keep"
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    assert server["startup_timeout_sec"] == 17
    assert server["tool_timeout_sec"] == 240
    assert server["required"] is True
    assert server["unrelated"] == "keep"
    assert Path(result["backup"]).read_text() == config_path.read_text()


def test_runtime_defaults_preserve_all_explicit_values_without_write(tmp_path):
    client = Client(tmp_path)
    explicit = {"startup_timeout_sec": 31, "tool_timeout_sec": 47,
                "required": False}
    for config in (client.config["config"], client.config["layers"][0]["config"]):
        config["mcp_servers"]["pseudolife-memory"].update(explicit)

    result = setup.configure_runtime_defaults(client, tmp_path, tmp_path)

    assert result["status"] == "ready"
    assert result["runtime_defaults"] == "preserved"
    assert all(method != "config/batchWrite" for method, _ in client.calls)
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    assert {key: server[key] for key in explicit} == explicit


@pytest.mark.parametrize("problem", ["registration", "project-registration", "version"])
def test_runtime_defaults_fail_closed_without_registration_or_version(
        tmp_path, problem):
    client = Client(tmp_path)
    if problem == "registration":
        client.config["config"]["mcp_servers"] = {}
        client.config["layers"][0]["config"]["mcp_servers"] = {}
    elif problem == "project-registration":
        client.config["layers"][0]["config"]["mcp_servers"] = {}
    else:
        client.config["layers"] = []

    with pytest.raises(setup.SetupError):
        setup.configure_runtime_defaults(client, tmp_path, tmp_path)

    assert all(method != "config/batchWrite" for method, _ in client.calls)


def test_runtime_defaults_never_flatten_project_or_managed_server_fields(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text("# user config\n")
    user_server = {"command": "python", "args": ["-m", "pseudolife_memory.cli"],
                   "env": {"USER_KEEP": "yes"}}
    state = {"user": user_server.copy()}

    class LayeredClient:
        def __init__(self):
            self.writes = []

        def rpc(self, method, params):
            if method == "config/batchWrite":
                self.writes.append(params)
                state["user"] = json.loads(json.dumps(params["edits"][0]["value"]))
                return {}
            effective = {**state["user"], "MANAGED_ONLY": "managed",
                         "PROJECT_ONLY": "project"}
            return {"config": {"mcp_servers": {"pseudolife-memory": effective}},
                    "layers": [{"name": {"type": "user", "file": str(config_path),
                                           "profile": None},
                                "version": "layer-version",
                                "config": {"mcp_servers": {
                                    "pseudolife-memory": state["user"]}}}]}

    client = LayeredClient()
    setup.configure_runtime_defaults(client, tmp_path, tmp_path)
    written = client.writes[0]["edits"][0]["value"]
    assert written["env"] == {"USER_KEEP": "yes"}
    assert written["startup_timeout_sec"] == 240
    assert written["tool_timeout_sec"] == 240
    assert written["required"] is True
    assert "MANAGED_ONLY" not in written and "PROJECT_ONLY" not in written


def test_runtime_defaults_verify_effective_readback(tmp_path):
    class Overridden(Client):
        def rpc(self, method, params):
            result = super().rpc(method, params)
            if method == "config/batchWrite":
                self.config["config"]["mcp_servers"]["pseudolife-memory"][
                    "tool_timeout_sec"] = 3
            return result

    with pytest.raises(setup.SetupError, match="effective"):
        setup.configure_runtime_defaults(Overridden(tmp_path), tmp_path, tmp_path)


def test_runtime_defaults_cli_reports_failure_without_transport_details(
        tmp_path, monkeypatch, capsys):
    @setup.hooks.contextmanager
    def codex(*args, **kwargs):
        yield object()

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(setup.hooks, "resolve_codex", lambda: "fixture-codex")
    monkeypatch.setattr(setup.hooks, "codex", codex)
    monkeypatch.setattr(
        setup, "configure_runtime_defaults",
        lambda *args: (_ for _ in ()).throw(RuntimeError("private transport detail")))
    monkeypatch.setattr(sys, "argv", ["setup-codex-coordination.py",
                                      "--runtime-defaults"])

    assert setup.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "needs-configuration"
    assert report["recovery"] == (
        "Check the Codex runtime and configuration; no transport details are displayed.")
    assert "private transport detail" not in json.dumps(report)


def test_credentials_cli_accepts_explicit_tokenless_installer_origin(
        tmp_path, monkeypatch, capsys):
    @setup.hooks.contextmanager
    def codex(*args, **kwargs):
        yield object()

    seen = []

    def configure(client, home, cwd, config=None, installer_connection=None):
        seen.append(installer_connection)
        return {"credential_file_configured": False,
                "credential_file_path": "",
                "daemon_url": installer_connection[0],
                "connection_configured": True,
                "migrated_literal": False, "backup": None,
                "connection_backup": None}

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(setup.hooks, "resolve_codex", lambda: "fixture-codex")
    monkeypatch.setattr(setup.hooks, "codex", codex)
    monkeypatch.setattr(setup.hooks, "configure_credential_file", configure)
    monkeypatch.setattr(sys, "argv", ["setup-codex-coordination.py", "--credentials",
                                      "--installer-daemon-url", "http://127.0.0.1:8765"])

    assert setup.main() == 0
    assert seen == [("http://127.0.0.1:8765", None)]
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "tokenless"
    assert report["connection_configured"] is True


@pytest.mark.parametrize("problem", ["token", "state", "transport", "daemon", "version"])
def test_enable_refuses_unsafe_or_unready_config_without_write(tmp_path, monkeypatch, problem):
    client = Client(tmp_path)
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    monkeypatch.setattr(setup, "probe",
                        lambda *args: "unavailable" if problem == "daemon" else "ready")
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
    monkeypatch.setattr(setup, "probe", lambda *args: "unavailable")
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
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    result = setup.configure(client, tmp_path, tmp_path, "enable")
    assert result["status"] == "enabled"
    assert len(client.calls) == 1


def test_check_requires_codex_writer_and_enabled_server(tmp_path, monkeypatch):
    client = Client(tmp_path)
    server = client.config["config"]["mcp_servers"]["pseudolife-memory"]
    server["env"]["PSEUDOLIFE_AGENT_COORDINATION"] = "1"
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
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

    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    with pytest.raises(setup.SetupError, match="effective"):
        setup.configure(Overridden(tmp_path), tmp_path, tmp_path, "enable")


@pytest.mark.parametrize("wake", ["1", "0", "true"])
def test_enable_preserves_an_existing_wake_setting(tmp_path, monkeypatch, wake):
    """Re-running --enable must not reset live delivery someone turned on."""
    client = Client(tmp_path)
    for config in (client.config["config"], client.config["layers"][0]["config"]):
        config["mcp_servers"]["pseudolife-memory"]["env"]["PSEUDOLIFE_AGENT_WAKE"] = wake
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    setup.configure(client, tmp_path, tmp_path, "enable")
    edits = next(params["edits"] for method, params in client.calls
                 if method == "config/batchWrite")
    assert not any(e["keyPath"].endswith('"PSEUDOLIFE_AGENT_WAKE"') for e in edits)
    env = client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
    assert env["PSEUDOLIFE_AGENT_WAKE"] == wake


def test_enable_reports_live_wake_only_with_the_bridge(tmp_path, monkeypatch):
    """PSEUDOLIFE_AGENT_WAKE alone is not live delivery: without the bridge the
    shim falls back, and since #434 that fallback is the doorbell when a
    codex CLI is found, pull-only when none is."""
    client = Client(tmp_path)
    env = client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
    env["PSEUDOLIFE_AGENT_WAKE"] = "1"
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    monkeypatch.setattr(setup, "codex_cli", lambda lookup: None)
    result = setup.configure(client, tmp_path, tmp_path, "enable")
    assert result["wake"] == "pull-only"
    assert "no codex CLI" in result["wake_reason"]
    monkeypatch.setattr(setup, "codex_cli", lambda lookup: ["codex"])
    assert setup.configure(client, tmp_path, tmp_path, "enable")["wake"] == "doorbell"
    env.update({"PSEUDOLIFE_CODEX_SERVER_URL": "ws://127.0.0.1:4500",
                "PSEUDOLIFE_CODEX_SERVER_TOKEN": "bridge-fixture"})
    result = setup.configure(client, tmp_path, tmp_path, "enable")
    assert result["wake"] == "live"
    assert "PSEUDOLIFE_AGENT_WAKE" in result["wake_reason"]
    # The bridge credential must differ from the bank bearer, as the shim insists.
    env["PSEUDOLIFE_CODEX_SERVER_TOKEN"] = "private-fixture"
    assert setup.configure(client, tmp_path, tmp_path, "enable")["wake"] == "doorbell"


def _ready_default_on(client, monkeypatch, cli=True):
    env = _codex_writer(client)
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    monkeypatch.setattr(setup, "served", lambda *args: True)
    monkeypatch.setattr(setup, "codex_cli",
                        lambda lookup: ["codex"] if cli else None)
    return env


def test_check_reports_the_doorbell_as_the_default_wake_path(tmp_path, monkeypatch):
    """Since #434 the Codex doorbell rings by default, so a ready default-on
    registration with a codex CLI is not pull-only: --check says so the way
    ``pseudolife-mcp doctor`` does, with the reason."""
    client = Client(tmp_path)
    _ready_default_on(client, monkeypatch)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "ready (default-on)"
    assert result["wake"] == "doorbell"
    assert "PSEUDOLIFE_CODEX_DOORBELL" in result["wake_reason"]
    assert "default" in result["wake_reason"]
    assert len(client.calls) == 1  # still read-only


def test_check_reports_live_delivery_over_the_doorbell(tmp_path, monkeypatch):
    client = Client(tmp_path)
    env = _ready_default_on(client, monkeypatch)
    env.update({"PSEUDOLIFE_AGENT_WAKE": "1",
                "PSEUDOLIFE_CODEX_SERVER_URL": "ws://127.0.0.1:4500",
                "PSEUDOLIFE_CODEX_SERVER_TOKEN": "bridge-fixture"})
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["wake"] == "live"


@pytest.mark.parametrize("file_token, literal, expected", [
    ("bridge-fixture", None, "doorbell"),   # the file's bearer is the bridge's
    ("file-bearer", "bridge-fixture", "live"),  # a stale literal is not the bearer
])
def test_live_delivery_compares_the_bridge_with_the_bearer_the_shim_sends(
        tmp_path, monkeypatch, file_token, literal, expected):
    """The shim refuses a bridge credential equal to its bank bearer, and its
    bearer comes from PSEUDOLIFE_MCP_TOKEN_FILE when one is configured (the
    --credentials setup), not from the literal (review of #439)."""
    client = Client(tmp_path)
    env = _ready_default_on(client, monkeypatch)
    token_file = tmp_path / "token"
    setup.hooks._write_token_file(token_file, file_token)
    env.pop("PSEUDOLIFE_MCP_TOKEN")
    if literal:
        env["PSEUDOLIFE_MCP_TOKEN"] = literal
    env.update({"PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file),
                "PSEUDOLIFE_AGENT_WAKE": "1",
                "PSEUDOLIFE_CODEX_SERVER_URL": "ws://127.0.0.1:4500",
                "PSEUDOLIFE_CODEX_SERVER_TOKEN": "bridge-fixture"})
    assert setup.configure(client, tmp_path, tmp_path, "check")["wake"] == expected
    assert setup.configure(client, tmp_path, tmp_path, "enable")["wake"] == expected


@pytest.mark.parametrize("change, reason", [
    ({"PSEUDOLIFE_CODEX_DOORBELL": "0"}, "PSEUDOLIFE_CODEX_DOORBELL=0"),
    ({"PSEUDOLIFE_CODEX_DOORBELL": "off"}, "PSEUDOLIFE_CODEX_DOORBELL=off"),
    ({"PSEUDOLIFE_AGENT_COORDINATION": "0"}, "PSEUDOLIFE_AGENT_COORDINATION=0"),
    ({"PSEUDOLIFE_WRITER_ID": "other"}, "PSEUDOLIFE_WRITER_ID is not codex"),
    ({"PSEUDOLIFE_AGENT_STATE": "shared.json"}, "PSEUDOLIFE_AGENT_STATE"),
    ({"PSEUDOLIFE_CODEX_BIN": "relative/codex"}, "PSEUDOLIFE_CODEX_BIN"),
])
def test_check_names_why_the_doorbell_is_off(tmp_path, monkeypatch, change, reason):
    """Each switch the shim reads (and doctor reports) turns the path to
    pull-only with that switch named, so an operator sees what to change."""
    client = Client(tmp_path)
    env = _ready_default_on(client, monkeypatch, cli=False if "PSEUDOLIFE_CODEX_BIN" in change else True)
    env.update(change)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["wake"] == "pull-only"
    assert reason in result["wake_reason"], result["wake_reason"]


def test_check_is_pull_only_without_a_codex_cli_or_a_served_board(tmp_path, monkeypatch):
    client = Client(tmp_path)
    env = _ready_default_on(client, monkeypatch, cli=False)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert (result["wake"], result["wake_reason"]) == ("pull-only", "no codex CLI")

    monkeypatch.setattr(setup, "codex_cli", lambda lookup: ["codex"])
    monkeypatch.setattr(setup, "served", lambda *args: False)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["wake"] == "pull-only"
    assert "does not serve the board" in result["wake_reason"]

    monkeypatch.setattr(setup, "served", lambda *args: True)
    env.pop("PSEUDOLIFE_MCP_TOKEN")
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert (result["wake"], result["wake_reason"]) == ("pull-only", "no bearer token")


def test_check_pull_only_names_a_daemon_that_refuses_the_principal(tmp_path, monkeypatch):
    client = Client(tmp_path)
    _ready_default_on(client, monkeypatch)
    monkeypatch.setattr(setup, "probe", lambda *args: "principal_not_allowed")
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["wake"] == "pull-only"
    assert result["wake_reason"] == setup.PRINCIPAL_REASON


def test_disable_reports_pull_only_with_the_master_switch(tmp_path, monkeypatch):
    client = Client(tmp_path)
    monkeypatch.setattr(setup, "codex_cli", lambda lookup: ["codex"])
    result = setup.configure(client, tmp_path, tmp_path, "disable")
    assert (result["wake"], result["wake_reason"]) == (
        "pull-only", "PSEUDOLIFE_AGENT_COORDINATION=0")


def test_codex_cli_lookup_reads_the_servers_env_over_the_process_env(monkeypatch):
    """The shim finds ``codex`` in its own process environment, which Codex
    builds from the server's ``env`` table over the launching environment, so
    the lookup passes the merged view (as doctor does)."""
    seen = []
    monkeypatch.setattr(setup, "codex_cli", lambda lookup: seen.append(lookup) or ["codex"])
    monkeypatch.setenv("PSEUDOLIFE_CODEX_BIN", "from-process")
    server_env = {"PSEUDOLIFE_WRITER_ID": "codex", "PSEUDOLIFE_MCP_TOKEN": "private-fixture",
                  "PSEUDOLIFE_CODEX_BIN": "from-server"}
    path, _ = setup.wake_path(server_env.get, server_env, True, "", "private-fixture")
    assert path == "doorbell"
    assert seen == [{**dict(__import__("os").environ), **server_env}]


def _codex_writer(client):
    env = client.config["config"]["mcp_servers"]["pseudolife-memory"]["env"]
    env["PSEUDOLIFE_WRITER_ID"] = "codex"
    return env


def test_check_treats_unset_coordination_as_default_on_when_served(
        tmp_path, monkeypatch):
    """Unset is the default since the board went on by default: the shim asks
    the daemon at startup, so --check asks it the same question."""
    client = Client(tmp_path)
    env = _codex_writer(client)
    asked = []
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    monkeypatch.setattr(setup, "served", lambda url, token: asked.append(token) or True)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "ready (default-on)"
    assert result["coordination_mode"] == "default-on"
    assert asked == ["private-fixture"]
    assert len(client.calls) == 1  # still read-only

    monkeypatch.setattr(setup, "served", lambda *args: False)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "needs-configuration"

    env["PSEUDOLIFE_AGENT_COORDINATION"] = "0"  # an explicit opt-out stays off
    monkeypatch.setattr(setup, "served", lambda *args: True)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "needs-configuration"
    assert result["coordination_mode"] == "disabled"


def test_check_reports_explicit_mode_distinctly(tmp_path, monkeypatch):
    client = Client(tmp_path)
    _codex_writer(client)["PSEUDOLIFE_AGENT_COORDINATION"] = "1"
    monkeypatch.setattr(setup, "probe", lambda *args: "ready")
    monkeypatch.setattr(setup, "served", lambda *args: pytest.fail(
        "an explicit opt-in skips the default-mode question, as the shim does"))
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "ready (explicit)"
    assert result["coordination_mode"] == "explicit"


PRINCIPAL_REASON = ("principal not allowed on the board (add 'codex' to "
                    "coordination.allowed_principals in config.yaml)")


@pytest.mark.parametrize("setting", [None, "1"])
def test_check_names_an_unlisted_principal(tmp_path, monkeypatch, setting):
    """A token-map principal is off the board until listed (2026-09-25
    decision); the shim then leaves coordination off silently, so --check
    must say why."""
    client = Client(tmp_path)
    env = _codex_writer(client)
    if setting:
        env["PSEUDOLIFE_AGENT_COORDINATION"] = setting
    monkeypatch.setattr(setup, "probe", lambda *args: "principal_not_allowed")
    monkeypatch.setattr(setup, "served", lambda *args: False)
    result = setup.configure(client, tmp_path, tmp_path, "check")
    assert result["status"] == "needs-configuration"
    assert result["reason"] == PRINCIPAL_REASON


def test_enable_refusal_names_an_unlisted_principal(tmp_path, monkeypatch):
    client = Client(tmp_path)
    monkeypatch.setattr(setup, "probe", lambda *args: "principal_not_allowed")
    with pytest.raises(setup.SetupError, match="add 'codex' to coordination.allowed_principals"):
        setup.configure(client, tmp_path, tmp_path, "enable")
    assert all(method != "config/batchWrite" for method, _ in client.calls)


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
    assert setup.probe("http://127.0.0.1:8765", "fixture-token") == "ready"
    assert seen[0].full_url.endswith("/api/coordination/agents")
    assert seen[0].data == b"{}"
    assert hmac.compare_digest(
        seen[0].get_header("Authorization") or "", "Bearer fixture-token")
    assert setup.probe("http://user:password@127.0.0.1:8765", "fixture-token") == "unavailable"
    assert setup.probe("http://127.0.0.1:8765/base", "fixture-token") == "unavailable"


@pytest.mark.parametrize(("status", "body"), [
    (400, b'{"error":"instance_authentication_required"}'),
    (401, b'{"error":"authentication_required"}'),
    (401, b'{"error":"unauthorized"}'),
    (401, b'not-json'),
])
def test_probe_rejects_other_status_and_error_pairs(monkeypatch, status, body):
    from urllib.error import HTTPError
    import io

    def respond(request, timeout):
        raise HTTPError(request.full_url, status, "fixture", {}, io.BytesIO(body))

    monkeypatch.setattr(setup, "urlopen", respond)
    assert setup.probe("http://127.0.0.1:8765", "fixture-token") == "unavailable"


@pytest.mark.parametrize(("status", "body", "code"), [
    (403, b'{"error":"principal_not_allowed"}', "principal_not_allowed"),
    (400, b'{"error":"principal_not_allowed"}', "unavailable"),
])
def test_probe_names_an_unlisted_principal(monkeypatch, status, body, code):
    from urllib.error import HTTPError
    import io

    def respond(request, timeout):
        raise HTTPError(request.full_url, status, "fixture", {}, io.BytesIO(body))

    monkeypatch.setattr(setup, "urlopen", respond)
    assert setup.probe("http://127.0.0.1:8765", "fixture-token") == code


@pytest.mark.parametrize(("body", "code"), [
    (b'{"enabled": false}', "disabled"),
    (b'{"agents": []}', "unavailable"),
    (b'not-json', "unavailable"),
])
def test_probe_names_a_disabled_board(monkeypatch, body, code):
    import io

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(setup, "urlopen", lambda request, timeout: Response(body))
    assert setup.probe("http://127.0.0.1:8765", "fixture-token") == code


def test_served_asks_the_shims_default_mode_question(monkeypatch):
    """The same GET the shim's startup probe makes: a non-empty check-in
    means the daemon serves the board to this bearer."""
    import io
    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    body = [b"Pseudolife coordination: ...\n"]

    def respond(request, timeout):
        seen.append(request)
        return Response(body[0])

    monkeypatch.setattr(setup, "urlopen", respond)
    assert setup.served("http://127.0.0.1:8765", "fixture-token") is True
    assert seen[0].full_url.endswith("/api/hook/coordination-start")
    assert seen[0].get_method() == "GET"
    assert hmac.compare_digest(
        seen[0].get_header("Authorization") or "", "Bearer fixture-token")
    body[0] = b"\n"
    assert setup.served("http://127.0.0.1:8765", "fixture-token") is False
    assert setup.served("http://user:password@127.0.0.1:8765", "fixture-token") is False
    assert setup.served("http://127.0.0.1:8765", None) is False

    def fail(request, timeout):
        raise OSError("unreachable")

    monkeypatch.setattr(setup, "urlopen", fail)
    assert setup.served("http://127.0.0.1:8765", "fixture-token") is False


# urllib's default handler re-sends a POST answered 301/302/303 as a GET
# carrying every header except Content-Length/Content-Type.
@pytest.mark.parametrize("status", [301, 302, 303])
def test_probe_refuses_redirect_without_forwarding_authorization(status):
    """A followed redirect would carry the bearer to the Location's host, and
    that host's authentication error would pass the probe."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    sent, forwarded = [], []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self):
            forwarded.append((self.command, bool(self.headers.get("Authorization"))))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"instance_authentication_required"}')

        do_GET = do_POST = reply

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            sent.append(hmac.compare_digest(
                self.headers.get("Authorization") or "", "Bearer fixture-token"))
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(status)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}{self.path}")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in (target, redirect)]
    for worker in workers:
        worker.start()
    try:
        passed = setup.probe(f"http://127.0.0.1:{redirect.server_port}", "fixture-token")
        assert sent == [True]  # The probe really ran, with the bearer.
        assert forwarded == []
        assert passed == "unavailable"
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


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


def test_real_codex_runtime_defaults_use_versioned_config_interface(
        tmp_path, monkeypatch):
    """Exercise the actual Codex config writer without a server or model turn."""
    import shutil

    binary = shutil.which("codex")
    if not binary:
        pytest.skip("Codex CLI unavailable")
    home = tmp_path / "home"
    home.mkdir()
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    for key in ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKEN_FILE",
                "PSEUDOLIFE_MCP_TOKENS", "PSEUDOLIFE_CODEX_SERVER_TOKEN",
                "PSEUDOLIFE_CODEX_SERVER_URL", "PSEUDOLIFE_AGENT_STATE",
                "PSEUDOLIFE_AGENT_STATE_DIR", "PSEUDOLIFE_AGENT_COORDINATION",
                "PSEUDOLIFE_AGENT_WAKE", "PSEUDOLIFE_MCP_CONNECTION_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", "http://127.0.0.1:1")
    config = home / "config.toml"
    trusted_hash = "sha256:" + "0" * 64
    config.write_text(
        '# Preserved user comment\nmodel = "fixture-model"\n'
        '[mcp_servers.unrelated]\ncommand = "fixture-command"\n'
        '[mcp_servers.pseudolife-memory]\ncommand = "python"\n'
        'args = ["-m", "pseudolife_memory.cli"]\n'
        'startup_timeout_sec = 19\n'
        '[hooks.state.fixture]\n'
        f'trusted_hash = "{trusted_hash}"\n')

    with setup.hooks.codex(binary, home, neutral) as client:
        result = setup.configure_runtime_defaults(client, home, neutral)
        readback = client.rpc(
            "config/read", {"includeLayers": True, "cwd": str(neutral)})["config"]

    server = readback["mcp_servers"]["pseudolife-memory"]
    assert result["status"] == "ready"
    assert result["runtime_defaults"] == "configured"
    assert server["startup_timeout_sec"] == 19
    assert server["tool_timeout_sec"] == 240
    assert server["required"] is True
    assert readback["mcp_servers"]["unrelated"]["command"] == "fixture-command"
    assert readback["hooks"]["state"]["fixture"]["trusted_hash"] == trusted_hash
    assert config.read_text().startswith("# Preserved user comment")
