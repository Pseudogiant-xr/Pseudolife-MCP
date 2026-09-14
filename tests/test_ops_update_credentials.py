"""Deploys use explicit file authentication without changing the caller's environment."""

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ops_harness import BASH, PWSH, hermetic_env


AUTH_FILES = {
    "both": "PSEUDOLIFE_MCP_TOKEN=\nPSEUDOLIFE_MCP_TOKENS=fixture:replacement\n",
    "singular": "PSEUDOLIFE_MCP_TOKEN=fixture-replacement\n",
    "map": "PSEUDOLIFE_MCP_TOKENS=fixture:replacement\n",
    "none": "# PSEUDOLIFE_MCP_TOKEN=ignored\n",
}


@pytest.mark.skipif(PWSH is None or sys.platform != "win32",
                   reason="Windows native command environment regression")
@pytest.mark.parametrize("parent_state", ["absent", "empty", "value", "mixed-empty", "mixed-value"])
@pytest.mark.parametrize("compose_fails", [True, False])
def test_update_ps1_native_child_gets_absent_auth_and_parent_state_is_restored(
        tmp_path, parent_state, compose_fails):
    ops = tmp_path / "ops"
    ops.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "ops/update.ps1", ops / "update.ps1")
    (ops / "docker-compose.yml").write_text("image: pseudolife-daemon:0.1.0\n")
    (ops / "prune-rollbacks.ps1").write_text("param($Keep, $Repository)\n")
    (ops / ".env").write_text(AUTH_FILES["map"])

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    observation = tmp_path / "native-child.json"
    helper = fake_bin / "docker_helper.py"
    helper.write_text('''
import json
import os
import sys

if sys.argv[1:2] == ["compose"]:
    names = ("PSEUDOLIFE_MCP_TOKEN", "PSEUDOLIFE_MCP_TOKENS")
    observed = {name: {"present": name in os.environ, "value": os.environ.get(name)}
                for name in names}
    with open(os.environ["FIXTURE_OBSERVATION"], "w", encoding="utf-8") as handle:
        json.dump(observed, handle)
    raise SystemExit(int(os.environ["FIXTURE_COMPOSE_FAILS"]))

print("same-image")
''', encoding="utf-8")
    (fake_bin / "docker.cmd").write_text(
        f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")

    result = tmp_path / "result.json"
    driver = tmp_path / "driver.ps1"
    if parent_state == "absent":
        establish_parent = """
Remove-Item -LiteralPath Env:\\PSEUDOLIFE_MCP_TOKEN -ErrorAction SilentlyContinue
Remove-Item -LiteralPath Env:\\PSEUDOLIFE_MCP_TOKENS -ErrorAction SilentlyContinue
"""
    elif parent_state == "empty":
        establish_parent = """
[Environment]::SetEnvironmentVariable('PSEUDOLIFE_MCP_TOKEN', '', 'Process')
[Environment]::SetEnvironmentVariable('PSEUDOLIFE_MCP_TOKENS', '', 'Process')
"""
    elif parent_state.startswith("mixed-"):
        fixture_token = "" if parent_state == "mixed-empty" else "fixture-old"
        fixture_map = "" if parent_state == "mixed-empty" else "fixture:old-map"
        establish_parent = f"""
[Environment]::SetEnvironmentVariable('pseudolife_mcp_token', '{fixture_token}', 'Process')
[Environment]::SetEnvironmentVariable('PseudoLife_Mcp_Tokens', '{fixture_map}', 'Process')
"""
    else:
        establish_parent = """
$env:PSEUDOLIFE_MCP_TOKEN = 'fixture-old'
$env:PSEUDOLIFE_MCP_TOKENS = 'fixture:old-map'
"""
    driver.write_text(f'''
$env:PATH = '{fake_bin};' + $env:PATH
$env:FIXTURE_OBSERVATION = '{observation}'
$env:FIXTURE_COMPOSE_FAILS = '{int(compose_fails)}'
{establish_parent}
$failed = $false
$global:healthObserved = $false
function global:Invoke-RestMethod {{
    $global:healthObserved = $true
    @{{status='ok';schema=40;persist_errors=0}}
}}
try {{ & '{ops / "update.ps1"}' -NoBackup -NoCachePrune -Tag fixture }}
catch {{ $failed = $true }}
@{{
    failed = $failed
    healthObserved = $global:healthObserved
    tokenPresent = Test-Path -LiteralPath Env:\\PSEUDOLIFE_MCP_TOKEN
    tokenName = (Get-Item -LiteralPath Env:\\PSEUDOLIFE_MCP_TOKEN -ErrorAction SilentlyContinue).Name
    token = [Environment]::GetEnvironmentVariable('PSEUDOLIFE_MCP_TOKEN', 'Process')
    mapPresent = Test-Path -LiteralPath Env:\\PSEUDOLIFE_MCP_TOKENS
    mapName = (Get-Item -LiteralPath Env:\\PSEUDOLIFE_MCP_TOKENS -ErrorAction SilentlyContinue).Name
    map = [Environment]::GetEnvironmentVariable('PSEUDOLIFE_MCP_TOKENS', 'Process')
}} | ConvertTo-Json | Set-Content -LiteralPath '{result}' -Encoding utf8
''', encoding="utf-8")

    client_home = tmp_path / "client-home"
    client_home.mkdir()
    env = hermetic_env(
        CODEX_HOME=client_home,
        PSEUDOLIFE_MCP_TOKEN=None,
        PSEUDOLIFE_MCP_TOKENS=None,
    )
    completed = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        env=env, capture_output=True, timeout=30,
    )
    assert completed.returncode == 0
    assert json.loads(observation.read_text(encoding="utf-8")) == {
        "PSEUDOLIFE_MCP_TOKEN": {"present": False, "value": None},
        "PSEUDOLIFE_MCP_TOKENS": {"present": False, "value": None},
    }
    restored = json.loads(result.read_text(encoding="utf-8-sig"))
    expected_present = parent_state != "absent"
    assert restored == {
        "failed": compose_fails,
        "healthObserved": not compose_fails,
        "tokenPresent": expected_present,
        "tokenName": ("pseudolife_mcp_token" if parent_state.startswith("mixed-")
                      else "PSEUDOLIFE_MCP_TOKEN") if expected_present else None,
        "token": {"absent": None, "empty": "", "value": "fixture-old",
                  "mixed-empty": "", "mixed-value": "fixture-old"}[parent_state],
        "mapPresent": expected_present,
        "mapName": ("PseudoLife_Mcp_Tokens" if parent_state.startswith("mixed-")
                    else "PSEUDOLIFE_MCP_TOKENS") if expected_present else None,
        "map": {"absent": None, "empty": "", "value": "fixture:old-map",
                "mixed-empty": "", "mixed-value": "fixture:old-map"}[parent_state],
    }


