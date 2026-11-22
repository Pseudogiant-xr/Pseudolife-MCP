"""The native installer (ops/install-native.*): the tests of test_client_install_ux.py
that the native client path changed, run against the native installer.

test_client_install_ux.py keeps the default (Python client) installer's tests.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_installer_client_only_native import _Shell, _stub
from tests.test_installer_client_only_ps_native import _PowerShell
from tests.test_installer_existing_upgrade import _bash_variants


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _marker_block(text: str, name: str) -> list[str]:
    """Lines strictly between ``# >>> <name> >>>`` and ``# <<< <name> <<<``."""
    lines = text.splitlines()
    return lines[lines.index(f"# >>> {name} >>>") + 1:
                 lines.index(f"# <<< {name} <<<")]


def _heredoc_payload(block: list[str]) -> list[str]:
    """The literal payload inside a marker block: a bash quoted heredoc
    (``cat <<'TAG'`` ... ``TAG``) or a PowerShell literal here-string
    (``@'`` ... ``'@``). Both terminators must sit at column 0."""
    for i, line in enumerate(block):
        stripped = line.strip()
        if stripped.endswith("@'"):
            term = "'@"
        elif "<<'" in stripped:
            term = stripped.split("<<'", 1)[1].split("'", 1)[0]
        else:
            continue
        for j in range(i + 1, len(block)):
            if block[j].rstrip() == term:
                return block[i + 1:j]
    raise AssertionError("no heredoc/here-string payload in marker block")


def test_installers_wire_codex_via_shim_by_default() -> None:
    """Shim transport applies to BOTH clients (2026-07-19): a Codex session
    spawns its own shim process and gets tier-1 per-session identity instead
    of inheriting the Claude hook's machine-scoped tier-3 pointer
    (configuration.md#session-identity, cross-client paragraph). The HTTP
    one-liner stays for --transport http and as the fallback when the
    native client cannot be installed."""
    ps = _read("ops/install-native.ps1")
    sh = _read("ops/install-native.sh")
    for text, stdio, http in (
        (ps, "codex mcp add pseudolife-memory", "codex mcp add pseudolife-memory --url"),
        (sh, "register_client codex shim-env pseudolife-memory", "register_client codex http pseudolife-memory --url"),
    ):
        assert any(stdio in line and "PSEUDOLIFE_MCP_NO_SPAWN=1" in line for line in text.splitlines())
        assert http in text


def test_hook_installers_wire_user_prompt_submit_for_both_clients() -> None:
    """Both clients get the per-turn memory-change note; Codex runs it after
    trust. The static line stays in both files only so re-runs can migrate
    it (test_installer_legacy_hooks.py runs the scripts)."""
    ps = _read("ops/install-hook.ps1")
    sh = _read("ops/install-hook-native.sh")
    for text in (ps, sh):
        assert "UserPromptSubmit" in text
        assert "pseudolife-mcp prompt-hook" in text
    # install-hook-native.sh's JSON edit is the native client's hook-settings step.
    assert '"$NATIVE_CLI" installer hook-settings install' in sh
    rust = _read("rust/shim/src/cli/installer/hook_settings.rs")
    assert 'add_group(prompt_groups, &prompt)' in rust and "UserPromptSubmit" in rust
    assert 'if ($Client -in "claude", "codex")' in ps


def test_installers_offer_dreamer_model_choice() -> None:
    """Claude-shim installs prompt for the dreamer model (2026-08-04): all
    current Anthropic tiers are offered, Opus is the recommended default
    (dreamer-choice-verdict.json; Opus 5.5 since 2026-09-29, on
    ladder-opus55-paired-verdict-threshold.json), and the choice reaches the
    autostart script instead of being hardcoded there."""
    ps = _read("ops/install-native.ps1")
    sh = _read("ops/install-native.sh")
    for text in (ps, sh):
        for model in ("claude-opus-5", "claude-opus-5-5", "claude-sonnet-5-5",
                      "claude-sonnet-5", "claude-haiku-5-5", "claude-haiku-4-5",
                      "claude-fable-5-1", "claude-fable-5"):
            assert model in text, f"missing model option: {model}"
    assert '"--model", $Model' in ps     # choice forwarded to autostart
    assert '--model "$MODEL"' in sh


