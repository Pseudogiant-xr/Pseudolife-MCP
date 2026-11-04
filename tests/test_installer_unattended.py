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


def test_ps_interactive_enter_uses_the_same_sidecar_default(tmp_path):
    source = text("install.ps1")
    block = source[source.index("# -- 3. extractor choice"):source.index('$claudeShimMode =')]
    proc = _PowerShell(tmp_path).run("$Extractor=''\n$ClientOnly=$false\n$interactive=$true\n"
                                    "function Read-Host { '' }\nfunction Assert-ExtractorArgs { }\n"
                                    + block + '\n"MODE=$Extractor"\n')
    assert proc.returncode == 0, _output(proc)
    assert "MODE=sidecar" in _output(proc)


@pytest.mark.parametrize("on_path", [False, True])
def test_ps_detected_codex_is_callable_even_in_off_path_official_install(tmp_path, on_path):
    source = text("install.ps1")
    block = source[source.index("# -- 1. provider selection"):source.index("# -- 2. preflight")]
    prelude = (
        "$Client=''\n$interactive=$false\n"
        "function fixture_codex { 'FIXTURE|' + ($args -join ' '); $global:LASTEXITCODE=0 }\n"
        "function Get-Command { if ($args[0] -eq 'codex' -and (Test-Path function:codex)) { [pscustomobject]@{Name='codex'} } }\n"
        "function Get-ProviderList($value) { $value }\nfunction Invoke-BankTokenQuestion { }\n")
    prelude += ("function codex { fixture_codex @args }\nfunction Get-ChildItem { throw 'PATH command must win' }\n"
                if on_path else
                "function Get-ChildItem { [pscustomobject]@{FullName='fixture_codex'; LastWriteTime=[datetime]::Now} }\n")
    proc = _PowerShell(tmp_path).run(prelude + block + "\ncodex mcp get pseudolife-memory\n")
    assert proc.returncode == 0, _output(proc)
    assert "Providers: codex" in _output(proc)
    assert "FIXTURE|mcp get pseudolife-memory" in _output(proc)


@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
@pytest.mark.parametrize("explicit", [False, True])
def test_unattended_fresh_default_does_not_overwrite_an_existing_extractor(tmp_path, script, explicit):
    from tests.test_installer_existing_upgrade import _bash_variants
    begin = "# >>> pseudolife-mcp install (managed block — installer rewrites between markers) >>>"
    end = "# <<< pseudolife-mcp install <<<"
    env = tmp_path / ".env"
    original = f"{begin}\nPSEUDOLIFE_DREAM_BASE_URL=http://example.invalid:8081/v1\n{end}\n"
    env.write_text(original, encoding="utf-8")
    source = text(script)
    if script.endswith(".ps1"):
        block = source[source.index("# -- 3. extractor choice"):source.index('$claudeShimMode =')]
        proc = _PowerShell(tmp_path).run(
            f"$envFile='{_q(env)}'\n$EnvBegin='{begin}'\n$EnvEnd='{end}'\n"
            f"$Extractor='{'endpoint' if explicit else ''}'\n$ClientOnly=$false\n$interactive=$false\nfunction Assert-ExtractorArgs {{ }}\n"
            + block + "\n'WOULD_REWRITE'\n")
        output = _output(proc)
    else:
        variants = _bash_variants()
        if not variants:
            pytest.skip("Bash unavailable")
        shell = _Shell(variants[0], tmp_path)
        block = source[source.index("# ── 3. extractor choice"):source.index('claude_shim_mode="";')]
        proc = shell.run(f"env_file='{shell.path(env)}'\nENV_BEGIN='{begin}'\nENV_END='{end}'\n"
                         f"EXTRACTOR='{'endpoint' if explicit else ''}'\nCLIENT_ONLY=''\ncheck_extractor_args() {{ :; }}\n"
                         + block + "\necho WOULD_REWRITE\n")
        output = proc.stdout + proc.stderr
    assert (proc.returncode == 0) == explicit, output
    assert ("WOULD_REWRITE" in output) == explicit
    assert env.read_text(encoding="utf-8") == original
