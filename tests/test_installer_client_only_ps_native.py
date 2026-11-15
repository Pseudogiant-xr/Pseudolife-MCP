"""The native installer (ops/install-native.*): the tests of test_installer_client_only_ps.py
that the native client path changed, run against the native installer.

test_installer_client_only_ps.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

import pytest

from tests import native_client_stub as native_stub

from pseudolife_memory.credentials import CredentialProvider, _write_token_file
from tests.test_installer_client_only import CODE_FORMS, NOT_CODES, _pair_report
from tests.test_installer_existing_upgrade import _between, _fixture_env, user_path_unchanged


ROOT = Path(__file__).resolve().parents[1]


INSTALL = "ops/install-native.ps1"


REMOTE = "http://100.64.0.2:8765"


FIXTURE_TOKEN = "fixture-remote-token-0123456789"


HEALTH_ON = '{"status": "ok", "version": "0.0.0", "auth": true}'


def _q(value: str | Path) -> str:
    return str(value).replace("'", "''")


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


_WRAPPED = re.compile(r"\r?\n[ \t]*\|[ \t]?")


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return _WRAPPED.sub("", _ANSI.sub("", proc.stdout + proc.stderr))


class _PowerShell:
    """A disposable home, and pwsh running install-native.ps1 fragments in it."""

    def __init__(self, tmp_path: Path):
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("PowerShell 7 (pwsh) is unavailable")
        self.pwsh = pwsh
        self.tmp = tmp_path
        self.env = _fixture_env(tmp_path / "env")
        self.home = Path(self.env["HOME"])
        self.calls = tmp_path / "calls.txt"

    def logged(self) -> list[str]:
        return (self.calls.read_text(encoding="utf-8").splitlines()
                if self.calls.exists() else [])

    def stub_function(self, name: str, body: str = "$global:LASTEXITCODE = 0") -> str:
        return (f"function global:{name} {{\n"
                f"    Add-Content -LiteralPath '{_q(self.calls)}' -Value ('{name}|' + (@($args) -join ' '))\n"
                f"    {body}\n}}\n")

    def run(self, body: str, *, extra_env: dict[str, str] | None = None,
            stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
        prelude = (
            "$ErrorActionPreference = 'Stop'\n"
            # Match the Python pipe codec even on a legacy Windows console.
            "[Console]::InputEncoding = [Console]::OutputEncoding = $OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
            # Plain text on every OS, and an uncaught error as one plain line
            # (exit 1, as pwsh gives it): see _output.
            "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\n"
            "trap { [Console]::Error.WriteLine(\"ERROR: $($_.Exception.Message)\"); exit 1 }\n"
            f"$repo = '{_q(ROOT)}'\n"
            "function Step($message) { Write-Output \"STEP: $message\" }\n"
            f"function Get-InstallerPython {{ '{_q(sys.executable)}' }}\n"
            # The side-by-side shim runtime is never really installed here:
            # ops/shim_runtime.py install fails in the shim-install-failed
            # scenario (FAKE_PIPX_EXIT), otherwise the launcher is the
            # installed shim path and nothing migrates.
            "$env:PSEUDOLIFE_SHIM_PYTHON = 'fake_runtime_python'\n"
            "function global:fake_runtime_python {\n"
            "    $callArgs = @($args)\n"
            "    $global:LASTEXITCODE = 0\n"
            "    if (\"$($callArgs[0])\" -notlike '*shim_runtime.py') { $global:LASTEXITCODE = 91; return }\n"
            "    if ($env:FAKE_PIPX_EXIT -and ($env:FAKE_PIPX_EXIT -ne '0')) { $global:LASTEXITCODE = 1; return }\n"
            "    if ($callArgs[1] -eq 'migrate') { $global:LASTEXITCODE = 3; return }\n"
            "    $bin = if ($env:FAKE_INSTALL_BIN) { $env:FAKE_INSTALL_BIN } else { '/fixture/installed-bin' }\n"
            "    return (Join-Path $bin 'pseudolife-mcp')\n"
            "}\n"
            + _between(INSTALL, "function Get-HelperStatus($output)", "\n}\n") + "\n}\n"
        )
        script = self.tmp / "run.ps1"
        script.write_text(prelude + body, encoding="utf-8")
        env = dict(self.env)
        env.update({"NO_COLOR": "1", "TERM": "dumb"})
        env.update(extra_env or {})
        stdin = {"input": stdin_text} if stdin_text is not None else {
            "stdin": subprocess.DEVNULL}
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, text=True, encoding="utf-8", timeout=120, env=env, check=False, **stdin)


def _token(ps: _PowerShell) -> Path:
    path = ps.tmp / "keys" / "remote.token"
    path.parent.mkdir()
    _write_token_file(path, FIXTURE_TOKEN)
    return path


def test_codex_credentials_come_from_the_token_file_and_the_remote_url(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    captured = tmp_path / "helper.txt"
    stages = re.search(r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# -- 10\. standing", (ROOT / INSTALL).read_text(encoding="utf-8"))[1]
    body = f"""$clients = @('codex'); $CodexHooks = 'auto'; $CodexHookTrust = 'yes'
