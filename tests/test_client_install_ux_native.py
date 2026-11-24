"""The native installer (ops/install-native.*): the tests of test_client_install_ux.py
that the native client path changed, run against the native installer.

test_client_install_ux.py keeps the default (Python client) installer's tests.
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_installer_client_only_native import _Shell, _stub
from tests.test_installer_client_only_ps_native import _PowerShell, _q
from tests.test_installer_existing_upgrade import _bash_variants
from tests.test_installer_reruns_native import BEGIN, END, managed


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
    # The definition and its three fresh-registration calls: the shim
    # registration, the HTTP fallback and the explicit --transport http (#1903).
    assert sh.count("configure_codex_runtime_defaults") == 4
    assert ps.count("Set-CodexRuntimeDefaults") == 3
    assert "configure_codex_runtime_defaults" not in sh.split(
        'if existing_codex=$(codex mcp get pseudolife-memory', 1)[1].split(
            'elif [ "$TRANSPORT" = "shim" ]', 1)[0]
    assert "Set-CodexRuntimeDefaults" not in ps.split(
        '$existingCodex = codex mcp get pseudolife-memory', 1)[1].split(
            '} elseif (($Transport -eq "shim")', 1)[0]
    assert 'CODEX_RUNTIME_DEFAULTS=failed' in sh
    assert '$script:codexRuntimeDefaults = "failed"' in ps


@pytest.mark.parametrize("bash", _bash_variants() or [None], ids=lambda name: Path(name).name if name else "none")
def test_remove_legacy_for_codex_names_the_installer_not_a_python_script(tmp_path, bash):
    """#1609: the refusal names a step an installed client has."""
    if bash is None:
        pytest.skip("Bash unavailable")
    proc = subprocess.run([bash, str(ROOT / "ops/install-hook-native.sh"), "--client", "codex", "--remove-legacy"],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)})
    assert proc.returncode == 2, proc
    assert proc.stderr == "--remove-legacy is for Claude Code; for Codex hooks, rerun the installer with --client codex.\n"
    assert ".py" not in proc.stderr


def test_the_plugin_readme_names_no_checkout_python_script():
    """#1609: the plugin README's Codex and update steps exist on an installed client."""
    text = _read("plugin/README.md")
    assert "setup-codex-hooks.py" not in text and "update_clients.py" not in text
    assert "rerun the installer with `--client codex`" in text
    assert "`pseudolife-mcp update --clients-only` on its" in text


INSTALLER_SHELLS = [*_bash_variants(), "powershell"]


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
def test_completion_lists_separate_failures_in_order(tmp_path, shell):
    if shell == "powershell":
        runner = _PowerShell(tmp_path)
        path = "ops/install-native.ps1"
        setup = (
            "$clients = @('claude', 'codex', 'gemini'); $requestedClients = @('claude', 'codex')\n"
            "$hookState = @{}; $instrState = @{}\n"
            "$extractorRequested = $true; $Extractor = 'claude-only'\n"
            "$script:extractorAutostartFailure = 'Fixture extractor repair.'\n"
            "Set-ClientSetupFailure claude 'Fixture Claude repair.'\n"
            "Set-ClientSetupFailure codex 'Fixture Codex repair.'\n"
            "Set-ClientSetupFailure gemini 'Fixture Gemini repair.'\n"
            "Write-ClientHttpAuthResult claude -Existing\n"
            "Write-ClientHttpAuthResult codex -Existing\n"
        )
    else:
        runner = _Shell(shell, tmp_path)
        path = "ops/install-native.sh"
        setup = (
            "CLIENTS='claude codex gemini' REQUESTED_CLIENTS='claude codex'\n"
            "extractor_requested=1 EXTRACTOR=claude-only\n"
            "EXTRACTOR_AUTOSTART_FAILURE='Fixture extractor repair.'\n"
            "record_client_failure claude 'Fixture Claude repair.'\n"
            "record_client_failure codex 'Fixture Codex repair.'\n"
            "record_client_failure gemini 'Fixture Gemini repair.'\n"
            "check_client_http_auth claude existing\n"
            "check_client_http_auth codex existing\n"
        )
    source = _read(path)
    proc = runner.run(
        "\n".join(_marker_block(source, "client registration state")) + "\n"
        + "\n".join(_marker_block(source, "client registration helpers")) + "\n" + setup
        + "\n".join(_marker_block(source, "client setup result")) + "\n"
    )
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert proc.stderr.splitlines()[-7:] == [
        "Warnings",
        "The claude HTTP registration was preserved. The daemon requires a bearer token. "
        "Verify the registration's bearer credential in the client.",
        "The codex HTTP registration was preserved. The daemon requires a bearer token. "
        "Verify the registration's bearer credential in the client.",
        "Detected client setup incomplete (Gemini CLI). Fixture Gemini repair.",
        "Requested client setup incomplete (Claude Code). Fixture Claude repair.",
        "Requested client setup incomplete (Codex). Fixture Codex repair.",
        "Requested extractor setup incomplete (claude-only). Fixture extractor repair.",
    ]


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


