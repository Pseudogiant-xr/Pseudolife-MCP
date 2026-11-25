"""The native installer (ops/install-native.*): the tests of test_installer_existing_upgrade.py
that the native client path changed, run against the native installer.

test_installer_existing_upgrade.py keeps the default (Python client) installer's tests.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests import installer_sandbox
from tests import native_client_stub as native_stub
from tests.installer_fragments import _bash_fixture_path, _bash_variants, _between, _marker_block
from tests.user_path_guard import _inside, _path_difference, user_path_unchanged


ROOT = Path(__file__).resolve().parents[1]


CLIENTS = ("codex", "claude", "gemini")


VISIBLE_ENV_CLIENTS = ("codex", "claude")


TRUTHY_NO_SPAWN = ("1", "true", "yes", "on")


FALSEY_NO_SPAWN = ("0", "false", "", "*****")


def _fixture_env(tmp_path: Path) -> dict[str, str]:
    """Build a credential-free environment for the disposable client harness."""
    env = {
        name: os.environ[name]
        for name in (
            "COMSPEC", "LANG", "LC_ALL", "PATHEXT", "SystemRoot", "TEMP",
            "TERM", "TMP", "TMPDIR", "WINDIR",
        )
        if name in os.environ
    }
    fixture_home = tmp_path / "home"
    fixture_home.mkdir(parents=True, exist_ok=True)
    env.update({
        "CODEX_HOME": str(fixture_home / "codex"),
        "HOME": str(fixture_home),
        "USERPROFILE": str(fixture_home),
        # Set, so nothing falls back to the system's real folders.
        "LOCALAPPDATA": str(fixture_home / "AppData" / "Local"),
        "APPDATA": str(fixture_home / "AppData" / "Roaming"),
        "XDG_CONFIG_HOME": str(fixture_home / "config"),
    })
    # Every harness built on this environment runs installer code.
    installer_sandbox.require(env, tmp_path)
    return env


def _managed_config(client: str, no_spawn: str = "1") -> str:
    if client == "gemini":
        return "pseudolife-memory: pseudolife-mcp (stdio) - Connected"
    if client == "codex":
        return json.dumps({
            "name": "pseudolife-memory",
            "transport": {
                "type": "stdio",
                "command": "pseudolife-mcp",
                "args": [],
                "env": {"PSEUDOLIFE_MCP_NO_SPAWN": no_spawn},
            },
        }, indent=2)
    return (
        "Type: stdio\nCommand: pseudolife-mcp\nEnvironment:\n"
        f"  PSEUDOLIFE_MCP_NO_SPAWN={no_spawn}\n"
        "  PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765"
    )


def _custom_config(client: str, command: str, no_spawn: str | None = None) -> str:
    if client == "gemini":
        return f"pseudolife-memory: {command} (stdio) - Connected"
    env = ("" if no_spawn is None else
           f"\nEnvironment:\n  PSEUDOLIFE_MCP_NO_SPAWN={no_spawn}")
    return f"transport: stdio\ncommand: {command}\nargs: -m private_bridge{env}"


def _http_config(client: str) -> str:
    if client == "gemini":
        return "pseudolife-memory: http://127.0.0.1:8765/mcp (http) - Connected"
    if client == "codex":
        return json.dumps({
            "name": "pseudolife-memory",
            "transport": {
                "type": "streamable_http",
                "url": "http://127.0.0.1:8765/mcp",
            },
        }, indent=2)
    return (
        "Type: http\nURL: http://127.0.0.1:8765/mcp"
    )


def _lookalike_guard_config(client: str) -> str:
    if client == "codex":
        return json.dumps({
            "name": "pseudolife-memory",
            "transport": {
                "type": "stdio",
                "command": "pseudolife-mcp",
                "args": ["--label=PSEUDOLIFE_MCP_NO_SPAWN=1"],
                "env": {"OTHER_PSEUDOLIFE_MCP_NO_SPAWN": "1"},
            },
        }, indent=2)
    return (
        "Type: stdio\nCommand: pseudolife-mcp\n"
        "Arguments: --label=PSEUDOLIFE_MCP_NO_SPAWN=1\nEnvironment:\n"
        "  OTHER_PSEUDOLIFE_MCP_NO_SPAWN=1"
    )


def _run_bash_existing(
    bash: str, tmp_path: Path, *, client: str, config: str, manager_exit: int,
    shadowed_path: bool = False, existing: bool = True, migrate_exit: int = 3,
    gemini_guard: str = "absent", complete: bool = False, gemini_credential: str = "",
    inherited_credential: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """``manager_exit``: the native client install's result (0 installs);
    ``migrate_exit``: 0 makes the launcher's registration move report a
    move (native_client_stub)."""
    install = (ROOT / "ops" / "install-native.sh").read_text(encoding="utf-8")
    helper = _between("ops/install-native.sh", "resolve_installed_shim() {", "\n}\n\n# Two env pairs") + "\n}"
    loop = "\n".join("\n".join(_marker_block(install, section)) for section in (
        "client registration state", "client registration helpers", "client registrations"))
    completion = "\n".join(_marker_block(install, "client setup result")) if complete else ""
    installer_stub = native_stub.BASH.replace("native_helper() {", "fixture_native_helper() {") + f"""
