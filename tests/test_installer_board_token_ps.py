"""Executable contracts for ops/install.ps1's board token.

The PowerShell twin of tests/test_installer_board_token.py: a default install
mints a bearer token so the agent board is on, and every client the installer
wires then carries it. Each block runs extracted from ops/install.ps1 under
pwsh against fake CLIs in a disposable home; the real
ops/client_credentials.py does the file work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from pseudolife_memory.credentials import CredentialProvider
from tests.test_installer_existing_upgrade import _between, _fixture_env


ROOT = Path(__file__).resolve().parents[1]
INSTALL = "ops/install.ps1"


def _block(name: str) -> str:
    text = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    assert f"# >>> {name} >>>\n" in text, f"install.ps1 has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _function(name: str) -> str:
    return _between(INSTALL, f"function {name}", "\n}\n") + "\n}"


def _q(value: str | Path) -> str:
    return str(value).replace("'", "''")


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

    def run(self, body: str, *, extra_env: dict[str, str] | None = None,
            ) -> subprocess.CompletedProcess[str]:
        prelude = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$repo = '{_q(ROOT)}'\n"
            f"function Get-InstallerPython {{ '{_q(sys.executable)}' }}\n"
            "function Step($message) { Write-Output \"STEP: $message\" }\n"
            + _function("Get-HelperStatus($output)") + "\n"
        )
        script = self.tmp / "run.ps1"
        script.write_text(prelude + body, encoding="utf-8")
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True, text=True, timeout=120, env=env, check=False)


def _output(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


# -- minting ------------------------------------------------------------------

def _mint(ps: _PowerShell, env_file: Path, *, no_token: bool = False,
          transport: str = "shim", python: str = "real", pipx: bool = False):
    """``python``: ``real`` (this interpreter, which has pip), ``none``, or
    ``broken`` (an interpreter that fails every call, as one whose pip
    cannot install would fail the shim-tooling probe)."""
    stub = ""
    if python == "none":
        stub = "function Get-InstallerPython { $null }\n"
    elif python == "broken":
        broken = ps.tmp / ("broken-python.cmd" if os.name == "nt" else "broken-python")
        broken.write_text("@exit /b 1\r\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n",
                          encoding="utf-8")
        broken.chmod(0o755)
        stub = f"function Get-InstallerPython {{ '{_q(broken)}' }}\n"
    if pipx:
        stub += "function pipx { $global:LASTEXITCODE = 0 }\n"
    stub += _function("Get-EnvValue($name)") + "\n"
    return ps.run(
        f"$envFile = '{_q(env_file)}'\n"
        f"$NoToken = [switch]${'true' if no_token else 'false'}\n"
        f"$Transport = '{transport}'\n"
        + stub + _block("mint token") + "\nWrite-Output \"TOKEN_STATE=$tokenState\"\n")


def _minted(env_file: Path) -> list[str]:
    return re.findall(r"(?m)^PSEUDOLIFE_MCP_TOKEN=(\S+)\r?$",
                      env_file.read_text(encoding="utf-8"))


@pytest.mark.parametrize("no_token,transport,python", [
    (False, "http", "real"),
    (False, "shim", "none"),
    (True, "shim", "real"),
])
def test_an_existing_token_is_reported_present_whatever_else_holds(tmp_path, no_token,
                                                                   transport, python):
    """The daemon is gated by a token already in ops/.env whether or not
    this run could have minted one (final review, 2026-09-28)."""
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-existing\n", encoding="utf-8")
    proc = _mint(ps, env_file, no_token=no_token, transport=transport, python=python)
    assert proc.returncode == 0, _output(proc)
    assert "TOKEN_STATE=present" in proc.stdout
    assert "fixture-existing" not in _output(proc)


def test_default_install_mints_a_token_and_never_prints_it(tmp_path):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _mint(ps, env_file)
    assert proc.returncode == 0, _output(proc)
    [token] = _minted(env_file)
    assert "TOKEN_STATE=minted" in proc.stdout
    assert "Minted a bearer token" in proc.stdout
    assert token not in _output(proc)
    again = _mint(ps, env_file)
    assert again.returncode == 0, _output(again)
    assert "TOKEN_STATE=present" in again.stdout
    assert _minted(env_file) == [token]


@pytest.mark.parametrize("no_token,transport,python,state", [
    (True, "shim", "real", "opted-out"),
    (False, "http", "real", "http"),
    # No shim can be installed, so stage 11 falls back to HTTP, which
    # cannot carry a token file: minting one would lock every client out.
    (False, "shim", "none", "no-shim"),
    (False, "shim", "broken", "no-shim"),
])
def test_open_loopback_installs_mint_nothing(tmp_path, no_token, transport, python, state):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _mint(ps, env_file, no_token=no_token, transport=transport, python=python)
    assert proc.returncode == 0, _output(proc)
    assert f"TOKEN_STATE={state}" in proc.stdout
    assert _minted(env_file) == []


def test_pipx_counts_as_shim_tooling_without_probing_pip(tmp_path):
    # With pipx present the pip probe is skipped, so the broken interpreter
    # gets as far as the mint itself (which it then fails).
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _mint(ps, env_file, python="broken", pipx=True)
    assert proc.returncode == 0, _output(proc)
    assert "TOKEN_STATE=failed" in proc.stdout
    assert "Could not mint a bearer token" in _output(proc)


def test_no_token_switch_is_a_documented_parameter():
    text = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
    params = text.split("param(", 1)[1].split("\n)\n", 1)[0]
    assert "[switch]$NoToken" in params
    header = text.split("param(", 1)[0]
    assert re.search(r"(?m)^#\s+.*-NoToken\s", header)


# -- client token files -------------------------------------------------------

def _client_files(ps: _PowerShell, env_file: Path, clients: list[str], *, hook: str = "",
                  extra_env: dict[str, str] | None = None):
    getters = "\n".join(_function(name) for name in (
        "Get-EnvValue($name)", "Get-DesktopTokenSource", "Get-DesktopTokensSource"))
    client_list = ", ".join(f"'{client}'" for client in clients)
    return ps.run(
        f"$envFile = '{_q(env_file)}'\n$clients = @({client_list})\n$Transport = 'shim'\n"
        f"$hookState = @{{ claude = '{hook}' }}\n" + getters + "\n"
        + _block("client token files")
        + "\nWrite-Output \"CLAUDE=$claudeTokenFile\"\nWrite-Output \"GEMINI=$geminiTokenFile\"\n"
        + "Write-Output \"LEFT=$env:PSEUDOLIFE_INSTALLER_TOKEN$env:PSEUDOLIFE_INSTALLER_TOKENS\"\n",
        extra_env=extra_env)


def test_claude_and_gemini_get_owner_only_token_files(tmp_path):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n", encoding="utf-8")
    proc = _client_files(ps, env_file, ["claude", "gemini"])
    assert proc.returncode == 0, _output(proc)
    assert "fixture-singular" not in _output(proc)
    assert re.search(r"(?m)^LEFT=\r?$", proc.stdout)
    for principal in ("claude-code", "gemini"):
        target = ps.home / ".pseudolife-mcp" / f"{principal}.token"
        assert CredentialProvider(path=target).snapshot().token == "fixture-singular"
        assert f"{'CLAUDE' if principal == 'claude-code' else 'GEMINI'}={target}" in proc.stdout


def test_token_map_entry_wins_for_its_principal(tmp_path):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n"
                        "PSEUDOLIFE_MCP_TOKENS=fixture-mapped:claude-code\n", encoding="utf-8")
    proc = _client_files(ps, env_file, ["claude"])
    assert proc.returncode == 0, _output(proc)
    assert "fixture-mapped" not in _output(proc)
    target = ps.home / ".pseudolife-mcp" / "claude-code.token"
    assert CredentialProvider(path=target).snapshot().token == "fixture-mapped"


def test_tokenless_install_writes_no_client_files(tmp_path):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("#PSEUDOLIFE_MCP_TOKEN=\n", encoding="utf-8")
    proc = _client_files(ps, env_file, ["claude", "gemini"], hook="plugin")
    assert proc.returncode == 0, _output(proc)
    assert re.search(r"(?m)^CLAUDE=\r?$", proc.stdout)
    assert re.search(r"(?m)^GEMINI=\r?$", proc.stdout)
    assert not (ps.home / ".pseudolife-mcp").exists()
    assert not (ps.home / ".claude" / "settings.json").exists()


def test_plugin_hooks_get_the_token_file_through_claude_settings(tmp_path):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n", encoding="utf-8")
    settings = ps.home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"enabledPlugins": {"x": True}}), encoding="utf-8")
    proc = _client_files(ps, env_file, ["claude"], hook="plugin")
    assert proc.returncode == 0, _output(proc)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["enabledPlugins"] == {"x": True}
    assert Path(data["env"]["PSEUDOLIFE_MCP_TOKEN_FILE"]) == (
        ps.home / ".pseudolife-mcp" / "claude-code.token")
    assert data["env"]["PSEUDOLIFE_MCP_DAEMON_URL"] == "http://127.0.0.1:8765"


def test_plugin_hooks_keep_a_token_the_user_environment_supplies(tmp_path):
    ps = _PowerShell(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("PSEUDOLIFE_MCP_TOKEN=fixture-singular\n", encoding="utf-8")
    proc = _client_files(ps, env_file, ["claude"], hook="plugin",
                         extra_env={"PSEUDOLIFE_MCP_TOKEN": "fixture-singular"})
    assert proc.returncode == 0, _output(proc)
    assert not (ps.home / ".claude" / "settings.json").exists()


# -- registrations ------------------------------------------------------------

def _wire(ps: _PowerShell, client: str, *, token_file: str, existing: str | None = None,
          claude_json: Path | None = None, state_dir: str = r"C:\fixture\claude-code-agents",
          ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the client-wiring loop with fake claude/gemini/pipx commands."""
    installed_bin = ps.tmp / "installed-bin"
    installed_bin.mkdir()
    shim = installed_bin / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(0o755)
    call_log = ps.tmp / "calls.txt"
    fakes = []
    for name, query in (("claude", "get"), ("gemini", "list")):
        fakes.append(f"""function global:{name} {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{_q(call_log)}' -Value ('{name}|' + ($callArgs -join ' '))
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq 'add') -and ($callArgs -contains '--help')) {{
        Write-Output '  -e, --env <env...>'
        $global:LASTEXITCODE = 0
        return
    }}
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq '{query}')) {{
        if ($env:FAKE_CONFIG) {{ Write-Output $env:FAKE_CONFIG; $global:LASTEXITCODE = 0; return }}
        $global:LASTEXITCODE = 1
        return
    }}
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq 'add')) {{ $global:LASTEXITCODE = 0; return }}
    $global:LASTEXITCODE = 91
}}""")
    helper = _between(INSTALL, "function Resolve-InstalledShimPath",
                      "\n}\n\n# Two env pairs") + "\n}"
    loop = _between(INSTALL,
                    'foreach ($selectedClient in $clients) {\n    if ($selectedClient -eq "claude-desktop") {',
                    "\n}\n\n# -- 12. health") + "\n}"
    gemini_file = token_file.replace("claude-code", "gemini") if token_file else ""
    config = ("pseudolife-memory\n" + existing) if existing else ""
    body = f"""$env:PATH = '{_q(installed_bin)}' + [IO.Path]::PathSeparator + $env:PATH
$env:FAKE_CONFIG = '{_q(config)}'
$clients = @('{client}')
$Transport = 'shim'
$codexCredentialBootstrapFailed = $false
$codexConnectionConfigured = $false
$codexCredentialFile = $null
$codexCredentialUrl = $null
$codexRuntimeDefaults = $null
$mcpState = @{{}}
$script:shimInstallResult = $null
$script:shimInstallPath = $null
$script:shimUpgradeHeld = $null
$claudeTokenFile = '{_q(token_file)}'
$geminiTokenFile = '{_q(gemini_file)}'
$clientDaemonUrl = 'http://127.0.0.1:8765'
$claudeAgentStateDir = '{_q(state_dir)}'
$claudeJson = '{_q(claude_json) if claude_json else ""}'
function Set-CodexRuntimeDefaults {{ $script:codexRuntimeDefaults = 'preserved' }}
{chr(10).join(fakes)}
function global:pipx {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{_q(call_log)}' -Value ('pipx|' + ($callArgs -join ' '))
    if ($callArgs[0] -eq 'environment') {{ Write-Output '{_q(installed_bin)}' }}
    $global:LASTEXITCODE = 0
}}
{helper}
function Get-ShimProcessTable {{ $null }}
{_function("Register-Result($provider, $okState, $okMessage)")}
{loop}
Write-Output ("STATE=" + $mcpState['{client}'])
"""
    proc = ps.run(body)
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return proc, calls


