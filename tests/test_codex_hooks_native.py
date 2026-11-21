"""The native installer (ops/install-native.*): the tests of test_codex_hooks.py
that the native client path changed, run against the native installer.

test_codex_hooks.py keeps the default (Python client) installer's tests.
"""
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

from tests import native_client_stub as native_stub

ROOT = Path(__file__).resolve().parents[1]


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


def bash_exe():
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
    return bash


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
def test_installer_codex_setup_that_cannot_run_reports_unavailable(
        tmp_path, shell, trust, instructions, expected, existing):
    """When the native client's Codex steps fail outright, Codex is reported
    unavailable with its recovery, nothing under the real home is touched,
    and the standing-instruction fallback still honours the consent given."""
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
                                  helper_fails=True)
    assert f"RESULT:unavailable:{expected}" in result
    assert "Codex credential setup failed" in result
    assert "Install Python" not in result
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
        " print(json.dumps({'status':'needs-configuration','recovery':'Fixture credential refusal. Correct the fixture and retry.'}))\n"
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
    text = (ROOT / "ops/install-native.sh").read_text(encoding="utf-8")
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
        + (native_stub.NATIVE_FROM_SHIM_BIN_SH
           if shim_available else native_stub.NATIVE_UNAVAILABLE_SH)
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
        assert "Fixture credential refusal. Correct the fixture and retry." in stdout
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
    print(json.dumps({'status':'needs-configuration','recovery':'Fixture credential refusal. Correct the fixture and retry.'}))
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
    source = (ROOT / "ops/install-native.ps1").read_text(encoding="utf-8")
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
{native_stub.NATIVE_FROM_SHIM_BIN_PS}
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
        assert "Fixture credential refusal. Correct the fixture and retry." in completed.stdout.decode()
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
        executable = bash_exe()
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

    source = (ROOT / ("ops/install-native.sh" if shell == "bash" else
                      "ops/install-native.ps1")).read_text(encoding="utf-8")
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
            + native_stub.NATIVE_FROM_SHIM_BIN_SH +
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
{native_stub.NATIVE_FROM_SHIM_BIN_PS}
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
        executable = bash_exe()
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

    source = (ROOT / ("ops/install-native.sh" if shell == "bash" else
                      "ops/install-native.ps1")).read_text(encoding="utf-8")
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
            + native_stub.NATIVE_FROM_SHIM_BIN_SH +
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
{native_stub.NATIVE_FROM_SHIM_BIN_PS}
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


def run_installer_stages(tmp_path, shell, repo, env, source, trust, instructions,
                         helper_fails=False, launch_throws=False):
    """Run sections 9 and 10 of an installer with the native client's
    installer steps answered by the stand-in helper (the Python helpers in
    ``repo``). ``helper_fails``: every native step exits 1 with no output.
    ``launch_throws`` (PowerShell): the credential step answers tokenless and
    launching the hook step throws."""
    if shell == "powershell":
        text = (ROOT / "ops/install-native.ps1").read_text(encoding="utf-8")
        stages = text.split("# -- 9. session lifecycle hooks", 1)[1].split(
            "# -- 11. wire into selected MCP clients", 1)[0].split("\n", 1)[1]
        if helper_fails:
            helper = "function Invoke-NativeHelper { $global:LASTEXITCODE = 1 }\n"
        elif launch_throws:
            helper = (
                "function Invoke-NativeHelper {\n"
                "  if ($args[0] -eq 'codex-credentials') {\n"
                "    $global:LASTEXITCODE = 0\n"
                "    Write-Output '{\"status\":\"tokenless\",\"credential_file_configured\":false,\"connection_configured\":false}'\n"
                "    return\n"
                "  }\n"
                "  throw 'fixture launch failure'\n"
                "}\n")
        else:
            helper = native_stub.HELPER_PS
        script = tmp_path / "stages.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$repo = '{repo.as_posix()}'\n$clients = @('codex')\n"
            f"$CodexHooks = '{source}'\n$CodexHookTrust = '{trust}'\n"
            f"$Instructions = '{instructions}'\n$interactive = $false\n"
            "function Step($text) { Write-Host $text }\n"
            "function Get-EnvValue($name) { return $null }\n"
            "function Read-Host($text) { throw 'Unexpected second prompt' }\n"
            + helper
            + stages + "\nWrite-Output ('RESULT:' + $hookState['codex'] + ':' + $instrState['codex'])\n",
            encoding="utf-8")
        return pwsh_run("-File", script, env=env).stdout
    # bash_exe() skips the native-Windows System32 WSL launcher for Git Bash.
    bash = bash_exe()
    text = (ROOT / "ops/install-native.sh").read_text(encoding="utf-8")
    parts = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)^# [^\n]*11\. wire into selected MCP clients", text)
    assert parts, "installer stage boundaries changed"
    script = tmp_path / "stages.sh"
    script.write_text(
        "set -euo pipefail\n"
        f"repo={shlex.quote(repo.as_posix())}\n"
        f"CLIENTS=codex\nCODEX_HOOKS={source}\nCODEX_HOOK_TRUST={trust}\nINSTRUCTIONS={instructions}\n"
        "CLAUDE_MD=''\nAGENTS_FILE=''\nstep() { echo \"$*\"; }\n"
        "get_env() { return 0; }\n"
        + ("native_helper() { return 1; }\n" if helper_fails else native_stub.HELPER_SH)
        + parts[1] + '\nprintf "RESULT:%s:%s\\n" "$HOOK_CODEX" "$INSTR_CODEX"\n',
        encoding="utf-8")
    result = subprocess.run([bash, script.as_posix()], env=env, capture_output=True,
                            timeout=HOOK_PROCESS_TIMEOUT, check=True)
    return result.stdout.decode("utf-8")