native_helper() {{
    if [ "$1 $2" = "credentials gemini-guard" ]; then
        [ "$3" = --config ] && [ "$4" = "$HOME/.gemini/settings.json" ] || return 1
        printf '%s\\n' '{{"status":"{gemini_guard}","credential":"{gemini_credential}"}}'
        return 0
    fi
    fixture_native_helper "$@"
}}
"""
    describe = _between("ops/install-native.sh", "describe_mcp() {", "\n}\ndescribe_instr()") + "\n}"
    marker = _between("ops/install-native.sh", "mcp_marker() {", "\n}\ndescribe_mcp()") + "\n}"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    installed_bin = tmp_path / "installed-bin"
    installed_bin.mkdir()
    installed_shim = installed_bin / "pseudolife-mcp"
    installed_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installed_shim.chmod(0o755)
    if shadowed_path:
        old_shim = fake_bin / "pseudolife-mcp"
        old_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        old_shim.chmod(0o755)
    call_log = tmp_path / "calls.txt"
    for name in CLIENTS:
        query = "list" if name == "gemini" else "get"
        script = (
            "#!/bin/sh\n"
            "printf '%s|%s\\n' \"$(basename \"$0\")\" \"$*\" >>\"$CALL_LOG\"\n"
            f"if [ \"$1\" = mcp ] && [ \"$2\" = {query} ]; then\n"
            "  if [ \"$FAKE_EXISTING\" = yes ]; then printf '%s\\n' \"$FAKE_CONFIG\"; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "exit 91\n"
        )
        path = fake_bin / name
        path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    repo = str(ROOT).replace("'", "'\\''")
    fixture_env = _fixture_env(tmp_path / "bash-env")
    if inherited_credential:
        fixture_env["PSEUDOLIFE_MCP_TOKEN"] = "synthetic-inherited"
    fake_bin_shell = _bash_fixture_path(bash, fake_bin, fixture_env).replace("'", "'\\''")
    installed_bin_shell = _bash_fixture_path(bash, installed_bin, fixture_env).replace("'", "'\\''")
    installed_shim_shell = _bash_fixture_path(bash, installed_shim, fixture_env).replace("'", "'\\''")
    call_log_shell = _bash_fixture_path(bash, call_log, fixture_env).replace("'", "'\\''")
    config_shell = ("pseudolife-memory\n" + config).replace("'", "'\\''")
    path_prefix = f"{fake_bin_shell}:{installed_bin_shell}" if shadowed_path else f"{installed_bin_shell}:{fake_bin_shell}"
    script = f"""set -u
PATH='{path_prefix}:/usr/bin:/bin'
export PATH CALL_LOG='{call_log_shell}'
FAKE_CONFIG='{config_shell}'
fixture_shim=$(cygpath -u '{installed_shim_shell}' 2>/dev/null || printf '%s' '{installed_shim_shell}')
FAKE_CONFIG=${{FAKE_CONFIG//__INSTALLED_SHIM__/$fixture_shim}}
  export FAKE_CONFIG FAKE_NATIVE_EXIT='{manager_exit}' FAKE_EXISTING='{"yes" if existing else "no"}'
export FAKE_LAUNCHER='{installed_bin_shell}/pseudolife-mcp' FAKE_MIGRATE_EXIT='{migrate_exit}'
export PSEUDOLIFE_SHIM_RUNTIMES='{installed_bin_shell}/runtimes' PSEUDOLIFE_SHIM_LAUNCHER='{installed_bin_shell}/pseudolife-mcp'
repo='{repo}'
CLIENTS='{client}'
REQUESTED_CLIENTS='{client}'
TRANSPORT=shim
TOKEN_STATE='{"present" if gemini_credential else "opted-out"}'
CODEX_CREDENTIAL_BOOTSTRAP_FAILED=''
CODEX_CONNECTION_CONFIGURED=''
CODEX_CREDENTIAL_FILE=''
CODEX_CREDENTIAL_URL=''
CODEX_RUNTIME_DEFAULTS=''
MCP_CODEX='' MCP_CLAUDE='' MCP_GEMINI=''
SHIM_TRIED='' SHIM_OK='' SHIM_PATH='{installed_shim_shell}'
step() {{ printf 'STEP: %s\\n' "$*"; }}
configure_codex_runtime_defaults() {{ CODEX_RUNTIME_DEFAULTS=preserved; }}
{helper}
{installer_stub}
{marker}
{describe}
{loop}
case '{client}' in
  codex) state="$MCP_CODEX" ;;
  claude) state="$MCP_CLAUDE" ;;
  gemini) state="$MCP_GEMINI" ;;
