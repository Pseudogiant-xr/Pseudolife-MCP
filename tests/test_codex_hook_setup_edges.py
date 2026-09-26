"""Exercise setup refusal paths without accessing the real user's Codex home."""
import argparse
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import re
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
    """What Codex lists for the plugin: one entry per handler in the shipped
    hooks.json (read from the file, not from setup's own constants, so a new
    event there cannot hide from these tests)."""
    directory = home / "plugin/hooks"
    shutil.copytree(ROOT / "plugin/hooks", directory)
    events = json.loads((directory / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    return [{"key": f"plugin-{event}-{group_index}-{hook_index}", "currentHash": "sha256:" + "a" * 64,
             "eventName": event[0].lower() + event[1:], "enabled": True, "trustStatus": "untrusted",
             "isManaged": False, "sourcePath": str(directory / "hooks.json"), "source": "plugin",
             "pluginId": setup.PLUGIN_ID, "handlerType": "command",
             "command": handler["commandWindows"]}
            for event, groups in events.items()
            for group_index, group in enumerate(groups)
            for hook_index, handler in enumerate(group["hooks"])]


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
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
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
        hooks = [h for h in hooks if h["eventName"] != "sessionEnd"]
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


# --- the plugin's Stop entry (Claude Code's opt-in wake hook) ---------------
# Codex loads the plugin's hooks.json too, so it lists the Stop entry beside
# the memory and coordination lifecycle hooks. It is a no-op in Codex, and
# setup approves it with them; manual installs omit Stop.

def approving_runtime(monkeypatch, home, hooks):
    """A runtime that accepts one trust write and then lists every hook trusted."""
    writes = []

    class Client:
        def rpc(self, method, params):
            if method == "config/read":
                return user_config(home)
            if method == "hooks/list":
                return {"data": [{"hooks": hooks, "errors": []}]}
            if method == "config/batchWrite":
                writes.append(params)
                for h in hooks:
                    h["trustStatus"] = "trusted"
                return {}
            pytest.fail("Unexpected RPC: " + method)

    @contextmanager
    def codex(*args, **kwargs):
        yield Client()

    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setattr(setup, "resolve_codex", lambda: "fixture-codex")
    monkeypatch.setattr(setup, "codex", codex)
    monkeypatch.setattr(setup, "verify", lambda *a, **kw: {"session_start": True})
    return writes


def test_setup_knows_every_event_the_plugin_ships():
    shipped = json.loads((ROOT / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(setup.PLUGIN_EVENTS.values()) == set(shipped)
    assert set(setup.EVENTS.values()) < set(shipped)


def test_plugin_runtime_commands_must_keep_their_manifest_event_roles(tmp_path):
    hooks = plugin_hooks(tmp_path)
    start = next(h for h in hooks if h["eventName"] == "sessionStart")
    prompt = next(h for h in hooks if h["eventName"] == "userPromptSubmit")
    start["command"], prompt["command"] = prompt["command"], start["command"]
    with pytest.raises(setup.SetupError, match="differ"):
        setup.vet_plugin(hooks)


def test_plugin_runtime_requires_each_role_once_across_platform_spellings(tmp_path):
    hooks = plugin_hooks(tmp_path)
    starts = [h for h in hooks if h["eventName"] == "sessionStart"]
    manifest = json.loads((tmp_path / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    starts[1]["command"] = manifest["SessionStart"][0]["hooks"][0]["command"]
    assert setup.complete_set(hooks, "plugin")  # Distinct strings still name one semantic role.
    with pytest.raises(setup.SetupError, match="differ"):
        setup.vet_plugin(hooks)


@pytest.mark.parametrize("field,expand", [("commandWindows", False), ("command", False),
                                           ("commandWindows", True), ("command", True)])
def test_plugin_runtime_accepts_both_manifest_platform_commands_and_root_expansion(
        tmp_path, field, expand):
    hooks = plugin_hooks(tmp_path)
    manifest = json.loads((tmp_path / "plugin/hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    for hook in hooks:
        event = hook["eventName"][0].upper() + hook["eventName"][1:]
        group, handler = map(int, hook["key"].rsplit("-", 2)[1:])
        command = manifest[event][group]["hooks"][handler][field]
        if expand:
            command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(tmp_path / "plugin"))
            command = command.replace("$env:CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
        hook["command"] = command
    setup.vet_plugin(hooks)


@pytest.mark.parametrize("listed", ["all", "without-stop"])
def test_approved_plugin_setup_trusts_every_listed_hook(tmp_path, monkeypatch, listed):
    """Codex 0.148+ lists the plugin's async Stop hook; older Codex skips
    async hooks outside SessionEnd and omit Stop. Both reach ready."""
    seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    if listed == "without-stop":
        hooks = [h for h in hooks if h["eventName"] != "stop"]
    writes = approving_runtime(monkeypatch, tmp_path, hooks)
    result = setup.setup(options())
    assert result["status"] == "ready", result
    [write] = writes
    assert sorted(edit["keyPath"] for edit in write["edits"]) == sorted(
        setup.dotted("hooks", "state", h["key"], "trusted_hash") for h in hooks)


def test_a_disabled_stop_hook_does_not_block_setup(tmp_path, monkeypatch):
    """A user who disabled the no-op Stop entry in /hooks keeps that choice;
    the six memory, memory-policy and coordination hooks are still approved."""
    seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    [stop] = [h for h in hooks if h["eventName"] == "stop"]
    stop["enabled"] = False
    writes = approving_runtime(monkeypatch, tmp_path, hooks)
    result = setup.setup(options())
    assert result["status"] == "ready", result
    assert len(writes[0]["edits"]) == 6
    assert all(stop["key"] not in edit["keyPath"] for edit in writes[0]["edits"])


def test_the_plugin_byte_check_covers_the_stop_script(tmp_path, monkeypatch):
    """stop-wake.sh is Codex's non-Windows Stop command, run outside the
    sandbox under the trust setup writes: a changed copy is skew."""
    original = seed_user_files(tmp_path)
    hooks = plugin_hooks(tmp_path)
    (tmp_path / "plugin/hooks/stop-wake.sh").write_text("exit 2\n")
    mock_runtime(monkeypatch, tmp_path, user_config(tmp_path), hooks)
    result = setup.setup(options())
    assert result["status"] == "unavailable" and "differ" in result["recovery"], result
    assert all(path.read_bytes() == data for path, data in original.items())


def test_a_plugin_from_before_the_stop_hook_is_reported_as_skew(tmp_path, monkeypatch):
    hooks = [h for h in plugin_hooks(tmp_path) if h["eventName"] != "stop"]
    directory = tmp_path / "plugin/hooks"
    stale = json.loads((directory / "hooks.json").read_text(encoding="utf-8"))
    del stale["hooks"]["Stop"]
    (directory / "hooks.json").write_text(json.dumps(stale), encoding="utf-8")
    (directory / "stop-wake.sh").unlink()
    mock_runtime(monkeypatch, tmp_path, user_config(tmp_path), hooks)
    result = setup.setup(options())
    assert result["status"] == "unavailable" and "differ" in result["recovery"], result


def _prompt_cursor(tmp_path, monkeypatch):
    """The cursor the memory-change prompt hook saves for the verification
    thread after an authorized answer; its first turn prints nothing."""
    import hashlib
    digests = tmp_path / "digests"
    digests.mkdir(exist_ok=True)
    monkeypatch.setenv("PSEUDOLIFE_DIGEST_DIR", str(digests))
    mark = digests / (hashlib.sha256(b"fixture-thread").hexdigest() + ".mark")
    mark.write_text("100.000000\n")
    return mark


def test_verification_counts_every_selected_hook(tmp_path, monkeypatch):
    mark = _prompt_cursor(tmp_path, monkeypatch)
    hooks = plugin_hooks(tmp_path)
    for h in hooks:
        h["trustStatus"] = "trusted"

    class Client:
        events = [{"method": "hook/completed", "params": {"run": {
            "eventName": event, "status": "completed", "entries": [{"text": text}]}}}
            for event, text in (("sessionStart", "Session episode: fixture"),
                                ("sessionStart", ""),
                                ("sessionStart", "memory_agents(action=list)"),
                                ("userPromptSubmit", ""),
                                ("userPromptSubmit", ""))]

        def rpc(self, method, params):
            answers = {"config/read": {"config": {}},
                       "hooks/list": {"data": [{"hooks": hooks, "errors": []}]},
                       "thread/start": {"thread": {"id": "fixture-thread"}}, "turn/start": {}}
            if method not in answers:
                pytest.fail("Unexpected RPC: " + method)
            return answers[method]

        def receive(self, timeout):
            pass

    @contextmanager
    def codex(*args, **kwargs):
        yield Client()

    opened = iter([True, False])
    monkeypatch.setattr(setup, "codex", codex)
    monkeypatch.setattr(setup, "wait_for_daemon", lambda: None)
    monkeypatch.setattr(setup, "episode_open", lambda thread: next(opened))
    monkeypatch.setattr(setup, "board_checkin_expected", lambda: True)
    verified = setup.verify("fixture-codex", tmp_path, tmp_path, {"config": {}}, hooks, hooks)
    assert verified == {"session_start": True, "user_prompt_submit": True, "session_end": True}
    assert not mark.exists()      # the verification cleans up after itself


def test_verification_needs_the_prompt_hooks_cursor(tmp_path, monkeypatch):
    """Completed prompt hooks that never reached the daemon (no cursor
    saved) are not a working memory-change note."""
    mark = _prompt_cursor(tmp_path, monkeypatch)
    mark.unlink()
    hooks = plugin_hooks(tmp_path)
    for h in hooks:
        h["trustStatus"] = "trusted"
    monkeypatch.setattr(setup, "codex", _verification_client(
        hooks, ["Session episode: fixture", "", "memory_agents(action=list)"]))
    monkeypatch.setattr(setup, "wait_for_daemon", lambda: None)
    monkeypatch.setattr(setup, "episode_open", lambda thread: True)
    monkeypatch.setattr(setup, "board_checkin_expected", lambda: True)
    with pytest.raises(setup.SetupError, match="UserPromptSubmit did not return"):
        setup.verify("fixture-codex", tmp_path, tmp_path, {"config": {}}, hooks, hooks)


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
    assert len(groups) == 4
    assert data["description"] == "User configuration"
    assert Path(result["backups"][0]).read_bytes() == original


@pytest.mark.parametrize("client", ["bash", "powershell"])
@pytest.mark.parametrize("source", ["manual", "plugin"])
def test_lightweight_coordination_hook_migrates_without_touching_other_hooks(tmp_path, client, source):
    bash_line = re.search(r'^COORDINATION_LINE="(.*)"$',
                          (ROOT / "ops/install-hook.sh").read_text(encoding="utf-8"), re.M)[1]
    ps_line = re.search(r'^\$coordinationLine = "(.*)"$',
                        (ROOT / "ops/install-hook.ps1").read_text(encoding="utf-8"), re.M)[1]
    assert bash_line == ps_line
    command = ("echo" if client == "bash" else "Write-Output") + f" '{bash_line}'"
    unrelated = {"type": "command", "command": "echo user-owned"}
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": command}, unrelated]}]}}))
    result = {"backups": []}
    setup.install_manual(tmp_path, result, plugin=source == "plugin")
    hooks = json.loads(path.read_text())["hooks"]
    starts = [h["command"] for group in hooks["SessionStart"] for h in group["hooks"]]
    assert command not in starts
    assert unrelated["command"] in starts
    assert len(starts) == (1 if source == "plugin" else 4)


@pytest.mark.parametrize("command", [
    "pseudolife-mcp prompt-hook",
    "docker exec -i pseudolife-mcp-daemon pseudolife-mcp prompt-hook"])
@pytest.mark.parametrize("source", ["manual", "plugin"])
def test_lightweight_prompt_hook_migrates_without_touching_other_hooks(tmp_path, command, source):
    """install-hook -Client codex writes the memory-change command (since
    2026-09-26) with commandWindows equal to it; setup takes it over like
    the static line it replaced."""
    written = {"type": "command", "command": command, "commandWindows": command, "timeout": 5}
    unrelated = {"type": "command", "command": "echo user-owned"}
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [written, unrelated]}]}}))
    result = {"backups": []}
    setup.install_manual(tmp_path, result, plugin=source == "plugin")
    hooks = json.loads(path.read_text())["hooks"]
    prompts = [h["command"] for group in hooks["UserPromptSubmit"] for h in group["hooks"]]
    assert command not in prompts
    assert unrelated["command"] in prompts
    assert len(prompts) == (1 if source == "plugin" else 3)


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


@pytest.mark.parametrize("source", ["manual", "plugin"])
@pytest.mark.parametrize("command", [
    "pseudolife-mcp briefing --hook-json --coordination",
    "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json --coordination",
])
def test_gated_lightweight_checkin_hook_migrates_too(tmp_path, source, command):
    unrelated = {"type": "command", "command": "echo user-owned"}
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": command, "commandWindows": command}, unrelated]}]}}))
    setup.install_manual(tmp_path, {"backups": []}, plugin=source == "plugin")
    starts = [h["command"] for group in json.loads(path.read_text())["hooks"]["SessionStart"]
              for h in group["hooks"]]
    assert command not in starts and unrelated["command"] in starts


def _verification_client(hooks, start_texts):
    class Client:
        events = [{"method": "hook/completed", "params": {"run": {
            "eventName": event, "status": "completed", "entries": [{"text": text}]}}}
            for event, text in ((("sessionStart", t) for t in start_texts))]
        events += [{"method": "hook/completed", "params": {"run": {
            "eventName": "userPromptSubmit", "status": "completed", "entries": [{"text": text}]}}}
            for text in ("", "")]

        def rpc(self, method, params):
            answers = {"config/read": {"config": {}},
                       "hooks/list": {"data": [{"hooks": hooks, "errors": []}]},
                       "thread/start": {"thread": {"id": "fixture-thread"}}, "turn/start": {}}
            if method not in answers:
                pytest.fail("Unexpected RPC: " + method)
            return answers[method]

        def receive(self, timeout):
            pass

    @contextmanager
    def codex(*args, **kwargs):
        yield Client()

    return codex


@pytest.mark.parametrize("available,checkin,ok", [
    (True, "memory_agents(action=list)", True),
    (True, "", False),
    (False, "", True),
])
def test_verification_expects_the_checkin_only_where_the_board_works(
        tmp_path, monkeypatch, available, checkin, ok):
    """A board that is off serves no check-in; setup must not call that a
    broken hook (the board is on by default only where it can work)."""
    _prompt_cursor(tmp_path, monkeypatch)
    hooks = plugin_hooks(tmp_path)
    for h in hooks:
        h["trustStatus"] = "trusted"
    opened = iter([True, False])
    # Three SessionStart handlers run: memory, the memory-policy output
    # (empty unless the daemon's variant asks for it) and coordination.
    monkeypatch.setattr(setup, "codex", _verification_client(
        hooks, ["Session episode: fixture", "", checkin]))
    monkeypatch.setattr(setup, "wait_for_daemon", lambda: None)
    monkeypatch.setattr(setup, "episode_open", lambda thread: next(opened))
    monkeypatch.setattr(setup, "board_checkin_expected", lambda: available)
    if ok:
        assert setup.verify("fixture-codex", tmp_path, tmp_path, {"config": {}}, hooks, hooks)["session_start"]
    else:
        with pytest.raises(setup.SetupError, match="CoordinationStart"):
            setup.verify("fixture-codex", tmp_path, tmp_path, {"config": {}}, hooks, hooks)


@pytest.mark.parametrize("setting,body,expected,asked", [
    (None, "Pseudolife coordination: fixture.\n", True, True),
    (None, "", False, True),
    ("0", "Pseudolife coordination: fixture.\n", False, False),
    (None, OSError("down"), False, True),
])
def test_board_checkin_expected_asks_the_daemon_like_the_hook(monkeypatch, setting, body, expected, asked):
    calls = []

    def daemon_request(path, *, text=False):
        calls.append((path, text))
        if isinstance(body, Exception):
            raise body
        return body

    monkeypatch.setattr(setup, "daemon_request", daemon_request)
    if setting is None:
        monkeypatch.delenv("PSEUDOLIFE_AGENT_COORDINATION", raising=False)
    else:
        monkeypatch.setenv("PSEUDOLIFE_AGENT_COORDINATION", setting)
    assert setup.board_checkin_expected() is expected
    assert calls == ([("/api/hook/coordination-start", True)] if asked else [])
