"""The native installer (ops/install-native.*): the tests of test_installer_board_token_ps.py
that the native client path changed, run against the native installer.

test_installer_board_token_ps.py keeps the default (Python client) installer's tests.
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

from tests import native_client_stub as native_stub

from pseudolife_memory.credentials import CredentialProvider
from tests.test_installer_existing_upgrade import _between, _fixture_env


ROOT = Path(__file__).resolve().parents[1]


INSTALL = "ops/install-native.ps1"


def _function(name: str) -> str:
    return _between(INSTALL, f"function {name}", "\n}\n") + "\n}"


def _q(value: str | Path) -> str:
    return str(value).replace("'", "''")


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
$env:FAKE_LAUNCHER = '{_q(shim)}'
$env:FAKE_KEEP_LAUNCHER = '1'
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
{helper}
{native_stub.POWERSHELL}
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