DOCKER_BRIEFING = "docker exec pseudolife-mcp-daemon pseudolife-mcp briefing --hook-json"


def _install_hooks(shell, client, settings, command):
    # A disposable home beside the settings, so neither installer reads or
    # runs anything of the developer's; install-hook-native.sh edits through the
    # native client (the real one, or the stand-in running its old heredocs).
    from tests import installer_sandbox
    from tests import native_client_stub as native_stub
    root = Path(settings).parent.parent
    keep = ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "COMSPEC", "PATHEXT")
    env = installer_sandbox.environment(root / "hook-home", {k: v for k, v in os.environ.items() if k in keep})
    env["PSEUDOLIFE_NATIVE_CLI"] = native_stub.native_cli(root / "hook-cli").as_posix()
    if shell == "bash":
        installer_sandbox.run([bash_exe(), str(ROOT / "ops/install-hook-native.sh"), "--client", client,
                               settings.as_posix(), command], env=installer_sandbox.layout(env, root), root=root,
                              capture_output=True, text=True, timeout=HOOK_PROCESS_TIMEOUT, check=True)
    else:
        installer_sandbox.require(env, root)
        pwsh_run("-File", ROOT / "ops/install-hook.ps1", "-Client", client,
                 "-SettingsPath", settings, "-Command", command, env=env)


@pytest.mark.parametrize("shell", ["bash", "powershell"])
@pytest.mark.parametrize("client", ["claude", "codex"])
def test_hook_installers_gate_the_coordination_checkin(tmp_path, shell, client):
    """The check-in comes from the daemon, which serves it only where the
    board works; an older install's unconditional echo is replaced in place
    and nothing else is touched (2026-09-25)."""
    legacy_line = re.search(r'^COORDINATION_LINE="(.*)"$',
                            (ROOT / "ops/install-hook-native.sh").read_text(encoding="utf-8"), re.M)[1]
    legacy = {"type": "command", "command": f"echo '{legacy_line}'"}
    if client == "codex":
        legacy.update(commandWindows=f"Write-Output '{legacy_line}'", timeout=5)
    unrelated = {"type": "command", "command": "echo user-owned"}
    # A user's own hook that merely mentions the phrase is not ours to remove.
    lookalike = {"type": "command", "command": "echo 'Pseudolife coordination: my own reminder'"}
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [legacy, unrelated, lookalike]}]}}), encoding="utf-8")
    for _ in range(2):
        _install_hooks(shell, client, settings, DOCKER_BRIEFING)
    starts = [h for g in json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
              for h in g["hooks"]]
    commands = [h["command"] for h in starts]
    gated = DOCKER_BRIEFING + " --coordination"
    assert commands.count(gated) == 1
    assert commands.count(DOCKER_BRIEFING) == 1
    assert legacy["command"] not in commands
    assert unrelated in starts and lookalike in starts
    if client == "codex" and shell == "powershell":  # the Windows override
        hook = next(h for h in starts if h["command"] == gated)
        assert hook["commandWindows"] == gated and hook["timeout"] == 5


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_hook_installers_add_a_missing_briefing_beside_the_checkin(tmp_path, shell):
    """The check-in command also names `pseudolife-mcp briefing`; it must
    not pass for the memory briefing itself."""
    settings = tmp_path / "settings.json"
    gated = "pseudolife-mcp briefing --hook-json --coordination"
    settings.write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command", "command": gated}]}]}}), encoding="utf-8")
    _install_hooks(shell, "claude", settings, "pseudolife-mcp briefing --hook-json")
    commands = [h["command"] for g in json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
                for h in g["hooks"]]
    assert sorted(commands) == ["pseudolife-mcp briefing --hook-json", gated]


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_hook_installers_fresh_install_gets_one_briefing_and_one_checkin(tmp_path, shell):
    settings = tmp_path / "settings.json"
    for _ in range(2):
        _install_hooks(shell, "claude", settings, "pseudolife-mcp briefing --hook-json")
    commands = [h["command"] for g in json.loads(settings.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
                for h in g["hooks"]]
    assert commands == ["pseudolife-mcp briefing --hook-json",
                        "pseudolife-mcp briefing --hook-json --coordination"]