EXTRACTOR_FAILURE = (
    "Shim autostart registration or start failed. "
    "Fix the reported error and rerun the installer with the same extractor mode."
)
REQUESTED_EXTRACTOR_FAILURE = "Requested extractor setup incomplete (claude-only). " + EXTRACTOR_FAILURE


def _run_extractor_fixture(runner, *, mode="", answer=None, locked=False, macos=False):
    """Run choice, managed record, stubbed autostart and completion in order."""
    tmp_path = runner.tmp
    env_file = tmp_path / ".env"
    if isinstance(runner, _PowerShell):
        path = "ops/install-native.ps1"
        (tmp_path / "scheduled_task_lock.ps1").touch()
        registrar_output = 'Write-Output ("AUTOSTART " + ($args -join " "))\n'
        if locked:
            registrar_output += (
                "if ($PSNativeCommandUseErrorActionPreference) { throw 'fixture native error promotion' }\n"
                "[Console]::Error.WriteLine('Could not determine the owner of the fixture task.')\nexit 3\n"
            )
        else:
            registrar_output += "exit 19\n"
        (tmp_path / "fixture-shim.ps1").write_text(registrar_output, encoding="utf-8")
        setup = (
            f"$envFile = '{_q(env_file)}'; $EnvBegin = '{BEGIN}'; $EnvEnd = '{END}'\n"
            f"$Extractor = '{mode}'; $interactive = {'$true' if answer is not None else '$false'}\n"
            "$ClientOnly = $false; $DaemonUrl = ''; $clientList = 'generic'; $writerId = 'fixture'\n"
            "$script:BankLocation = 'local'; $CodexHooks = 'auto'; $ClaudePlugin = 'auto'\n"
            "$ClaudeLegacyHooks = 'keep'; $Transport = 'shim'\n"
            "$clients = @(); $hookState = @{}; $instrState = @{}\n"
            "$ShimPort = 0; $Model = 'haiku'\n"
            f"$PSNativeCommandUseErrorActionPreference = {'$true' if locked else '$false'}\n"
            "$script:shimInstallPath = Join-Path $PSScriptRoot 'fixture-shim.ps1'\n"
            f"function Read-Host {{ '{answer or ''}' }}\n"
            "function Assert-ExtractorArgs {}\n"
            "function Get-ExtractorEnvLines {}\n"
            "function claude { throw 'unexpected Claude invocation' }\n"
            "function codex { throw 'unexpected Codex invocation' }\n"
            "function Install-ShimOnce { return $true }\n"
            "function Get-ScheduledTask { return $null }\n"
            "function Get-NetTCPConnection { return $null }\n"
        )
        continued = "Write-Output REMAINING_STEPS_FINISHED\n"
    else:
        path = "ops/install-native.sh"
        launcher = runner.bin / "fixture-shim"
        registrar_output = "printf 'AUTOSTART %s\\n' \"$*\"\n"
        if locked:
            registrar_output += "echo 'Could not determine the owner of the fixture task.' >&2\nexit 3\n"
        else:
            registrar_output += "exit 19\n"
        _stub(launcher, registrar_output)
        _stub(runner.bin / "systemctl", "exit 1\n")
        system = "Darwin" if macos else "Linux"
        setup = (
            f"uname() {{ printf '%s\\n' '{system}'; }}\n"
            f"env_file='{runner.path(env_file)}' ENV_BEGIN='{BEGIN}' ENV_END='{END}'\n"
            f"EXTRACTOR='{mode}' EXTRACTOR_URL='' DAEMON_URL='' CLIENT='' EXPLICIT_ARGS=''\n"
            f"CLIENTS='' CLIENT_ONLY='' MODEL=haiku SHIM_PORT=0 SHIM_OK=1 SHIM_PATH='{runner.path(launcher)}'\n"
            "CLIENT_LIST=generic WRITER_ID=fixture BANK_LOCATION=local CODEX_HOOKS=auto CLAUDE_PLUGIN=auto\n"
            "CLAUDE_LEGACY_HOOKS=keep TRANSPORT=shim\n"
            "check_extractor_args() { :; }\n"
            "extractor_env_lines() { :; }\n"
            "ensure_shim() { :; }\n"
        )
        if answer is not None:
            setup += (
                # Answer only the extractors menu. The fixture passes its
                # script over stdin, so there is no real terminal to detect.
                "function [ { if [[ $* == '! -t 0 ]' ]]; then return 1; fi; builtin [ \"$@\"; }\n"
                f"read() {{ if [[ $* == '-r choice' ]]; then choice='{answer}'; else builtin read \"$@\"; fi; }}\n"
            )
        continued = "echo REMAINING_STEPS_FINISHED\n"
    source = _read(path)
    return runner.run(
        setup + "\n".join(_marker_block(source, "recorded choices")) + "\n"
        + _stage(path, "3. extractor choice", "# >>> endpoint preflight >>>")
        + "\n".join(_marker_block(source, "managed configuration")) + "\n"
        + "\n".join(_marker_block(source, "client registration state")) + "\n"
        + "\n".join(_marker_block(source, "shim autostart")) + "\n"
        + continued + "\n".join(_marker_block(source, "client setup result")) + "\n"
    )


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
@pytest.mark.parametrize("choice", ["flag", "prompt", "restored", "default", "enter", "macos",
                                   "locked-flag", "locked-restored"])
