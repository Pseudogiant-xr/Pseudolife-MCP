"""Scoped consent, preservation, and real runtime coverage for hook setup."""
import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("codex_hook_setup", ROOT / "ops/setup-codex-hooks.py")
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


def options(**kwargs):
    return argparse.Namespace(**dict(dict(source="auto", trust="ask", instructions="auto",
                                         non_interactive=False), **kwargs))


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("answer,approved,fallback,source", [
    ("1\n", True, True, "auto"), ("\n", True, True, "auto"),
    ("2\n", False, True, "skip"), ("3\n", False, False, "skip"),
    ("", False, False, "auto"), ("unexpected\n", False, False, "skip"),
])
def test_single_consent_choice(monkeypatch, answer, approved, fallback, source):
    monkeypatch.setattr(setup.sys, "stdin", Terminal(answer))
    args = options()
    assert setup.consent(args) == (approved, fallback)
    assert args.source == source


@pytest.mark.parametrize("trust,instructions,expected", [
    ("ask", "auto", (False, False)), ("ask", "append", (False, True)),
    ("yes", "auto", (True, True)), ("no", "auto", (False, False)),
    ("no", "append", (False, True)),
])
def test_unattended_consent_is_explicit(monkeypatch, trust, instructions, expected):
    monkeypatch.setattr(setup.sys, "stdin", Terminal("1\n"))
    assert setup.consent(options(trust=trust, instructions=instructions, non_interactive=True)) == expected


@pytest.mark.parametrize("choice", ["append", "skip"])
def test_explicit_instruction_choice_survives_hook_only_prompt(monkeypatch, choice):
    monkeypatch.setattr(setup.sys, "stdin", Terminal("n\n"))
    args = options(instructions=choice)
    assert setup.consent(args) == (False, choice == "append")
    assert args.instructions == choice


def test_explicit_source_skip_does_not_prompt(monkeypatch):
    monkeypatch.setattr(setup.sys, "stdin", Terminal("1\n"))
    args = options(source="skip", instructions="append")
    assert setup.consent(args) == (False, True)
    assert setup.sys.stdin.tell() == 0


def report():
    return {"backups": []}


def test_manual_install_preserves_unrelated_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / "hooks.json"
    other = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo unrelated"}]}
    path.write_text(json.dumps({"description": "User hooks", "hooks": {"SessionStart": [other]}}))
    result = report()
    setup.install_manual(tmp_path, result)
    first = path.read_bytes()
    data = json.loads(first)
    assert data["description"] == "User hooks"
    assert data["hooks"]["SessionStart"][0] == other
    assert set(data["hooks"]) == set(setup.EVENTS.values())
    assert len(result["backups"]) == 1
    assert json.loads(Path(result["backups"][0]).read_bytes())["hooks"]["SessionStart"] == [other]
    setup.install_manual(tmp_path, result)
    assert path.read_bytes() == first and len(result["backups"]) == 1


def test_script_changes_cannot_hide_under_same_manual_bundle(tmp_path):
    setup.install_manual(tmp_path, report())
    installed = next((tmp_path / "pseudolife/hooks").glob("*/lifecycle.ps1"))
    installed.write_text("modified by user")
    with pytest.raises(setup.SetupError, match="modified"):
        setup.install_manual(tmp_path, report())
    assert installed.read_text() == "modified by user"


def test_read_only_rerun_rejects_modified_trusted_manual_scripts(tmp_path, monkeypatch):
    setup.install_manual(tmp_path, report())
    installed = next((tmp_path / "pseudolife/hooks").glob("*/lifecycle.ps1"))
    definitions = setup.manual_definitions(installed.parent)
    hooks = [hook(tmp_path, event, command=definitions[name]["commandWindows"], trustStatus="trusted")
             for event, name in setup.EVENTS.items()]
    setup.vet_manual(hooks, tmp_path)
    installed.write_text("modified by user")
    with pytest.raises(setup.SetupError, match="modified"):
        setup.vet_manual(hooks, tmp_path)