$Instructions = 'auto'; $ClaudeMd = ''; $interactive = $false
$ClientOnly = [switch]$true; $DaemonUrl = '{REMOTE}'; $TokenFile = '{_q(token)}'
function Get-EnvValue($name) {{
    if ($name -eq 'PSEUDOLIFE_MCP_TOKEN') {{ return 'fixture-local-token' }}
    if ($name -eq 'PSEUDOLIFE_MCP_DAEMON_URL') {{ return 'http://127.0.0.1:8765' }}
    return $null
}}
$env:PATH = '{_q(tmp_path)}'
function global:python {{
    $global:LASTEXITCODE = 0
    if ("$args" -match 'sys\\.executable') {{ return 'fake-codex-python' }}
}}
function global:fake-codex-python {{
    $stdin = @($input) -join ''
    $callArgs = @($args)
    $global:LASTEXITCODE = 0
    if ("$($callArgs[0])" -like '*setup-codex-coordination.py') {{
        Set-Content -LiteralPath '{_q(captured)}' -Value ("ARGS=" + ($callArgs -join ' ') + "`nSTDIN=" + $stdin)
        return ('{{"status":"ready","credential_file_configured":true,"connection_configured":true,' +
                '"credential_file_path":"/fixture/codex/token","daemon_url":"{REMOTE}"}}')
    }}
    if ("$($callArgs[0])" -like '*setup-codex-hooks.py') {{
        return '{{"status":"ready","source":"manual","instructions":"covered-by-hooks","recovery":null}}'
    }}
}}
$env:FAKE_LAUNCHER = '{_q(tmp_path / "native" / ("pseudolife-mcp.cmd" if os.name == "nt" else "pseudolife-mcp"))}'
$env:FAKE_LAUNCHER_SH = @'
{native_stub.LAUNCHER_SH}
'@
{native_stub.POWERSHELL}
{stages}
Write-Output "URL=$codexCredentialUrl"
"""
    proc = ps.run(body)
    assert proc.returncode == 0, _output(proc)
    helper = captured.read_text(encoding="utf-8")
    assert "--installer-token-stdin" in helper
    assert f"--installer-daemon-url {REMOTE}" in helper
    assert f"STDIN={FIXTURE_TOKEN}" in helper
    assert "fixture-local-token" not in helper
    assert FIXTURE_TOKEN not in _output(proc)
    assert f"URL={REMOTE}" in proc.stdout.splitlines()


CRED_READY = ('{"status":"ready","credential_file_configured":true,"connection_configured":true,'
              '"credential_file_path":"/fixture/codex/token","daemon_url":"%s"}')


CODEX_STUB = """$global:LASTEXITCODE = 0
    $callArgs = @($args)
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq 'get')) {
        if ($env:FAKE_EXISTING) { return $env:FAKE_EXISTING }
        $global:LASTEXITCODE = 1
        return
    }
    if ($callArgs -contains '--help') { return '  --env <KEY=VALUE>' }"""


def _codex_wiring(ps: _PowerShell, *, python_ok: bool = True, cred: str | None = None,
                  cred_exit: int = 0, pipx_exit: int = 0, existing: str = ""):
    """Sections 9 to 11 for a Codex-only client-only install, with stub
    python, native client (``pipx_exit`` is its install result) and codex
    commands. Returns the run and the codex adds."""
    token = _token(ps)
    installed = ps.tmp / "installed-bin"
    installed.mkdir()
    launcher = installed / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    launcher.write_text("placeholder\n", encoding="utf-8")
    if "FAKE_INSTALL_BIN" in ps.env:  # a test's own launcher that answers connect
        launcher = Path(ps.env["FAKE_INSTALL_BIN"]) / "pseudolife-mcp.ps1"
    stages = re.search(r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)^# -- 12\. health",
                       (ROOT / INSTALL).read_text(encoding="utf-8"))[1]
    body = f"""$env:PATH = '{_q(ps.tmp)}'