esac
printf 'STATE=%s\\n' "$state"
printf 'STATUS=%s %s\\n' "$(mcp_marker "$state")" "$(describe_mcp "$state")"
{completion}
"""
    return subprocess.run(
        [bash], input=script.encode(), capture_output=True, check=False, timeout=15,
        env=fixture_env,
    )


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_upgrades_managed_shim_without_rewriting_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client),
        manager_exit=0,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert "native|install" in calls
    assert not any(" mcp add" in call or " mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in output
    assert "[x] already wired; it runs the native client from this release" in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_reports_existing_shim_upgrade_failure_in_status(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client), manager_exit=7,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "[!] registration or shim upgrade FAILED" in output
    assert "registration was preserved" in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_preserves_custom_stdio_registration_and_warns(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_custom_config(client, "/opt/private/pseudolife-mcp"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "launcher|installer migrate-registrations --client " + {"claude": "claude-code"}.get(client, client) in calls
    assert "custom registered command" in output
    assert "/opt/private/pseudolife-mcp" not in output
    assert "STATE=failed" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in output
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_reports_shadowed_bare_shim_without_rewriting_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client, config=_managed_config(client),
        manager_exit=0, shadowed_path=True,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "still resolves to a different executable" in output
    assert "put the installed scripts directory first" in output
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_rerun_upgrades_exact_manager_absolute_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_custom_config(client, "__INSTALLED_SHIM__", "1"), manager_exit=0,
        migrate_exit=4,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "STATE=present-current\n" in output.replace("\r", "")
    assert "registration preserved; it already runs the native client" in output
    assert "custom registered command" not in output


@pytest.mark.parametrize("value", TRUTHY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_accepts_only_effective_no_spawn_values(
    bash: str, tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=present-upgraded" in output
    assert "no-spawn guard is missing or cannot be verified" not in output


@pytest.mark.parametrize("value", FALSEY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_rejects_false_no_spawn_values_without_rewriting(
    bash: str, tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output
    assert not any("mcp remove" in call for call in calls)
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_requires_exact_no_spawn_environment_key(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_lookalike_guard_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_gemini_checks_only_pseudolife_transport(
    bash: str, tmp_path: Path,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client="gemini",
        config="other-server: http://127.0.0.1:9999/mcp (http) - Connected\n"
        + _managed_config("gemini"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_http_registration_needs_no_spawn_guard(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client, config=_http_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=present" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN" not in output
    assert "native|install" not in calls


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_flagless_fresh_cli_fails_before_registration(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client, config="", manager_exit=0, existing=False,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no env flag" in output
    assert "registration was skipped" in output
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert not any("mcp remove" in call for call in calls)


def _run_powershell_existing(
    tmp_path: Path, *, client: str, config: str, manager_exit: int,
    shadowed_path: bool = False, existing: bool = True, migrate_exit: int = 3,
    gemini_guard: str = "absent", complete: bool = False, gemini_credential: str = "",
    inherited_credential: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    """``manager_exit`` and ``migrate_exit`` as for ``_run_bash_existing``."""
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is unavailable")
    helper = _between(
        "ops/install-native.ps1", "function Resolve-InstalledShimPath", "\n}\n\n# Two env pairs"
    ) + "\n}"
    install = (ROOT / "ops" / "install-native.ps1").read_text(encoding="utf-8")
    loop = "\n".join("\n".join(_marker_block(install, section)) for section in (
        "client registration state", "client registration helpers", "client registrations"))
    completion = "\n".join(_marker_block(install, "client setup result")) if complete else ""
    installer_stub = native_stub.POWERSHELL.replace(
        "function Invoke-NativeHelper", "function Invoke-FixtureNativeHelper") + f"""
