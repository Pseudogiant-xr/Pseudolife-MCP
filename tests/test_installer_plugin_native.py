"""The native installer (ops/install-native.*): the tests of test_installer_plugin.py
that the native client path changed, run against the native installer.

test_installer_plugin.py keeps the default (Python client) installer's tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from tests.installer_fragments import _bash_fixture_path, _bash_variants, _between
from tests.test_installer_existing_upgrade_native import _fixture_env

ROOT = Path(__file__).resolve().parents[1]


PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"


MARKETPLACE_SOURCE = "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git"


FAKE_VERSION = "0.15.0"


def _plugins_dir(home: Path) -> Path:
    path = home / ".claude" / "plugins"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_plugin(home: Path, version: str = FAKE_VERSION) -> None:
    (_plugins_dir(home) / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {PLUGIN_ID: [{"scope": "user", "version": version,
                                 "gitCommitSha": "0" * 40}]},
    }, indent=2), encoding="utf-8")


def _record_marketplace(home: Path) -> None:
    (_plugins_dir(home) / "known_marketplaces.json").write_text(json.dumps({
        "pseudolife-mcp": {"source": {"source": "git", "url": MARKETPLACE_SOURCE}},
    }, indent=2), encoding="utf-8")


class Scenario:
    def __init__(self, *, clients="claude", flag="auto", cli=True, marketplace=False,
                 plugin=False, marketplace_exit=0, install_exit=0, install_records=True,
                 help_has_yes=True, helper_out="", helper_exit=0, requested=None, profile_var=None,
                 profile_dir="active [profile]"):
        self.clients, self.flag, self.cli = clients, flag, cli
        self.requested = requested
        self.profile_var = profile_var
        self.profile_dir = profile_dir
        # The native client's plugin-marketplace step as the installer runs
        # it (native_helper / Invoke-NativeHelper): a stub that records its
        # call and answers with ``helper_out`` / ``helper_exit``.
        self.helper_out, self.helper_exit = helper_out, helper_exit
        self.marketplace, self.plugin = marketplace, plugin
        self.marketplace_exit, self.install_exit = marketplace_exit, install_exit
        self.install_records, self.help_has_yes = install_records, help_has_yes


def _run_bash(bash: str, tmp_path: Path, sc: Scenario) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
    block = _between("ops/install-native.sh", "# >>> claude plugin >>>", "# <<< claude plugin <<<")
    env = _fixture_env(tmp_path / "bash-env")
    home = Path(env["HOME"])
    if sc.profile_var:
        env[sc.profile_var] = _bash_fixture_path(bash, tmp_path / sc.profile_dir, env)
        if sc.profile_var == "CLAUDE_CODE_PLUGIN_CACHE_DIR":
            env["CLAUDE_CONFIG_DIR"] = _bash_fixture_path(bash, tmp_path / "separate config", env)
    if sc.marketplace:
        _record_marketplace(home)
    if sc.plugin:
        _record_plugin(home)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    call_log = tmp_path / "calls.txt"
    if sc.cli:
        # Writes the same registry files the real CLI does, pretty-printed
        # like the real ones, so the installer's read-back sees them.
        (fake_bin / "claude").write_text(
            "#!/bin/sh\n"
            "printf 'claude|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
            'plugins="${CLAUDE_CODE_PLUGIN_CACHE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins}"\n'
            "if [ \"$1\" = plugin ] && [ \"$2\" = install ] && [ \"$3\" = --help ]; then\n"
            "  [ \"$FAKE_HELP_YES\" = yes ] && echo '  -y, --yes   Accept the displayed command'\n"
            # Pads past the pipe buffer and, like a real CLI, dies when the
            # reader has gone: a `grep -q` probe would SIGPIPE it and, under
            # pipefail, read a present flag as absent.
            "  head -c 300000 /dev/zero | tr '\\0' 'x' || exit 1\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = plugin ] && [ \"$2\" = marketplace ] && [ \"$3\" = add ]; then\n"
            "  if [ \"$FAKE_MARKET_EXIT\" = 0 ]; then\n"
            "    mkdir -p \"$plugins\"\n"
            "    printf '{\\n  \"pseudolife-mcp\": {\\n    \"source\": {\\n      \"source\": \"github\",\\n      \"repo\": \"%s\"\\n    }\\n  }\\n}\\n' \"$4\" >\"$plugins/known_marketplaces.json\"\n"
            "  fi\n"
            "  exit \"$FAKE_MARKET_EXIT\"\n"
            "fi\n"
            "if [ \"$1\" = plugin ] && [ \"$2\" = install ]; then\n"
            "  if [ \"$FAKE_INSTALL_EXIT\" = 0 ] && [ \"$FAKE_INSTALL_RECORD\" = yes ]; then\n"
            "    mkdir -p \"$plugins\"\n"
            "    printf '{\\n  \"version\": 2,\\n  \"plugins\": {\\n    \"pseudolife-memory@pseudolife-mcp\": [\\n      {\\n        \"scope\": \"user\",\\n        \"version\": \"%s\"\\n      }\\n    ]\\n  }\\n}\\n' \"$FAKE_VERSION\" >\"$plugins/installed_plugins.json\"\n"
            "  fi\n"
            "  exit \"$FAKE_INSTALL_EXIT\"\n"
            "fi\n"
            "exit 91\n", encoding="utf-8")
        (fake_bin / "claude").chmod(0o755)
    fake_bin_shell = _bash_fixture_path(bash, fake_bin, env).replace("'", "'\\''")
    call_log_shell = _bash_fixture_path(bash, call_log, env).replace("'", "'\\''")
    helper_log_shell = _bash_fixture_path(bash, tmp_path / "helper-calls.txt", env).replace("'", "'\\''")
    repo_shell = _bash_fixture_path(bash, ROOT, env).replace("'", "'\\''")
    finish = ""
    if sc.requested is not None:
        finish = (
            f"REQUESTED_CLIENTS={'claude' if sc.requested else ''}\n"
            + _between("ops/install-native.sh", 'instruction_choice="${INSTRUCTIONS:', '\nHOOK_CLAUDE=')
            + "\n"
            "echo REMAINING_STEPS_FINISHED\n"
            + _between("ops/install-native.sh", "# >>> client registration state >>>",
                       "# <<< client registration state <<<")
            + "\n" + _between("ops/install-native.sh", "# >>> client setup result >>>",
                              "# <<< client setup result <<<")
        )
    # The installer's own strict mode: an abort in the block must fail here.
    script = f"""set -euo pipefail