def hook(home, event="sessionStart", **kwargs):
    return dict(dict(key="our-key", currentHash="sha256:" + "a" * 64,
                     eventName=event, enabled=True, trustStatus="untrusted", isManaged=False,
                     sourcePath=str(home / "hooks.json"), source="user", command="echo unrelated"), **kwargs)


def test_disabled_plugin_never_replaced_by_manual(tmp_path):
    hooks = [hook(tmp_path, pluginId=setup.PLUGIN_ID, source="plugin", enabled=False)]
    with pytest.raises(setup.SetupError, match="disabled"):
        setup.select_hooks(hooks, tmp_path, "auto")


def test_explicit_manual_does_not_stack_plugin(tmp_path):
    with pytest.raises(setup.SetupError, match="already owns"):
        setup.select_hooks([hook(tmp_path, pluginId=setup.PLUGIN_ID)], tmp_path, "manual")


def test_partial_plugin_rejected():
    with pytest.raises(setup.SetupError, match="missing"):
        setup.vet_plugin([])


@pytest.mark.parametrize("ready,choice,allowed,expected", [
    (True, "auto", True, "covered-by-hooks"), (False, "auto", True, "appended"),
    (False, "auto", False, "skipped"), (True, "append", False, "appended"),
    (False, "skip", True, "skipped"),
])
def test_fallback_depends_on_readiness_and_consent(tmp_path, ready, choice, allowed, expected):
    result = report()
    assert setup.standing_instructions(tmp_path, choice, allowed, ready, result) == expected
    assert (tmp_path / "AGENTS.md").exists() == (expected == "appended")


def test_fallback_preserves_override_and_backs_it_up(tmp_path):
    override = tmp_path / "AGENTS.override.md"
    original = b"User project rules.\n"
    override.write_bytes(original)
    result = report()
    assert setup.standing_instructions(tmp_path, "auto", True, False, result) == "appended"
    assert override.read_bytes().startswith(original)
    assert Path(result["backups"][0]).read_bytes() == original
    assert not (tmp_path / "AGENTS.md").exists()
    first = override.read_bytes()
    assert setup.standing_instructions(tmp_path, "auto", True, False, result) == "present"
    assert first == override.read_bytes() and len(result["backups"]) == 1


class Writer:
    def __init__(self):
        self.calls = []

    def rpc(self, method, params):
        self.calls.append((method, params))
        return {}


def config(home):
    return {"layers": [{"name": {"type": "user", "file": str(home / "config.toml"), "profile": None},
                        "version": "snapshot-token"}]}


