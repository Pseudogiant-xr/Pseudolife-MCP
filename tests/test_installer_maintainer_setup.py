"""The installers offer the maintainer's passkey setup at the end.

A daemon-host install (not client-only) with a bearer token asks once,
default no (the maintainer, 2026-10-05: it can put the daemon on the
tailnet), whether to run `pseudolife-mcp maintainer setup`, the guided
command that names the Console over HTTPS, writes the daemon's config and
enrols the first passkey (2026-10-05: the install should handle everything,
or ask with simple instructions only where it must). An install without a
terminal skips it with one line naming the command; a tokenless install
skips it silently (the daemon refuses passkeys, and the board line already
says why). A failure there leaves the install complete. Blocks run
extracted from ops/install.sh and ops/install.ps1 with stub commands.
"""
from __future__ import annotations

import pytest

from tests.test_installer_client_only import BASH, ROOT, _Shell, _stub
from tests.test_installer_client_only_ps import _PowerShell, _output

COMMAND = "pseudolife-mcp maintainer setup"


def _text(name: str) -> str:
    return (ROOT / "ops" / name).read_text(encoding="utf-8")


def _block(name: str, script: str) -> str:
    text = _text(script)
    assert f"# >>> {name} >>>\n" in text, f"{script} has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


@pytest.mark.parametrize("script,call", [
    ("install.sh", '\nif [ -z "$CLIENT_ONLY" ]; then offer_maintainer_setup; fi\n'),
    ("install.ps1", "\nif (-not $ClientOnly) { Invoke-MaintainerSetupOffer }\n"),
])
def test_it_is_offered_once_after_the_board_line_on_a_daemon_host(script, call):
    text = _text(script)
    assert text.count(call) == 1, f"{script} does not offer the setup on a daemon host"
    name = "offer_maintainer_setup" if script.endswith(".sh") else "Invoke-MaintainerSetupOffer"
    calls = [line for line in text.splitlines()
             if name in line and not line.lstrip().startswith(("#", "function ", f"{name}()"))]
    assert len(calls) == 1, calls
    called = text.index(call)
    assert text.index("# <<< board line <<<") < called < text.index("# >>> update line >>>")


# -- install.sh ----------------------------------------------------------------

def _sh(shell: _Shell, *, tty: bool = True, reply: str = "", token: str = "minted",
        setup_exit: int = 0, shim: bool = True, check_exit: int = 1):
    _stub(shell.bin / "fakeshim",
          "case \"$*\" in *--check*) exit \"$FAKE_CHECK_EXIT\" ;; esac\n"
          "printf 'shim|%s|%s\\n' \"$*\" \"${PSEUDOLIFE_MCP_TOKEN_FILE:-}\" >>\"$CALL_LOG\"\n"
          "exit \"$FAKE_SETUP_EXIT\"\n")
    return shell.run(
        f"bank_can_ask() {{ {'return 0' if tty else 'return 1'}; }}\n"
        "bank_read() { printf '%s' \"$1\"; BANK_REPLY=\"$FAKE_REPLY\"; }\n"
        "bank_trimmed() { printf '%s' \"$1\"; }\n"
        "ensure_shim() { "
        + ("SHIM_OK=1; SHIM_PATH=fakeshim; }\n" if shim else "SHIM_OK=; SHIM_PATH=; }\n")
        + f"TOKEN_STATE={token}\nboard_file=/fixture/claude-code.token\n"
        + _block("maintainer setup offer", "install.sh")
        + "\noffer_maintainer_setup\necho DONE\n",
        extra_env={"FAKE_REPLY": reply, "FAKE_SETUP_EXIT": str(setup_exit),
                   "FAKE_CHECK_EXIT": str(check_exit)})


def _shim_calls(shell: _Shell) -> list[str]:
    return [line for line in shell.logged() if line.startswith("shim|")]


@BASH
@pytest.mark.parametrize("reply", ["y", "Yes"])
def test_sh_yes_runs_the_setup_with_the_board_token(bash, tmp_path, reply):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply=reply)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == ["shim|maintainer setup --yes|/fixture/claude-code.token"]
    assert "[y/N]" in proc.stdout and "DONE" in proc.stdout
    # the one question names the persistent changes the yes answers for
    assert "tailscale serve" in proc.stdout and "restart" in proc.stdout
    assert "tailnet" in proc.stdout and ":8443" in proc.stdout


@BASH
@pytest.mark.parametrize("reply", ["", "n"])
def test_sh_enter_or_no_skips_with_the_command_named(bash, tmp_path, reply):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply=reply)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    assert COMMAND in proc.stdout


@BASH
def test_sh_without_a_terminal_one_line_names_the_command(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, tty=False)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    assert [line for line in proc.stdout.splitlines() if COMMAND in line] and "[y/N]" not in proc.stdout


