"""Run Windows hook installation and native hook commands in disposable homes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pwsh_run(*args, input=None, env=None):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    return subprocess.run([pwsh, "-NoProfile", *map(str, args)], input=input,
                          env=env, capture_output=True, text=True, timeout=30,
                          check=True)


def test_codex_hook_install_preserves_existing_hooks_and_is_idempotent(tmp_path):
    settings = tmp_path / "hooks.json"
    original = {"type": "command", "command": "echo existing"}
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [original]}]}}), encoding="utf-8")
    for _ in range(2):
        pwsh_run("-File", ROOT / "ops/install-hook.ps1", "-Client", "codex",
                 "-SettingsPath", settings)
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    starts = [h for g in hooks["SessionStart"] for h in g["hooks"]]
    assert starts[0] == original
    assert len(starts) == 2
    assert starts[1]["commandWindows"] == starts[1]["command"]
    prompts = [h for g in hooks["UserPromptSubmit"] for h in g["hooks"]]
    assert len(prompts) == 1
    result = pwsh_run("-Command", prompts[0]["commandWindows"])
    assert "memory_lesson_search" in result.stdout


def test_plugin_native_windows_prompt_context():
    manifest = json.loads((ROOT / "plugin/hooks/hooks.json").read_text())
    hook = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    command = hook["commandWindows"]
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugin")}
    result = pwsh_run("-Command", command, input='{"session_id":"fixture"}', env=env)
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "memory_lesson_search" in output["hookSpecificOutput"]["additionalContext"]


def test_windows_installer_no_longer_skips_codex_hooks():
    text = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    assert '($selectedClient -eq "codex") -and $IsWindows' not in text


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
def test_native_hook_forwards_session_identity_to_fixture_daemon(event):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Fixture memory briefing")

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(
                int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        env = {**os.environ, "PSEUDOLIFE_MCP_DAEMON_URL":
               f"http://127.0.0.1:{server.server_port}",
               "PSEUDOLIFE_MCP_TOKEN": "fixture-token"}
        result = pwsh_run("-File", ROOT / "plugin/hooks/lifecycle.ps1", "-Event", event,
                          input=json.dumps({"session_id": "fixture & session", "source": "startup"}), env=env)
        assert len(requests) == 1
        if event == "SessionStart":
            path, auth = requests[0]
            assert parse_qs(urlsplit(path).query)["session_id"] == ["fixture & session"]
            assert auth == "Bearer fixture-token"
            output = json.loads(result.stdout)["hookSpecificOutput"]
            assert output == {"hookEventName": event, "additionalContext": "Fixture memory briefing"}
        else:
            assert requests == [("/api/hook/session-end", {"session_id": "fixture & session"})]
            assert not result.stdout.strip()
        assert "fixture-token" not in result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_native_prompt_reminder_matches_claude_script():
    import re
    ps = (ROOT / "plugin/hooks/lifecycle.ps1").read_text(encoding="utf-8")
    sh = (ROOT / "plugin/hooks/user-prompt-submit.sh").read_text(encoding="utf-8")
    assert re.search(r'\$disciplineLine = "(.*)"', ps)[1] == re.search(r'echo "(.*)"', sh)[1]


@pytest.mark.parametrize("source", ["plugin", "skip"])
def test_installer_hook_stage_respects_codex_hook_source(tmp_path, source):
    ps = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    stage = ps.split("# -- 9. session lifecycle hooks", 1)[1].split(
        "# -- 10. standing memory instructions", 1)[0]
    # Discard the rest of the section-header line, execute ONLY this stage.
    stage = stage.split("\n", 1)[1]
    script = tmp_path / "stage.ps1"
    script.write_text(
        f"$env:USERPROFILE = '{tmp_path.as_posix()}'\n"
        f"$CodexHooks = '{source}'\n$clients = @('codex')\n"
        "function Step($text) {}\n" + stage +
        "\n$hookState['codex'] | ConvertTo-Json\n", encoding="utf-8")
    result = pwsh_run("-File", script)
    assert json.loads(result.stdout) == source
    assert not (tmp_path / ".codex/hooks.json").exists()