def test_installer_env_block_covers_codex_modes() -> None:
    """openai-fallback / openai-only write the same env-triple shapes as the
    claude pair (fallback => auto + sidecar pair; only => primary), and the
    installer-managed override marker keeps recognizing files written by
    earlier installs (the '(sonnet-only)' and '(shim-only extractor)' texts)
    while writing the generalized marker."""
    ps = _read("ops/install-native.ps1")
    sh = _read("ops/install-native.sh")
    for text in (ps, sh):
        assert "managed override (sidecar disabled)" in text
        assert "managed override (shim-only extractor)" in text  # legacy accepted
        assert "managed override (sonnet-only)" in text     # legacy accepted
    # codex modes reach the autostart stage with the codex script, not the
    # claude one.
    assert '@("extractor-shim", "install-autostart", $shimAutostartFamily' in ps
    assert '(extractor-shim install-autostart "$shim_autostart_family"' in sh
    assert "shim_autostart_family=codex" in sh and 'elseif ($codexShimMode) { "codex" }' in ps


def test_the_shim_autostart_runs_the_stable_launcher() -> None:
    """The launcher re-executes the newest numbered runtime, so without
    --native-exe the service would record that runtime and keep running it
    after an update (or fail once it is pruned)."""
    assert '"${shim_autostart_args[@]}" --native-exe "$SHIM_PATH"' in _read("ops/install-native.sh")
    assert '$autostartArgs += @("--native-exe", $script:shimInstallPath)' in _read("ops/install-native.ps1")


def test_preflight_checks_for_no_python_tooling_on_either_platform() -> None:
    """The client is the native binary on every platform, so neither
    preflight checks for Python, venv or pipx any more (they used to, when
    the shim was a Python package); the Docker ownership check is the native
    client's ``installer preflight-check``."""
    for rel in ("ops/preflight-native.sh", "ops/preflight-native.ps1"):
        text = _read(rel)
        assert "pipx" not in text and "ensurepip" not in text, rel
        assert "installer preflight-check" in text, rel


def test_codex_installers_bootstrap_and_register_rotatable_credentials() -> None:
    for rel in ("ops/install-native.sh", "ops/install-native.ps1"):
        text = _read(rel)
        # The native client's codex-credentials step (what
        # ops/setup-codex-coordination.py --credentials did).
        assert "codex-credentials" in text, rel
        assert "setup-codex-coordination.py" not in text, rel
        assert "PSEUDOLIFE_MCP_TOKEN_FILE" in text, rel


def test_codex_installers_apply_runtime_defaults_only_after_fresh_registration() -> None:
    sh = _read("ops/install-native.sh")
    ps = _read("ops/install-native.ps1")
    for rel, text in (("ops/install-native.sh", sh), ("ops/install-native.ps1", ps)):
        # The native client's step for setup-codex-coordination.py --runtime-defaults.
        assert "codex-runtime-defaults" in text, rel
        assert "startup_timeout_sec = 240" not in text, rel
        assert "tool_timeout_sec = 180" not in text, rel
        assert "runtime defaults were not confirmed" in text, rel
    assert sh.count("configure_codex_runtime_defaults") == 3
    assert ps.count("Set-CodexRuntimeDefaults") == 3
    assert "configure_codex_runtime_defaults" not in sh.split(
        'if existing_codex=$(codex mcp get pseudolife-memory', 1)[1].split(
            'elif [ "$TRANSPORT" = "shim" ]', 1)[0]
    assert "Set-CodexRuntimeDefaults" not in ps.split(
        '$existingCodex = codex mcp get pseudolife-memory', 1)[1].split(
            '} elseif (($Transport -eq "shim")', 1)[0]
    assert 'CODEX_RUNTIME_DEFAULTS=failed' in sh
    assert '$script:codexRuntimeDefaults = "failed"' in ps


INSTALLER_SHELLS = [*_bash_variants(), "powershell"]


