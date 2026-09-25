"""The installer installs the Claude Code plugin; the two /plugin commands
are no longer a manual step.

The plugin (hooks + commands) was the one install beside the daemon and the
shim that a new user had to type inside Claude Code, and the one an updating
user forgot (2026-09-21). Both installers now add the marketplace and install
the plugin when Claude Code is a selected client, leave an installed plugin
alone, and report the result on the wiring ladder. These tests run only the
plugin step of each script with a fake ``claude`` CLI that records its calls
and writes the same registry files the real one does.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_installer_existing_upgrade import (
    _bash_fixture_path, _bash_variants, _between, _fixture_env,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = "pseudolife-memory@pseudolife-mcp"
MARKETPLACE_SOURCE = "Pseudogiant-xr/Pseudolife-MCP"
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
        "pseudolife-mcp": {"source": {"source": "github", "repo": MARKETPLACE_SOURCE}},
    }, indent=2), encoding="utf-8")


class Scenario:
    def __init__(self, *, clients="claude", flag="auto", cli=True, marketplace=False,
                 plugin=False, marketplace_exit=0, install_exit=0, install_records=True,
                 help_has_yes=True):
        self.clients, self.flag, self.cli = clients, flag, cli
        self.marketplace, self.plugin = marketplace, plugin
        self.marketplace_exit, self.install_exit = marketplace_exit, install_exit
        self.install_records, self.help_has_yes = install_records, help_has_yes


def _run_bash(bash: str, tmp_path: Path, sc: Scenario) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
    block = _between("ops/install.sh", "# >>> claude plugin >>>", "# <<< claude plugin <<<")
    env = _fixture_env(tmp_path / "bash-env")
    home = Path(env["HOME"])
    if sc.marketplace:
        _record_marketplace(home)
    if sc.plugin:
        _record_plugin(home)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.txt"
    if sc.cli:
        # Writes the same registry files the real CLI does, pretty-printed
        # like the real ones, so the installer's read-back sees them.
        (fake_bin / "claude").write_text(
            "#!/bin/sh\n"
            "printf 'claude|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
            "plugins=\"$HOME/.claude/plugins\"\n"
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
    # The installer's own strict mode: an abort in the block must fail here.
    script = f"""set -euo pipefail
PATH='{fake_bin_shell}:/usr/bin:/bin'
export PATH CALL_LOG='{call_log_shell}'
export FAKE_HELP_YES='{"yes" if sc.help_has_yes else "no"}' FAKE_MARKET_EXIT='{sc.marketplace_exit}'
export FAKE_INSTALL_EXIT='{sc.install_exit}' FAKE_INSTALL_RECORD='{"yes" if sc.install_records else "no"}' FAKE_VERSION='{FAKE_VERSION}'
CLIENTS='{sc.clients}'
CLAUDE_PLUGIN='{sc.flag}'
step() {{ printf 'STEP: %s\\n' "$*"; }}
{block}
install_claude_plugin
printf 'STATE=%s\\n' "$PLUGIN_CLAUDE"
printf 'LINE=%s\\n' "$(describe_plugin "$PLUGIN_CLAUDE")"
"""
    proc = subprocess.run([bash], input=script.encode(), capture_output=True, check=False,
                          timeout=20, env=env)
    calls = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    return proc, calls


def _run_powershell(tmp_path: Path, sc: Scenario) -> tuple[subprocess.CompletedProcess[bytes], list[str]]:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is unavailable")
    block = _between("ops/install.ps1", "# >>> claude plugin >>>", "# <<< claude plugin <<<")
    env = _fixture_env(tmp_path / "powershell-env")
    home = Path(env["USERPROFILE"])
    if sc.marketplace:
        _record_marketplace(home)
    if sc.plugin:
        _record_plugin(home)
    call_log = tmp_path / "calls.txt"
    escaped_log = str(call_log).replace("'", "''")
    fake = "" if not sc.cli else f"""function global:claude {{
    $callArgs = @($args)
    Add-Content -LiteralPath '{escaped_log}' -Value ('claude|' + ($callArgs -join ' '))
    $plugins = Join-Path $env:USERPROFILE '.claude\\plugins'
    if ($callArgs[0] -eq 'plugin' -and $callArgs[1] -eq 'install' -and $callArgs[2] -eq '--help') {{
        if ($env:FAKE_HELP_YES -eq 'yes') {{ Write-Output '  -y, --yes   Accept the displayed command' }}
        $global:LASTEXITCODE = 0; return
    }}
    if ($callArgs[0] -eq 'plugin' -and $callArgs[1] -eq 'marketplace' -and $callArgs[2] -eq 'add') {{
        if ($env:FAKE_MARKET_EXIT -eq '0') {{
            New-Item -ItemType Directory -Force $plugins | Out-Null
            @{{ 'pseudolife-mcp' = @{{ source = @{{ source = 'github'; repo = $callArgs[3] }} }} }} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $plugins 'known_marketplaces.json')
        }}
        $global:LASTEXITCODE = [int]$env:FAKE_MARKET_EXIT; return
    }}
    if ($callArgs[0] -eq 'plugin' -and $callArgs[1] -eq 'install') {{
        if ($env:FAKE_INSTALL_EXIT -eq '0' -and $env:FAKE_INSTALL_RECORD -eq 'yes') {{
            New-Item -ItemType Directory -Force $plugins | Out-Null
            @{{ version = 2; plugins = @{{ '{PLUGIN_ID}' = @(@{{ scope = 'user'; version = $env:FAKE_VERSION }}) }} }} | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $plugins 'installed_plugins.json')
        }}
        $global:LASTEXITCODE = [int]$env:FAKE_INSTALL_EXIT; return
    }}
    $global:LASTEXITCODE = 91
}}"""
    script = f"""$ErrorActionPreference = 'Stop'