function Invoke-NativeHelper {{
    if ($args[0] -eq 'credentials' -and $args[1] -eq 'gemini-guard') {{
        $global:LASTEXITCODE = 0
        if ($args[2] -eq '--config' -and $args[3] -eq (Join-Path $env:USERPROFILE ".gemini\\settings.json")) {{
            return '{{"status":"{gemini_guard}","credential":"{gemini_credential}"}}'
        }}
        return
    }}
    if ($MyInvocation.ExpectingInput) {{ $input | Invoke-FixtureNativeHelper @args }}
    else {{ Invoke-FixtureNativeHelper @args }}
}}
"""
    register = _between("ops/install-native.ps1", "function Register-Result(", "\n}\nfunction Set-CodexRuntimeDefaults") + "\n}"
    describe = _between("ops/install-native.ps1", "function Describe-Mcp($state) {", "\n}\nfunction Get-McpMarker") + "\n}"
    marker = _between("ops/install-native.ps1", "function Get-McpMarker($state) {", "\n}\nfunction Describe-Instr") + "\n}"
    log = tmp_path / "calls.txt"
    installed_bin = tmp_path / "installed-bin"
    installed_bin.mkdir()
    # A .cmd on Windows: the stub launcher must run (an .exe cannot be a script).
    shim_name = "pseudolife-mcp.cmd" if os.name == "nt" else "pseudolife-mcp"
    installed_shim = installed_bin / shim_name
    installed_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    installed_shim.chmod(0o755)
    old_bin = tmp_path / "old-bin"
    old_bin.mkdir()
    if shadowed_path:
        old_shim = old_bin / shim_name
        old_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        old_shim.chmod(0o755)
    escaped_log = str(log).replace("'", "''")
    escaped_repo = str(ROOT).replace("'", "''")
    escaped_config = ("pseudolife-memory\n" + config.replace("__INSTALLED_SHIM__", str(installed_shim))).replace("'", "''")
    escaped_installed_shim = str(installed_shim).replace("'", "''")
    escaped_path_bin = str(old_bin if shadowed_path else installed_bin).replace("'", "''")
    function_defs = []
    for name in CLIENTS:
        query = "list" if name == "gemini" else "get"
        function_defs.append(f"""function global:{name} {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('{name}|' + ($callArgs -join ' '))
    if (($callArgs[0] -eq 'mcp') -and ($callArgs[1] -eq '{query}')) {{
        if ($env:FAKE_EXISTING -eq 'yes') {{
            Write-Output $env:FAKE_CONFIG
            $global:LASTEXITCODE = 0
            return
        }}
        $global:LASTEXITCODE = 1
        return
    }}
    $global:LASTEXITCODE = 91
}}""")
    script = f"""$ErrorActionPreference = 'Stop'