@pytest.mark.skipif(PWSH is None, reason="PowerShell unavailable")
@pytest.mark.parametrize("file_keys", ["both", "singular", "map", "none"])
@pytest.mark.parametrize("compose_fails", [True, False])
def test_update_auth_environment_is_scoped(tmp_path, file_keys, compose_fails):
    ops = tmp_path / "ops"
    ops.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / "ops/update.ps1", ops / "update.ps1")
    (ops / "docker-compose.yml").write_text("image: pseudolife-daemon:0.1.0\n")
    (ops / "prune-rollbacks.ps1").write_text("param($Keep, $Repository)\n")
    (ops / ".env").write_text(AUTH_FILES[file_keys])
    result = tmp_path / "result.json"
    driver = tmp_path / "driver.ps1"
    driver.write_text(f'''
$env:PSEUDOLIFE_MCP_TOKEN = 'fixture-old'
$env:PSEUDOLIFE_MCP_TOKENS = 'fixture:old-map'
$global:composeObserved = $false
function global:docker {{
    if ($args[0] -eq 'compose') {{
        $global:composeObserved = $true
        $global:tokenCleared = [string]::IsNullOrEmpty($env:PSEUDOLIFE_MCP_TOKEN)
        $global:mapCleared = [string]::IsNullOrEmpty($env:PSEUDOLIFE_MCP_TOKENS)
        $global:LASTEXITCODE = {1 if compose_fails else 0}
        return
    }}
    $global:LASTEXITCODE = 0
    return 'same-image'
}}
function global:Invoke-RestMethod {{ @{{status='ok';schema=40;persist_errors=0}} }}
$failed = $false
try {{ & '{ops / "update.ps1"}' -NoBackup -NoCachePrune -Tag fixture }}
catch {{ $failed = $true }}
@{{ observed=$global:composeObserved; cleared=$global:tokenCleared;
   mapCleared=$global:mapCleared; failed=$failed;
   restored=($env:PSEUDOLIFE_MCP_TOKEN -eq 'fixture-old' -and
             $env:PSEUDOLIFE_MCP_TOKENS -eq 'fixture:old-map')
}} | ConvertTo-Json | Set-Content '{result}'
''', encoding="utf-8")
    completed = subprocess.run([PWSH, "-NoProfile", "-File", str(driver)],
                               env=hermetic_env(), capture_output=True, timeout=30)
    assert completed.returncode == 0
    assert json.loads(result.read_text(encoding="utf-8-sig")) == {
        "observed": True, "cleared": file_keys != "none", "mapCleared": file_keys != "none",
        "restored": True, "failed": compose_fails,
    }


