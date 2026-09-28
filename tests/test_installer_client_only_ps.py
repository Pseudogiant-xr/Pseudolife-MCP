"""Executable contracts for ops/install.ps1's client-only mode.

The PowerShell twin of tests/test_installer_client_only.py: ``-ClientOnly
-DaemonUrl <url> -TokenFile <path>``, or a ``-DaemonUrl`` whose host is not
loopback, wires this machine's clients to a daemon running elsewhere and
never touches Docker, volumes, ops/.env or a token of its own. Blocks run
extracted from ops/install.ps1 under pwsh with stub commands in a disposable
home, and one test runs the whole script against a local stand-in daemon.
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

from pseudolife_memory.credentials import _write_token_file
from tests.test_installer_existing_upgrade import _between, _fixture_env


ROOT = Path(__file__).resolve().parents[1]
INSTALL = "ops/install.ps1"
REMOTE = "http://100.64.0.2:8765"
FIXTURE_TOKEN = "fixture-remote-token-0123456789"
HEALTH_ON = '{"status": "ok", "version": "0.0.0", "auth": true}'
HEALTH_OPEN = '{"status": "ok", "version": "0.0.0", "auth": false}'


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install.ps1 has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _q(value: str | Path) -> str:
    return str(value).replace("'", "''")


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


class _PowerShell:
    """A disposable home, and pwsh running install.ps1 fragments in it."""

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
            ) -> subprocess.CompletedProcess[str]:
        prelude = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$repo = '{_q(ROOT)}'\n"
            "function Step($message) { Write-Output \"STEP: $message\" }\n"
        )
        script = self.tmp / "run.ps1"
        script.write_text(prelude + body, encoding="utf-8")
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, text=True, timeout=120, env=env, check=False,
            stdin=subprocess.DEVNULL)


# -- flags and what they imply --------------------------------------------------

def _mode(ps: _PowerShell, *, daemon_url: str = "", client_only: bool = False,
          token_file: str = "", extractor: str = "", model: str = "",
          shim_port: int = 0, no_token: bool = False, transport: str = "shim",
          extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return ps.run(
        f"$DaemonUrl = '{_q(daemon_url)}'\n"
        f"$ClientOnly = [switch]${'true' if client_only else 'false'}\n"
        f"$TokenFile = '{_q(token_file)}'\n$Extractor = '{extractor}'\n$Model = '{model}'\n"
        f"$ShimPort = {shim_port}\n$NoToken = [switch]${'true' if no_token else 'false'}\n"
        f"$Transport = '{transport}'\n"
        + _block("client-only mode")
        + "\nWrite-Output \"CLIENT_ONLY=$([bool]$ClientOnly)\"\n"
        + "Write-Output \"DAEMON_URL=$DaemonUrl\"\n",
        extra_env=extra_env)


@pytest.mark.parametrize("url", [
    REMOTE, "https://pl.example.invalid", "http://pl.example.invalid:8765/"])
def test_a_daemon_url_off_this_machine_implies_client_only(tmp_path, url):
    proc = _mode(_PowerShell(tmp_path), daemon_url=url)
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout
    assert f"DAEMON_URL={url.rstrip('/')}" in proc.stdout.splitlines()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765", "http://localhost:8765/", "http://LOCALHOST:8765",
    "http://[::1]:8765"])
def test_a_loopback_daemon_url_keeps_the_local_install(tmp_path, url):
    proc = _mode(_PowerShell(tmp_path), daemon_url=url)
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=False" in proc.stdout


def test_the_environment_url_is_honoured_when_the_flag_is_absent(tmp_path):
    proc = _mode(_PowerShell(tmp_path), extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout
    assert f"DAEMON_URL={REMOTE}" in proc.stdout.splitlines()


def test_the_flag_wins_over_the_environment_url(tmp_path):
    proc = _mode(_PowerShell(tmp_path), daemon_url="http://127.0.0.1:8765",
                 extra_env={"PSEUDOLIFE_MCP_DAEMON_URL": REMOTE})
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=False" in proc.stdout
    assert "DAEMON_URL=http://127.0.0.1:8765" in proc.stdout.splitlines()


def test_an_explicit_client_only_install_may_use_a_loopback_tunnel(tmp_path):
    proc = _mode(_PowerShell(tmp_path), client_only=True,
                 daemon_url="http://127.0.0.1:18765")
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in proc.stdout


def test_client_only_requires_a_daemon_url(tmp_path):
    proc = _mode(_PowerShell(tmp_path), client_only=True)
    assert proc.returncode == 2
    assert "-ClientOnly needs" in _output(proc)
    assert "-DaemonUrl" in _output(proc)


def test_a_malformed_daemon_url_is_refused(tmp_path):
    proc = _mode(_PowerShell(tmp_path), daemon_url="pl.example.invalid:8765")
    assert proc.returncode == 2
    assert "invalid daemon URL" in _output(proc)


@pytest.mark.parametrize("flag,kwargs", [
    ("-Extractor", {"extractor": "sidecar"}),
    ("-Model", {"model": "claude-opus-5"}),
    ("-ShimPort", {"shim_port": 8082}),
    ("-NoToken", {"no_token": True}),
    ("-Transport http", {"transport": "http"}),
])
def test_client_only_refuses_the_flags_of_a_local_daemon(tmp_path, flag, kwargs):
    proc = _mode(_PowerShell(tmp_path), daemon_url=REMOTE, **kwargs)
    assert proc.returncode == 2
    assert flag in _output(proc)


def test_a_token_file_flag_needs_client_only(tmp_path):
    proc = _mode(_PowerShell(tmp_path), token_file=r"C:\fixture\remote.token")
    assert proc.returncode == 2
    assert "-TokenFile" in _output(proc)


def test_the_script_itself_refuses_client_only_without_a_url(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = subprocess.run(
        [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(ROOT / INSTALL),
         "-ClientOnly", "-NoArt"],
        capture_output=True, text=True, timeout=120, env=ps.env, check=False,
        stdin=subprocess.DEVNULL)
    assert proc.returncode == 2
    assert "-ClientOnly needs" in _output(proc)


def test_the_new_parameters_are_declared_and_documented():
    text = (ROOT / INSTALL).read_text(encoding="utf-8")
    params = text.split("param(", 1)[1].split("\n)\n", 1)[0]
    assert "[string]$DaemonUrl" in params
    assert "[string]$TokenFile" in params
    assert "[switch]$ClientOnly" in params
    header = text.split("param(", 1)[0]
    for flag in ("-DaemonUrl", "-TokenFile", "-ClientOnly"):
        assert re.search(rf"(?m)^#\s+.*{flag}\b", header), flag


# -- preflight: the token file and the remote daemon ----------------------------

def _preflight(ps: _PowerShell, *, daemon_url: str = REMOTE, token_file: str = "",
               health: str = HEALTH_ON, unreachable: bool = False,
               clients: tuple[str, ...] = ("claude",), missing: tuple[str, ...] = (),
               extra_env: dict[str, str] | None = None,
               cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {"FAKE_HEALTH": health, "FAKE_UNREACHABLE": "1" if unreachable else ""}
    env.update(extra_env or {})
    stubs = "".join(ps.stub_function(name) for name in ("claude", "codex", "gemini")
                    if name not in missing)
    client_list = ", ".join(f"'{client}'" for client in clients)
    body = (
        f"$env:PATH = '{_q(ps.tmp)}'\n"
        + (f"Set-Location -LiteralPath '{_q(cwd)}'\n" if cwd else "")
        + stubs
        + ps.stub_function("Invoke-RestMethod",
                           "if ($env:FAKE_UNREACHABLE) { throw 'unreachable' }\n"
                           "    return ($env:FAKE_HEALTH | ConvertFrom-Json)")
        + f"$DaemonUrl = '{_q(daemon_url)}'\n$ClientOnly = [switch]$true\n"
        f"$TokenFile = '{_q(token_file)}'\n$clients = @({client_list})\n"
        "$Extractor = ''; $Model = ''; $ShimPort = 0; $NoToken = [switch]$false\n"
        "$Transport = 'shim'\n"
        + _block("client-only mode") + _block("client-only preflight")
        + "\nInvoke-ClientOnlyPreflight\nWrite-Output \"TOKEN_FILE=$TokenFile\"\n")
    return ps.run(body, extra_env=env)


def _token(ps: _PowerShell) -> Path:
    path = ps.tmp / "keys" / "remote.token"
    path.parent.mkdir()
    _write_token_file(path, FIXTURE_TOKEN)
    return path


def test_client_only_never_mints_a_token_it_needs_one_given(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps)
    assert proc.returncode == 2
    assert "-TokenFile" in _output(proc)
    assert "never mints" in _output(proc)
    assert not any(call.startswith("Invoke-RestMethod|") for call in ps.logged())


def test_a_missing_token_file_is_refused(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _preflight(ps, token_file=str(tmp_path / "absent.token"))
    assert proc.returncode == 1
    assert "absent.token" in _output(proc)
    assert "missing or empty" in _output(proc)


def test_the_token_file_may_come_from_the_environment(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": str(token)})
    assert proc.returncode == 0, _output(proc)
    assert f"TOKEN_FILE={token}" in proc.stdout.splitlines()
    assert FIXTURE_TOKEN not in _output(proc)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_a_token_file_other_users_can_read_is_refused(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    token.chmod(0o644)
    proc = _preflight(ps, token_file=str(token))
    assert proc.returncode == 1
    assert "chmod 600" in _output(proc)


def test_an_unreachable_daemon_is_refused_with_how_to_check(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), unreachable=True)
    assert proc.returncode == 1
    assert f"{REMOTE}/health" in _output(proc)
    assert "tailnet" in _output(proc)
    [call] = [call for call in ps.logged() if call.startswith("Invoke-RestMethod|")]
    assert f"{REMOTE}/health" in call
    assert "-TimeoutSec" in call


def test_an_open_daemon_is_never_reached_over_a_network(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), health=HEALTH_OPEN)
    assert proc.returncode == 1
    assert "auth: false" in _output(proc)


def test_an_open_daemon_through_a_loopback_tunnel_is_allowed(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, daemon_url="http://127.0.0.1:18765", token_file=str(token),
                      health=HEALTH_OPEN)
    assert proc.returncode == 0, _output(proc)
    assert "unencrypted" not in _output(proc)


def test_plain_http_off_this_machine_warns_once_and_continues(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token))
    assert proc.returncode == 0, _output(proc)
    warnings = [line for line in _output(proc).splitlines() if "unencrypted" in line]
    assert len(warnings) == 1
    assert "tailnet" in warnings[0]
    assert FIXTURE_TOKEN not in _output(proc)


def test_https_off_this_machine_does_not_warn(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, daemon_url="https://pl.example.invalid", token_file=str(token))
    assert proc.returncode == 0, _output(proc)
    assert "unencrypted" not in _output(proc)


def test_a_relative_token_file_is_made_absolute(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=os.path.join("keys", "remote.token"), cwd=tmp_path)
    assert proc.returncode == 0, _output(proc)
    assert f"TOKEN_FILE={token}" in proc.stdout.splitlines()


def test_a_selected_cli_that_is_missing_is_refused_up_front(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    proc = _preflight(ps, token_file=str(token), clients=("claude", "gemini"),
                      missing=("gemini",))
    assert proc.returncode == 1
    assert "gemini CLI is not on PATH" in _output(proc)


# -- Codex: the credential helper gets the operator's token and the remote URL --

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


def test_client_only_briefing_hook_runs_the_host_shim_not_docker(tmp_path):
    text = (ROOT / INSTALL).read_text(encoding="utf-8")
    lines = [line for line in text.splitlines()
             if line.startswith("$briefingCommand = ")
             or line.startswith("if ($ClientOnly) { $briefingCommand = ")]
    assert len(lines) == 2, lines
    ps = _PowerShell(tmp_path)
    proc = ps.run("$ClientOnly = [switch]$true\n" + "\n".join(lines)
                  + "\nWrite-Output \"CMD=$briefingCommand\"\n")
    assert proc.returncode == 0, _output(proc)
    assert "CMD=pseudolife-mcp briefing --hook-json" in proc.stdout.splitlines()


# -- the whole script, client-only, against a local stand-in daemon ------------

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
    for rel in ("ops/install.ps1", "ops/install-hook.ps1", "ops/client_credentials.py",
                "ops/.env.example", "examples/CLAUDE.memory.md"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo / rel)
    return repo


def _script_stub(bin_dir: Path, calls: Path, name: str, body: str = "exit 0") -> None:
    (bin_dir / f"{name}.ps1").write_text(
        f"Add-Content -LiteralPath '{_q(calls)}' -Value ('{name}|' + (@($args) -join ' '))\n"
        + body + "\n", encoding="utf-8")


def test_a_client_only_install_wires_clients_to_the_remote_daemon(tmp_path, daemon_server):
    ps = _PowerShell(tmp_path)
    token = _token(ps)
    repo = _copy_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    installed = tmp_path / "installed-bin"
    installed.mkdir()
    shim = installed / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    shim.write_text("placeholder\n", encoding="utf-8")
    shim.chmod(0o755)
    calls = ps.calls
    for name in ("docker", "codex"):
        _script_stub(fake_bin, calls, name, "exit 99")
    _script_stub(fake_bin, calls, "python", f"& '{_q(sys.executable)}' @args\nexit $LASTEXITCODE")
    _script_stub(fake_bin, calls, "pipx",
                 "if (($args[0] -eq 'environment') -and ($args[2] -eq 'PIPX_BIN_DIR')) "
                 f"{{ Write-Output '{_q(installed)}' }}\nexit 0")
    _script_stub(fake_bin, calls, "claude",
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'get')) { exit 1 }\n"
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'add') -and ($args -contains '--help')) "
                 "{ Write-Output '  -e, --env <env...>' }\nexit 0")
    _script_stub(fake_bin, calls, "gemini",
                 "if (($args[0] -eq 'mcp') -and ($args[1] -eq 'add') -and ($args -contains '--help')) "
                 "{ Write-Output '  -e, --env <env...>' }\nexit 0")
    env = dict(ps.env)
    system_dirs = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")] \
        if os.name == "nt" else ["/usr/bin", "/bin"]
    env.update({"PATH": os.pathsep.join([str(fake_bin), *system_dirs]),
                "PYTHONPATH": str(ROOT)})
    proc = subprocess.run(
        [ps.pwsh, "-NoProfile", "-NonInteractive", "-File", str(repo / "ops" / "install.ps1"),
         "-ClientOnly", "-DaemonUrl", daemon_server, "-TokenFile", str(token),
         "-Client", "claude,gemini", "-ClaudePlugin", "skip", "-Instructions", "skip",
         "-NoArt"],
        capture_output=True, text=True, timeout=300, env=env, check=False,
        stdin=subprocess.DEVNULL)
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
    assert "pseudolife-mcp briefing --hook-json" in commands
    assert "docker exec" not in commands
    assert settings["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == daemon_server
    assert Path(settings["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token
    assert f"remote at {daemon_server}" in proc.stdout
    assert re.search(r"\[x\] Agent board\s+on", proc.stdout)
    assert any(seen.endswith(f"Bearer {FIXTURE_TOKEN}") for seen in _Daemon.seen)
    assert "Waiting for the daemon" not in proc.stdout