$repo = '{escaped_repo}'
$clients = @('{client}')
$requestedClients = $clients
$hookState = @{{}}
$instrState = @{{}}
$tunnelExit = 0
$Transport = 'shim'
$tokenState = '{"present" if gemini_credential else "opted-out"}'
$codexCredentialBootstrapFailed = $false
$codexConnectionConfigured = $false
$codexCredentialFile = $null
$codexCredentialUrl = $null
$codexRuntimeDefaults = $null
$mcpState = @{{}}
$script:shimInstallResult = $null
$script:shimInstallPath = '{escaped_installed_shim}'
$script:shimUpgradeHeld = $null
$script:shimManagerKind = $null
$env:PATH = '{escaped_path_bin}' + [IO.Path]::PathSeparator + $env:PATH
$env:FAKE_CONFIG = '{escaped_config}'
$env:FAKE_EXISTING = '{"yes" if existing else "no"}'
$env:FAKE_NATIVE_EXIT = '{manager_exit}'
$env:FAKE_MIGRATE_EXIT = '{migrate_exit}'
$env:FAKE_LAUNCHER = '{escaped_installed_shim}'
$env:CALL_LOG = '{escaped_log}'
function Step($message) {{ Write-Output "STEP: $message" }}
function Set-CodexRuntimeDefaults {{ $script:codexRuntimeDefaults = 'preserved' }}
{''.join(function_defs)}
$env:PSEUDOLIFE_SHIM_RUNTIMES = '{str(installed_bin / "runtimes").replace("'", "''")}'
$env:PSEUDOLIFE_SHIM_LAUNCHER = '{escaped_installed_shim}'
{helper}
{installer_stub}
{describe}
{marker}
{register}
{loop}
$state = $mcpState['{client}']
Write-Output "STATE=$state"
Write-Output ("STATUS=" + (Get-McpMarker $state) + " " + (Describe-Mcp $state))
{completion}
"""
    runner = tmp_path / "run-existing.ps1"
    runner.write_text(script, encoding="utf-8")
    fixture_env = _fixture_env(tmp_path / "powershell-env")
    if inherited_credential:
        fixture_env["PSEUDOLIFE_MCP_TOKEN"] = "synthetic-inherited"
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
        capture_output=True, check=False, timeout=15,
        env=fixture_env,
    )


@pytest.mark.parametrize("guard_status, credential, inherited, exit_code", [
    ("enabled", "", False, 0),
    ("not-verified", "", False, 0),
    ("absent", "", False, 5),
    ("enabled", "missing", False, 5),
    ("enabled", "missing", True, 0),
    ("enabled", "configured", False, 0),
])
@pytest.mark.parametrize("shell", [*_bash_variants(), "powershell"], ids=lambda name: Path(name).name)
def test_requested_gemini_rerun_checks_settings_when_listing_hides_guard(
    tmp_path: Path, shell: str, guard_status: str, credential: str, inherited: bool, exit_code: int,
) -> None:
    config = _managed_config("gemini")
    assert "PSEUDOLIFE_MCP_NO_SPAWN" not in config
    args = dict(client="gemini", config=config, manager_exit=0, migrate_exit=4,
                gemini_guard=guard_status, complete=True, gemini_credential=credential,
                inherited_credential=inherited)
    if shell == "powershell":
        proc = _run_powershell_existing(tmp_path, **args)
    else:
        proc = _run_bash_existing(shell, tmp_path, **args)
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == exit_code, output
    assert ("was not verified" in output) == (guard_status == "not-verified")
    if guard_status == "not-verified":
        # The helper returns this status for unreadable settings, including comments.
        assert "Warnings\n" in output.replace("\r\n", "\n")
        assert "Check the pseudolife-memory entry in ~/.gemini/settings.json." in output
    assert ("Requested client setup incomplete (Gemini CLI)" in output) == (exit_code == 5)
    if exit_code == 5:
        if credential == "missing":
            reason = (
                "The daemon requires a bearer token, and the existing Gemini CLI stdio entry has no credential in "
                "its settings or this launch environment. Set PSEUDOLIFE_MCP_TOKEN_FILE in ~/.gemini/settings.json "
                "to a valid token file, then rerun."
            )
        else:
            reason = (
                "The existing stdio registration has no verifiable no-spawn guard. "
                "Set PSEUDOLIFE_MCP_NO_SPAWN=1 in the existing entry, then rerun."
            )
        assert proc.stderr.decode().splitlines()[-1] == f"Requested client setup incomplete (Gemini CLI). {reason}"
    else:
        assert "STATE=present-current" in output
    assert "synthetic-inherited" not in output
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8")
    assert "mcp add" not in calls
    assert "mcp remove" not in calls


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_upgrades_managed_shim_without_rewriting_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client),
        manager_exit=0,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert "native|install" in calls
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in output
    assert "[x] already wired; it runs the native client from this release" in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_reports_existing_shim_upgrade_failure_in_status(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client), manager_exit=7,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "[!] registration or shim upgrade FAILED" in output
    assert "registration was preserved" in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_preserves_custom_stdio_registration_and_warns(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_custom_config(client, r"D:\Private\pseudolife-mcp.exe"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "launcher|installer migrate-registrations --client " + {"claude": "claude-code"}.get(client, client) in calls
    assert "custom registered command" in output
    assert r"D:\Private\pseudolife-mcp.exe" not in output
    assert "STATE=failed" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN=1" in output
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_reports_shadowed_bare_shim_without_rewriting_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client, config=_managed_config(client), manager_exit=0,
        shadowed_path=True,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "still resolves to a different executable" in output
    assert "put the installed scripts directory first" in output
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_rerun_upgrades_exact_manager_absolute_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_custom_config(client, "__INSTALLED_SHIM__", "1"), manager_exit=0,
        migrate_exit=4,
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "STATE=present-current\n" in output.replace("\r", "")
    assert "registration preserved; it already runs the native client" in output
    assert "custom registered command" not in output


@pytest.mark.parametrize("value", TRUTHY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_accepts_only_effective_no_spawn_values(
    tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=present-upgraded" in output
    assert "no-spawn guard is missing or cannot be verified" not in output


@pytest.mark.parametrize("value", FALSEY_NO_SPAWN)
@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_rejects_false_no_spawn_values_without_rewriting(
    tmp_path: Path, client: str, value: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_managed_config(client, value), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output
    assert not any("mcp remove" in call for call in calls)
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert "mcp remove pseudolife-memory" not in output
    assert "mcp add pseudolife-memory --env" not in output


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_requires_exact_no_spawn_environment_key(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_lookalike_guard_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


def test_install_ps1_gemini_checks_only_pseudolife_transport(tmp_path: Path) -> None:
    proc = _run_powershell_existing(
        tmp_path, client="gemini",
        config="other-server: http://127.0.0.1:9999/mcp (http) - Connected\n"
        + _managed_config("gemini"),
        manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no-spawn guard is missing or cannot be verified" in output


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_http_registration_needs_no_spawn_guard(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client, config=_http_config(client), manager_exit=0,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=present" in output
    assert "PSEUDOLIFE_MCP_NO_SPAWN" not in output
    assert "native|install" not in calls


@pytest.mark.parametrize("client", CLIENTS)
def test_install_ps1_flagless_fresh_cli_fails_before_registration(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client, config="", manager_exit=0, existing=False,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    calls = (tmp_path / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert proc.returncode == 0
    assert "STATE=failed" in output
    assert "no env flag" in output
    assert "registration was skipped" in output
    assert not any("mcp add" in call and "--help" not in call for call in calls)
    assert not any("mcp remove" in call for call in calls)


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda p: Path(p).parent.name)
def test_install_sh_reports_a_registration_already_on_the_launcher_as_preserved(
    bash: str, tmp_path: Path, client: str,
) -> None:
    proc = _run_bash_existing(
        bash, tmp_path, client=client,
        config=_custom_config(client, "/opt/tools/pseudolife-mcp", "1"),
        manager_exit=0, migrate_exit=4,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace")
    assert proc.returncode == 0, output
    assert "STATE=present-current\n" in output
    assert "registration preserved; it already runs the native client" in output
    assert "registration moved" not in output


@pytest.mark.parametrize("client", VISIBLE_ENV_CLIENTS)
def test_install_ps1_reports_a_registration_already_on_the_launcher_as_preserved(
    tmp_path: Path, client: str,
) -> None:
    proc = _run_powershell_existing(
        tmp_path, client=client,
        config=_custom_config(client, r"D:\tools\pseudolife-mcp.exe", "1"),
        manager_exit=0, migrate_exit=4,
    )
    output = (proc.stdout + proc.stderr).decode(errors="replace").replace("\r", "")
    assert proc.returncode == 0, output
    assert "STATE=present-current\n" in output
    assert "registration preserved; it already runs the native client" in output
    assert "registration moved" not in output


# The real user PATH guard (tests/user_path_guard.py), which the whole-installer
# tests wrap their runs in: never the real registry, a fake winreg over a dict.

class _FakeWinreg:
    """Just enough of winreg for user_path_unchanged, over a dict."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2

    def __init__(self, path):
        self.values = {} if path is None else {"Path": path}

    @contextlib.contextmanager
    def OpenKey(self, *args):
        yield self

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        del self.values[name]