$env:FAKE_HELP_YES = '{"yes" if sc.help_has_yes else "no"}'
$env:FAKE_MARKET_EXIT = '{sc.marketplace_exit}'
$env:FAKE_INSTALL_EXIT = '{sc.install_exit}'
$env:FAKE_INSTALL_RECORD = '{"yes" if sc.install_records else "no"}'
$env:FAKE_VERSION = '{FAKE_VERSION}'
$env:PATH = ''
$clients = @({", ".join("'" + c + "'" for c in sc.clients.split())})
$ClaudePlugin = '{sc.flag}'
function Step($message) {{ Write-Output "STEP: $message" }}
{fake}
{block}
Install-ClaudePlugin
Write-Output "STATE=$($script:pluginClaude)"
Write-Output ("LINE=" + (Describe-Plugin $script:pluginClaude))
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


def _run_instructions(variant, tmp_path: Path, *, choice: str, plugin: bool,
                      block_present: bool = False) -> tuple[str, str, Path]:
    """Run section 10 (standing memory instructions) of one installer for
    Claude alone, against a disposable home. Returns the recorded state, the
    ladder line, and the Claude standing file the section would write."""
    kind, bash = variant
    env = _fixture_env(tmp_path / f"{kind}-env")
    home = Path(env["HOME"])
    claude_md = home / ".claude" / "CLAUDE.md"
    if block_present:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text("# mine\n\n## Memory (pseudolife-memory MCP)\n", encoding="utf-8")
    if kind == "bash":
        section = _between("ops/install.sh", "# ── 10. standing memory instructions",
                           "# ── 11. wire into selected MCP clients")
        describe = _between("ops/install.sh", "describe_instr() {", "\n}\n") + "\n}"
        repo = _bash_fixture_path(bash, ROOT, env).replace("'", "'\\''")
        script = f"""set -euo pipefail
repo='{repo}'
CLIENTS='claude'
CLAUDE_PLUGIN_INSTALLED='{"1" if plugin else ""}'
instruction_choice='{choice}'
INSTR_CODEX='' CODEX_SETUP_VALID='' CODEX_HOOK_TRUST=ask CODEX_HOOK_RECOVERY='' AGENTS_FILE=''
step() {{ printf 'STEP: %s\\n' "$*"; }}
{section}
{describe}
printf 'STATE=%s\\n' "$INSTR_CLAUDE"
printf 'LINE=%s\\n' "$(describe_instr "$INSTR_CLAUDE")"
"""
        proc = subprocess.run([bash], input=script.encode(), capture_output=True,
                              check=False, timeout=20, env=env)
    else:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("PowerShell 7 is unavailable")
        section = _between("ops/install.ps1", "# -- 10. standing memory instructions",
                           "# -- 11. wire into selected MCP clients")
        describe = _between("ops/install.ps1", "function Describe-Instr($state) {", "\n}\n") + "\n}"
        script = f"""$ErrorActionPreference = 'Stop'
$repo = '{str(ROOT).replace("'", "''")}'
$clients = @('claude')
$claudePluginInstalled = ${"true" if plugin else "false"}
$instructionChoice = '{choice}'
$codexSetup = $null
$codexSetupValid = $false
$CodexHookTrust = 'ask'
$AgentsFile = ''
$interactive = $false
function Step($message) {{ Write-Output "STEP: $message" }}
{section}
{describe}
Write-Output "STATE=$($instrState['claude'])"
Write-Output ("LINE=" + (Describe-Instr $instrState['claude']))
"""
        runner = tmp_path / "run-instructions.ps1"
        runner.write_text(script, encoding="utf-8")
        proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                              capture_output=True, check=False, timeout=30, env=env)
    out = proc.stdout.decode(errors="replace")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace") + out
    state = next(line[len("STATE="):] for line in out.splitlines() if line.startswith("STATE="))
    line = next(line[len("LINE="):] for line in out.splitlines() if line.startswith("LINE="))
    return state, line, claude_md


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_explicit_append_writes_the_claude_block_beside_the_plugin(variant, tmp_path):
    """An explicit `--instructions append` / `-Instructions append` always
    writes the standing block (README install section), plugin or not: the
    plugin hook serves only a compact core, and subagents read CLAUDE.md,
    not hook output. Codex already honoured an explicit append; Claude
    recorded covered-by-plugin before reading the choice (2026-09-25 review)."""
    state, line, claude_md = _run_instructions(variant, tmp_path, choice="append", plugin=True)
    block = (ROOT / "examples" / "CLAUDE.memory.md").read_text(encoding="utf-8")
    assert claude_md.is_file()
    assert block.strip() in claude_md.read_text(encoding="utf-8")
    assert state.startswith("appended:") and state.endswith("CLAUDE.md")
    assert line.startswith("[x] Standing file")


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
@pytest.mark.parametrize("choice", ["auto", "skip"])
def test_auto_and_skip_leave_claude_md_alone_beside_the_plugin(variant, tmp_path, choice):
    """Only an explicit append edits CLAUDE.md beside the plugin; the
    default keeps the 2026-09-25 decision to skip on the plugin path."""
    state, line, claude_md = _run_instructions(variant, tmp_path, choice=choice, plugin=True)
    assert not claude_md.exists()
    assert state == "covered-by-plugin"
    assert line.startswith("[-] Standing file")


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
def test_explicit_append_beside_the_plugin_never_doubles_the_block(variant, tmp_path):
    state, line, claude_md = _run_instructions(variant, tmp_path, choice="append",
                                               plugin=True, block_present=True)
    assert claude_md.read_text(encoding="utf-8").count("pseudolife-memory") == 1
    assert state.startswith("present:")
    assert line.startswith("[x] Standing file")