def _stage(path, start, end):
    return _read(path).split(start, 1)[1].split("\n", 1)[1].split(end, 1)[0]


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
def test_requested_client_instruction_write_failure_is_a_warning(tmp_path, shell):
    if shell == "powershell":
        runner = _PowerShell(tmp_path)
        path = "ops/install-native.ps1"
        setup = (
            "$clients = @('claude'); $requestedClients = $clients; $hookState = @{claude = 'hook'}\n"
            "$instructionChoice = 'append'\n"
        )
        continued = "Write-Output REMAINING_STEPS_FINISHED\n"
    else:
        runner = _Shell(shell, tmp_path)
        path = "ops/install-native.sh"
        setup = (
            "CLIENTS=claude REQUESTED_CLIENTS=claude HOOK_CLAUDE=hook instruction_choice=append\n"
            "CLAUDE_PLUGIN_INSTALLED=''\n"
        )
        continued = "echo REMAINING_STEPS_FINISHED\n"
    # A file in place of the parent makes the write fail even for an elevated runner.
    (runner.home / ".claude").write_text("fixture", encoding="utf-8")
    target = runner.home / ".claude" / "CLAUDE.md"
    display_path = target if shell == "powershell" else runner.path(target)
    proc = runner.run(
        setup + _stage(path, "10. standing memory instructions", "11. wire into selected MCP clients")
        .rsplit("\n#", 1)[0] + "\n" + continued
        + "\n".join(_marker_block(_read(path), "client registration state")) + "\n"
        + "\n".join(_marker_block(_read(path), "client setup result")) + "\n"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
    assert proc.stderr.splitlines()[-2:] == [
        "Warnings",
        f"Standing instructions could not be written to {display_path}. Check permissions and rerun.",
    ]


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
@pytest.mark.parametrize("choice", ["flag", "prompt", "restored", "default"])
def test_extractor_autostart_failure_at_completion(tmp_path, shell, choice):
    mode = "claude-only" if choice in ("flag", "restored") else ""
    restored = choice == "restored"
    if shell == "powershell":
        runner = _PowerShell(tmp_path)
        path = "ops/install-native.ps1"
        (tmp_path / "scheduled_task_lock.ps1").touch()
        (tmp_path / "fixture-shim.ps1").write_text(
            "Write-Output (\"AUTOSTART \" + ($args -join ' '))\nexit 19\n", encoding="utf-8"
        )
        setup = (
            f"$Extractor = '{mode}'; $restoredExtractor = {'$true' if restored else '$false'}\n"
            "$clients = @(); $hookState = @{}; $instrState = @{}\n"
            f"$ShimPort = 0; $Model = 'haiku'; $interactive = {'$true' if choice == 'prompt' else '$false'}\n"
            "$script:shimInstallPath = Join-Path $PSScriptRoot 'fixture-shim.ps1'\n"
            "function Read-Host { '1' }\n"
            "function Assert-ExtractorArgs {}\n"
            "function claude { throw 'unexpected Claude invocation' }\n"
            "function Install-ShimOnce { return $true }\n"
            "function Get-ScheduledTask { return $null }\n"
            "function Get-NetTCPConnection { return $null }\n"
        )
        continued = "Write-Output REMAINING_STEPS_FINISHED\n"
    else:
        runner = _Shell(shell, tmp_path)
        path = "ops/install-native.sh"
        launcher = runner.bin / "fixture-shim"
        _stub(launcher, "printf 'AUTOSTART %s\\n' \"$*\"\nexit 19\n")
        _stub(runner.bin / "systemctl", "exit 1\n")
        setup = (
            f"EXTRACTOR='{mode}' restored_extractor='{'1' if restored else ''}'\n"
            f"CLIENTS='' CLIENT_ONLY='' MODEL=haiku SHIM_PORT=0 SHIM_OK=1 SHIM_PATH='{runner.path(launcher)}'\n"
            "check_extractor_args() { :; }\n"
            "ensure_shim() { :; }\n"
        )
        if choice == "prompt":
            setup += (
                # Answer only the extractors menu. The fixture passes its
                # script over stdin, so there is no real terminal to detect.
                "function [ { if [[ $* == '! -t 0 ]' ]]; then return 1; fi; builtin [ \"$@\"; }\n"
                "read() { if [[ $* == '-r choice' ]]; then choice=1; else builtin read \"$@\"; fi; }\n"
            )
        continued = "echo REMAINING_STEPS_FINISHED\n"
    proc = runner.run(
        setup + _stage(path, "3. extractor choice", "# >>> endpoint preflight >>>")
        + "\n".join(_marker_block(_read(path), "client registration state")) + "\n"
        + "\n".join(_marker_block(_read(path), "shim autostart")) + "\n"
        + continued + "\n".join(_marker_block(_read(path), "client setup result")) + "\n"
    )
    output = proc.stdout + proc.stderr
    assert ("AUTOSTART extractor-shim install-autostart claude" in proc.stdout) == (choice != "default")
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.returncode == (5 if choice in ("flag", "prompt") else 0), output
    if choice in ("flag", "prompt"):
        assert proc.stderr.splitlines()[-1] == (
            "Requested extractor setup incomplete (claude-only). Shim autostart registration or start failed. "
            "Fix the reported error and rerun the installer with the same extractor mode."
        )
    else:
        assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
        if restored:
            assert proc.stderr.splitlines()[-2:] == [
                "Warnings", "Extractor shim autostart failed. See the repair command above."
            ]
        else:
            assert proc.stderr == ""


def test_install_sh_shim_failure_falls_back_instead_of_aborting() -> None:
    """A failed shim install must leave SHIM_OK unset so the HTTP fallback
    fires, not kill the run via errexit (issue #176). The native client
    install is the condition of an `if`, so errexit is suspended around it,
    and its own failure paths return a status instead of exiting."""
    sh = _read("ops/install-native.sh")
    ensure = sh[sh.index("ensure_shim() {"):]
    ensure = ensure[:ensure.index("\n}\n")]
    assert "    if native_install; then" in ensure
    install = sh[sh.index("native_install() {"):]
    install = install[:install.index("\n}\n")]
    # The install's status is captured rather than piped, so exit 3 (a
    # refused move, issue #1632) can be told apart; any other failure still
    # returns 1 instead of exiting.
    assert '--version "$NATIVE_VERSION")" || native_install_status=$?' in install
    assert '[ "$native_install_status" = 0 ] || return 1' in install
    assert "|| return 1" in install.split("SHIM_PATH=", 1)[0]


def test_installers_register_the_shim_autostart_after_installing_the_shim() -> None:
    """The MCP shim install (pipx, or pip --user) is what puts the package
    where an interpreter imports it on a host without a checkout venv, so the
    autostart registration must run after it, or the picker builds a venv of
    its own that the installer then duplicates a few steps later."""
    sh = _read("ops/install-native.sh")
    ps = _read("ops/install-native.ps1")
    assert sh.index("ensure_shim() {") < sh.index("CLI shim autostart (Claude / Codex modes)")
    assert ps.index("function Install-ShimOnce") < ps.index("CLI shim autostart (Claude / Codex modes)")
    # And it sits OUTSIDE every stage range the installer tests execute
    # (stages 9 through the '12. health' header, and stage 11's loop up to
    # that header): a test that reached it would call the real systemctl
    # or Task Scheduler and could disable a live shim on the machine running
    # the suite. Before the health wait, so an unhealthy daemon still leaves
    # the autostart registered.
    for text, health, wait in (
            (sh, "\n# ── 12. health", 'step "Waiting for the daemon to report healthy..."'),
            (ps, "\n# -- 12. health", 'Step "Waiting for the daemon to report healthy..."')):
        start = text.index("# >>> shim autostart >>>")
        assert text.count("# >>> shim autostart >>>") == 1
        assert text.index(health) < start < text.index("# <<< shim autostart <<<") < text.index(wait)
    # The failure hint names the interpreter refusal beside the systemd one.
    assert "extractor-shim install-autostart" in sh.split("CLI shim autostart (Claude / Codex modes)", 1)[1]


def test_installers_wire_claude_desktop_through_the_shared_register_script() -> None:
    """Claude Desktop has no `mcp add` CLI and launches MCP servers with a
    sanitized environment (no PATH extras, no user env vars), so its entry
    is merged into claude_desktop_config.json by the native client's
    claude-desktop step (ops/register_claude_desktop.py's logic) — one
    implementation both installers call — with the shim's ABSOLUTE
    path and, for a token-gated daemon, a token FILE path: never a token
    value, never a reliance on the OS environment (2026-09-19 incident:
    four days of 401s behind "unhandled errors in a TaskGroup")."""
    sh = _read("ops/install-native.sh")
    ps = _read("ops/install-native.ps1")
    for text, rel, call in ((sh, "install-native.sh", "native_helper claude-desktop"),
                            (ps, "install-native.ps1", "Invoke-NativeHelper claude-desktop")):
        assert call in text, rel
        assert "--writer-id" in text and "--token-file" in text, rel
        assert "Claude Desktop" in text, rel          # menu + ladder
        assert "fully quit" in text, rel              # the relaunch step
        assert "sanitized environment" in text, rel   # the why, in the script
        # The token VALUE reaches the registrar only by env-var NAME, so it
        # never appears on a command line or in the config file.
        assert "--token-from-env PSEUDOLIFE_DESKTOP_TOKEN_SOURCE" in text.replace(
            '", "', " ").replace('"', ""), rel
        assert "--token " not in text and '"--token"' not in text, rel
        assert "PSEUDOLIFE_MCP_TOKEN=" not in text.split("claude-desktop", 1)[1].split(
            "generic", 1)[0].replace("PSEUDOLIFE_MCP_TOKEN=<token>", ""), rel
    # Provider validation admits it, in both spellings of the list.
    assert 'claude|claude-desktop|codex|gemini|generic) expanded="$expanded $tok"' in sh
    assert '{ $_ -in "claude", "claude-desktop", "codex", "gemini", "generic" }' in ps
    # Preflight accepts it (install.* passes the list straight through).
    for rel in ("ops/preflight-native.sh", "ops/preflight-native.ps1"):
        assert "claude-desktop" in _read(rel), rel
    # The synced capability matrix names it.
    matrix = "\n".join(_heredoc_payload(_marker_block(sh, "capability-matrix")))
    assert "Claude Desktop" in matrix
    # A single-client Desktop install gets its own daemon-side writer id.
    assert "claude-desktop) WRITER_ID=claude-desktop" in sh
    assert '"claude-desktop" { "claude-desktop" }' in ps


def test_install_ps1_puts_the_launcher_on_the_user_path_and_says_so_in_the_summary() -> None:
    """Once the native client installs, the launcher directory goes first on
    the user PATH (HKCU only), so `pseudolife-mcp` in a new terminal is the
    launcher rather than an older pipx copy (2026-09-29); the outcome is
    repeated in the summary, before the board line, marked [!] when the step
    failed."""
    ps = _read("ops/install-native.ps1")
    native = ps[ps.index("function Install-NativeClient"):]
    native = native[:native.index("\n}\n")]
    assert "Add-NativeLauncherToUserPath $launcher" in native
    assert native.index("Add-NativeLauncherToUserPath") < native.index("return $launcher")
    add_path = ps[ps.index("function Add-NativeLauncherToUserPath"):]
    add_path = add_path[:add_path.index("\n}\n")]
    assert '& (Join-Path $repo "ops/add-user-path.ps1") -Dir $dir' in add_path
    # a test or custom layout (PSEUDOLIFE_SHIM_LAUNCHER) never touches PATH
    assert "if ($env:PSEUDOLIFE_SHIM_LAUNCHER) { return $null }" in add_path
    helper = _read("ops/add-user-path.ps1")
    # %VAR% entries stay as written: the raw value, written back with its kind
    assert "DoNotExpandEnvironmentNames" in helper
    assert '$key.SetValue("Path", ((@($dir) + $rest) -join ";"), $kind)' in helper
    assert "Registry]::CurrentUser" in helper and "LocalMachine" not in helper
    assert "add-user-path.ps1" in _read("ops/install-native.sh")
    once = ps[ps.index("function Install-ShimOnce"):]
    once = once[:once.index("\n}\n")]
    assert once.index("Install-NativeClient") < once.index("$script:shimInstallResult = $true")
    summary = ps[ps.index('if ($script:shimPathNotes.Count -gt 0) {'):]
    assert summary.index("Launcher on PATH") < summary.index("# >>> board line >>>")
    assert '$pathMarker = if ($script:shimPathState -eq "failed") { "[!]" } else { "[x]" }' in summary
    # the machine PATH is never written
    assert "'Machine'" not in ps and '"Machine"' not in ps