HOME = "C:\\Users\\<user>\\AppData\\Local\\Temp\\pytest-1\\env\\home"
BEFORE = ("C:\\tools;%USERPROFILE%\\.local\\bin", 2)


def _guarded(monkeypatch, before):
    """A fake winreg (never the real registry), os.name nt, and a recorder
    of the guard's broadcasts."""
    import sys
    fake = _FakeWinreg(before)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(os, "name", "nt")
    announced = []
    return fake, announced, lambda: user_path_unchanged(HOME, broadcast=lambda: announced.append(1))


@pytest.mark.parametrize("before", [BEFORE, None], ids=["set", "absent"])
def test_the_user_path_guard_takes_back_only_the_tests_own_entries(monkeypatch, before):
    fake, announced, guard = _guarded(monkeypatch, before)
    with guard():
        pass
    assert announced == []
    leaked = HOME + "\\shim\\bin"
    with pytest.raises(AssertionError, match="put .* on the real user PATH; removed them again"):
        with guard():
            # What ensure_user_path writes: the directory first, the rest kept.
            fake.values["Path"] = (f"{leaked};{before[0]}" if before else leaked, 2)
    assert fake.values.get("Path") == before
    # Codex review of faea97a1a: the leak was announced, so the restore is too.
    assert announced == [1]


