"""Executable contracts for upgrading shims behind existing registrations.

The fixtures run only the installer client-wiring loop with fake MCP clients and
package managers.  They never touch live registrations or installed packages.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


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
        "XDG_CONFIG_HOME": str(fixture_home / "config"),
    })
    return env


def _bash_fixture_path(
    bash: str, path: Path, env: dict[str, str],
) -> str:
    """Return the path spelling understood by the selected Bash runtime."""
    if os.name != "nt":
        return str(path)
    converted = subprocess.run(
        [bash, "-c", 'cygpath -u -- "$1"', "fixture", str(path)],
        capture_output=True, check=True, text=True, timeout=5,
        env=env,
    )
    return converted.stdout.strip()


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


def _between(path: str, start: str, end: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    return start + text.split(start, 1)[1].split(end, 1)[0]


def _bash_variants() -> list[str]:
    variants: list[str] = []
    for candidate in (
        shutil.which("bash") if os.name != "nt" else None,
        r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else None,
    ):
        if candidate and Path(candidate).is_file() and candidate not in variants:
            variants.append(candidate)
    return variants


def _run_bash_existing(
    bash: str, tmp_path: Path, *, client: str, config: str, manager_exit: int,
    shadowed_path: bool = False, existing: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    install = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
    helper = _between("ops/install.sh", "resolve_installed_shim() {", "\n}\n\n# Two env pairs") + "\n}"
    loop_start = 'for selected_client in $CLIENTS; do\n    if [ "$selected_client" = claude-desktop ]; then'
    loop = _between("ops/install.sh", loop_start, "\ndone\n\n# ── 12. health") + "\ndone"
    describe = _between("ops/install.sh", "describe_mcp() {", "\n}\ndescribe_instr()") + "\n}"
    marker = _between("ops/install.sh", "mcp_marker() {", "\n}\ndescribe_mcp()") + "\n}"
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
    pipx = fake_bin / "pipx"
    pipx.write_text(
        "#!/bin/sh\n"
        "printf 'pipx|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
        "if [ \"$1\" = environment ]; then printf '%s\\n' \"$FAKE_INSTALL_BIN\"; exit 0; fi\n"
        "exit \"$FAKE_MANAGER_EXIT\"\n",
        encoding="utf-8",
    )
    pipx.chmod(0o755)
    repo = str(ROOT).replace("'", "'\\''")
    fixture_env = _fixture_env(tmp_path / "bash-env")
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
  export FAKE_CONFIG FAKE_MANAGER_EXIT='{manager_exit}' FAKE_INSTALL_BIN='{installed_bin_shell}' FAKE_EXISTING='{"yes" if existing else "no"}'
repo='{repo}'
CLIENTS='{client}'
TRANSPORT=shim
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
    assert f"pipx|install --force {ROOT}" in calls
    assert not any(" mcp add" in call or " mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in output
    assert "[x] already wired; checkout shim upgraded" in output


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
    assert not any(call.startswith("pipx|install ") for call in calls)
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
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "STATE=present-upgraded" in output
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
    assert not any(call.startswith("pipx|install ") for call in calls)


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
    shadowed_path: bool = False, existing: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        pytest.skip("PowerShell is unavailable")
    helper = _between(
        "ops/install.ps1", "function Resolve-InstalledShimPath", "\n}\n\n# Two env pairs"
    ) + "\n}"
    loop_start = 'foreach ($selectedClient in $clients) {\n    if ($selectedClient -eq "claude-desktop") {'
    loop = _between("ops/install.ps1", loop_start, "\n}\n\n# -- 12. health") + "\n}"
    describe = _between("ops/install.ps1", "function Describe-Mcp($state) {", "\n}\nfunction Get-McpMarker") + "\n}"
    marker = _between("ops/install.ps1", "function Get-McpMarker($state) {", "\n}\nfunction Describe-Instr") + "\n}"
    log = tmp_path / "calls.txt"
    installed_bin = tmp_path / "installed-bin"
    installed_bin.mkdir()
    shim_name = "pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp"
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
$Transport = 'shim'
$codexCredentialBootstrapFailed = $false
$codexConnectionConfigured = $false
$codexCredentialFile = $null
$codexCredentialUrl = $null
$codexRuntimeDefaults = $null
$mcpState = @{{}}
$script:shimInstallResult = $null
$script:shimInstallPath = '{escaped_installed_shim}'
$env:PATH = '{escaped_path_bin}' + [IO.Path]::PathSeparator + $env:PATH
$env:FAKE_CONFIG = '{escaped_config}'
$env:FAKE_EXISTING = '{"yes" if existing else "no"}'
$env:FAKE_MANAGER_EXIT = '{manager_exit}'
function Step($message) {{ Write-Output "STEP: $message" }}
function Set-CodexRuntimeDefaults {{ $script:codexRuntimeDefaults = 'preserved' }}
{''.join(function_defs)}
function global:pipx {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('pipx|' + ($callArgs -join ' '))
    if ($callArgs[0] -eq 'environment') {{ Write-Output '{str(installed_bin).replace("'", "''")}'; $global:LASTEXITCODE = 0; return }}
    $global:LASTEXITCODE = [int]$env:FAKE_MANAGER_EXIT
}}
{helper}
{describe}
{marker}
{loop}
$state = $mcpState['{client}']
Write-Output "STATE=$state"
Write-Output ("STATUS=" + (Get-McpMarker $state) + " " + (Describe-Mcp $state))
"""
    runner = tmp_path / "run-existing.ps1"
    runner.write_text(script, encoding="utf-8")
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
        capture_output=True, check=False, timeout=15,
        env=_fixture_env(tmp_path / "powershell-env"),
    )


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
    assert f"pipx|install --force {ROOT}" in calls
    assert not any("mcp add" in call or "mcp remove" in call for call in calls)
    assert "STATE=present-upgraded" in output
    assert "[x] already wired; checkout shim upgraded" in output


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
    assert not any(call.startswith("pipx|install ") for call in calls)
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
    )
    output = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "STATE=present-upgraded" in output
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
    assert not any(call.startswith("pipx|install ") for call in calls)


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
