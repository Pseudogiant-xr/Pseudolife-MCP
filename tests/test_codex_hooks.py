"""Run Windows hook installation and native hook commands in disposable homes."""
import json
import os
import re
import shlex
from pathlib import Path
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


def pwsh_run(*args, input=None, env=None, raw=False):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    return subprocess.run([pwsh, "-NoProfile", *map(str, args)], input=input,
                          env=env, capture_output=True, text=not raw, timeout=30,
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


@pytest.mark.parametrize("event,codepage,first_status", [
    ("SessionStart", 437, 200), ("SessionStart", 65001, 200),
    ("SessionStart", 437, 503), ("SessionStart", 437, 401),
    ("SessionStart", 437, "repeated503"), ("SessionStart", 437, "timeout"),
    ("SessionEnd", 437, 200),
])
def test_native_hook_forwards_session_identity_to_fixture_daemon(event, codepage, first_status):
    requests = []
    briefing = "Fixture memory \u2192 briefing \u2026"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if first_status == "timeout" and len(requests) == 1:
                time.sleep(6)  # longer than the hook's first request budget
            status = (503 if first_status == "repeated503" else
                      first_status if isinstance(first_status, int) and len(requests) == 1 else 200)
            self.send_response(status)
            self.end_headers()
            try:
                self.wfile.write(briefing.encode("utf-8"))
            except (BrokenPipeError, ConnectionResetError):
                pass  # timed-out client already closed this fixture request

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
        # Pin a child-local OEM console as well as UTF-8: the parent test
        # runner's encoding must not hide corruption of redirected JSON bytes.
        command = (f"[Console]::OutputEncoding = [Text.Encoding]::GetEncoding({codepage}); "
                   f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event {event}")
        result = pwsh_run("-Command", command, raw=True,
                          input=json.dumps({"session_id": "fixture & session", "source": "startup"}).encode(), env=env)
        assert len(requests) == (2 if first_status in (503, "repeated503", "timeout") else 1)
        if event == "SessionStart":
            path, auth = requests[0]
            assert parse_qs(urlsplit(path).query)["session_id"] == ["fixture & session"]
            assert auth == "Bearer fixture-token"
            output = json.loads(result.stdout)["hookSpecificOutput"]
            if first_status in (401, "repeated503"):
                assert "session briefing unavailable" in output["additionalContext"]
            else:
                assert output == {"hookEventName": event, "additionalContext": briefing}
        else:
            assert requests == [("/api/hook/session-end", {"session_id": "fixture & session"})]
            assert not result.stdout.strip()
        assert b"fixture-token" not in result.stdout + result.stderr
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
    assert stage.strip() and "$hookState = @{}" in stage
    script = tmp_path / "stage.ps1"
    script.write_text(
        f"$env:USERPROFILE = '{tmp_path.as_posix()}'\n"
        f"$CodexHooks = '{source}'\n$clients = @('codex')\n"
        "function Step($text) {}\n" + stage +
        "\n$hookState['codex'] | ConvertTo-Json\n", encoding="utf-8")
    result = pwsh_run("-File", script)
    assert json.loads(result.stdout) == source
    assert not (tmp_path / ".codex/hooks.json").exists()


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("source,interactive,instructions,expected_block,answer,existing", [
    ("skip", True, "auto", True, "", False),
    ("skip", False, "auto", False, "", False),
    ("manual", True, "auto", False, "", False),
    ("plugin", True, "auto", False, "", False),
    ("skip", False, "append", True, "", False),
    ("skip", True, "skip", False, "", False),
    ("skip", True, "auto", False, "n", False),
    ("manual", False, "append", True, "", False),
    ("plugin", False, "append", True, "", False),
    ("skip", True, "auto", True, "", True),
])
def test_installer_hook_and_instruction_stages_together(
        tmp_path, shell, source, interactive, instructions, expected_block, answer, existing):
    """Exercise the instruction decision AFTER hook ownership is resolved.

    Only these adjacent stages run; Docker, MCP registration, and the user's
    home are outside this fixture. Simulate prompt availability/answers at
    the shell boundary, leaving the production decision branches intact.
    """
    home = tmp_path / "home"
    home.mkdir()
    block = home / ".codex/AGENTS.md"
    original = b"Existing pseudolife-memory guidance: memory_search.\n"
    if existing:
        block.parent.mkdir()
        block.write_bytes(original)
    if shell == "powershell":
        ps = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
        start = "# -- 9. session lifecycle hooks"
        end = "# -- 11. wire into selected MCP clients"
        assert ps.count(start) == ps.count(end) == 1
        stages = ps.split(start, 1)[1].split(end, 1)[0].split("\n", 1)[1]
        assert "$hookState = @{}" in stages and "$instructionChoice =" in stages
        shutil.copyfile(ROOT / "ops/install-hook.ps1", tmp_path / "install-hook.ps1")
        script = tmp_path / "stages.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$env:USERPROFILE = '{home.as_posix()}'\n$repo = '{ROOT.as_posix()}'\n"
            f"$CodexHooks = '{source}'\n$clients = @('codex')\n"
            f"$Instructions = '{instructions}'\n$interactive = ${str(interactive).lower()}\n"
            "function Step($text) {}\n"
            f"function Read-Host($text) {{ return '{answer}' }}\n"
            + stages + "\nWrite-Output ('RESULT:' + $hookState['codex'])\n", encoding="utf-8")
        result = pwsh_run("-File", script)
    else:
        bash = shutil.which("bash")
        if os.name == "nt":
            # Windows' system32 bash is a WSL launcher, not Git Bash; it
            # cannot consume the native fixture paths used by this test.
            git = shutil.which("git")
            candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
            bash = str(candidate) if candidate and candidate.is_file() else None
        if not bash:
            pytest.skip("Bash is not installed")
        text = (ROOT / "ops/install.sh").read_text(encoding="utf-8")
        parts = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)^# [^\n]*11\. wire into selected MCP clients", text)
        assert parts, "installer stage boundaries changed"
        stages = parts[1]
        assert 'HOOK_CODEX=""' in stages and 'instruction_choice=' in stages
        script = tmp_path / "stages.sh"
        script.write_text(
            "set -euo pipefail\n"
            f"repo={shlex.quote(ROOT.as_posix())}\n"
            f"CLIENTS=codex\nCODEX_HOOKS={source}\nINSTRUCTIONS={instructions}\n"
            "CLAUDE_MD=''\nAGENTS_FILE=''\nstep() { :; }\n"
            # Only the terminal-availability query is substituted; all file
            # tests use the real shell builtin. Input below accepts the prompt.
            "function [() { if [[ $# == 3 && $1 == -t && $2 == 0 ]]; then "
            f"{str(interactive).lower()}; else builtin [ \"$@\"; fi; }}\n"
            + stages + '\nprintf "RESULT:%s\\n" "$HOOK_CODEX"\n', encoding="utf-8")
        # Bytes avoid Python translating LF to CRLF on Windows: a pipe is
        # not a terminal, so Bash would otherwise receive a literal n\r.
        result = subprocess.run([bash, script.as_posix()], input=(answer + "\n").encode(),
                                env={**os.environ, "HOME": home.as_posix()},
                                capture_output=True, timeout=30, check=True)
    stdout = result.stdout.decode("utf-8") if isinstance(result.stdout, bytes) else result.stdout
    assert f"RESULT:{'hook' if source == 'manual' else source}" in stdout
    assert (home / ".codex/hooks.json").exists() == (source == "manual")
    assert block.exists() == expected_block
    if expected_block:
        assert "memory_search" in block.read_text(encoding="utf-8")
    if existing:
        assert block.read_bytes() == original