def _adds(calls: list[str]) -> list[str]:
    return [call for call in calls if "|mcp add" in call and "--help" not in call]


def test_fresh_claude_code_registration_carries_the_board_credential(tmp_path):
    ps = _PowerShell(tmp_path)
    proc, calls = _wire(ps, "claude", token_file=r"C:\fixture\claude-code.token")
    assert proc.returncode == 0, _output(proc)
    [add] = _adds(calls)
    assert add.startswith("claude|mcp add --scope user pseudolife-memory --env ")
    for pair in ("PSEUDOLIFE_WRITER_ID=claude-code", "PSEUDOLIFE_MCP_NO_SPAWN=1",
                 r"PSEUDOLIFE_MCP_TOKEN_FILE=C:\fixture\claude-code.token",
                 "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765",
                 r"PSEUDOLIFE_AGENT_STATE_DIR=C:\fixture\claude-code-agents"):
        assert f" {pair} " in add
    assert "STATE=shim-env" in proc.stdout


def test_tokenless_claude_code_registration_is_unchanged(tmp_path):
    ps = _PowerShell(tmp_path)
    proc, calls = _wire(ps, "claude", token_file="")
    assert proc.returncode == 0, _output(proc)
    [add] = _adds(calls)
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in add
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in add
    assert "PSEUDOLIFE_AGENT_STATE_DIR" not in add
    assert "STATE=shim-env" in proc.stdout


