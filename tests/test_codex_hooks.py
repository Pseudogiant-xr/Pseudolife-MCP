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


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("source,trust,instructions,status,fallback,exit_code", [
    ("auto", "yes", "auto", "ready", "covered-by-hooks", 0),
    ("manual", "yes", "append", "ready", "appended", 0),
    ("plugin", "ask", "auto", "pending", "present", 0),
    ("plugin", "no", "skip", "pending", "skipped", 1),
    ("skip", "no", "append", "skipped", "appended", 0),
    ("auto", "yes", "auto", "unavailable", "appended", 0),
    ("auto", "ask", "auto", "unavailable", "skipped", 1),
    ("auto", "yes", "auto", "ready", "covered-by-hooks", 2),
])
def test_installer_hook_and_instruction_stages_together(
        tmp_path, shell, source, trust, instructions, status, fallback, exit_code):
    """Real shell stages delegate Codex ownership and propagate readiness.

    The helper fixture records its arguments and returns the setup contract.
    Stage 10 must not write or prompt again, even for explicit append. Docker,
    MCP registration, and the real user home remain outside this fixture.
    """
    home = tmp_path / "home"
    home.mkdir()
    fixture_repo = tmp_path / "repo"
    (fixture_repo / "ops").mkdir(parents=True)
    (fixture_repo / "examples").mkdir()
    shutil.copyfile(ROOT / "examples/CLAUDE.memory.md", fixture_repo / "examples/CLAUDE.memory.md")
    result_data = {"source": source if source != "auto" else "manual",
                   "status": status, "instructions": fallback,
                   "recovery": "Review the hook in /hooks." if status != "ready" else None}
    helper = fixture_repo / "ops/setup-codex-hooks.py"
    helper.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(os.environ['FIXTURE_CALL']).write_text(json.dumps({'args': sys.argv[1:], "
        "'codex_home': os.environ['CODEX_HOME']}))\n"
        f"print({json.dumps(result_data)!r})\nsys.exit({exit_code})\n", encoding="utf-8")
    call = tmp_path / "call.json"
    codex_home = home / "custom codex"
    env = {**os.environ, "HOME": home.as_posix(), "USERPROFILE": home.as_posix(),
           "CODEX_HOME": codex_home.as_posix(), "FIXTURE_CALL": str(call)}
    result = run_installer_stages(tmp_path, shell, fixture_repo, env, source, trust, instructions)
    expected_status = status if exit_code < 2 else "unavailable"
    expected_fallback = fallback if exit_code < 2 else "appended"
    assert f"RESULT:{expected_status}:{expected_fallback}" in result
    recorded = json.loads(call.read_text())
    assert recorded == {"args": ["--source", source, "--trust", trust, "--instructions",
                                 instructions, "--non-interactive"],
                        "codex_home": codex_home.as_posix()}
    assert (codex_home / "AGENTS.md").exists() == (exit_code == 2)
    assert not (home / ".codex/hooks.json").exists()
    assert not (home / ".codex/AGENTS.md").exists()


@pytest.mark.parametrize("shell", ["powershell", "bash"])
@pytest.mark.parametrize("trust,instructions,expected,existing", [
    ("yes", "auto", "appended", None),
    ("no", "append", "appended", None),
    ("ask", "append", "appended", "override"),
    ("yes", "auto", "appended", "mention"),
    ("yes", "auto", "present", "complete"),
    ("yes", "auto", "appended", "empty-override"),
    ("yes", "skip", "skipped", None),
    ("ask", "auto", "skipped", None),
    ("no", "auto", "skipped", None),
])
def test_installer_codex_without_python_reports_unavailable(
        tmp_path, shell, trust, instructions, expected, existing):
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / "custom codex"
    codex_home.mkdir()
    target = codex_home / ("AGENTS.override.md" if existing == "override" else "AGENTS.md")
    old = {"override": b"User override rules.\n", "mention": b"Consider pseudolife-memory; use memory_search.\n",
           "complete": (ROOT / "examples/CLAUDE.memory.md").read_bytes()}.get(existing)
    if old:
        target.write_bytes(old)
    if existing == "empty-override":
        (codex_home / "AGENTS.override.md").write_bytes(b" \n")
    env = {**os.environ, "HOME": home.as_posix(), "USERPROFILE": home.as_posix(),
           "CODEX_HOME": codex_home.as_posix()}
    result = run_installer_stages(tmp_path, shell, ROOT, env, "auto", trust, instructions,
                                  missing_python=True)
    assert f"RESULT:unavailable:{expected}" in result
    assert "Install Python 3.10 or newer" in result
    assert not (home / ".codex").exists()
    assert target.exists() == (expected != "skipped")
    if expected == "appended":
        text = target.read_text(encoding="utf-8")
        assert all(term in text for term in ("## Memory", "pseudolife-memory", "RECALL", "CAPTURE", "REFLECT"))
        if old:
            assert target.read_bytes().startswith(old)
            backups = list(codex_home.glob(target.name + ".bak-pseudolife-*"))
            assert len(backups) == 1 and backups[0].read_bytes() == old
    elif expected == "present":
        assert target.read_bytes() == old
        assert not list(codex_home.glob("*.bak-pseudolife-*"))
    if existing == "empty-override":
        assert (codex_home / "AGENTS.override.md").read_bytes() == b" \n"


def run_installer_stages(tmp_path, shell, repo, env, source, trust, instructions,
                         missing_python=False):
    if shell == "powershell":
        text = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
        stages = text.split("# -- 9. session lifecycle hooks", 1)[1].split(
            "# -- 11. wire into selected MCP clients", 1)[0].split("\n", 1)[1]
        script = tmp_path / "stages.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$repo = '{repo.as_posix()}'\n$clients = @('codex')\n"
            f"$CodexHooks = '{source}'\n$CodexHookTrust = '{trust}'\n"
            f"$Instructions = '{instructions}'\n$interactive = $false\n"
            "function Step($text) { Write-Host $text }\n"
            "function Read-Host($text) { throw 'Unexpected second prompt' }\n"
            + ("function Get-Command($Name) { return $null }\n" if missing_python else "")
            + stages + "\nWrite-Output ('RESULT:' + $hookState['codex'] + ':' + $instrState['codex'])\n",
            encoding="utf-8")
        return pwsh_run("-File", script, env=env).stdout
    bash = shutil.which("bash")
    if os.name == "nt":
        # The system32 bash is a WSL launcher; use native Git Bash here.
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
        bash = str(candidate) if candidate and candidate.is_file() else None
    if not bash:
        pytest.skip("Bash is not installed")
    text = (ROOT / "ops/install.sh").read_text(encoding="utf-8")
    parts = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)^# [^\n]*11\. wire into selected MCP clients", text)
    assert parts, "installer stage boundaries changed"
    script = tmp_path / "stages.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"repo={shlex.quote(repo.as_posix())}\n"
        f"CLIENTS=codex\nCODEX_HOOKS={source}\nCODEX_HOOK_TRUST={trust}\nINSTRUCTIONS={instructions}\n"
        "CLAUDE_MD=''\nAGENTS_FILE=''\nstep() { echo \"$*\"; }\n"
        + ("command() { case \"$*\" in '-v python'|'-v python3') return 1;; "
           "*) builtin command \"$@\";; esac; }\n" if missing_python else "")
        + parts[1] + '\nprintf "RESULT:%s:%s\\n" "$HOOK_CODEX" "$INSTR_CODEX"\n',
        encoding="utf-8")
    result = subprocess.run([bash, script.as_posix()], env=env, capture_output=True,
                            timeout=30, check=True)
    return result.stdout.decode("utf-8")
