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
    assert hmac.compare_digest(
        seen[0].get_header("Authorization") or "", "Bearer fixture-token")
    assert not setup.probe("http://user:password@127.0.0.1:8765", "fixture-token")
    assert not setup.probe("http://127.0.0.1:8765/base", "fixture-token")


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
        assert passed is False
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