$clients = @('codex'); $CodexHooks = 'auto'; $CodexHookTrust = 'yes'
$Instructions = 'auto'; $ClaudeMd = ''; $AgentsFile = ''; $interactive = $false
$Transport = 'shim'
$ClientOnly = [switch]$true; $DaemonUrl = '{REMOTE}'; $TokenFile = '{_q(token)}'
function Get-EnvValue($name) {{ return $null }}
function global:python {{
    $global:LASTEXITCODE = 1
    if (($env:FAKE_PY_OK -eq '1') -and ("$args" -match 'sys\\.executable')) {{
        $global:LASTEXITCODE = 0
        return 'fake-codex-python'
    }}
}}
function global:fake-codex-python {{
    $null = @($input)
    $callArgs = @($args)
    $global:LASTEXITCODE = 0
    if ("$($callArgs[0])" -like '*setup-codex-coordination.py') {{
        if ($callArgs -contains '--runtime-defaults') {{
            return '{{"status":"ready","runtime_defaults":"configured"}}'
        }}
        $global:LASTEXITCODE = [int]$env:FAKE_CRED_EXIT
        return $env:FAKE_CRED
    }}
    if ("$($callArgs[0])" -like '*setup-codex-hooks.py') {{
        return '{{"status":"ready","source":"manual","instructions":"covered-by-hooks","recovery":null}}'
    }}
}}
{native_stub.POWERSHELL}
{ps.stub_function("codex", CODEX_STUB)}
{stages}
Write-Output ("MCP_CODEX=" + $mcpState['codex'])
"""
    proc = ps.run(body, extra_env={
        "FAKE_PY_OK": "1" if python_ok else "0",
        "FAKE_CRED": cred if cred is not None else CRED_READY % REMOTE,
        "FAKE_CRED_EXIT": str(cred_exit), "FAKE_NATIVE_EXIT": str(pipx_exit),
        "FAKE_EXISTING": existing, "FAKE_LAUNCHER": str(launcher), "FAKE_KEEP_LAUNCHER": "1"})
    adds = [call for call in ps.logged()
            if call.startswith("codex|mcp add") and "--help" not in call]
    return proc, adds


def test_codex_registers_with_the_remote_url_and_its_credential_file(tmp_path):
    proc, adds = _codex_wiring(_PowerShell(tmp_path))
    assert proc.returncode == 0, _output(proc)
    [add] = adds
    assert f"PSEUDOLIFE_MCP_DAEMON_URL={REMOTE}" in add
    assert "PSEUDOLIFE_MCP_TOKEN_FILE=/fixture/codex/token" in add
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in add
    assert "MCP_CODEX=shim-env" in proc.stdout.splitlines()


@pytest.mark.parametrize("scenario,kwargs,says", [
    ("no python", {"python_ok": False}, "Python 3.10"),
    ("credential setup failed",
     {"cred": '{"status":"needs-configuration","recovery":"fixture"}', "cred_exit": 1},
     "credential setup failed"),
    ("shim install failed", {"pipx_exit": 1}, "stdio shim"),
    ("existing registration elsewhere",
     {"cred": CRED_READY % "http://127.0.0.1:8765",
      "existing": "pseudolife-memory\n  command: pseudolife-mcp\n"
                  "  env: PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765"},
     f"not the daemon at {REMOTE}"),
])
def test_codex_is_never_left_on_another_daemon(tmp_path, scenario, kwargs, says):
    proc, adds = _codex_wiring(_PowerShell(tmp_path), **kwargs)
    assert proc.returncode == 0, _output(proc)
    assert adds == [], scenario
    assert "MCP_CODEX=failed" in proc.stdout.splitlines(), scenario
    assert says in _output(proc), scenario


class _Daemon(http.server.BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        _Daemon.seen.append(f"{self.path}|{self.headers.get('Authorization', '')}")
        if self.path == "/health":
            body = HEALTH_ON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"check-in"
            self.send_response(200)
            self.send_header("X-PL-Board", "on")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the test log
        pass


@pytest.fixture
def daemon_server():
    _Daemon.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Daemon)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _copy_repo(tmp_path: Path) -> Path:
    """The files a client-only run reads, in a scratch checkout: a bug that
    wrote ops/.env or the compose override must not touch the real tree."""
    repo = tmp_path / "repo"
    for rel in ("ops/install-native.ps1", "ops/install-hook.ps1", "ops/client_credentials.py",
                "ops/.env.example", "examples/CLAUDE.memory.md", "pyproject.toml"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    return repo


def _script_stub(bin_dir: Path, calls: Path, name: str, body: str = "exit 0") -> None:
    (bin_dir / f"{name}.ps1").write_text(
        f"Add-Content -LiteralPath '{_q(calls)}' -Value ('{name}|' + (@($args) -join ' '))\n"
        + body + "\n", encoding="utf-8")


FRESH_CONNECT = '{"url": null, "rows": [], "exit": 3, "error": "none"}'


def _whole_client_only_run(tmp_path, daemon_server, *, connect_report: str = FRESH_CONNECT, connect_exit: int = 3):
    """The whole client-only run; the staged client answers connect's dry run with
    ``connect_report`` and exit ``connect_exit`` (a fresh machine's: no registration, 3)."""
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    repo = _copy_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    shim = installed / "pseudolife-mcp"
    # The release's client answers connect's dry run as the real one does on
    # a fresh machine (its report of no registration, exit 3).
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"))[1]
    release_env = native_stub.write_release(
        tmp_path / "release", version,
        f"printf 'shim|%s\\n' \"$*\" >>'{ps.calls}'\n"
        "if [ \"$1\" = connect ]; then\n"
        f"  echo '{connect_report}'; exit {connect_exit}\nfi\n"
        "exit 0\n")
    calls = ps.calls
    for name in ("docker", "codex"):
        _script_stub(fake_bin, calls, name, "exit 99")
    _script_stub(fake_bin, calls, "python", f"& '{_q(sys.executable)}' @args\nexit $LASTEXITCODE")
    _script_stub(fake_bin, calls, "claude",
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'get')) { exit 1 }\n"
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'add') -and ($args -contains '--help')) "
                 "{ Write-Output '  -e, --env <env...>' }\nexit 0")
    _script_stub(fake_bin, calls, "gemini",
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'add') -and ($args -contains '--help')) "
                 "{ Write-Output '  -e, --env <env...>' }\nexit 0")
    # An earlier local install left its own file and loopback URL for the hooks.
    settings_path = ps.home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {
        "PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/local/claude-code.token",
        "PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}}), encoding="utf-8")
    env = dict(ps.env)
    system_dirs = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")] \
        if os.name == "nt" else ["/usr/bin", "/bin"]
    env.update(release_env)
    env.update({"PSEUDOLIFE_SHIM_RUNTIMES": str(installed / "runtimes"),
                "PSEUDOLIFE_SHIM_LAUNCHER": str(shim)})
    env.update({"PATH": os.pathsep.join([str(fake_bin), *system_dirs]),
                "PYTHONPATH": str(ROOT), "NO_COLOR": "1", "TERM": "dumb",
                # The installer's own environment carries an older credential.
                "PSEUDOLIFE_MCP_TOKEN": "fixture-local-literal"})
    with user_path_unchanged(ps.home):
        proc = subprocess.run(
            [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / "install-native.ps1"),
             "-ClientOnly", "-DaemonUrl", daemon_server, "-TokenFile", str(token),
             "-Client", "claude,gemini", "-ClaudePlugin", "skip", "-Instructions", "skip",
             "-NoArt"],
            capture_output=True, text=True, timeout=300, env=env, check=False,
            stdin=subprocess.DEVNULL)
    return env, installed, proc, ps, repo, shim, token