PATH='{fake_bin_shell}:/usr/bin:/bin'
export PATH CALL_LOG='{call_log_shell}'
export FAKE_HELP_YES='{"yes" if sc.help_has_yes else "no"}' FAKE_MARKET_EXIT='{sc.marketplace_exit}'
export FAKE_INSTALL_EXIT='{sc.install_exit}' FAKE_INSTALL_RECORD='{"yes" if sc.install_records else "no"}' FAKE_VERSION='{FAKE_VERSION}'
CLIENTS='{sc.clients}'
CLAUDE_PLUGIN='{sc.flag}'
repo='{repo_shell}'
export HELPER_LOG='{helper_log_shell}' FAKE_HELPER_OUT={shlex.quote(sc.helper_out)} FAKE_HELPER_EXIT='{sc.helper_exit}'
native_helper() {{ printf 'helper|%s\\n' "$*" >>"$HELPER_LOG"; [ -z "$FAKE_HELPER_OUT" ] || printf '%s\\n' "$FAKE_HELPER_OUT"; return "$FAKE_HELPER_EXIT"; }}
step() {{ printf 'STEP: %s\\n' "$*"; }}
{block}
install_claude_plugin
printf 'STATE=%s\\n' "$PLUGIN_CLAUDE"
printf 'LINE=%s\\n' "$(describe_plugin "$PLUGIN_CLAUDE")"
{finish}
"""
    proc = subprocess.run([bash], input=script.encode(), capture_output=True, check=False,
                          timeout=20, env=env)
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return proc, calls


def _run_powershell(tmp_path: Path, sc: Scenario) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    block = _between("ops/install-native.ps1", "# >>> claude plugin >>>", "# <<< claude plugin <<<")
    env = _fixture_env(tmp_path / "powershell-env")
    home = Path(env["USERPROFILE"])
    if sc.profile_var:
        env[sc.profile_var] = str(tmp_path / sc.profile_dir)
        if sc.profile_var == "CLAUDE_CODE_PLUGIN_CACHE_DIR":
            env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "separate config")
    if sc.marketplace:
        _record_marketplace(home)
    if sc.plugin:
        _record_plugin(home)
    call_log = tmp_path / "calls.txt"
    escaped_helper_log = str(tmp_path / "helper-calls.txt").replace("'", "''")
    escaped_log = str(call_log).replace("'", "''")
    finish = ""
    if sc.requested is not None:
        requested = "'claude'" if sc.requested else ""
        finish = (
            f"$requestedClients = @({requested})\n"
            "$hookState = @{}; $instrState = @{}\n"
            + _between("ops/install-native.ps1", '$installedPlugins =', '\n$hookState =')
            + "\n"
            "Write-Output REMAINING_STEPS_FINISHED\n"
            + _between("ops/install-native.ps1", "# >>> client registration state >>>",
                       "# <<< client registration state <<<")
            + "\n" + _between("ops/install-native.ps1", "# >>> client setup result >>>",
                              "# <<< client setup result <<<")
        )
    fake = "" if not sc.cli else f"""function global:claude {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('claude|' + ($callArgs -join ' '))
    $plugins = if ($env:CLAUDE_CODE_PLUGIN_CACHE_DIR) {{ $env:CLAUDE_CODE_PLUGIN_CACHE_DIR }}
               elseif ($env:CLAUDE_CONFIG_DIR) {{ Join-Path $env:CLAUDE_CONFIG_DIR 'plugins' }}
               else {{ Join-Path $env:USERPROFILE '.claude\\plugins' }}
    if ($callArgs[0] -eq 'plugin' -and $callArgs[1] -eq 'install' -and $callArgs[2] -eq '--help') {{
        if ($env:FAKE_HELP_YES -eq 'yes') {{ Write-Output '  -y, --yes   Accept the displayed command' }}
        $global:LASTEXITCODE = 0; return
    }}
    if ($callArgs[0] -eq 'plugin' -and $callArgs[1] -eq 'marketplace' -and $callArgs[2] -eq 'add') {{
        if ($env:FAKE_MARKET_EXIT -eq '0') {{
            [IO.Directory]::CreateDirectory($plugins) | Out-Null
            @{{ 'pseudolife-mcp' = @{{ source = @{{ source = 'github'; repo = $callArgs[3] }} }} }} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $plugins 'known_marketplaces.json')
        }}
        $global:LASTEXITCODE = [int]$env:FAKE_MARKET_EXIT; return
    }}
    if ($callArgs[0] -eq 'plugin' -and $callArgs[1] -eq 'install') {{
        if ($env:FAKE_INSTALL_EXIT -eq '0' -and $env:FAKE_INSTALL_RECORD -eq 'yes') {{
            [IO.Directory]::CreateDirectory($plugins) | Out-Null
            @{{ version = 2; plugins = @{{ '{PLUGIN_ID}' = @(@{{ scope = 'user'; version = $env:FAKE_VERSION }}) }} }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $plugins 'installed_plugins.json')
        }}
        $global:LASTEXITCODE = [int]$env:FAKE_INSTALL_EXIT; return
    }}
    $global:LASTEXITCODE = 91
}}"""
    # Only the built-in modules are discoverable. The block's `Get-Command
    # claude` misses in the no-CLI scenarios, and a miss makes PowerShell
    # scan every module on PSModulePath for an exported command of that
    # name. The windows-2025 runner image carries Az, AWSPowershell and
    # Microsoft.Graph, and that scan with a cold module-analysis cache ran
    # past this call's 30 s on two unrelated PRs (test-lite-windows, runs
    # 37503897287 attempt 1 and 37515413330, 2026-10-07; each rerun passed).
    # Measured locally (2026-10-07, 74 modules on PSModulePath): pwsh
    # startup 0.2 s, the miss 1.0 s, the miss over $PSHOME\Modules alone
    # 0.4 s; a hit on the fake function costs nothing. The real CLI is an
    # application on PATH, so the narrower module path leaves what the
    # block checks unchanged.
    script = f"""$ErrorActionPreference = 'Stop'
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$env:FAKE_HELP_YES = '{"yes" if sc.help_has_yes else "no"}'
$env:FAKE_MARKET_EXIT = '{sc.marketplace_exit}'
$env:FAKE_INSTALL_EXIT = '{sc.install_exit}'
$env:FAKE_INSTALL_RECORD = '{"yes" if sc.install_records else "no"}'
$env:FAKE_VERSION = '{FAKE_VERSION}'
$env:PATH = ''
$clients = @({", ".join("'" + c + "'" for c in sc.clients.split())})
$ClaudePlugin = '{sc.flag}'
$repo = '{str(ROOT).replace("'", "''")}'
$env:FAKE_HELPER_OUT = '{sc.helper_out.replace("'", "''")}'
$env:FAKE_HELPER_EXIT = '{sc.helper_exit}'
function Invoke-NativeHelper {{
    Add-Content -LiteralPath '{escaped_helper_log}' -Value ('helper|' + (@($args) -join ' '))
    if ($env:FAKE_HELPER_OUT) {{ Write-Output $env:FAKE_HELPER_OUT }}
    $global:LASTEXITCODE = [int]$env:FAKE_HELPER_EXIT
}}
function Step($message) {{ Write-Output "STEP: $message" }}
{fake}
{block}
Install-ClaudePlugin
Write-Output "STATE=$($script:pluginClaude)"
Write-Output ("LINE=" + (Describe-Plugin $script:pluginClaude))
{finish}
"""
    runner = tmp_path / "run-plugin.ps1"
    runner.write_text(script, encoding="utf-8")
    proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                          capture_output=True, check=False, timeout=30, env=env)
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return proc, calls


def _variants():
    return [("bash", b) for b in _bash_variants()] + [("pwsh", None)]


def _run(variant, tmp_path, sc):
    kind, bash = variant
    proc, calls = _run_bash(bash, tmp_path, sc) if kind == "bash" else _run_powershell(tmp_path, sc)
    out = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace") + out
    state = next(line[len("STATE="):] for line in out.splitlines() if line.startswith("STATE="))
    line = next(line[len("LINE="):] for line in out.splitlines() if line.startswith("LINE="))
    return state, line, calls, proc


def _all_output(proc) -> str:
    """bash warns on stderr; PowerShell's Write-Warning lands on stdout when
    the host is non-interactive — the wording is what matters."""
    return proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")


VARIANTS = _variants()


IDS = [v[0] if v[1] is None else Path(v[1]).parent.name for v in VARIANTS]


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_fresh_machine_gets_marketplace_and_plugin(variant, tmp_path):
    state, line, calls, _ = _run(variant, tmp_path, Scenario())
    acted = [c for c in calls if "--help" not in c]
    assert acted == [f"claude|plugin marketplace add {MARKETPLACE_SOURCE}",
                     f"claude|plugin install --yes {PLUGIN_ID}"]
    assert state == f"installed:{FAKE_VERSION}"
    assert line.startswith("[x] Plugin") and FAKE_VERSION in line
    assert "restart" in line.lower() or "reload" in line.lower()


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_existing_marketplace_is_not_re_added(variant, tmp_path):
    state, _, calls, _ = _run(variant, tmp_path, Scenario(marketplace=True))
    assert not any("marketplace add" in c for c in calls)
    assert any(f"plugin install --yes {PLUGIN_ID}" in c for c in calls)
    assert state == f"installed:{FAKE_VERSION}"


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_installed_plugin_is_left_alone(variant, tmp_path):
    """Re-running the installer must not touch a plugin that is already
    there: its cache moves through /plugin update, not through us."""
    state, line, calls, _ = _run(variant, tmp_path, Scenario(marketplace=True, plugin=True))
    assert not any("plugin" in c for c in calls)
    assert state == f"present:{FAKE_VERSION}"
    assert line.startswith("[x] Plugin") and "already" in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_skip_flag_runs_nothing_and_says_so(variant, tmp_path):
    state, line, calls, _ = _run(variant, tmp_path, Scenario(flag="skip"))
    assert calls == []
    assert state == "skipped"
    assert line.startswith("[-] Plugin") and "skip" in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_skip_with_plugin_present_still_reports_present(variant, tmp_path):
    """Section 9 hands hook ownership to a recorded plugin regardless of the
    flag, so the ladder must not claim the hooks come from settings.json
    (reviewer finding, 2026-09-21)."""
    state, line, calls, _ = _run(variant, tmp_path,
                                 Scenario(flag="skip", marketplace=True, plugin=True))
    assert calls == []
    assert state == f"present:{FAKE_VERSION}"
    assert line.startswith("[x] Plugin") and "already" in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_missing_cli_reports_the_manual_commands(variant, tmp_path):
    state, line, calls, _ = _run(variant, tmp_path, Scenario(cli=False))
    assert calls == []
    assert state == "no-cli"
    assert line.startswith("[!] Plugin")
    assert f"/plugin marketplace add {MARKETPLACE_SOURCE}" in line
    assert f"/plugin install {PLUGIN_ID}" in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_marketplace_failure_stops_before_install_and_names_the_command(variant, tmp_path):
    state, line, calls, proc = _run(variant, tmp_path, Scenario(marketplace_exit=1))
    assert any("marketplace add" in c for c in calls)
    assert not any(f"plugin install --yes {PLUGIN_ID}" in c for c in calls)
    assert state == "failed"
    assert line.startswith("[!] Plugin") and "marketplace add" in line
    assert "WARNING" in _all_output(proc)


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_install_failure_is_reported_not_fatal(variant, tmp_path):
    state, line, _, proc = _run(variant, tmp_path, Scenario(install_exit=1, install_records=False))
    assert state == "failed"
    assert line.startswith("[!] Plugin") and "plugin install" in line
    assert "WARNING" in _all_output(proc)


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
@pytest.mark.parametrize("requested", [True, False], ids=["requested", "detected"])
def test_plugin_failure_is_reported_at_completion(variant, tmp_path, requested):
    kind, bash = variant
    scenario = Scenario(install_exit=19, requested=requested)
    proc, calls = (_run_bash(bash, tmp_path, scenario) if kind == "bash"
                   else _run_powershell(tmp_path, scenario))
    assert f"claude|plugin install --yes {PLUGIN_ID}" in calls
    assert "REMAINING_STEPS_FINISHED" in proc.stdout.decode()
    assert proc.returncode == (5 if requested else 0), _all_output(proc)
    lines = proc.stderr.decode().splitlines()
    label = "Requested" if requested else "Detected"
    assert lines[-1] == (
        f"{label} client setup incomplete (Claude Code). Plugin was not installed. "
        f"'claude plugin install {PLUGIN_ID}' failed. Fix the reported error and rerun the installer."
    )
    if not requested:
        assert lines[-2] == "Warnings"


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
@pytest.mark.parametrize("profile_var", ["CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PLUGIN_CACHE_DIR"])
def test_plugin_in_active_profile_is_installed_and_preserved(variant, tmp_path, profile_var):
    root = tmp_path / "active [profile]"
    if profile_var == "CLAUDE_CODE_PLUGIN_CACHE_DIR":
        # The override is the root. A record in the nested cache cannot stand
        # in for installed_plugins.json beside the marketplaces and cache.
        cache = root / "cache"
        cache.mkdir(parents=True)
        (cache / "installed_plugins.json").write_text(
            json.dumps({"plugins": {PLUGIN_ID: [{"version": "decoy"}]}}), encoding="utf-8")
        registry = root / "installed_plugins.json"
    else:
        registry = root / "plugins" / "installed_plugins.json"
    scenario = Scenario(requested=True, profile_var=profile_var)
    state, _, calls, proc = _run(variant, tmp_path, scenario)
    assert state == f"installed:{FAKE_VERSION}"
    assert PLUGIN_ID in json.loads(registry.read_text(encoding="utf-8-sig"))["plugins"]
    assert "plugin detected" in proc.stdout.decode()
    assert f"claude|plugin install --yes {PLUGIN_ID}" in calls
    state, _, rerun_calls, proc = _run(variant, tmp_path, scenario)
    assert state == f"present:{FAKE_VERSION}"
    assert "plugin detected" in proc.stdout.decode()
    assert rerun_calls == calls


@pytest.mark.parametrize("clients,status", [("claude", 5), ("codex", 0)])
def test_powershell_unreadable_plugin_registry_does_not_abort(tmp_path, clients, status):
    registry = tmp_path / "unreadable profile" / "installed_plugins.json"
    registry.mkdir(parents=True)
    proc, calls = _run_powershell(
        tmp_path, Scenario(clients=clients, requested=True, profile_var="CLAUDE_CODE_PLUGIN_CACHE_DIR",
                           profile_dir=registry.parent.name))
    assert proc.returncode == status, _all_output(proc)
    assert "REMAINING_STEPS_FINISHED" in proc.stdout.decode()
    assert calls == []
    if clients == "claude":
        assert proc.stderr.decode().splitlines()[-1] == (
            f"Requested client setup incomplete (Claude Code). Plugin was not installed. "
            f"Could not read the Claude Code plugin registry at {registry}. Fix its permissions and rerun the installer."
        )
    else:
        assert proc.stderr == b""
        assert "Could not read the Claude Code plugin registry" not in proc.stdout.decode()


@pytest.mark.parametrize("bash", _bash_variants(), ids=lambda name: Path(name).name)
@pytest.mark.parametrize("requested", [True, False], ids=["requested", "detected"])
def test_bash_unreadable_plugin_registry_does_not_abort(tmp_path, bash, requested):
    registry = tmp_path / "unreadable profile" / "installed_plugins.json"
    registry.mkdir(parents=True)
    proc, calls = _run_bash(
        bash, tmp_path, Scenario(requested=requested, install_records=False,
                                 profile_var="CLAUDE_CODE_PLUGIN_CACHE_DIR",
                                 profile_dir=registry.parent.name))
    assert "REMAINING_STEPS_FINISHED" in proc.stdout.decode()
    assert proc.returncode == (5 if requested else 0), _all_output(proc)
    registry_path = _bash_fixture_path(bash, registry, _fixture_env(tmp_path / "bash-env"))
    lines = proc.stderr.decode().splitlines()
    label = "Requested" if requested else "Detected"
    assert lines[-1] == (
        f"{label} client setup incomplete (Claude Code). Plugin was not installed. "
        f"Could not read the Claude Code plugin registry at {registry_path}. "
        "Fix its permissions and rerun the installer."
    )
    if not requested:
        assert lines[-2] == "Warnings"
    assert calls == []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_success_exit_without_a_record_is_a_failure(variant, tmp_path):
    """The read-back is the proof, not the exit code: a CLI that returns 0
    without recording the plugin leaves the hooks unwired."""
    state, line, _, _ = _run(variant, tmp_path, Scenario(install_records=False))
    assert state == "failed"
    assert line.startswith("[!] Plugin") and "installed_plugins.json" in line


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_older_cli_without_yes_flag_is_called_flagless(variant, tmp_path):
    state, _, calls, _ = _run(variant, tmp_path, Scenario(help_has_yes=False))
    assert f"claude|plugin install {PLUGIN_ID}" in calls
    assert not any("--yes" in c for c in calls)
    assert state == f"installed:{FAKE_VERSION}"


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_other_clients_do_not_touch_the_plugin(variant, tmp_path):
    state, _, calls, _ = _run(variant, tmp_path, Scenario(clients="codex gemini"))
    assert calls == []
    assert state == ""


MOVED = ("The pseudolife-mcp plugin marketplace moved from its github source (SSH) to HTTPS, "
         "https://github.com/Pseudogiant-xr/Pseudolife-MCP.git (backups in /x/backup).")


CURRENT = "The pseudolife-mcp plugin marketplace already fetches over HTTPS; nothing to change."


def _helper_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "helper-calls.txt"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
@pytest.mark.parametrize("said", [MOVED, CURRENT], ids=["moved", "current"])
def test_a_rerun_checks_the_marketplace_source_and_says_so_in_one_line(variant, tmp_path, said):
    """Up to 0.16.1 the installers added the marketplace by the owner/repo
    shorthand, which Claude Code refreshes over SSH; on a host with no
    GitHub SSH key the plugin stopped updating (homelab box, 2026-10-04).
    Rerunning the installer on an installed host now moves it to HTTPS
    through the native client's plugin-marketplace step (the
    ops/plugin_marketplace.py logic it replaced is tested in
    tests/test_update_clients.py) and reports that in one line; an HTTPS
    marketplace is reported current in one line."""
    state, _, calls, proc = _run(variant, tmp_path, Scenario(marketplace=True, plugin=True, helper_out=said))
    assert _helper_calls(tmp_path) == ["helper|plugin-marketplace"]
    assert _all_output(proc).count(said) == 1
    assert state == f"present:{FAKE_VERSION}"
    assert not any("plugin" in c for c in calls)


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_a_fresh_machine_runs_the_check_silently(variant, tmp_path):
    state, _, _, proc = _run(variant, tmp_path, Scenario())
    assert _helper_calls(tmp_path) == ["helper|plugin-marketplace"]
    assert "marketplace moved" not in _all_output(proc) and "WARNING" not in _all_output(proc)
    assert state == f"installed:{FAKE_VERSION}"


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_a_failed_move_is_a_warning_not_a_stop(variant, tmp_path):
    said = ("The pseudolife-mcp plugin marketplace could not be moved from its github source (SSH) to HTTPS: "
            "claude plugin marketplace add failed (offline).")
    state, _, _, proc = _run(variant, tmp_path, Scenario(marketplace=True, plugin=True, helper_out=said, helper_exit=1))
    assert "WARNING" in _all_output(proc) and "could not be moved" in _all_output(proc)
    assert state == f"present:{FAKE_VERSION}"


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
@pytest.mark.parametrize("scenario", [dict(flag="skip", marketplace=True, plugin=True), dict(cli=False)],
                         ids=["skip-flag", "no-cli"])
def test_the_check_needs_the_cli_and_respects_skip(variant, tmp_path, scenario):
    _run(variant, tmp_path, Scenario(**scenario))
    assert _helper_calls(tmp_path) == []