def test_the_user_path_guards_default_announcement_needs_no_package(monkeypatch):
    # Without a broadcast argument the guard announces the restore with its
    # own copy of the WM_SETTINGCHANGE broadcast, never the frozen package's.
    import sys
    from tests import user_path_guard

    fake, announced, _ = _guarded(monkeypatch, BEFORE)
    for name in ("pseudolife_memory", "pseudolife_memory.runtimes"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setattr(user_path_guard, "_broadcast_environment_change", lambda: announced.append("own"))
    with pytest.raises(AssertionError, match="put .* on the real user PATH; removed them again"):
        with user_path_unchanged(HOME):
            fake.values["Path"] = (HOME + "\\shim\\bin;" + BEFORE[0], 2)
    assert fake.values["Path"] == BEFORE and announced == ["own"]


def test_the_user_path_guard_leaves_someone_elses_change_alone(monkeypatch):
    # The conductor's review of d4daf4358: on a shared PC another session or
    # a real installer may change the user PATH meanwhile. That is not
    # undone; the test fails naming it.
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    changed = ("C:\\someone-else\\bin;" + BEFORE[0], 2)
    with pytest.raises(AssertionError, match="in a way this test did not cause") as raised:
        with guard():
            fake.values["Path"] = changed
    assert "C:\\\\someone-else\\\\bin" in str(raised.value)
    assert fake.values["Path"] == changed and announced == []


@pytest.mark.parametrize("change", ["mixed", "removed", "type"])
def test_the_user_path_guard_touches_nothing_unless_the_change_is_only_the_leak(monkeypatch, change):
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    changed = {"mixed": (HOME + "\\shim\\bin;C:\\someone-else\\bin;" + BEFORE[0], 2),
               "removed": ("C:\\tools", 2),
               "type": (BEFORE[0], 1)}[change]
    with pytest.raises(AssertionError, match="in a way this test did not cause"):
        with guard():
            fake.values["Path"] = changed
    assert fake.values["Path"] == changed and announced == []


def test_the_user_path_guard_leaves_a_later_unrelated_change_alone(monkeypatch):
    # Codex review of d356bbb9b: the reread decides too. The leak, then
    # someone else's entry before the guard rereads: nothing is written.
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    leaked = HOME + "\\shim\\bin"
    later = ("C:\\later\\bin;" + leaked + ";" + BEFORE[0], 2)
    real_read = fake.QueryValueEx
    reads = []

    def query(key, name):
        reads.append(name)
        if len(reads) == 3:   # the reread just before any write
            fake.values["Path"] = later
        return real_read(key, name)

    fake.QueryValueEx = query
    with pytest.raises(AssertionError, match="in a way this test did not cause"):
        with guard():
            fake.values["Path"] = (leaked + ";" + BEFORE[0], 2)
    assert fake.values["Path"] == later and announced == []


@pytest.mark.parametrize("reread", ["deleted", "type", "removed"])
def test_the_user_path_guard_writes_nothing_when_the_reread_moved(monkeypatch, reread):
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    leaked = HOME + "\\shim\\bin"
    real_read = fake.QueryValueEx
    reads = []
    moved = {"deleted": None, "type": (leaked + ";" + BEFORE[0], 1), "removed": (leaked, 2)}[reread]

    def query(key, name):
        reads.append(name)
        if len(reads) == 3:
            if moved is None:
                fake.values.pop("Path", None)
            else:
                fake.values["Path"] = moved
        return real_read(key, name)

    fake.QueryValueEx = query
    with pytest.raises(AssertionError, match="in a way this test did not cause"):
        with guard():
            fake.values["Path"] = (leaked + ";" + BEFORE[0], 2)
    assert fake.values.get("Path") == moved and announced == []


def test_the_user_path_guard_keeps_an_original_duplicate(monkeypatch):
    # Codex review of d356bbb9b: a fixture entry already on the PATH and
    # added again; only the added copy may go.
    leaked = HOME + "\\shim\\bin"
    before = (leaked + ";" + BEFORE[0], 2)
    fake, announced, guard = _guarded(monkeypatch, before)
    with pytest.raises(AssertionError, match="removed them again"):
        with guard():
            fake.values["Path"] = (leaked + ";" + before[0], 2)
    assert fake.values["Path"] == before and announced == [1]


def test_entries_inside_the_fixture_home_are_matched_as_windows_paths(monkeypatch):
    monkeypatch.setenv("PL_GUARD_HOME", HOME)
    assert _inside(HOME.upper() + "/shim/bin", HOME)
    assert _inside(HOME, HOME)
    assert _inside('"' + HOME + '\\shim\\bin"', HOME)              # a quoted entry
    assert _inside("%PL_GUARD_HOME%\\shim\\bin", HOME)               # Windows expands %VAR%
    assert not _inside("$PL_GUARD_HOME\\shim\\bin", HOME)            # but not $VAR
    assert not _inside("${PL_GUARD_HOME}\\shim\\bin", HOME)
    assert not _inside(HOME + "-other\\bin", HOME)
    assert not _inside("C:\\tools", HOME) and not _inside("", HOME)


def test_the_user_path_guard_leaves_a_reorder_alone_even_beside_the_leak(monkeypatch):
    # Codex review of 4cf7cdec2: someone reordering the old entries changes
    # which program wins; with a fixture entry added too, nothing is written.
    fake, announced, guard = _guarded(monkeypatch, ("C:\\a;C:\\b", 2))
    changed = (HOME + "\\bin;C:\\b;C:\\a", 2)
    # Codex review of 721fb8c00: the failure names the reorder, not only the addition.
    with pytest.raises(AssertionError, match="did not cause .*in a different order"):
        with guard():
            fake.values["Path"] = changed
    assert fake.values["Path"] == changed and announced == []


def test_the_user_path_guard_names_a_type_change_beside_the_leak(monkeypatch):
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    changed = (HOME + "\\bin;" + BEFORE[0], 1)
    with pytest.raises(AssertionError, match="did not cause .*registry type changed from 2 to 1"):
        with guard():
            fake.values["Path"] = changed
    assert fake.values["Path"] == changed and announced == []


def test_a_variable_that_expands_to_several_entries_is_not_inside_the_home(monkeypatch):
    # Codex review of 721fb8c00: %VAR% naming a home entry and an outside one
    # must not let the guard remove the outside one.
    monkeypatch.setenv("PL_GUARD_TWO", HOME + "\\bin;C:\\other\\bin")
    assert not _inside("%PL_GUARD_TWO%", HOME)
    fake, announced, guard = _guarded(monkeypatch, BEFORE)
    changed = ("%PL_GUARD_TWO%;" + BEFORE[0], 2)
    with pytest.raises(AssertionError, match="in a way this test did not cause"):
        with guard():
            fake.values["Path"] = changed
    assert fake.values["Path"] == changed and announced == []


def test_the_user_path_guard_names_a_reorder_on_its_own(monkeypatch):
    fake, announced, guard = _guarded(monkeypatch, ("C:\\a;C:\\b", 2))
    with pytest.raises(AssertionError, match="in a different order"):
        with guard():
            fake.values["Path"] = ("C:\\b;C:\\a", 2)
    assert fake.values["Path"] == ("C:\\b;C:\\a", 2) and announced == []


def test_the_user_path_guard_restores_an_empty_original(monkeypatch):
    # Codex review of 4cf7cdec2: over an empty value ensure_user_path writes
    # just its directory, with no separator.
    fake, announced, guard = _guarded(monkeypatch, ("", 2))
    with pytest.raises(AssertionError, match="removed them again"):
        with guard():
            fake.values["Path"] = (HOME + "\\shim\\bin", 2)
    assert fake.values["Path"] == ("", 2) and announced == [1]


def test_a_variable_folded_back_by_normpath_is_not_inside_the_home(monkeypatch):
    # Codex review of 38aafbdce: "home\bin;C:\.." normalises to home itself,
    # so the separator check comes before normpath.
    monkeypatch.setenv("PL_GUARD_FOLD", HOME + "\\bin;C:\\..")
    assert not _inside("%PL_GUARD_FOLD%", HOME)


@pytest.mark.parametrize(("old", "new", "names", "not_named"), [
    # Codex review of 38aafbdce: a removed trailing duplicate is a removal, not a reorder.
    ("C:\\a;C:\\b;C:\\a", "C:\\a;C:\\b", "removed ['C:\\\\a']", "different order"),
    ("C:\\a;C:\\b;C:\\a", "C:\\b;C:\\a", "removed ['C:\\\\a']", "different order"),
    ("C:\\a;C:\\b", "C:\\b;C:\\a", "different order", "removed ['C:"),
])
def test_the_failure_names_removals_and_moves_apart(old, new, names, not_named):
    message = _path_difference((old, 2), (new, 2))
    assert names in message and not_named not in message, message


def test_the_failure_names_a_created_or_deleted_value():
    assert "the value was created" in _path_difference(None, ("", 2))
    assert "the value was deleted" in _path_difference(("", 2), None)


def test_a_plain_string_path_expands_no_variable(monkeypatch):
    # Codex review of 7519cc866: Windows expands %VAR% only in a REG_EXPAND_SZ
    # value; in a REG_SZ one the entry is the literal text, not the home.
    monkeypatch.setenv("PL_GUARD_HOME", HOME)
    assert not _inside("%PL_GUARD_HOME%\\bin", HOME, expand_vars=False)
    fake, announced, guard = _guarded(monkeypatch, ("C:\\tools", 1))
    changed = ("%PL_GUARD_HOME%\\bin;C:\\tools", 1)
    with pytest.raises(AssertionError, match="in a way this test did not cause"):
        with guard():
            fake.values["Path"] = changed
    assert fake.values["Path"] == changed and announced == []


def test_a_deletion_among_duplicates_is_not_reported_as_a_move():
    # Codex review of 7519cc866: the new entries are a subsequence of the old.
    message = _path_difference(("B;C;D;A;B;X;C;Y;D;E", 2), ("A;B;C;D;E", 2))
    assert "different order" not in message, message
