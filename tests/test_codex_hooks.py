"""Run Windows hook installation and native hook commands in disposable homes."""
import json
import hmac
import base64
import os
import re
import select
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]

# A hang guard for every hook and installer process this file runs, not a
# timing contract: the hooks' real caps (Codex 15/5/3s, Claude 15/5/10s)
# are asserted separately, at the fixture daemon, in
# test_codex_session_end_fits_the_three_second_cap and statically in
# tests/test_plugin_packaging.py. On a loaded windows-latest runner
# process creation under Git Bash costs seconds per spawn: run 35603265565
# (2026-09-21) measured 19s wall-clock for session-end.sh, which spawns about
# seven processes around a two-second curl, and session-start.sh, which
# spawns roughly four times as many (stdin parse, manifest read, the
# four-file hooks digest), then hit the previous 30s guard.
HOOK_PROCESS_TIMEOUT = 180


def _fake_shim_bin(tmp_path: Path) -> Path:
    shim_bin = tmp_path / "shim-bin"
    shim_bin.mkdir()
    for name in ("pseudolife-mcp", "pseudolife-mcp.exe"):
        executable = shim_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    return shim_bin


def connection_payload(url, token_file):
    encode = lambda value: base64.b64encode(str(value).encode()).decode()
    return {"version": 1, "daemon_url": encode(url), "token_file": encode(token_file)}


