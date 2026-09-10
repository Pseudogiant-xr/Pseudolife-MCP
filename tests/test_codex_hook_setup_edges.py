"""Exercise setup refusal paths without accessing the real user's Codex home."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_hook_setup_edges", ROOT / "ops/setup-codex-hooks.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def options(**changes):
    return argparse.Namespace(**dict({"source": "auto", "trust": "yes", "instructions": "auto",
                                      "non_interactive": True}, **changes))


def user_config(home, **features):
    return {"config": {"features": features}, "layers": [
        {"name": {"type": "user", "file": str(home / "config.toml"), "profile": None},
         "version": "fixture-version"}]}


def plugin_hooks(home):
    directory = home / "plugin/hooks"
    shutil.copytree(ROOT / "plugin/hooks", directory)
    return [{"key": "plugin-" + event, "currentHash": "sha256:" + "a" * 64,
             "eventName": event, "enabled": True, "trustStatus": "untrusted", "isManaged": False,
             "sourcePath": str(directory / "hooks.json"), "source": "plugin",
             "pluginId": setup.PLUGIN_ID, "handlerType": "command"}
            for event in setup.EVENTS]


def mock_runtime(monkeypatch, home, config, hooks):
    calls = []

    class Client:
        def rpc(self, method, params):
            calls.append((method, params))
            if method == "config/read":
                return config
            if method == "hooks/list":
                return {"data": [{"hooks": hooks, "errors": []}]}
            pytest.fail("Unexpected mutation or verification RPC: " + method)

    @contextmanager
    def codex(*args, **kwargs):
        yield Client()

    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(setup, "resolve_codex", lambda: "fixture-codex")
    monkeypatch.setattr(setup, "codex", codex)
    monkeypatch.setattr(setup, "verify", lambda *a, **kw: pytest.fail("Unapproved verification"))
    return calls


def seed_user_files(home):
    config = home / "config.toml"
    config.write_bytes(b'# Preserve trust and preferences\n[hooks.state.other]\ntrusted_hash = "user-hash"\n')
    hooks = home / "hooks.json"
    hooks.write_bytes(b'{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo user"}]}]}}\n')
    return {path: path.read_bytes() for path in (config, hooks)}


@pytest.mark.parametrize("source", ["auto", "manual", "plugin"])
@pytest.mark.parametrize("trust", ["no", "ask"])
def test_unapproved_setup_preserves_hook_files_and_trust(tmp_path, monkeypatch, source, trust):
    original = seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path) if source == "plugin" else []
    calls = mock_runtime(monkeypatch, tmp_path, user_config(tmp_path), hooks)
    result = setup.setup(options(source=source, trust=trust))
    assert result["status"] == "pending", result
    assert result["instructions"] == "skipped"
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not (tmp_path / "pseudolife").exists()
    assert not result["backups"]
    assert all(method in ("config/read", "hooks/list") for method, _ in calls)


@pytest.mark.parametrize("disabled", ["feature", "hook"])
def test_explicit_disabled_hooks_survive_approval_and_get_fallback(tmp_path, monkeypatch, disabled):
    original = seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    config = user_config(tmp_path, hooks=disabled != "feature")
    if disabled == "hook":
        hooks[0]["enabled"] = False
    calls = mock_runtime(monkeypatch, tmp_path, config, hooks)
    result = setup.setup(options())
    assert result["status"] == "unavailable", result
    assert "disabled" in result["recovery"]
    assert result["instructions"] == "appended"
    assert "memory_search" in (tmp_path / "AGENTS.md").read_text()
    assert all(path.read_bytes() == data for path, data in original.items())
    assert all(method in ("config/read", "hooks/list") for method, _ in calls)


@pytest.mark.parametrize("problem", ["partial", "script-mismatch"])
def test_unexpected_plugin_is_not_trusted_or_replaced(tmp_path, monkeypatch, problem):
    original = seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    if problem == "partial":
        hooks.pop()
    else:
        script = tmp_path / "plugin/hooks/lifecycle.ps1"
        script.write_text("Write-Output 'custom user script'\n")
    mock_runtime(monkeypatch, tmp_path, user_config(tmp_path), hooks)
    result = setup.setup(options())
    assert result["status"] == "unavailable", result
    assert result["source"] == "plugin"
    assert result["instructions"] == "appended"
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not (tmp_path / "pseudolife").exists()
    if problem == "script-mismatch":
        assert script.read_text() == "Write-Output 'custom user script'\n"


def test_legacy_migration_removes_exact_commands_and_preserves_lookalikes(tmp_path):
    command = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
    legacy = {"type": "command", "command": command, "commandWindows": command, "timeout": 15}
    custom = [
        {"type": "command", "command": command + " --custom"},
        {"type": "command", "command": "echo before; " + command},
    ]
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"description": "User configuration", "hooks": {
        "SessionStart": [{"matcher": "startup", "hooks": [legacy, *custom]}]}}))
    original = path.read_bytes()
    result = {"backups": []}
    setup.install_manual(tmp_path, result)
    data = json.loads(path.read_bytes())
    groups = data["hooks"]["SessionStart"]
    assert groups[0] == {"matcher": "startup", "hooks": custom}
    assert len(groups) == 2
    assert data["description"] == "User configuration"
    assert Path(result["backups"][0]).read_bytes() == original


@pytest.mark.parametrize("custom_field", ["command", "commandWindows"])
def test_custom_platform_override_stops_migration_without_duplicate_hooks(tmp_path, monkeypatch, custom_field):
    original = seed_user_files(tmp_path)
    command = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"
    customized = {"type": "command", "command": command, "commandWindows": command}
    customized[custom_field] = "echo user customization"
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [customized]}]}}))
    original[path] = path.read_bytes()
    mock_runtime(monkeypatch, tmp_path, user_config(tmp_path), [])
    result = setup.setup(options(source="manual"))
    assert result["status"] == "unavailable", result
    assert result["instructions"] == "appended"
    assert "custom" in result["recovery"].lower(), result
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not (tmp_path / "pseudolife").exists()


@pytest.mark.parametrize("layers", [[], [{"name": {"type": "user", "file": "unused"}}]])
def test_unknown_or_unversioned_config_api_does_not_write(tmp_path, layers):
    original = seed_user_files(tmp_path)
    if layers:
        layers[0]["name"]["file"] = str(tmp_path / "config.toml")
    hooks = plugin_hooks(tmp_path)

    class Client:
        def rpc(self, method, params):
            pytest.fail("Config mutation without supported version: " + method)

    result = {"backups": []}
    with pytest.raises(setup.SetupError, match="versioned"):
        setup.trust_hooks(Client(), {"layers": layers}, hooks, tmp_path, result)
    assert all(path.read_bytes() == data for path, data in original.items())
    assert not result["backups"]


def test_transient_failure_normalizes_auto_source_and_preserves_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    def unavailable():
        raise ConnectionError("private diagnostic must not leak")

    monkeypatch.setattr(setup, "resolve_codex", unavailable)
    result = setup.setup(options())
    assert result["source"] in ("manual", "plugin", "skip")
    assert result["status"] == "unavailable"
    assert result["instructions"] == "appended"
    assert "private diagnostic" not in json.dumps(result)