@pytest.mark.parametrize("variant", VARIANTS, ids=IDS)
@pytest.mark.parametrize("choice", ["append", "auto"])
def test_plugin_banner_does_not_promise_to_skip_a_requested_append(variant, tmp_path, choice):
    """Section 9 announces what the plugin covers before section 10 runs; it
    must not say the CLAUDE.md block is skipped when append was requested
    (reviewer finding on this change, 2026-09-25)."""
    kind, bash = variant
    env = _fixture_env(tmp_path / f"{kind}-env")
    _record_plugin(Path(env["HOME"]))
    if kind == "bash":
        banner = _between("ops/install.sh", 'if grep -q "pseudolife-memory@pseudolife-mcp"',
                          '\nHOOK_CLAUDE=""')
        script = f"""set -euo pipefail
CLIENTS='claude'
instruction_choice='{choice}'
step() {{ printf 'STEP: %s\\n' "$*"; }}
{banner}
"""
        proc = subprocess.run([bash], input=script.encode(), capture_output=True,
                              check=False, timeout=20, env=env)
    else:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            pytest.skip("PowerShell 7 is unavailable")
        banner = _between("ops/install.ps1",
                          'if ($claudePluginInstalled -and ($clients -contains "claude")) {',
                          "\n$hookState = @{}")
        runner = tmp_path / "run-banner.ps1"
        runner.write_text(f"""$ErrorActionPreference = 'Stop'
$clients = @('claude')
$claudePluginInstalled = $true
$instructionChoice = '{choice}'
function Step($message) {{ Write-Output "STEP: $message" }}
{banner}
""", encoding="utf-8")
        proc = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                              capture_output=True, check=False, timeout=30, env=env)
    out = _all_output(proc)
    assert proc.returncode == 0, out
    assert "plugin detected" in out
    if choice == "append":
        assert "CLAUDE.md block is still appended" in out
        assert "hook and CLAUDE.md block" not in out
    else:
        assert "hook and CLAUDE.md block" in out
        assert "still appended" not in out


def test_both_installers_document_the_flag_and_call_the_step():
    sh = (ROOT / "ops/install.sh").read_text(encoding="utf-8")
    ps1 = (ROOT / "ops/install.ps1").read_text(encoding="utf-8")
    assert "--claude-plugin auto|skip" in sh.split("# <<< usage <<<")[0]
    assert '[ValidateSet("auto", "skip")]' in ps1 and "$ClaudePlugin" in ps1
    assert sh.count("\ninstall_claude_plugin\n") == 1
    assert ps1.count("\nInstall-ClaudePlugin\n") == 1
    # The step precedes hook ownership so section 9 sees the fresh install.
    assert sh.index("\ninstall_claude_plugin\n") < sh.index("# ── 9. session lifecycle hooks")
    assert ps1.index("\nInstall-ClaudePlugin\n") < ps1.index("# -- 9. session lifecycle hooks")