@pytest.mark.skipif(BASH is None, reason="Bash unavailable")
@pytest.mark.parametrize("file_keys", AUTH_FILES)
@pytest.mark.parametrize("compose_fails", [True, False])
def test_update_sh_auth_environment_is_scoped(tmp_path, file_keys, compose_fails):
    ops = tmp_path / "ops"
    ops.mkdir()
    source = Path(__file__).resolve().parents[1] / "ops/update.sh"
    (ops / "update.sh").write_text(source.read_text(), newline="\n")
    (ops / "docker-compose.yml").write_text("image: pseudolife-daemon:0.1.0\n")
    (ops / "prune-rollbacks.sh").write_text("#!/usr/bin/env bash\nexit 0\n", newline="\n")
    (ops / "prune-rollbacks.sh").chmod(0o700)
    (ops / ".env").write_text(AUTH_FILES[file_keys], newline="\n")
    observed = tmp_path / "observed.txt"
    result = tmp_path / "result.txt"
    driver = tmp_path / "driver.sh"
    driver.write_text(f'''
export PSEUDOLIFE_MCP_TOKEN=fixture-old
export PSEUDOLIFE_MCP_TOKENS=fixture:old-map
docker() {{
    if [ "$1" = compose ]; then
        if [ -z "${{PSEUDOLIFE_MCP_TOKEN:-}}" ] && [ -z "${{PSEUDOLIFE_MCP_TOKENS:-}}" ]; then
            echo cleared > {shlex.quote(observed.as_posix())}
        else
            echo inherited > {shlex.quote(observed.as_posix())}
        fi
        return {1 if compose_fails else 0}
    fi
    echo same-image
}}
curl() {{ echo '{{"status":"ok"}}'; }}
export -f docker curl
bash {shlex.quote((ops / "update.sh").as_posix())} --no-backup --no-cache-prune --tag fixture
ec=$?
if [ "$PSEUDOLIFE_MCP_TOKEN" = fixture-old ] && [ "$PSEUDOLIFE_MCP_TOKENS" = fixture:old-map ]; then
    echo "$ec restored" > {shlex.quote(result.as_posix())}
fi
''', newline="\n")
    completed = subprocess.run([BASH, str(driver)], env=hermetic_env(),
                               capture_output=True, timeout=30)
    assert completed.returncode == 0
    assert observed.read_text().strip() == ("inherited" if file_keys == "none" else "cleared")
    assert result.read_text().strip() == f"{1 if compose_fails else 0} restored"