def test_extractor_autostart_failure_at_completion(tmp_path, shell, choice):
    if choice == "macos" and shell == "powershell":
        pytest.skip("macOS runs the native Bash installer")
    locked = choice.startswith("locked-")
    if choice in ("restored", "locked-restored"):
        choices = {"PSEUDOLIFE_INSTALL_EXTRACTOR": "claude-only"}
        if locked:
            choices["PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED"] = "claude-only"
        (tmp_path / ".env").write_text(managed(**choices), encoding="utf-8")
    mode = "claude-only" if choice in ("flag", "macos", "locked-flag") else ""
    answer = {"prompt": "1", "enter": ""}.get(choice)
    runner = _PowerShell(tmp_path) if shell == "powershell" else _Shell(shell, tmp_path)
    proc = _run_extractor_fixture(runner, mode=mode, answer=answer, locked=locked, macos=choice == "macos")
    output = proc.stdout + proc.stderr
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert proc.returncode == (5 if choice in ("flag", "prompt", "locked-flag") else 0), output
    assert ("AUTOSTART extractor-shim install-autostart claude" in proc.stdout) == (
        choice in ("flag", "prompt", "restored") or locked
    )
    if choice in ("flag", "prompt"):
        assert proc.stderr.splitlines()[-1] == REQUESTED_EXTRACTOR_FAILURE
    elif choice == "locked-flag":
        assert proc.stderr.splitlines() == [
            "Could not determine the owner of the fixture task.",
            "Requested extractor setup incomplete (claude-only). An existing Windows scheduled task was left unchanged "
            "because it was registered from an elevated shell, belongs to another account, or its run-as account could "
            "not be verified. Ask the task owner or a Windows administrator to remove it before rerunning the installer "
            "with the same extractor mode.",
        ]
    elif choice == "locked-restored":
        assert proc.stderr.splitlines() == ["Could not determine the owner of the fixture task."]
        assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
    elif choice == "macos":
        lead = "WARNING: Native shim autostart is unavailable on macOS. Start the shim by hand with "
        tail = ", or rerun the installer with --extractor sidecar."
        line, blank, heading, warning = proc.stderr.splitlines()
        assert [blank, heading, warning] == ["", "Warnings", line.removeprefix("WARNING: ")]
        assert line.startswith(lead) and line.endswith(tail)
        assert shlex.split(line[len(lead):-len(tail)]) == [
            runner.path(runner.bin / "fixture-shim"), "extractor-shim", "claude", "--repo", runner.path(ROOT),
            "--port", "8082", "--model", "haiku",
        ]
    else:
        assert proc.stdout.splitlines()[-1] == "Done. First session: tell your coding agent to remember something."
        if choice == "restored":
            assert proc.stderr.splitlines()[-2:] == [
                "Warnings", "Extractor setup incomplete (claude-only). " + EXTRACTOR_FAILURE
            ]
        else:
            assert "Extractor mode: sidecar" in proc.stdout
            assert proc.stderr == ""


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
@pytest.mark.parametrize("selection", ["flag", "prompt"])
def test_explicit_extractor_failure_survives_a_plain_rerun(tmp_path, shell, selection):
    runner = _PowerShell(tmp_path) if shell == "powershell" else _Shell(shell, tmp_path)
    first = _run_extractor_fixture(
        runner, mode="claude-only" if selection == "flag" else "",
        answer="1" if selection == "prompt" else None,
    )
    record = (tmp_path / ".env").read_text(encoding="utf-8")
    second = _run_extractor_fixture(runner)
    for proc in (first, second):
        assert proc.returncode == 5, proc.stdout + proc.stderr
        assert proc.stderr.splitlines()[-1] == REQUESTED_EXTRACTOR_FAILURE
    assert "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=claude-only\n" in record
    assert "Managed configuration unchanged" in second.stdout


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
@pytest.mark.parametrize("marker", [None, "openai-only", "decoy"], ids=["old", "mismatch", "client-value"])
def test_only_matching_managed_provenance_restores_an_explicit_extractor(tmp_path, shell, marker):
    choices = {"PSEUDOLIFE_INSTALL_EXTRACTOR": "claude-only"}
    if marker == "decoy":
        choices["PSEUDOLIFE_INSTALL_CLIENT_REQUESTED"] = "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=claude-only"
    elif marker:
        choices["PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED"] = marker
    # A marker outside the managed block cannot promote a restored choice.
    (tmp_path / ".env").write_text(
        "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=claude-only\n" + managed(**choices), encoding="utf-8")
    runner = _PowerShell(tmp_path) if shell == "powershell" else _Shell(shell, tmp_path)
    proc = _run_extractor_fixture(runner)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr.splitlines()[-2:] == [
        "Warnings", "Extractor setup incomplete (claude-only). " + EXTRACTOR_FAILURE,
    ]


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
def test_default_clears_an_explicit_extractor_marker(tmp_path, shell):
    runner = _PowerShell(tmp_path) if shell == "powershell" else _Shell(shell, tmp_path)
    env_file = tmp_path / ".env"
    first = _run_extractor_fixture(runner, mode="claude-only")
    assert first.returncode == 5, first.stdout + first.stderr
    marker = "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=claude-only\n"
    assert marker in env_file.read_text(encoding="utf-8")
    # With the saved mode missing, the managed configuration falls back to sidecar.
    env_file.write_text(managed(PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED="claude-only"), encoding="utf-8")
    default = _run_extractor_fixture(runner, answer="")
    assert default.returncode == 0, default.stdout + default.stderr
    assert "Extractor mode: sidecar" in default.stdout
    assert "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=" not in env_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