def test_fresh_gemini_registration_carries_the_token_file(tmp_path):
    ps = _PowerShell(tmp_path)
    proc, calls = _wire(ps, "gemini", token_file=r"C:\fixture\claude-code.token")
    assert proc.returncode == 0, _output(proc)
    [add] = _adds(calls)
    assert r"-e PSEUDOLIFE_MCP_TOKEN_FILE=C:\fixture\gemini.token " in add
    assert "-e PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765 " in add
    assert "-e PSEUDOLIFE_MCP_NO_SPAWN=1 " in add
    assert "STATE=shim-env" in proc.stdout


def test_tokenless_gemini_registration_is_unchanged(tmp_path):
    ps = _PowerShell(tmp_path)
    proc, calls = _wire(ps, "gemini", token_file="")
    assert proc.returncode == 0, _output(proc)
    [add] = _adds(calls)
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" not in add
    assert "-e PSEUDOLIFE_MCP_NO_SPAWN=1 " in add


def _existing_claude_json(ps: _PowerShell) -> Path:
    claude_json = ps.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": "pseudolife-mcp", "args": [],
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}),
        encoding="utf-8")
    return claude_json


def test_existing_claude_code_registration_gains_the_credential_in_place(tmp_path):
    ps = _PowerShell(tmp_path)
    claude_json = _existing_claude_json(ps)
    existing = "Type: stdio\nCommand: pseudolife-mcp\nEnvironment:\n  PSEUDOLIFE_MCP_NO_SPAWN=1"
    token_file = ps.home / ".pseudolife-mcp" / "claude-code.token"
    state_dir = ps.home / ".pseudolife-mcp" / "claude-code-agents"
    proc, calls = _wire(ps, "claude", token_file=str(token_file), existing=existing,
                        claude_json=claude_json, state_dir=str(state_dir))
    assert proc.returncode == 0, _output(proc)
    assert _adds(calls) == []
    assert not any("mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in proc.stdout
    env = json.loads(claude_json.read_text(encoding="utf-8"))["mcpServers"][
        "pseudolife-memory"]["env"]
    assert Path(env["PSEUDOLIFE_MCP_TOKEN_FILE"]) == token_file
    assert Path(env["PSEUDOLIFE_AGENT_STATE_DIR"]) == state_dir
    assert env["PSEUDOLIFE_MCP_DAEMON_URL"] == "http://127.0.0.1:8765"
    assert env["PSEUDOLIFE_MCP_NO_SPAWN"] == "1"
    assert "restart" in proc.stdout.lower()
    assert "carries none" not in _output(proc)


def test_existing_custom_claude_code_registration_gets_the_manual_fix(tmp_path):
    ps = _PowerShell(tmp_path)
    claude_json = _existing_claude_json(ps)
    before = claude_json.read_text(encoding="utf-8")
    existing = ("Type: stdio\nCommand: private-bridge\nEnvironment:\n"
                "  PSEUDOLIFE_MCP_NO_SPAWN=1")
    proc, calls = _wire(ps, "claude", token_file=r"C:\fixture\claude-code.token",
                        existing=existing, claude_json=claude_json)
    assert proc.returncode == 0, _output(proc)
    assert _adds(calls) == []
    assert not any("mcp remove" in call for call in calls)
    assert claude_json.read_text(encoding="utf-8") == before
    assert "carries none" in _output(proc)
    assert r"PSEUDOLIFE_MCP_TOKEN_FILE=C:\fixture\claude-code.token" in _output(proc)


def test_existing_gemini_registration_gets_the_manual_fix(tmp_path):
    # `gemini mcp list` hides env values, so the installer cannot tell whether
    # the registration carries a token: it leaves settings.json alone and
    # says how to add one.
    ps = _PowerShell(tmp_path)
    settings = ps.home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "command": "pseudolife-mcp", "env": {"PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}),
        encoding="utf-8")
    before = settings.read_text(encoding="utf-8")
    proc, calls = _wire(ps, "gemini", token_file=r"C:\fixture\claude-code.token",
                        existing="pseudolife-memory: pseudolife-mcp (stdio) - Connected")
    assert proc.returncode == 0, _output(proc)
    assert _adds(calls) == []
    assert not any("mcp remove" in call for call in calls)
    assert settings.read_text(encoding="utf-8") == before
    assert "Edit it in place in ~/.gemini/settings.json" in _output(proc)
    assert r"PSEUDOLIFE_MCP_TOKEN_FILE=C:\fixture\gemini.token" in _output(proc)


def test_tokenless_existing_gemini_registration_gets_no_token_warning(tmp_path):
    ps = _PowerShell(tmp_path)
    proc, calls = _wire(ps, "gemini", token_file="",
                        existing="pseudolife-memory: pseudolife-mcp (stdio) - Connected")
    assert proc.returncode == 0, _output(proc)
    assert "Edit it in place in ~/.gemini/settings.json" not in _output(proc)


# -- the board line -----------------------------------------------------------

def _board(ps: _PowerShell, *, token_state: str, mcp_state: str = "@{}"):
    return ps.run(
        "$claudeTokenFile = $null; $geminiTokenFile = $null; $codexCredentialFile = $null\n"
        f"$clientDaemonUrl = 'http://127.0.0.1:1'; $tokenState = '{token_state}'\n"
        f"$mcpState = {mcp_state}\n"
        "function Get-DesktopTokenSource { $null }\n" + _block("board line"))


def test_final_ladder_prints_one_board_line(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _board(ps, token_state="opted-out")
    assert proc.returncode == 0, _output(proc)
    [line] = [line for line in proc.stdout.splitlines() if "Agent board" in line]
    assert re.fullmatch(r"\s*\[!\] Agent board\s+off - no bearer token.*-NoToken.*", line)
    assert "HTTP registration" not in _output(proc)


def test_board_line_names_missing_shim_tooling(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _board(ps, token_state="no-shim")
    assert proc.returncode == 0, _output(proc)
    [line] = [line for line in proc.stdout.splitlines() if "Agent board" in line]
    assert "no pipx or pip-capable Python >= 3.10 to install the shim" in line


def test_board_line_warns_when_a_gated_daemon_got_an_http_registration(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _board(ps, token_state="minted", mcp_state="@{ claude = 'http' }")
    assert proc.returncode == 0, _output(proc)
    assert "HTTP registration sends no bearer token" in _output(proc)