def write_connection(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")
    if os.name == "nt":
        from pseudolife_memory.credentials import _secure_windows_file
        _secure_windows_file(path)
    else:
        path.chmod(0o600)


def isolated_env(codex_home):
    env = {key: value for key, value in os.environ.items()
           if key not in ("PSEUDOLIFE_MCP_TOKEN_FILE", "PSEUDOLIFE_MCP_TOKEN",
                           "PSEUDOLIFE_MCP_DAEMON_URL", "PSEUDOLIFE_CODEX_HOOK",
                           "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "CODEX_HOME")}
    env["CODEX_HOME"] = Path(codex_home).as_posix()
    return env


def pwsh_run(*args, input=None, env=None, raw=False):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    return subprocess.run([pwsh, "-NoProfile", *map(str, args)], input=input,
                          env=env, capture_output=True, text=not raw,
                          timeout=HOOK_PROCESS_TIMEOUT, check=True)


def bash_run(script, *, input, env):
    bash = shutil.which("bash")
    if os.name == "nt":
        # Git for Windows puts git.exe under cmd/, bin/ or mingw64/bin/
        # depending on which directory PATH lists first; bash.exe is always
        # <install>/bin/bash.exe, one or two levels up from it.
        git = shutil.which("git")
        candidates = ([Path(git).parents[1] / "bin/bash.exe",
                       Path(git).parents[2] / "bin/bash.exe"] if git else [])
        bash = next((str(c) for c in candidates if c.is_file()), None)
    if not bash:
        pytest.skip("Bash is not installed")
    return subprocess.run([bash, str(script)], input=input, env=env,
                          capture_output=True, text=True,
                          timeout=HOOK_PROCESS_TIMEOUT, check=True)


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


def test_plugin_native_windows_prompt_context(tmp_path):
    manifest = json.loads((ROOT / "plugin/hooks/hooks.json").read_text())
    hook = manifest["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    command = hook["commandWindows"]
    env = isolated_env(tmp_path / "codex-home")
    env["CLAUDE_PLUGIN_ROOT"] = str(ROOT / "plugin")
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
def test_native_hook_forwards_session_identity_to_fixture_daemon(
        tmp_path, event, codepage, first_status):
    requests = []
    briefing = "Fixture memory \u2192 briefing \u2026"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, hmac.compare_digest(
                self.headers.get("Authorization") or "", "Bearer fixture-token")))
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
        env = isolated_env(tmp_path / "codex-home")
        env.update({"PSEUDOLIFE_MCP_DAEMON_URL":
               f"http://127.0.0.1:{server.server_port}",
               "PSEUDOLIFE_MCP_TOKEN": "fixture-token",
               "CODEX_HOME": str(tmp_path / "codex-home")})
        # Pin a child-local OEM console as well as UTF-8: the parent test
        # runner's encoding must not hide corruption of redirected JSON bytes.
        command = (f"[Console]::OutputEncoding = [Text.Encoding]::GetEncoding({codepage}); "
                   f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event {event}")
        result = pwsh_run("-Command", command, raw=True,
                          input=json.dumps({"session_id": "fixture & session", "source": "startup"}).encode(), env=env)
        assert len(requests) == (2 if first_status in (503, "repeated503", "timeout") else 1)
        if event == "SessionStart":
            path, auth_matches = requests[0]
            assert parse_qs(urlsplit(path).query)["session_id"] == ["fixture & session"]
            assert auth_matches
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


def test_native_hook_rereads_rotated_token_file_each_invocation(tmp_path):
    from pseudolife_memory.credentials import _write_token_file

    requests = []
    expected = {"token": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(hmac.compare_digest(
                self.headers.get("Authorization") or "",
                "Bearer " + expected["token"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    token_file = tmp_path / "token"
    try:
        env = isolated_env(tmp_path / "codex-home")
        env.update({"PSEUDOLIFE_MCP_DAEMON_URL":
               f"http://127.0.0.1:{server.server_port}",
               "PSEUDOLIFE_MCP_TOKEN_FILE": str(token_file),
               "PSEUDOLIFE_MCP_TOKEN": "stale-token"})
        command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
        for token in ("first-token", "second-token"):
            expected["token"] = token
            _write_token_file(token_file, token)
            pwsh_run("-Command", command, input='{"session_id":"fixture","source":"startup"}',
                     env=env)
        assert len(requests) == 2 and all(requests)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_native_hook_uses_managed_connection_and_rejects_url_conflict(tmp_path):
    from pseudolife_memory.credentials import _write_token_file

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(hmac.compare_digest(
                self.headers.get("Authorization") or "", "Bearer managed-token"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "codex"
    token_file = home / "pseudolife" / "token"
    _write_token_file(token_file, "managed-token")
    connection = token_file.with_name("connection.json")
    write_connection(connection, connection_payload(
        f"http://127.0.0.1:{server.server_port}", token_file))
    command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
    try:
        env = isolated_env(home)
        result = pwsh_run("-Command", command, input='{"session_id":"fixture","source":"startup"}',
                          env=env)
        assert len(requests) == 1 and requests[0]
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "fixture"
        env["PSEUDOLIFE_MCP_DAEMON_URL"] = "http://127.0.0.1:1"
        result = pwsh_run("-Command", command, input='{"session_id":"fixture","source":"startup"}',
                          env=env)
        assert len(requests) == 1 and requests[0]
        assert "briefing unavailable" in json.loads(
            result.stdout)["hookSpecificOutput"]["additionalContext"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_native_hook_requires_matching_url_for_explicit_credential_override(tmp_path):
    from pseudolife_memory.credentials import _write_token_file

    requests = []
    expected = {"token": ""}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(hmac.compare_digest(
                self.headers.get("Authorization") or "",
                "Bearer " + expected["token"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "codex"
    managed = home / "pseudolife" / "managed-token"
    override = home / "pseudolife" / "override-token"
    _write_token_file(managed, "managed-fixture")
    _write_token_file(override, "override-fixture")
    url = f"http://127.0.0.1:{server.server_port}"
    write_connection(managed.with_name("connection.json"),
                     connection_payload(url, managed))
    env = isolated_env(home)
    env["PSEUDOLIFE_MCP_TOKEN_FILE"] = str(override)
    command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
    try:
        result = pwsh_run("-Command", command,
                          input='{"session_id":"fixture","source":"startup"}', env=env)
        assert requests == []
        assert "briefing unavailable" in json.loads(
            result.stdout)["hookSpecificOutput"]["additionalContext"]
        env["PSEUDOLIFE_MCP_DAEMON_URL"] = url
        expected["token"] = "override-fixture"
        pwsh_run("-Command", command,
                 input='{"session_id":"fixture","source":"startup"}', env=env)
        assert requests == [True]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_native_hook_checks_managed_url_before_reading_credential():
    script = (ROOT / "plugin/hooks/lifecycle.ps1").read_text(encoding="utf-8")
    conflict = script.index("explicit daemon URL conflicts")
    credential_read = script.index("Get-PseudolifeToken $tokenFile")
    assert conflict < credential_read


@pytest.mark.parametrize("managed_url", [
    "file:///tmp/bank",
    "http://user:password@127.0.0.1:8765",
    "http://127.0.0.1:8765/path?query=1",
    "http://127.0.0.1:8765/path#fragment",
    "http://127.0.0.1:8765/base",
])
def test_native_hook_rejects_unsafe_managed_urls(tmp_path, managed_url):
    from pseudolife_memory.credentials import _write_token_file

    home = tmp_path / "codex"
    token_file = home / "pseudolife" / "token"
    _write_token_file(token_file, "fixture-token")
    write_connection(token_file.with_name("connection.json"),
                     connection_payload(managed_url, token_file))
    command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
    result = pwsh_run("-Command", command, input='{"session_id":"fixture","source":"startup"}',
                      env=isolated_env(home))
    assert "briefing unavailable" in json.loads(
        result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("record", [
    '{"version":1,"daemon_url":"aHR0cDovLzEyNy4wLjAuMTo4NzY1",'
    '"daemon_url":"aHR0cDovLzEyNy4wLjAuMTo4NzY1","token_file":"dG9rZW4="}',
    '{"version":1,"daemon_url":"%%%","token_file":"dG9rZW4="}',
])
def test_native_hook_rejects_noncanonical_managed_connection(tmp_path, record):
    home = tmp_path / "codex"
    connection = home / "pseudolife" / "connection.json"
    connection.parent.mkdir(parents=True)
    connection.write_text(record + "\n", encoding="utf-8")
    if os.name == "nt":
        from pseudolife_memory.credentials import _secure_windows_file
        _secure_windows_file(connection)
    else:
        connection.chmod(0o600)
    command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
    result = pwsh_run("-Command", command,
                      input='{"session_id":"fixture","source":"startup"}',
                      env=isolated_env(home))
    assert "briefing unavailable" in json.loads(
        result.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics require Windows")
def test_native_hook_rejects_broadened_managed_connection_acl(tmp_path):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append(True)
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "codex"
    token_file = home / "pseudolife" / "token"
    from pseudolife_memory.credentials import _write_token_file
    _write_token_file(token_file, "fixture-token")
    connection = token_file.with_name("connection.json")
    write_connection(connection, connection_payload(
        f"http://127.0.0.1:{server.server_port}", token_file))
    widened = subprocess.run(
        ["icacls", str(connection), "/grant", "*S-1-1-0:R"],
        capture_output=True, text=True, timeout=10)
    assert widened.returncode == 0
    command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
    try:
        result = pwsh_run("-Command", command,
                          input='{"session_id":"fixture","source":"startup"}',
                          env=isolated_env(home))
        assert requests == []
        assert "briefing unavailable" in json.loads(
            result.stdout)["hookSpecificOutput"]["additionalContext"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_native_hook_refuses_redirect_without_forwarding_authorization(tmp_path):
    from pseudolife_memory.credentials import _write_token_file

    target_auth = []
    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            target_auth.append(bool(self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"unexpected")
    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/briefing")
            self.end_headers()
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [Thread(target=server.serve_forever, daemon=True)
               for server in (target, redirect)]
    for worker in workers:
        worker.start()
    home = tmp_path / "codex"
    token_file = home / "pseudolife" / "token"
    _write_token_file(token_file, "fixture-token")
    write_connection(token_file.with_name("connection.json"), connection_payload(
        f"http://127.0.0.1:{redirect.server_port}", token_file))
    command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
    try:
        result = pwsh_run("-Command", command,
                          input='{"session_id":"fixture","source":"startup"}',
                          env=isolated_env(home))
        assert target_auth == []
        assert "briefing unavailable" in json.loads(
            result.stdout)["hookSpecificOutput"]["additionalContext"]
    finally:
        for server in (redirect, target):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership semantics require a POSIX host")
def test_posix_hooks_use_managed_rotatable_credential(tmp_path, event):
    matches = []
    expected = {"token": "first-token"}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def answer(self):
            matches.append(hmac.compare_digest(
                self.headers.get("Authorization") or "",
                "Bearer " + expected["token"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")
        do_GET = answer
        do_POST = answer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    home = tmp_path / "codex home \u2603"
    token_file = home / "pseudolife" / "token"
    connection = token_file.with_name("connection.json")
    env = isolated_env(home)
    env["PSEUDOLIFE_CODEX_HOOK"] = "1"
    try:
        for token in ("first-token", "second-token"):
            expected["token"] = token
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token + "\n", encoding="utf-8")
            token_file.chmod(0o600)
            connection.write_text(json.dumps(connection_payload(
                f"http://127.0.0.1:{server.server_port}", token_file.as_posix()),
                indent=2) + "\n")
            connection.chmod(0o600)
            script = ROOT / "plugin/hooks" / (
                "session-start.sh" if event == "SessionStart" else "session-end.sh")
            result = bash_run(script, input='{"session_id":"fixture","source":"startup"}',
                              env=env)
            assert "second-token" not in result.stdout + result.stderr
        assert len(matches) == 2 and all(matches)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_codex_session_end_fits_the_three_second_cap(tmp_path):
    """Codex caps SessionEnd at three seconds (ops/setup-codex-hooks.py),
    while Claude's hooks.json allows ten. The bash script's Codex branch
    must finish inside the cap: one attempt, no retry, like lifecycle.ps1.

    The live half is measured at the fixture daemon, not around the
    subprocess: each attempt is held without a reply and timed until curl
    hangs up, which is the budget curl actually applied, and the attempts
    are counted. The outer wall-clock on a loaded runner is dominated by
    process creation the script does not control — 19s for this hook on
    windows-latest (run 35603265565, 2026-09-21) around a curl that
    honoured its two-second budget — and a wrapper ``curl`` on PATH cannot
    stand in, because Git's ``bin/bash.exe`` launcher prepends
    ``/mingw64/bin:/usr/bin`` to PATH ahead of anything the test sets."""
    script = (ROOT / "plugin/hooks/session-end.sh").read_text(encoding="utf-8")
    codex = re.search(r'CODEX_HOOK_CONTEXT"\s*\];?\s*then\s*(?:#[^\n]*\n\s*)*CURL_BUDGET=\(([^)]*)\)', script)
    assert codex, "session-end.sh needs a Codex-context curl budget"
    assert "--retry" not in codex.group(1)
    max_time = int(re.search(r"--max-time\s+(\d+)", codex.group(1)).group(1))
    setup = (ROOT / "ops/setup-codex-hooks.py").read_text(encoding="utf-8")
    cap = int(re.search(r'"SessionEnd":\s*(\d+)\}\[event\]', setup).group(1))
    assert max_time + 1 <= cap
    # Live: a daemon that never answers must not hold the hook past the cap.
    attempts = []  # seconds each attempt's connection stayed open

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            # Drain the body, then hold the reply and wait for curl to hang
            # up: the socket turns readable (EOF) the moment curl gives up.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            started = time.monotonic()
            select.select([self.connection], [], [], 6)
            attempts.append(time.monotonic() - started)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    env = isolated_env(tmp_path / "codex-home")
    env["PSEUDOLIFE_CODEX_HOOK"] = "1"
    env["PSEUDOLIFE_MCP_DAEMON_URL"] = f"http://127.0.0.1:{server.server_port}"
    try:
        bash_run(ROOT / "plugin/hooks/session-end.sh", input='{"session_id":"fixture"}', env=env)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=8)
    assert len(attempts) == 1, attempts
    assert attempts[0] < cap, attempts


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
    (fixture_repo / "ops/setup-codex-coordination.py").write_text(
        "import json\nprint(json.dumps({'status':'tokenless',"
        "'credential_file_configured':False,'connection_configured':False}))\n",
        encoding="utf-8")
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
    env = isolated_env(codex_home)
    env.update({"HOME": home.as_posix(), "USERPROFILE": home.as_posix(),
                "FIXTURE_CALL": str(call)})
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
    env = isolated_env(codex_home)
    env.update({"HOME": home.as_posix(), "USERPROFILE": home.as_posix()})
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


def test_powershell_installer_recovers_when_hook_helper_launch_throws(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = isolated_env(home / "codex-home")
    env.update({"HOME": home.as_posix(), "USERPROFILE": home.as_posix()})
    result = run_installer_stages(
        tmp_path, "powershell", ROOT, env, "auto", "yes", "auto",
        launch_throws=True)
    assert "RESULT:unavailable:appended" in result
    assert "Codex setup did not return a valid result" in result


@pytest.mark.parametrize(("bootstrap_success", "shim_available", "runtime_success", "expected_state"), [
    (True, True, True, "shim-env"),
    (True, True, False, "failed"),
    (True, False, True, "failed"),
    (False, True, True, "failed"),
])
def test_bash_fresh_codex_stages_bind_file_url_and_preserve_ambient_env(
        tmp_path, bootstrap_success, shim_available, runtime_success, expected_state):
    bash = shutil.which("bash")
    if os.name == "nt":
        # Git for Windows puts git.exe under cmd/, bin/ or mingw64/bin/
        # depending on which directory PATH lists first; bash.exe is always
        # <install>/bin/bash.exe, one or two levels up from it.
        git = shutil.which("git")
        candidates = ([Path(git).parents[1] / "bin/bash.exe",
                       Path(git).parents[2] / "bin/bash.exe"] if git else [])
        bash = next((str(c) for c in candidates if c.is_file()), None)
    if not bash:
        pytest.skip("Bash is not installed")
    repo = tmp_path / "repo"
    shim_bin = _fake_shim_bin(tmp_path)
    (repo / "ops").mkdir(parents=True)
    (repo / "examples").mkdir()
    shutil.copyfile(ROOT / "examples/CLAUDE.memory.md", repo / "examples/CLAUDE.memory.md")
    credential = repo / "ops/setup-codex-coordination.py"
    credential.write_text(
        "import base64, json, os, sys\nfrom pathlib import Path\n"
        "from pseudolife_memory.credentials import _write_token_file\n"
        "args=sys.argv[1:]\n"
        "if os.environ['FIXTURE_CREDENTIAL_SUCCESS']!='1':\n"
        " print(json.dumps({'status':'needs-configuration','recovery':'credential setup failed'}))\n"
        " raise SystemExit(1)\n"
        "if args==['--runtime-defaults']:\n"
        " assert Path(os.environ['FIXTURE_CODEX_CALL']).exists()\n"
        " Path(os.environ['FIXTURE_RUNTIME']).write_text('called')\n"
        " if os.environ['FIXTURE_RUNTIME_SUCCESS']=='1':\n"
        "  print(json.dumps({'status':'ready','runtime_defaults':'configured'}))\n"
        "  raise SystemExit(0)\n"
        " print(json.dumps({'status':'needs-configuration'}))\n"
        " raise SystemExit(1)\n"
        "home=Path(os.environ['CODEX_HOME']); target=home/'pseudolife'/'token'\n"
        "token=sys.stdin.read().rstrip('\\r\\n')\n"
        "assert token=='installer-file-token'\n"
        "assert args==['--credentials','--installer-token-stdin',"
        "'--installer-daemon-url','http://127.0.0.1:9876']\n"
        "assert token not in ' '.join(sys.argv)\n"
        "assert all(key in os.environ for key in ('PSEUDOLIFE_MCP_TOKEN',"
        "'PSEUDOLIFE_MCP_TOKEN_FILE','PSEUDOLIFE_MCP_DAEMON_URL'))\n"
        "_write_token_file(target, token)\n"
        "target_s=target.as_posix()\n"
        "connection=target.with_name('connection.json'); connection.write_text(json.dumps("
        "{'version':1,"
        "'daemon_url':base64.b64encode(b'http://127.0.0.1:9876').decode(),"
        "'token_file':base64.b64encode(target_s.encode()).decode()}, indent=2))\n"
        "print(json.dumps({'status':'ready','credential_file_configured':True,'connection_configured':True,"
        "'credential_file_path':target_s,'daemon_url':'http://127.0.0.1:9876'}))\n")
    hook = repo / "ops/setup-codex-hooks.py"
    hook.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "from pseudolife_memory.credentials import CredentialProvider\n"
        "path=os.environ['PSEUDOLIFE_MCP_TOKEN_FILE']\n"
        "assert 'PSEUDOLIFE_MCP_TOKEN' not in os.environ\n"
        "assert os.environ['PSEUDOLIFE_MCP_DAEMON_URL']=='http://127.0.0.1:9876'\n"
        "Path(os.environ['FIXTURE_HOOK']).write_text(path)\n"
        "assert CredentialProvider(path=path).snapshot().token=='installer-file-token'\n"
        "Path(os.environ['FIXTURE_HOOK']).write_text('verified')\n"
        "print(json.dumps({'source':'manual','status':'ready',"
        "'instructions':'covered-by-hooks','recovery':None}))\n")
    text = (ROOT / "ops/install.sh").read_text(encoding="utf-8")
    stages = re.search(
        r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
        r"^# [^\n]*12\. health", text)[1]
    script = tmp_path / "fresh-stages.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"repo={shlex.quote(repo.as_posix())}\n"
        "CLIENTS=codex\nCODEX_HOOKS=auto\nCODEX_HOOK_TRUST=yes\n"
        "INSTRUCTIONS=auto\nCLAUDE_MD=''\nAGENTS_FILE=''\nTRANSPORT=shim\n"
        "step() { :; }\n"
        "get_env() { case \"$1\" in PSEUDOLIFE_MCP_TOKEN) printf installer-file-token ;; "
        "PSEUDOLIFE_MCP_DAEMON_URL) printf http://127.0.0.1:9876 ;; esac; }\n"
        + ("pipx() { if [ \"$1\" = environment ]; then printf '%s\\n' \"$FIXTURE_SHIM_BIN\"; fi; return 0; }\n"
           if shim_available else "pipx() { return 1; }\n")
        +
        "codex() {\n"
        "  if [ \"$1 $2\" = 'mcp get' ]; then return 1; fi\n"
        "  if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo --env; return 0; fi\n"
        "  if [ \"$1 $2\" = 'mcp add' ]; then printf '%s\\n' \"$@\" > \"$FIXTURE_CODEX_CALL\"; return 0; fi\n"
            "  return 1\n}\n"
            + stages
            + "\nrestored=0\n"
              "if [ \"$PSEUDOLIFE_MCP_TOKEN\" = ambient-static-token ] && "
              "[ \"$PSEUDOLIFE_MCP_TOKEN_FILE\" = \"$FIXTURE_AMBIENT_FILE\" ] && "
              "[ \"$PSEUDOLIFE_MCP_DAEMON_URL\" = http://127.0.0.1:4321 ]; then restored=1; fi\n"
              "printf 'RESULT:%s:%s\\n' \"$MCP_CODEX\" \"$restored\"\n",
        encoding="utf-8")
    home = tmp_path / "home"
    ambient = tmp_path / "ambient-token"
    from pseudolife_memory.credentials import CredentialProvider, _write_token_file
    _write_token_file(ambient, "ambient-file-token")
    hook_marker = tmp_path / "hook-verified"
    codex_call = tmp_path / "codex-call"
    runtime_marker = tmp_path / "runtime-called"
    env = isolated_env(home)
    env.update({
        "HOME": home.as_posix(),
        "USERPROFILE": home.as_posix(),
        "PSEUDOLIFE_MCP_TOKEN": "ambient-static-token",
        "PSEUDOLIFE_MCP_TOKEN_FILE": ambient.as_posix(),
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:4321",
        "FIXTURE_HOOK": str(hook_marker),
        "FIXTURE_CODEX_CALL": str(codex_call),
        "FIXTURE_RUNTIME": str(runtime_marker),
        "FIXTURE_RUNTIME_SUCCESS": "1" if runtime_success else "0",
        "FIXTURE_CREDENTIAL_SUCCESS": "1" if bootstrap_success else "0",
        "FIXTURE_AMBIENT_FILE": ambient.as_posix(),
        "FIXTURE_SHIM_BIN": shim_bin.as_posix(),
        "PYTHONPATH": str(ROOT),
    })
    result = subprocess.run([bash, script.as_posix()], env=env, capture_output=True,
                            timeout=HOOK_PROCESS_TIMEOUT, check=True)
    stdout = result.stdout.decode()
    assert f"RESULT:{expected_state}:1" in stdout
    assert hook_marker.exists() == bootstrap_success, (stdout, result.stderr.decode())
    if bootstrap_success:
        assert hook_marker.read_text() == "verified"
    managed = home / "pseudolife" / "token"
    if not bootstrap_success:
        assert not managed.exists()
        assert not codex_call.exists()
        assert not runtime_marker.exists()
        assert "Codex credential setup failed" in stdout
        return
    assert CredentialProvider(path=managed).snapshot().token == "installer-file-token"
    connection = json.loads(managed.with_name("connection.json").read_text())
    assert connection == connection_payload(
        "http://127.0.0.1:9876", managed.as_posix())
    if shim_available:
        assert runtime_marker.read_text() == "called"
        call = codex_call.read_text()
        assert shim_bin.joinpath("pseudolife-mcp").as_posix() in call
        assert "PSEUDOLIFE_MCP_TOKEN_FILE=" + managed.as_posix() in call
        assert "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:9876" in call
        assert all(token not in call for token in (
            "installer-file-token", "ambient-file-token", "ambient-static-token"))
        if not runtime_success:
            assert "runtime defaults were not confirmed" in result.stderr.decode()
    else:
        assert not runtime_marker.exists()
        assert not codex_call.exists()
        assert "HTTP fallback was not registered" in result.stderr.decode()


@pytest.mark.parametrize(("bootstrap_success", "runtime_success", "expected_state"), [
    (True, True, "shim-env"),
    (True, False, "failed"),
    (False, True, "failed"),
])
def test_powershell_explicit_installer_connection_isolated_and_recoverable(
        tmp_path, bootstrap_success, runtime_success, expected_state):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 unavailable")
    repo = tmp_path / "repo"
    shim_bin = _fake_shim_bin(tmp_path)
    (repo / "ops").mkdir(parents=True)
    (repo / "examples").mkdir()
    shutil.copyfile(ROOT / "examples/CLAUDE.memory.md", repo / "examples/CLAUDE.memory.md")
    (repo / "ops/setup-codex-coordination.py").write_text('''
import json, os, sys
from pathlib import Path
from pseudolife_memory.credentials import _write_token_file
args=sys.argv[1:]
if os.environ['FIXTURE_CREDENTIAL_SUCCESS']!='1':
    print(json.dumps({'status':'needs-configuration','recovery':'credential setup failed'}))
    raise SystemExit(1)
if args==['--runtime-defaults']:
    assert Path(os.environ['FIXTURE_CODEX_CALL']).exists()
    Path(os.environ['FIXTURE_RUNTIME']).write_text('called')
    if os.environ['FIXTURE_RUNTIME_SUCCESS']=='1':
        print(json.dumps({'status':'ready','runtime_defaults':'configured'}))
        raise SystemExit(0)
    print(json.dumps({'status':'needs-configuration'}))
    raise SystemExit(1)
token=sys.stdin.read().rstrip('\\r\\n')
checks={
 'args':args==['--credentials','--installer-token-stdin','--installer-daemon-url','http://127.0.0.1:9876'],
 'stdin':token=='installer-file-token',
 'argv':token not in ' '.join(sys.argv),
 'ambient':all(key in os.environ for key in (
   'PSEUDOLIFE_MCP_TOKEN','PSEUDOLIFE_MCP_TOKEN_FILE','PSEUDOLIFE_MCP_DAEMON_URL'))}
assert all(checks.values())
Path(os.environ['FIXTURE_CREDENTIAL']).write_text(json.dumps(checks))
target=Path(os.environ['CODEX_HOME'])/'pseudolife'/'token'
_write_token_file(target, token)
print(json.dumps({'status':'ready','credential_file_configured':True,'connection_configured':True,
 'credential_file_path':str(target),'daemon_url':'http://127.0.0.1:9876'}))
''')
    (repo / "ops/setup-codex-hooks.py").write_text('''
import json, os
from pathlib import Path
from pseudolife_memory.credentials import CredentialProvider
path=Path(os.environ['PSEUDOLIFE_MCP_TOKEN_FILE'])
checks={
 'same_file':path==Path(os.environ['CODEX_HOME'])/'pseudolife'/'token',
 'same_url':os.environ.get('PSEUDOLIFE_MCP_DAEMON_URL')=='http://127.0.0.1:9876',
 'no_literal':'PSEUDOLIFE_MCP_TOKEN' not in os.environ,
 'valid_file':CredentialProvider(path=path).snapshot().token=='installer-file-token'}
assert all(checks.values())
Path(os.environ['FIXTURE_HOOK']).write_text(json.dumps(checks))
print(json.dumps({'source':'manual','status':'ready',
 'instructions':'covered-by-hooks','recovery':None}))
''')
    source = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    stages = re.search(
        r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)^# -- 12\. health",
        source)[1]
    home = tmp_path / "home"
    ambient = tmp_path / "ambient-token"
    from pseudolife_memory.credentials import _write_token_file
    _write_token_file(ambient, "ambient-file-token")
    credential_marker = tmp_path / "credential.json"
    hook_marker = tmp_path / "hook.json"
    runtime_marker = tmp_path / "runtime.txt"
    codex_call = tmp_path / "codex-call.json"
    result_path = tmp_path / "result.json"
    driver = tmp_path / "driver.ps1"
    driver.write_text(f'''
$ErrorActionPreference='Stop'
$repo='{repo.as_posix()}'
$clients=@('codex'); $Transport='shim'; $CodexHooks='auto'; $CodexHookTrust='yes'
$Instructions='auto'; $interactive=$false
function Step($text) {{ }}
function Get-EnvValue($name) {{
    if ($name -eq 'PSEUDOLIFE_MCP_TOKEN') {{ return 'installer-file-token' }}
    if ($name -eq 'PSEUDOLIFE_MCP_DAEMON_URL') {{ return 'http://127.0.0.1:9876' }}
    return $null
}}
function Read-Host {{ throw 'unexpected prompt' }}
function python {{ & '{Path(sys.executable).as_posix()}' @args }}
function pipx {{
    $global:LASTEXITCODE=0
    if ($args[0] -eq 'environment') {{ return $env:FIXTURE_SHIM_BIN }}
}}
function codex {{
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'get') {{ $global:LASTEXITCODE=1; return }}
    if ($args -contains '--help') {{ $global:LASTEXITCODE=0; return '--env' }}
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'add') {{
        ConvertTo-Json -InputObject @($args) | Set-Content '{codex_call.as_posix()}'
        $global:LASTEXITCODE=0; return
    }}
    $global:LASTEXITCODE=1
}}
{stages}
@{{ state=$mcpState['codex'];
 restored=($env:PSEUDOLIFE_MCP_TOKEN -eq 'ambient-static-token' -and
 $env:PSEUDOLIFE_MCP_TOKEN_FILE -eq '{ambient.as_posix()}' -and
 $env:PSEUDOLIFE_MCP_DAEMON_URL -eq 'http://127.0.0.1:4321')
}} | ConvertTo-Json | Set-Content '{result_path.as_posix()}'
''', encoding="utf-8")
    env = isolated_env(home)
    env.update({
        "HOME": home.as_posix(), "USERPROFILE": home.as_posix(),
        "PYTHONPATH": str(ROOT), "FIXTURE_CREDENTIAL": str(credential_marker),
        "FIXTURE_HOOK": str(hook_marker), "FIXTURE_RUNTIME": str(runtime_marker),
        "FIXTURE_RUNTIME_SUCCESS": "1" if runtime_success else "0",
        "FIXTURE_CREDENTIAL_SUCCESS": "1" if bootstrap_success else "0",
        "FIXTURE_CODEX_CALL": str(codex_call),
        "FIXTURE_SHIM_BIN": shim_bin.as_posix(),
        "PSEUDOLIFE_MCP_TOKEN": "ambient-static-token",
        "PSEUDOLIFE_MCP_TOKEN_FILE": ambient.as_posix(),
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:4321",
    })
    completed = subprocess.run([pwsh, "-NoProfile", "-File", str(driver)],
                               env=env, capture_output=True, timeout=HOOK_PROCESS_TIMEOUT)
    assert completed.returncode == 0
    assert json.loads(result_path.read_text(encoding="utf-8-sig")) == {
        "state": expected_state, "restored": True}
    if not bootstrap_success:
        assert not credential_marker.exists()
        assert not hook_marker.exists()
        assert not runtime_marker.exists()
        assert not codex_call.exists()
        assert "Codex credential setup failed" in completed.stdout.decode()
        return
    assert all(json.loads(credential_marker.read_text()).values())
    assert all(json.loads(hook_marker.read_text()).values())
    assert runtime_marker.read_text() == "called"
    arguments = json.loads(codex_call.read_text(encoding="utf-8-sig"))
    assert str(shim_bin / "pseudolife-mcp.exe") in arguments
    assert not any(token in value for value in arguments for token in (
        "installer-file-token", "ambient-file-token", "ambient-static-token"))
    if not runtime_success:
        assert "runtime defaults were not confirmed" in completed.stdout.decode()


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("env_supported", [True, False])
def test_fresh_tokenless_install_ignores_unforwarded_ambient_connection(
        tmp_path, shell, env_supported):
    if shell == "bash":
        executable = shutil.which("bash")
        if os.name == "nt":
            git = shutil.which("git")
            candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
            executable = str(candidate) if candidate and candidate.is_file() else None
    else:
        executable = shutil.which("pwsh")
    if not executable:
        pytest.skip(f"{shell} is not installed")

    repo = tmp_path / "repo"
    shim_bin = _fake_shim_bin(tmp_path)
    (repo / "ops").mkdir(parents=True)
    (repo / "examples").mkdir()
    shutil.copyfile(ROOT / "examples/CLAUDE.memory.md", repo / "examples/CLAUDE.memory.md")
    credential_marker = tmp_path / "credential.json"
    runtime_marker = tmp_path / "runtime.txt"
    codex_call = tmp_path / "codex-call.json"
    result_marker = tmp_path / "result.txt"
    coordinator = repo / "ops/setup-codex-coordination.py"
    coordinator.write_text('''
import importlib.util, json, os, sys
from pathlib import Path
if sys.argv[1:]==['--runtime-defaults']:
    Path(os.environ['FIXTURE_RUNTIME']).write_text('called')
    print(json.dumps({'status':'ready','runtime_defaults':'configured'}))
    raise SystemExit(0)
args=sys.argv[1:]
assert args==['--credentials'] or (len(args)==3 and args[:2]==[
    '--credentials','--installer-daemon-url'])
spec=importlib.util.spec_from_file_location('fixture_hooks', os.environ['FIXTURE_HOOK_HELPER'])
hooks=importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
class Client:
    def rpc(self, method, params):
        assert method=='config/read'
        return {'config':{'mcp_servers':{}},'layers':[]}
home=Path(os.environ['CODEX_HOME'])
installer_connection=(args[2], None) if len(args)==3 else None
result=hooks.configure_credential_file(
    Client(), home, home, installer_connection=installer_connection)
checks={
 'credential_file_configured':result['credential_file_configured'],
 'connection_created':(home/'pseudolife'/'connection.json').exists(),
 'selected_ambient_file':result.get('credential_file_path')==os.environ['FIXTURE_AMBIENT_FILE'],
 'selected_ambient_url':result.get('daemon_url')==os.environ['FIXTURE_AMBIENT_URL'],
 'selected_installer_url':result.get('daemon_url')==os.environ['FIXTURE_INSTALLER_URL']}
Path(os.environ['FIXTURE_CREDENTIAL']).write_text(json.dumps(checks))
result['status']='ready' if result['credential_file_configured'] else 'tokenless'
print(json.dumps(result))
''', encoding="utf-8")
    (repo / "ops/setup-codex-hooks.py").write_text(
        "import json\nprint(json.dumps({'source':'manual','status':'ready',"
        "'instructions':'covered-by-hooks','recovery':None}))\n",
        encoding="utf-8")

    source = (ROOT / ("ops/install.sh" if shell == "bash" else
                      "ops/install.ps1")).read_text(encoding="utf-8")
    if shell == "bash":
        stages = re.search(
            r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
            r"^# [^\n]*12\. health", source)[1]
        driver = tmp_path / "driver.sh"
        driver.write_text(
            "set -euo pipefail\n"
            f"repo={shlex.quote(repo.as_posix())}\n"
            "CLIENTS=codex\nCODEX_HOOKS=auto\nCODEX_HOOK_TRUST=yes\n"
            "INSTRUCTIONS=auto\nCLAUDE_MD=''\nAGENTS_FILE=''\nTRANSPORT=shim\n"
            "step() { :; }\n"
            "get_env() { if [ \"$1\" = PSEUDOLIFE_MCP_DAEMON_URL ]; then printf '%s' \"$FIXTURE_INSTALLER_URL\"; fi; }\n"
            "pipx() { if [ \"$1\" = environment ]; then printf '%s\\n' \"$FIXTURE_SHIM_BIN\"; fi; return 0; }\n"
            "codex() {\n"
            " if [ \"$1 $2\" = 'mcp get' ]; then return 1; fi\n"
            " if [ \"$1 $2 $3\" = 'mcp add --help' ]; then [ \"$FIXTURE_ENV_SUPPORTED\" = 1 ] && echo --env; return 0; fi\n"
            " if [ \"$1 $2\" = 'mcp add' ]; then printf '%s\\n' \"$@\" > \"$FIXTURE_CODEX_CALL\"; return 0; fi\n"
            " return 1\n}\n" + stages +
            "\nprintf '%s' \"$MCP_CODEX\" > \"$FIXTURE_RESULT\"\n",
            encoding="utf-8")
        command = [executable, driver.as_posix()]
    else:
        stages = re.search(
            r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)^# -- 12\. health",
            source)[1]
        driver = tmp_path / "driver.ps1"
        driver.write_text(f'''
$ErrorActionPreference='Stop'
$repo='{repo.as_posix()}'
$clients=@('codex'); $Transport='shim'; $CodexHooks='auto'; $CodexHookTrust='yes'
$Instructions='auto'; $interactive=$false
function Step($text) {{ }}
function Get-EnvValue($name) {{
    if ($name -eq 'PSEUDOLIFE_MCP_DAEMON_URL') {{ return $env:FIXTURE_INSTALLER_URL }}
    return $null
}}
function Read-Host {{ throw 'unexpected prompt' }}
function python {{ & '{Path(sys.executable).as_posix()}' @args }}
function pipx {{
    $global:LASTEXITCODE=0
    if ($args[0] -eq 'environment') {{ return $env:FIXTURE_SHIM_BIN }}
}}
function codex {{
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'get') {{ $global:LASTEXITCODE=1; return }}
    if ($args -contains '--help') {{
        $global:LASTEXITCODE=0
        if ($env:FIXTURE_ENV_SUPPORTED -eq '1') {{ return '--env' }}
        return
    }}
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'add') {{
        ConvertTo-Json -InputObject @($args) | Set-Content $env:FIXTURE_CODEX_CALL
        $global:LASTEXITCODE=0; return
    }}
    $global:LASTEXITCODE=1
}}
{stages}
$mcpState['codex'] | Set-Content $env:FIXTURE_RESULT
''', encoding="utf-8")
        command = [executable, "-NoProfile", "-File", str(driver)]

    home = tmp_path / "home"
    ambient = tmp_path / "ambient-token"
    from pseudolife_memory.credentials import _write_token_file
    _write_token_file(ambient, "ambient-file-token")
    intended_requests = []
    wrong_requests = []

    def handler(requests):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self):
                requests.append({"authorization_present": bool(
                    self.headers.get("Authorization"))})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fixture context")

            do_GET = answer
            do_POST = answer
        return Handler

    intended = ThreadingHTTPServer(("127.0.0.1", 0), handler(intended_requests))
    wrong = ThreadingHTTPServer(("127.0.0.1", 0), handler(wrong_requests))
    workers = [Thread(target=server.serve_forever, daemon=True)
               for server in (intended, wrong)]
    for worker in workers:
        worker.start()
    intended_url = f"http://127.0.0.1:{intended.server_port}"
    wrong_url = f"http://127.0.0.1:{wrong.server_port}"
    env = isolated_env(home)
    env.update({
        "HOME": home.as_posix(), "USERPROFILE": home.as_posix(),
        "PYTHONPATH": str(ROOT), "FIXTURE_HOOK_HELPER": str(ROOT / "ops/setup-codex-hooks.py"),
        "FIXTURE_CREDENTIAL": str(credential_marker), "FIXTURE_RUNTIME": str(runtime_marker),
        "FIXTURE_CODEX_CALL": str(codex_call), "FIXTURE_RESULT": str(result_marker),
        "FIXTURE_INSTALLER_URL": intended_url,
        "FIXTURE_AMBIENT_FILE": ambient.as_posix(),
        "FIXTURE_AMBIENT_URL": wrong_url,
        "FIXTURE_ENV_SUPPORTED": "1" if env_supported else "0",
        "FIXTURE_SHIM_BIN": shim_bin.as_posix(),
        "PSEUDOLIFE_MCP_TOKEN": "ambient-static-token",
        "PSEUDOLIFE_MCP_TOKEN_FILE": ambient.as_posix(),
        "PSEUDOLIFE_MCP_DAEMON_URL": wrong_url,
    })
    try:
        completed = subprocess.run(command, env=env, capture_output=True, timeout=HOOK_PROCESS_TIMEOUT)
        assert completed.returncode == 0, (completed.stdout.decode(), completed.stderr.decode())
        assert result_marker.read_text(encoding="utf-8-sig").strip() == (
            "shim-env" if env_supported else "failed"), (
                completed.stdout.decode(), completed.stderr.decode())
        assert credential_marker.exists(), (
            completed.stdout.decode(), completed.stderr.decode())
        checks = json.loads(credential_marker.read_text())
        assert checks == {
            "credential_file_configured": False,
            "connection_created": True,
            "selected_ambient_file": False,
            "selected_ambient_url": False,
            "selected_installer_url": True,
        }
        if env_supported:
            assert runtime_marker.read_text() == "called"
            arguments = codex_call.read_text(encoding="utf-8-sig")
            expected_shim = shim_bin / (
                "pseudolife-mcp" if shell == "bash" else "pseudolife-mcp.exe")
            if shell == "bash":
                assert expected_shim.as_posix() in arguments.splitlines()
            else:
                assert str(expected_shim) in json.loads(arguments)
            assert ambient.as_posix() not in arguments
            assert wrong_url not in arguments
            assert "PSEUDOLIFE_MCP_DAEMON_URL=" + intended_url in arguments
        else:
            assert not runtime_marker.exists()
            assert not codex_call.exists()

        # Git Bash reports Windows DACL-private files as mode 0644. Codex uses
        # the native PowerShell hook on Windows; exercise the Bash hook on POSIX.
        if shell == "bash" and os.name == "nt":
            return

        payload = b'{"session_id":"fixture","source":"startup"}'
        if shell == "bash":
            hook_command = [executable, str(ROOT / "plugin/hooks/session-start.sh")]
            env["PSEUDOLIFE_CODEX_HOOK"] = "1"
        else:
            hook_command = [executable, "-NoProfile", "-File",
                            str(ROOT / "plugin/hooks/lifecycle.ps1"),
                            "-Event", "SessionStart"]
        hook = subprocess.run(hook_command, input=payload, env=env,
                              capture_output=True, timeout=HOOK_PROCESS_TIMEOUT)
        assert hook.returncode == 0
        assert intended_requests == [{"authorization_present": False}]
        assert wrong_requests == []
    finally:
        for server in (intended, wrong):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("installer_token", [False, True])
def test_installer_preserves_existing_forwarded_user_credential(
        tmp_path, shell, installer_token):
    if shell == "bash":
        executable = shutil.which("bash")
        if os.name == "nt":
            git = shutil.which("git")
            candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
            executable = str(candidate) if candidate and candidate.is_file() else None
    else:
        executable = shutil.which("pwsh")
    if not executable:
        pytest.skip(f"{shell} is not installed")

    repo = tmp_path / "repo"
    shim_bin = _fake_shim_bin(tmp_path)
    (repo / "ops").mkdir(parents=True)
    (repo / "examples").mkdir()
    shutil.copyfile(ROOT / "examples/CLAUDE.memory.md", repo / "examples/CLAUDE.memory.md")
    (tmp_path / "home").mkdir()
    (tmp_path / "home/config.toml").write_text("# fixture\n")
    marker = tmp_path / "credential.json"
    codex_call = tmp_path / "codex-call.json"
    result_marker = tmp_path / "result.txt"
    (repo / "ops/setup-codex-coordination.py").write_text('''
import importlib.util, json, os, sys
from pathlib import Path
if '--runtime-defaults' in sys.argv:
    print(json.dumps({'status':'ready','runtime_defaults':'configured'}))
    raise SystemExit(0)
args=sys.argv[1:]
token=sys.stdin.read().strip() if '--installer-token-stdin' in args else None
url=args[args.index('--installer-daemon-url')+1]
spec=importlib.util.spec_from_file_location('fixture_hooks', os.environ['FIXTURE_HOOK_HELPER'])
hooks=importlib.util.module_from_spec(spec); spec.loader.exec_module(hooks)
home=Path(os.environ['CODEX_HOME'])
server={'command':'pseudolife-mcp','env':{},'env_vars':[
    'PSEUDOLIFE_MCP_TOKEN_FILE','PSEUDOLIFE_MCP_DAEMON_URL']}
config={'config':{'mcp_servers':{'pseudolife-memory':server}},'layers':[
    {'name':{'type':'user','file':str(home/'config.toml')},'version':'fixture-v1',
     'config':{'mcp_servers':{'pseudolife-memory':json.loads(json.dumps(server))}}}]}
class Client:
    def rpc(self, method, params):
        if method=='config/read': return config
        if method=='config/batchWrite':
            for edit in params['edits']:
                if edit['keyPath'].endswith('."env"'):
                    for cfg in (config['config'], config['layers'][0]['config']):
                        cfg['mcp_servers']['pseudolife-memory']['env']=edit['value']
            return {}
        raise AssertionError(method)
result=hooks.configure_credential_file(
    Client(), home, home, installer_connection=(url, token))
effective=config['config']['mcp_servers']['pseudolife-memory']
Path(os.environ['FIXTURE_CREDENTIAL']).write_text(json.dumps({
    'selected_forwarded_file':Path(result['credential_file_path']).resolve()==
        Path(os.environ['FIXTURE_FORWARDED_FILE']).resolve(),
    'selected_forwarded_url':result['daemon_url']==os.environ['FIXTURE_FORWARDED_URL'],
    'preserved_env_vars':effective['env_vars']==[
        'PSEUDOLIFE_MCP_TOKEN_FILE','PSEUDOLIFE_MCP_DAEMON_URL']}))
result['status']='ready'
print(json.dumps(result))
''', encoding="utf-8")
    (repo / "ops/setup-codex-hooks.py").write_text(
        "import json\nprint(json.dumps({'source':'manual','status':'ready',"
        "'instructions':'covered-by-hooks','recovery':None}))\n",
        encoding="utf-8")

    source = (ROOT / ("ops/install.sh" if shell == "bash" else
                      "ops/install.ps1")).read_text(encoding="utf-8")
    if shell == "bash":
        stages = re.search(
            r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
            r"^# [^\n]*12\. health", source)[1]
        driver = tmp_path / "driver.sh"
        driver.write_text(
            "set -euo pipefail\n"
            f"repo={shlex.quote(repo.as_posix())}\n"
            "CLIENTS=codex\nCODEX_HOOKS=auto\nCODEX_HOOK_TRUST=yes\n"
            "INSTRUCTIONS=auto\nCLAUDE_MD=''\nAGENTS_FILE=''\nTRANSPORT=shim\n"
            "step() { :; }\n"
            "get_env() {\n"
            " [ \"$1\" = PSEUDOLIFE_MCP_DAEMON_URL ] && printf '%s' \"$FIXTURE_INSTALLER_URL\"\n"
            " [ \"$1\" = PSEUDOLIFE_MCP_TOKEN ] && printf '%s' \"$FIXTURE_INSTALLER_TOKEN\"\n"
            " return 0\n}\n"
            "pipx() { [ \"$1\" = environment ] && printf '%s\\n' \"$FIXTURE_SHIM_BIN\"; return 0; }\n"
            "codex() {\n"
            " if [ \"$1 $2\" = 'mcp get' ]; then return 1; fi\n"
            " if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo --env; return 0; fi\n"
            " if [ \"$1 $2\" = 'mcp add' ]; then printf '%s\\n' \"$@\" > \"$FIXTURE_CODEX_CALL\"; return 0; fi\n"
            " return 1\n}\n" + stages +
            "\nprintf '%s' \"$MCP_CODEX\" > \"$FIXTURE_RESULT\"\n",
            encoding="utf-8")
        command = [executable, driver.as_posix()]
    else:
        stages = re.search(
            r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)^# -- 12\. health",
            source)[1]
        driver = tmp_path / "driver.ps1"
        driver.write_text(f'''
$ErrorActionPreference='Stop'
$repo='{repo.as_posix()}'
$clients=@('codex'); $Transport='shim'; $CodexHooks='auto'; $CodexHookTrust='yes'
$Instructions='auto'; $interactive=$false
function Step($text) {{ }}
function Get-EnvValue($name) {{
    if ($name -eq 'PSEUDOLIFE_MCP_DAEMON_URL') {{ return $env:FIXTURE_INSTALLER_URL }}
    if ($name -eq 'PSEUDOLIFE_MCP_TOKEN') {{ return $env:FIXTURE_INSTALLER_TOKEN }}
    return $null
}}
function Read-Host {{ throw 'unexpected prompt' }}
function python {{ & '{Path(sys.executable).as_posix()}' @args }}
function pipx {{
    $global:LASTEXITCODE=0
    if ($args[0] -eq 'environment') {{ return $env:FIXTURE_SHIM_BIN }}
}}
function codex {{
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'get') {{ $global:LASTEXITCODE=1; return }}
    if ($args -contains '--help') {{ $global:LASTEXITCODE=0; return '--env' }}
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'add') {{
        ConvertTo-Json -InputObject @($args) | Set-Content $env:FIXTURE_CODEX_CALL
        $global:LASTEXITCODE=0; return
    }}
    $global:LASTEXITCODE=1
}}
{stages}
$mcpState['codex'] | Set-Content $env:FIXTURE_RESULT
''', encoding="utf-8")
        command = [executable, "-NoProfile", "-File", str(driver)]

    home = tmp_path / "home"
    forwarded = tmp_path / "forwarded-token"
    from pseudolife_memory.credentials import _write_token_file
    _write_token_file(forwarded, "forwarded-fixture")
    env = isolated_env(home)
    env.update({
        "HOME": home.as_posix(), "USERPROFILE": home.as_posix(),
        "PYTHONPATH": str(ROOT), "FIXTURE_HOOK_HELPER": str(ROOT / "ops/setup-codex-hooks.py"),
        "FIXTURE_CREDENTIAL": str(marker), "FIXTURE_CODEX_CALL": str(codex_call),
        "FIXTURE_RESULT": str(result_marker), "FIXTURE_FORWARDED_FILE": forwarded.as_posix(),
        "FIXTURE_FORWARDED_URL": "http://127.0.0.1:9876",
        "FIXTURE_INSTALLER_URL": "http://127.0.0.1:4321",
        "FIXTURE_INSTALLER_TOKEN": "installer-fixture" if installer_token else "",
        "FIXTURE_SHIM_BIN": shim_bin.as_posix(),
        "PSEUDOLIFE_MCP_TOKEN": "unforwarded-literal",
        "PSEUDOLIFE_MCP_TOKEN_FILE": forwarded.as_posix(),
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:9876",
    })
    completed = subprocess.run(command, env=env, capture_output=True, timeout=HOOK_PROCESS_TIMEOUT)
    assert completed.returncode == 0, (completed.stdout.decode(), completed.stderr.decode())
    assert result_marker.read_text(encoding="utf-8-sig").strip() == "shim-env"
    assert json.loads(marker.read_text()) == {
        "selected_forwarded_file": True,
        "selected_forwarded_url": True,
        "preserved_env_vars": True,
    }
    arguments = codex_call.read_text(encoding="utf-8-sig")
    expected_shim = shim_bin / (
        "pseudolife-mcp" if shell == "bash" else "pseudolife-mcp.exe")
    if shell == "bash":
        assert expected_shim.as_posix() in arguments.splitlines()
    else:
        assert str(expected_shim) in json.loads(arguments)
    assert "PSEUDOLIFE_MCP_TOKEN_FILE=" in arguments
    assert forwarded.name in arguments
    assert "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:9876" in arguments
    assert "http://127.0.0.1:4321" not in arguments
    assert "installer-fixture" not in arguments
    assert "unforwarded-literal" not in arguments


@pytest.mark.parametrize("codex_context", ["plugin", "claude"])
@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
@pytest.mark.skipif(os.name == "nt", reason="POSIX hook permissions require a POSIX host")
def test_bash_tokenless_connection_is_scoped_to_codex_context(
        tmp_path, codex_context, event):
    managed_requests = []
    ambient_requests = []

    def handler(requests):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append({"authorization_present": bool(
                    self.headers.get("Authorization"))})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fixture context")

            do_POST = do_GET
        return Handler

    managed = ThreadingHTTPServer(("127.0.0.1", 0), handler(managed_requests))
    ambient = ThreadingHTTPServer(("127.0.0.1", 0), handler(ambient_requests))
    workers = [Thread(target=server.serve_forever, daemon=True)
               for server in (managed, ambient)]
    for worker in workers:
        worker.start()
    home = tmp_path / "home"
    token_file = tmp_path / "ambient-token"
    token_file.write_text("ambient-fixture")
    token_file.chmod(0o600)
    connection = home / "pseudolife" / "connection.json"
    connection.parent.mkdir(parents=True)
    write_connection(connection, connection_payload(
        f"http://127.0.0.1:{managed.server_port}", ""))
    env = isolated_env(home)
    env.update({
        "HOME": home.as_posix(),
        "PSEUDOLIFE_MCP_TOKEN_FILE": token_file.as_posix(),
        "PSEUDOLIFE_MCP_DAEMON_URL": f"http://127.0.0.1:{ambient.server_port}",
        "CLAUDE_PLUGIN_ROOT": "/fixture/plugin",
    })
    if codex_context == "plugin":
        env["PLUGIN_ROOT"] = env["CLAUDE_PLUGIN_ROOT"]
    try:
        result = subprocess.run(
            [shutil.which("bash"), str(ROOT / "plugin/hooks" / (
                "session-start.sh" if event == "SessionStart" else "session-end.sh"))],
            input=b'{"session_id":"fixture","source":"startup"}', env=env,
            capture_output=True, timeout=HOOK_PROCESS_TIMEOUT)
        assert result.returncode == 0
        if codex_context == "plugin":
            assert managed_requests == [{"authorization_present": False}]
            assert ambient_requests == []
        else:
            assert managed_requests == []
            assert ambient_requests == [{"authorization_present": True}]
    finally:
        for server in (managed, ambient):
            server.shutdown()
            server.server_close()
        for worker in workers:
            worker.join(timeout=2)


def run_installer_stages(tmp_path, shell, repo, env, source, trust, instructions,
                         missing_python=False, launch_throws=False):
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
            "function Get-EnvValue($name) { return $null }\n"
            "function Read-Host($text) { throw 'Unexpected second prompt' }\n"
            + ("function Get-Command($Name) { return $null }\n" if missing_python else "")
            + ("function Get-Command($Name) { if ($Name -eq 'python') { return 'python' }; return $null }\n"
               "function python { $global:LASTEXITCODE = 0; Write-Output 'fixture-python' }\n"
               "function fixture-python {\n"
               "  if ($args[0] -like '*setup-codex-coordination.py') {\n"
               "    $global:LASTEXITCODE = 0\n"
               "    Write-Output '{\"status\":\"tokenless\",\"credential_file_configured\":false,\"connection_configured\":false}'\n"
               "    return\n"
               "  }\n"
               "  throw 'fixture launch failure'\n"
               "}\n"
               if launch_throws else "")
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
        "get_env() { return 0; }\n"
        + ("command() { case \"$*\" in '-v python'|'-v python3') return 1;; "
           "*) builtin command \"$@\";; esac; }\n" if missing_python else "")
        + parts[1] + '\nprintf "RESULT:%s:%s\\n" "$HOOK_CODEX" "$INSTR_CODEX"\n',
        encoding="utf-8")
    result = subprocess.run([bash, script.as_posix()], env=env, capture_output=True,
                            timeout=HOOK_PROCESS_TIMEOUT, check=True)
    return result.stdout.decode("utf-8")


def _plugin_version():
    return json.loads((ROOT / "plugin/.claude-plugin/plugin.json").read_text(encoding="utf-8"))["version"]


def _recording_daemon():
    """A fixture daemon that records each GET's path and answers 200."""
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker, paths


def _hook_env(tmp_path, port):
    env = isolated_env(tmp_path / "codex-home")
    env.update({"PSEUDOLIFE_MCP_DAEMON_URL": f"http://127.0.0.1:{port}",
                "PSEUDOLIFE_MCP_TOKEN": "fixture-token"})
    return env


def test_bash_session_start_sends_the_plugin_version(tmp_path):
    """The SessionStart hook tells the daemon which plugin release it runs
    from (read beside the script, in .claude-plugin/plugin.json), with and
    without a session id, so the briefing can open with a mismatch notice.
    2026-09-21: a stale plugin cache ran an hour against a newer daemon
    with nothing to say so."""
    server, worker, paths = _recording_daemon()
    try:
        env = _hook_env(tmp_path, server.server_port)
        bash_run(ROOT / "plugin/hooks/session-start.sh",
                 input='{"session_id":"fixture","source":"startup"}', env=env)
        bash_run(ROOT / "plugin/hooks/session-start.sh", input="{}", env=env)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(paths) == 2
    with_sid, without_sid = (parse_qs(urlsplit(p).query) for p in paths)
    assert with_sid["session_id"] == ["fixture"]
    assert with_sid["plugin_version"] == [_plugin_version()]
    assert "session_id" not in without_sid
    assert without_sid["plugin_version"] == [_plugin_version()]


def test_bash_session_start_sends_no_version_without_a_manifest(tmp_path):
    """Codex runs a content-addressed copy of plugin/hooks with no manifest
    beside it; the hook then simply omits the parameter."""
    hooks = tmp_path / "hooks"
    shutil.copytree(ROOT / "plugin/hooks", hooks)
    server, worker, paths = _recording_daemon()
    try:
        bash_run(hooks / "session-start.sh", input='{"session_id":"fixture","source":"startup"}',
                 env=_hook_env(tmp_path, server.server_port))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(paths) == 1
    assert "plugin_version" not in parse_qs(urlsplit(paths[0]).query)


def test_native_session_start_sends_the_plugin_version(tmp_path):
    server, worker, paths = _recording_daemon()
    try:
        env = _hook_env(tmp_path, server.server_port)
        command = f"& '{ROOT.as_posix()}/plugin/hooks/lifecycle.ps1' -Event SessionStart"
        pwsh_run("-Command", command, input='{"session_id":"fixture","source":"startup"}', env=env)
        pwsh_run("-Command", command, input="{}", env=env)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(paths) == 2
    with_sid, without_sid = (parse_qs(urlsplit(p).query) for p in paths)
    assert with_sid["session_id"] == ["fixture"]
    assert with_sid["plugin_version"] == [_plugin_version()]
    assert "session_id" not in without_sid
    assert without_sid["plugin_version"] == [_plugin_version()]


def _plugin_with_version(tmp_path, version):
    """A plugin tree whose manifest carries ``version``; hooks run from it."""
    root = tmp_path / "plugin"
    shutil.copytree(ROOT / "plugin", root)
    manifest = root / ".claude-plugin/plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = version
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return root


@pytest.mark.parametrize("hook", ["bash", "native"])
def test_session_start_encodes_a_local_version_label(tmp_path, hook):
    """``0.15.0+local`` must arrive intact: an unencoded ``+`` decodes to a
    space on the daemon, fails the shape check there, and mutes the notice
    (reviewer finding, 2026-09-21)."""
    root = _plugin_with_version(tmp_path, "0.15.0+local")
    server, worker, paths = _recording_daemon()
    try:
        env = _hook_env(tmp_path, server.server_port)
        if hook == "bash":
            bash_run(root / "hooks/session-start.sh", input="{}", env=env)
        else:
            pwsh_run("-Command", f"& '{root.as_posix()}/hooks/lifecycle.ps1' -Event SessionStart",
                     input="{}", env=env)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert len(paths) == 1
    assert parse_qs(urlsplit(paths[0]).query)["plugin_version"] == ["0.15.0+local"]