@BASH
@pytest.mark.parametrize("token", ["opted-out", "http", "no-shim", "failed"])
def test_sh_a_tokenless_install_is_not_offered_passkeys(bash, tmp_path, token):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, token=token)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and COMMAND not in proc.stdout


@BASH
@pytest.mark.parametrize("tty", [True, False])
def test_sh_passkeys_in_place_are_not_asked_about_again(bash, tmp_path, tty):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, tty=tty, check_exit=0)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    assert "Maintainer passkeys: in place" in proc.stdout


@BASH
def test_sh_a_failed_setup_warns_and_the_install_completes(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply="y", setup_exit=4)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and COMMAND in proc.stderr
    assert "DONE" in proc.stdout


@BASH
def test_sh_without_the_shim_it_says_so_and_goes_on(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply="y", shim=False)
    assert proc.returncode == 0, proc.stderr
    assert "WARNING" in proc.stderr and COMMAND in proc.stderr
    assert "DONE" in proc.stdout


# -- install.ps1 ---------------------------------------------------------------

def _ps(ps: _PowerShell, *, tty: bool = True, reply: str = "", token: str = "minted",
        setup_exit: int = 0, shim: bool = True, check_exit: int = 1):
    return ps.run(
        "function global:fakeshim {\n"
        f"    if (@($args) -contains '--check') {{ $global:LASTEXITCODE = {check_exit}; return }}\n"
        f"    Add-Content -LiteralPath '{ps.calls}' -Value ('fakeshim|' + (@($args) -join ' '))\n"
        f"    Add-Content -LiteralPath '{ps.calls}' -Value ('token|' + $env:PSEUDOLIFE_MCP_TOKEN_FILE)\n"
        f"    $global:LASTEXITCODE = {setup_exit}\n}}\n"
        + f"$interactive = ${'true' if tty else 'false'}\n"
        + f"function Read-BankAnswer([string]$Prompt) {{ Write-Output \"$Prompt\" | Out-Host; '{reply}' }}\n"
        + "function Get-BankReply($reply) { \"$reply\".Trim() }\n"
        + ("function Install-ShimOnce { $script:shimInstallPath = 'fakeshim'; $true }\n" if shim
           else "function Install-ShimOnce { $script:shimInstallPath = $null; $false }\n")
        + f"$tokenState = '{token}'\n$boardFile = '/fixture/claude-code.token'\n"
        + _block("maintainer setup offer", "install.ps1")
        + "\nInvoke-MaintainerSetupOffer\nWrite-Output DONE\n")


@pytest.mark.parametrize("reply", ["y", "Yes"])
def test_ps_yes_runs_the_setup_with_the_board_token(tmp_path, reply):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, reply=reply)
    assert proc.returncode == 0, _output(proc)
    assert [line for line in ps.logged() if line.startswith("fakeshim|")] == [
        "fakeshim|maintainer setup --yes"]
    assert "token|/fixture/claude-code.token" in ps.logged()
    assert "[y/N]" in _output(proc) and "DONE" in proc.stdout
    assert "tailscale serve" in _output(proc) and "restart" in _output(proc)
    assert "tailnet" in _output(proc) and ":8443" in _output(proc)


@pytest.mark.parametrize("reply", ["", "n"])
def test_ps_enter_or_no_skips_with_the_command_named(tmp_path, reply):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, reply=reply)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == []
    assert COMMAND in _output(proc)


def test_ps_without_a_terminal_one_line_names_the_command(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, tty=False)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == []
    assert COMMAND in _output(proc) and "[y/N]" not in _output(proc)


@pytest.mark.parametrize("token", ["opted-out", "http", "no-shim", "failed"])
def test_ps_a_tokenless_install_is_not_offered_passkeys(tmp_path, token):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, token=token)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == [] and COMMAND not in _output(proc)


@pytest.mark.parametrize("tty", [True, False])
def test_ps_passkeys_in_place_are_not_asked_about_again(tmp_path, tty):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, tty=tty, check_exit=0)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == [] and "[y/N]" not in _output(proc)
    assert "Maintainer passkeys: in place" in _output(proc)


def test_ps_a_failed_setup_warns_and_the_install_completes(tmp_path):
    proc = _ps(_PowerShell(tmp_path), reply="y", setup_exit=4)
    assert proc.returncode == 0, _output(proc)
    assert "WARNING" in _output(proc) and COMMAND in _output(proc)
    assert "DONE" in proc.stdout


def test_ps_without_the_shim_it_says_so_and_goes_on(tmp_path):
    proc = _ps(_PowerShell(tmp_path), reply="y", shim=False)
    assert proc.returncode == 0, _output(proc)
    assert "WARNING" in _output(proc) and COMMAND in _output(proc)
    assert "DONE" in proc.stdout