def test_new_explicit_extractor_replaces_the_saved_marker(tmp_path, shell):
    env_file = tmp_path / ".env"
    env_file.write_text(
        managed(PSEUDOLIFE_INSTALL_EXTRACTOR="claude-only",
                PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED="claude-only"),
        encoding="utf-8",
    )
    runner = _PowerShell(tmp_path) if shell == "powershell" else _Shell(shell, tmp_path)
    proc = _run_extractor_fixture(runner, mode="openai-only")
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert proc.stderr.splitlines()[-1] == (
        "Requested extractor setup incomplete (openai-only). " + EXTRACTOR_FAILURE
    )
    record = env_file.read_text(encoding="utf-8")
    assert "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=openai-only\n" in record
    assert "PSEUDOLIFE_INSTALL_EXTRACTOR_REQUESTED=claude-only\n" not in record


@pytest.mark.parametrize("shell", INSTALLER_SHELLS, ids=lambda name: Path(name).name)
@pytest.mark.parametrize("requested", [True, False], ids=["requested", "detected"])
def test_claude_hook_credentials_failure_at_completion(tmp_path, shell, requested):
    config = tmp_path / "active profile"
    settings = config / "settings.json"
    if shell == "powershell":
        runner = _PowerShell(tmp_path)
        path = "ops/install-native.ps1"
        setup = (
            f"$clients = @('claude'); $requestedClients = {'$clients' if requested else '@()'}\n"
            "$hookState = @{claude = 'hook'}; $instrState = @{}\n"
            "$DaemonUrl = 'http://127.0.0.1:9'; $ClientOnly = $true; $TokenFile = 'fixture.token'\n"
            f"$env:CLAUDE_CONFIG_DIR = '{_q(config)}'\n"
            "$PSNativeCommandUseErrorActionPreference = $true\n"
            "function Invoke-NativeHelper {\n"
            "    if ($args[0] -ne 'credentials' -or $args[1] -ne 'claude-settings-env') { throw 'Unexpected helper' }\n"
            "    if ($PSNativeCommandUseErrorActionPreference) { throw 'fixture native error promotion' }\n"
            "    $global:LASTEXITCODE = 19\n"
            "    '{\"status\":\"failed\",\"error\":\"fixture settings write failed\"}'\n"
            "}\n"
        )
        settings_display = str(settings)
        continued = "Write-Output REMAINING_STEPS_FINISHED\n"
    else:
        runner = _Shell(shell, tmp_path)
        path = "ops/install-native.sh"
        setup = (
            f"CLIENTS=claude REQUESTED_CLIENTS='{'claude' if requested else ''}' HOOK_CLAUDE=hook\n"
            "DAEMON_URL=http://127.0.0.1:9 CLIENT_ONLY=1 TOKEN_FILE=fixture.token\n"
            f"CLAUDE_CONFIG_DIR='{runner.path(config)}'\n"
            "native_helper() {\n"
            "    [ \"$1 $2\" = 'credentials claude-settings-env' ] || return 2\n"
            "    echo '{\"status\":\"failed\",\"error\":\"fixture settings write failed\"}'\n"
            "    return 19\n"
            "}\n"
        )
        settings_display = runner.path(settings)
        continued = "echo REMAINING_STEPS_FINISHED\n"
    source = _read(path)
    proc = runner.run(
        setup + "\n".join(_marker_block(source, "client registration state")) + "\n"
        + "\n".join(_marker_block(source, "client token files")) + "\n"
        + continued + "\n".join(_marker_block(source, "client setup result")) + "\n"
    )
    assert proc.returncode == (5 if requested else 0), proc.stdout + proc.stderr
    assert "REMAINING_STEPS_FINISHED" in proc.stdout
    assert "fixture settings write failed" in proc.stdout + proc.stderr
    label = "Requested" if requested else "Detected"
    assert proc.stderr.splitlines()[-1] == (
        f"{label} client setup incomplete (Claude Code). Hook credentials were not written. "
        f"Set the daemon URL and token file in the env block of {settings_display} as shown above."
    )
    if not requested:
        assert proc.stderr.splitlines()[-2] == "Warnings"


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
