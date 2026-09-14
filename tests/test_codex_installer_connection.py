"""Windows installer stages keep one connection through hook and MCP setup."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from pseudolife_memory.credentials import CredentialProvider, _write_token_file


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("shim_available", [True, False])
def test_powershell_fresh_install_keeps_connection_across_stages(tmp_path, shim_available):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 unavailable")
    repo = tmp_path / "repo"
    (repo / "ops").mkdir(parents=True)
    (repo / "examples").mkdir()
    shutil.copyfile(ROOT / "examples/CLAUDE.memory.md", repo / "examples/CLAUDE.memory.md")
    (repo / "ops/setup-codex-coordination.py").write_text('''
import json, os, sys
from pathlib import Path
from pseudolife_memory.credentials import _write_token_file
if '--runtime-defaults' in sys.argv:
    Path(os.environ['FIXTURE_RUNTIME']).write_text('verified')
    print(json.dumps({'status':'ready','runtime_defaults':'configured'}))
    raise SystemExit(0)
assert sys.argv[1:]==['--credentials','--installer-token-stdin',
                      '--installer-daemon-url','http://127.0.0.1:9876']
token=sys.stdin.read().strip()
assert token=='fixture-installer'
assert all(key in os.environ for key in (
    'PSEUDOLIFE_MCP_TOKEN','PSEUDOLIFE_MCP_TOKEN_FILE','PSEUDOLIFE_MCP_DAEMON_URL'))
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
report={'same_file':path==Path(os.environ['CODEX_HOME'])/'pseudolife'/'token',
 'same_url':os.environ.get('PSEUDOLIFE_MCP_DAEMON_URL')=='http://127.0.0.1:9876',
 'no_literal':'PSEUDOLIFE_MCP_TOKEN' not in os.environ,
 'valid_file':CredentialProvider(path=path).snapshot().token=='fixture-installer'}
Path(os.environ['FIXTURE_HOOK']).write_text(json.dumps(report))
print(json.dumps({'source':'manual','status':'ready',
 'instructions':'covered-by-hooks','recovery':None}))
''')
    source = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    stages = re.search(r"(?ms)^# -- 9\. session lifecycle hooks[^\n]*\n(.*?)^# -- 12\. health", source)[1]
    home = tmp_path / "home"
    ambient = tmp_path / "ambient-token"
    _write_token_file(ambient, "ambient-file")
    marker, calls, result, runtime = (tmp_path / name for name in (
        "hook.json", "calls.json", "result.json", "runtime.txt"))
    driver = tmp_path / "driver.ps1"
    driver.write_text(f'''
$ErrorActionPreference='Stop'
$repo='{repo.as_posix()}'
$clients=@('codex'); $Transport='shim'; $CodexHooks='auto'; $CodexHookTrust='yes'
$Instructions='auto'; $interactive=$false
function Step($text) {{ }}
function Get-EnvValue($name) {{
    if ($name -eq 'PSEUDOLIFE_MCP_TOKEN') {{ return 'fixture-installer' }}
    if ($name -eq 'PSEUDOLIFE_MCP_DAEMON_URL') {{ return 'http://127.0.0.1:9876' }}
    return $null
}}
function Read-Host {{ throw 'unexpected prompt' }}
function python {{ & '{Path(sys.executable).as_posix()}' @args }}
function pipx {{
    $global:LASTEXITCODE={0 if shim_available else 1}
    if ($args[0] -eq 'list') {{ return 'package pseudolife-mcp 1' }}
}}
function codex {{
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'get') {{ $global:LASTEXITCODE=1; return }}
    if ($args -contains '--help') {{ $global:LASTEXITCODE=0; return '--env' }}
    if ($args[0] -eq 'mcp' -and $args[1] -eq 'add') {{
        ConvertTo-Json -InputObject @($args) | Set-Content '{calls.as_posix()}'
        $global:LASTEXITCODE=0; return
    }}
    $global:LASTEXITCODE=1
}}
{stages}
@{{ state=$mcpState['codex'];
 restored=($env:PSEUDOLIFE_MCP_TOKEN -eq 'fixture-literal' -and
 $env:PSEUDOLIFE_MCP_TOKEN_FILE -eq '{ambient.as_posix()}' -and
 $env:PSEUDOLIFE_MCP_DAEMON_URL -eq 'http://127.0.0.1:4321')
}} | ConvertTo-Json | Set-Content '{result.as_posix()}'
''', encoding="utf-8")
    environment = os.environ.copy()
    environment.update(CODEX_HOME=home.as_posix(), HOME=home.as_posix(), USERPROFILE=home.as_posix(),
        PYTHONPATH=str(ROOT), FIXTURE_HOOK=str(marker), FIXTURE_RUNTIME=str(runtime),
        PSEUDOLIFE_MCP_TOKEN="fixture-literal",
        PSEUDOLIFE_MCP_TOKEN_FILE=ambient.as_posix(), PSEUDOLIFE_MCP_DAEMON_URL="http://127.0.0.1:4321")
    completed = subprocess.run([pwsh, "-NoProfile", "-File", str(driver)], env=environment,
                               capture_output=True, timeout=40)
    assert completed.returncode == 0
    assert json.loads(result.read_text(encoding="utf-8-sig")) == {
        "state": "shim-env" if shim_available else "failed", "restored": True}
    assert json.loads(marker.read_text()) == dict(same_file=True, same_url=True, no_literal=True, valid_file=True)
    managed = home / "pseudolife/token"
    assert (CredentialProvider(path=managed).snapshot().token == "fixture-installer") is True
    if shim_available:
        assert runtime.read_text() == "verified"
        arguments = json.loads(calls.read_text(encoding="utf-8-sig"))
        assert "PSEUDOLIFE_MCP_TOKEN_FILE=" + str(managed) in arguments
        assert "PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:9876" in arguments
        assert not any("fixture-literal" in value or "ambient-file" in value or
                       "fixture-installer" in value for value in arguments)
    else:
        assert not calls.exists()
        assert not runtime.exists()