@pytest.mark.skipif(os.name == "nt", reason=(
    "the installer runs the staged native client before anything else, and a "
    "Windows stub cannot be a script; test_installer_native_client_ps.py covers "
    "that stage with a real binary"))
def test_a_client_only_install_wires_clients_to_the_remote_daemon(tmp_path, daemon_server):
    env, installed, proc, ps, repo, shim, token = _whole_client_only_run(tmp_path, daemon_server)
    output = _output(proc)
    assert proc.returncode == 0, output
    logged = ps.logged()
    assert not any(call.startswith(("docker|", "codex|")) for call in logged)
    assert not (repo / "ops" / ".env").exists()
    assert not (repo / "ops" / "docker-compose.override.yml").exists()
    assert FIXTURE_TOKEN not in output
    [claude] = [call for call in logged if call.startswith("claude|mcp add --scope user")]
    for pair in ("PSEUDOLIFE_WRITER_ID=claude-code", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 f"PSEUDOLIFE_MCP_TOKEN_FILE={token}",
                 f"PSEUDOLIFE_MCP_DAEMON_URL={daemon_server}"):
        assert f" {pair} " in claude, pair
    [gemini] = [call for call in logged if call.startswith("gemini|mcp add -s user")]
    for pair in ("PSEUDOLIFE_WRITER_ID=gemini", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 f"PSEUDOLIFE_MCP_TOKEN_FILE={token}",
                 f"PSEUDOLIFE_MCP_DAEMON_URL={daemon_server}"):
        assert f"-e {pair} " in gemini, pair
    settings = json.loads((ps.home / ".claude" / "settings.json").read_text(
        encoding="utf-8-sig"))
    commands = json.dumps(settings.get("hooks", {}))
    # The installed shim by its path: a hook's PATH need not include pipx's.
    briefings = [hook["command"] for group in settings["hooks"]["SessionStart"]
                 for hook in group["hooks"]
                 if hook["command"].endswith(" briefing --hook-json")]
    assert [Path(command.rsplit(" briefing", 1)[0]) for command in briefings] == [
        installed / "pseudolife-mcp"]
    assert "docker exec" not in commands
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == daemon_server
    assert Path(settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token
    assert "fixture-local-literal" not in output
    assert list((ps.home / ".claude").glob("settings.json.bak-pseudolife-*"))
    assert f"remote at {daemon_server}" in proc.stdout
    assert re.search(r"\[x\] Agent board\s+on", proc.stdout)
    assert any(seen.endswith(f"Bearer {FIXTURE_TOKEN}") for seen in _Daemon.seen)
    assert "Waiting for the daemon" not in proc.stdout
    connects = [call for call in ps.logged() if call.startswith("shim|connect")]
    if os.name == "nt":
        assert connects == [] and "not checked" in output
    else:
        assert len(connects) == 1 and "--dry-run" in connects[0]
        assert "not checked" not in output


@pytest.mark.skipif(os.name == "nt", reason=(
    "the installer runs the staged native client before anything else, and a "
    "Windows stub cannot be a script; test_installer_native_client_ps.py covers "
    "that stage with a real binary"))
def test_an_exit_after_the_native_client_is_installed_says_the_run_stopped_part_way(tmp_path, daemon_server):
    env, installed, proc, ps, repo, shim, token = _whole_client_only_run(tmp_path, daemon_server, connect_report='{"url": null, "rows": [], "exit": 1, "error": "the fixture refuses"}', connect_exit=1)
    output = _output(proc)
    assert proc.returncode == 1, output
    assert "could not check this machine's existing registrations" in output
    assert "Nothing was changed" not in output and "nothing was changed" not in output
    assert "stopped part way" in output and "the native client" in output and "Rerun the installer" in output