def test_trust_uses_runtime_hash_and_versioned_scoped_config_write(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# Keep user configuration\n")
    writer = Writer()
    entry = hook(tmp_path, key='pseudolife-memory@pseudolife-mcp:hooks/hooks.json:session_start:0:0')
    result = report()
    setup.trust_hooks(writer, config(tmp_path), [entry], tmp_path, result)
    method, params = writer.calls[0]
    assert method == "config/batchWrite" and params["expectedVersion"] == "snapshot-token"
    assert params["edits"] == [{"keyPath": setup.dotted("hooks", "state", entry["key"], "trusted_hash"),
                                "value": entry["currentHash"], "mergeStrategy": "replace"}]
    assert Path(result["backups"][0]).read_bytes() == path.read_bytes()


def test_already_trusted_does_not_write(tmp_path):
    writer = Writer()
    setup.trust_hooks(writer, config(tmp_path), [hook(tmp_path, trustStatus="trusted")], tmp_path, report())
    assert not writer.calls


def test_modified_definition_can_be_approved_for_current_hash(tmp_path):
    writer = Writer()
    setup.trust_hooks(writer, config(tmp_path), [hook(tmp_path, trustStatus="modified")], tmp_path, report())
    assert len(writer.calls[0][1]["edits"]) == 1


def test_daemon_readiness_retries_cold_start(monkeypatch):
    states = iter([{"status": "starting"}, {"status": "ok"}])
    monkeypatch.setattr(setup, "daemon_request", lambda _: next(states))
    monkeypatch.setattr(setup.time, "sleep", lambda _: None)
    setup.wait_for_daemon()


def test_daemon_readiness_failure_is_bounded(monkeypatch):
    monkeypatch.setattr(setup, "daemon_request", lambda _: {"status": "starting"})
    with pytest.raises(setup.SetupError, match="not ready"):
        setup.wait_for_daemon(timeout=0)


@pytest.mark.parametrize("extra", [{"currentHash": "unknown"}, {"isManaged": True},
                                  {"trustStatus": "future-policy"}])
def test_unknown_or_managed_trust_never_written(tmp_path, extra):
    writer = Writer()
    with pytest.raises(setup.SetupError):
        setup.trust_hooks(writer, config(tmp_path), [hook(tmp_path, **extra)], tmp_path, report())
    assert not writer.calls


def test_failed_setup_keeps_promised_fallback_and_redacts_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    def broken():
        raise RuntimeError("secret-token")
    monkeypatch.setattr(setup, "resolve_codex", broken)
    result = setup.setup(options(trust="yes"))
    assert result["status"] == "unavailable" and result["instructions"] == "appended"
    assert "secret-token" not in json.dumps(result)


@pytest.mark.parametrize("existing_config", [True, False])
def test_real_codex_manual_trust_and_lifecycle(tmp_path, monkeypatch, existing_config):
    """Use a disposable Codex home, real CLI, and in-process daemon fixture."""
    codex = shutil.which("codex")
    if not codex or (os.name == "nt" and not shutil.which("pwsh")):
        pytest.skip("Codex CLI and native hook runtime are required")
    sessions = set()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, content_type="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self.send(b'{"status":"ok"}')
            elif parsed.path == "/api/hook/session-start":
                sid = parse_qs(parsed.query)["session_id"][0]
                sessions.add(sid)
                seen.append("start")
                self.send(b"Session episode: abcdef123456 -- fixture briefing", "text/plain; charset=utf-8")
            elif parsed.path == "/api/episodes":
                self.send(json.dumps({"episodes": [{"session_key": s} for s in sessions]}).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/api/hook/session-end"
            sessions.discard(body["session_id"])
            seen.append("end")
            self.send(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "home"
    home.mkdir()
    if existing_config:
        (home / "config.toml").write_text('# User comment must survive\n[features]\nmemories = false\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DAEMON_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.delenv("PSEUDOLIFE_MCP_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_CLI_PATH", codex)
    try:
        result = setup.setup(options(trust="yes", non_interactive=True))
        assert result["status"] == "ready", result
        assert result["verified"] == {"session_start": True, "user_prompt_submit": True, "session_end": True}
        assert result["instructions"] == "covered-by-hooks"
        assert seen == ["start", "end"] and not sessions
        text = (home / "config.toml").read_text()
        assert text.count("trusted_hash") == 3
        if existing_config:
            assert text.startswith("# User comment must survive")
        assert "dangerously-bypass" not in text
        # A separately trusted user hook must neither run during our probe nor
        # lose its persisted settings. Only temporary runtime overrides exclude it.
        marker = home / "unrelated-ran.txt"
        command = (f"Set-Content -LiteralPath '{str(marker).replace(chr(39), chr(39)*2)}' -Value ran"
                   if os.name == "nt" else "touch " + setup.shlex.quote(str(marker)))
        manifest_path = home / "hooks.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["hooks"]["SessionStart"].append({"hooks": [{"type": "command", "command": command,
                                                          "commandWindows": command}]})
        manifest_path.write_text(json.dumps(manifest))
        with setup.codex(codex, home, tmp_path) as client:
            current_config, current_hooks = setup.inventory(client, tmp_path)
            unrelated = [h for h in current_hooks if h["command"] == command]
            assert len(unrelated) == 1
            setup.trust_hooks(client, current_config, unrelated, home, report())
        first = (home / "config.toml").read_bytes()
        result = setup.setup(options(trust="no", non_interactive=True))
        assert result["status"] == "ready", result
        assert (home / "config.toml").read_bytes() == first
        assert not result["backups"]
        assert not marker.exists()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
