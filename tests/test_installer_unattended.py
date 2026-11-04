"""No-prompt defaults in both installers, including explicitly signalled ptys."""
from pathlib import Path

import pytest

from tests.test_installer_client_only import BASH, ROOT, _Shell, _block
from tests.test_installer_client_only_ps import _PowerShell, _q, _output


def text(script):
    return (ROOT / "ops" / script).read_text(encoding="utf-8")


@BASH
def test_sh_yes_is_accepted_before_invalid_client_refuses(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"'{shell.path(ROOT / 'ops/install.sh')}' --yes --client invalid --extractor sidecar --no-art\n")
    assert proc.returncode == 2
    assert "Unattended install" in proc.stdout
    assert "invalid --client" in proc.stderr
    assert shell.logged() == []


def test_ps_yes_is_accepted_before_invalid_client_refuses(tmp_path):
    proc = _PowerShell(tmp_path).run(f"& '{_q(ROOT / 'ops/install.ps1')}' -Yes -Client invalid -Extractor sidecar -NoArt\nexit $LASTEXITCODE\n")
    assert proc.returncode == 2, _output(proc)
    assert "Unattended install" in _output(proc)
    assert "invalid -Client" in _output(proc)


@BASH
@pytest.mark.parametrize("installed,expected", [("codex", "codex"), ("", "generic")])
def test_sh_only_installed_client_is_selected(bash, tmp_path, installed, expected):
    shell = _Shell(bash, tmp_path)
    for client in ("claude", "codex", "gemini"):
        if client != installed:
            (shell.bin / client).unlink()
    source = text("install.sh")
    block = source[source.index("# ── 1. provider selection"):source.index("# ── 2. preflight")]
    proc = shell.run("CLIENT=''\nnormalize_clients() { echo \"$1\"; }\nchoose_bank_token() { :; }\n" + block)
    assert proc.returncode == 0, proc.stderr
    assert f"Providers: {expected}" in proc.stdout


@pytest.mark.parametrize("installed,expected", [("codex", "codex"), ("", "generic")])
def test_ps_only_installed_client_is_selected(tmp_path, installed, expected):
    ps = _PowerShell(tmp_path)
    source = text("install.ps1")
    block = source[source.index("# -- 1. provider selection"):source.index("# -- 2. preflight")]
    proc = ps.run("$Client=''\n$interactive=$false\n"
                  f"function Get-Command {{ if ($args[0] -eq '{installed}') {{ 'fixture-cli' }} }}\n"
                  "function Get-ChildItem { }\n"
                  "function Get-ProviderList($value) { $value }\nfunction Invoke-BankTokenQuestion { }\n" + block)
    assert proc.returncode == 0, _output(proc)
    assert f"Providers: {expected}" in _output(proc)


@BASH
@pytest.mark.parametrize("yes,signal,can_ask", [("", "1", False), ("1", "0", False), ("", "0", True)])
def test_explicit_agent_signal_disables_questions_even_with_a_tty(bash, tmp_path, yes, signal, can_ask):
    shell = _Shell(bash, tmp_path)
    source = text("install.sh")
    marker = "# >>> unattended mode >>>\n"
    mode = source.split(marker, 1)[1].split("# <<< unattended mode <<<", 1)[0] if marker in source else ""
    proc = shell.run("[() { if test \"${1:-}\" = -t; then return 0; fi; "
                     "if test \"${1:-}\" = '!' && test \"${2:-}\" = -t; then return 1; fi; builtin [ \"$@\"; }\n"
                     + f"YES='{yes}'\n"
                     + mode + _block("bank location")
                     + "\nif bank_can_ask; then echo WOULD_PROMPT; fi\n",
                     extra_env={"PSEUDOLIFE_UNATTENDED": signal})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("WOULD_PROMPT" in proc.stdout) == can_ask


@pytest.mark.parametrize("yes,signal,can_ask", [(False, "1", False), (True, "0", False), (False, "0", True)])
def test_ps_explicit_agent_signal_disables_questions_even_with_a_tty(tmp_path, yes, signal, can_ask):
    source = text("install.ps1")
    marker = "# >>> unattended mode >>>\n"
    mode = source.split(marker, 1)[1].split("# <<< unattended mode <<<", 1)[0] if marker in source else "$interactive=$true"
    mode = mode.replace("[Environment]::UserInteractive -and -not [Console]::IsInputRedirected", "$true")
    proc = _PowerShell(tmp_path).run(f"$Yes=${str(yes).lower()}\n" + mode + "\nif ($interactive) { 'WOULD_PROMPT' }\n"
                                    + f"if ($env:PSEUDOLIFE_UNATTENDED -ne '{signal}') {{ throw 'changed caller environment' }}\n",
                                    extra_env={"PSEUDOLIFE_UNATTENDED": signal})
    assert proc.returncode == 0, _output(proc)
    assert ("WOULD_PROMPT" in _output(proc)) == can_ask


@BASH
def test_sh_unattended_extractor_default_needs_no_login(bash, tmp_path):
    source = text("install.sh")
    block = source[source.index("# ── 3. extractor choice"):source.index('claude_shim_mode="";')]
    proc = _Shell(bash, tmp_path).run("EXTRACTOR=''\nCLIENT_ONLY=''\nEXTRACTOR_MODES=sidecar\n"
                                     "check_extractor_args() { :; }\n" + block + "\necho MODE=$EXTRACTOR\n")
    assert proc.returncode == 0, proc.stderr
    assert "MODE=sidecar" in proc.stdout


def test_ps_unattended_extractor_default_needs_no_login(tmp_path):
    source = text("install.ps1")
    block = source[source.index("# -- 3. extractor choice"):source.index('$claudeShimMode =')]
    proc = _PowerShell(tmp_path).run("$Extractor=''\n$ClientOnly=$false\n$interactive=$false\n"
                                    "$extractorModes=@('sidecar')\nfunction Assert-ExtractorArgs { }\n"
                                    + block + '\n"MODE=$Extractor"\n')
    assert proc.returncode == 0, _output(proc)
    assert "MODE=sidecar" in _output(proc)
