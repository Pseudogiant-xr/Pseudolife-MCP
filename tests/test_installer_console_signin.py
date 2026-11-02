"""The installers end a daemon-host install with the Console open, signed in.

A daemon-host install (not client-only) with a bearer token runs
`pseudolife-mcp console` at the end (spec 2026-11-02, "Install, update and
the surfaces that change"): it asks the daemon for a one-time sign-in link
with this machine's token and opens it in the browser, which asks the person
to confirm once. Opening a browser is not a persistent change, so nothing is
asked. An install without a terminal prints one line naming the command; a
tokenless install prints nothing (its Console needs no sign-in). A failure
there leaves the install complete with one warning naming the command. It
runs after the passkey offer, so a link made after that setup names the
Console's HTTPS address. A client-only install names the command in its
notes. Blocks run extracted from ops/install.sh and ops/install.ps1 with
stub commands.
"""
from __future__ import annotations

import pytest

from tests.test_client_install_ux import _heredoc_payload, _marker_block
from tests.test_installer_client_only import BASH, ROOT, _Shell, _stub
from tests.test_installer_client_only_ps import _PowerShell, _output

COMMAND = "pseudolife-mcp console"
OPENING = "Opening the Console in your browser, signed in (confirm the sign-in there)."
UNATTENDED = "Open the Console signed in: pseudolife-mcp console"


def _text(name: str) -> str:
    return (ROOT / "ops" / name).read_text(encoding="utf-8")


def _block(name: str, script: str) -> str:
    text = _text(script)
    assert f"# >>> {name} >>>\n" in text, f"{script} has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


@pytest.mark.parametrize("script,offer,call", [
    ("install.sh", '\nif [ -z "$CLIENT_ONLY" ]; then offer_maintainer_setup; fi\n',
     '\nif [ -z "$CLIENT_ONLY" ]; then open_console_signin; fi\n'),
    ("install.ps1", "\nif (-not $ClientOnly) { Invoke-MaintainerSetupOffer }\n",
     "\nif (-not $ClientOnly) { Invoke-ConsoleSignin }\n"),
])
def test_it_runs_once_after_the_passkey_offer_on_a_daemon_host(script, offer, call):
    text = _text(script)
    assert text.count(call) == 1, f"{script} does not open the Console on a daemon host"
    name = "open_console_signin" if script.endswith(".sh") else "Invoke-ConsoleSignin"
    calls = [line for line in text.splitlines()
             if name in line and not line.lstrip().startswith(("#", "function ", f"{name}()"))]
    assert len(calls) == 1, calls
    # Right after the passkey offer, so the link names its HTTPS address,
    # and before the update line.
    assert text.index(offer) + len(offer) - 1 == text.index(call)
    assert text.index(call) < text.index("# >>> update line >>>")
    # Defined before it is called.
    assert text.index("# >>> console sign-in >>>") < text.index(call)


# -- install.sh ----------------------------------------------------------------

def _sh(shell: _Shell, *, tty: bool = True, token: str = "minted",
        console_exit: int = 0, shim: bool = True, board: bool = True, setup_done: bool = False):
    _stub(shell.bin / "fakeshim",
          "printf 'shim|%s|%s\\n' \"$*\" \"${PSEUDOLIFE_MCP_TOKEN_FILE:-}\" >>\"$CALL_LOG\"\n"
          "exit \"$FAKE_CONSOLE_EXIT\"\n")
    return shell.run(
        f"bank_can_ask() {{ {'return 0' if tty else 'return 1'}; }}\n"
        "bank_read() { echo UNEXPECTED-QUESTION; BANK_REPLY=; }\n"
        "ensure_shim() { "
        + ("SHIM_OK=1; SHIM_PATH=fakeshim; }\n" if shim else "SHIM_OK=; SHIM_PATH=; }\n")
        + f"TOKEN_STATE={token}\n"
        + ("board_file=/fixture/claude-code.token\n" if board else "")
        + ("MAINTAINER_SETUP_DONE=1\n" if setup_done else "")
        + _block("console sign-in", "install.sh")
        + "\nopen_console_signin\necho \"BLOCK-EXIT $?\"\necho DONE\n",
        extra_env={"FAKE_CONSOLE_EXIT": str(console_exit)})


def _shim_calls(shell: _Shell) -> list[str]:
    return [line for line in shell.logged() if line.startswith("shim|")]


@BASH
def test_sh_interactive_opens_the_console_with_the_board_token(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == ["shim|console|/fixture/claude-code.token"]
    assert OPENING in proc.stdout and "BLOCK-EXIT 0" in proc.stdout
    # opening a browser is not a persistent change: nothing is asked
    assert "UNEXPECTED-QUESTION" not in proc.stdout and "[y/N]" not in proc.stdout
    assert proc.stderr == ""


@BASH
def test_sh_without_a_board_token_file_it_uses_the_environment_token(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, token="present", board=False)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == ["shim|console|"]


@BASH
def test_sh_without_a_terminal_one_line_names_the_command(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, tty=False)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    assert [line for line in proc.stdout.splitlines() if COMMAND in line] == [UNATTENDED]
    assert OPENING not in proc.stdout and proc.stderr == ""


@BASH
@pytest.mark.parametrize("token", ["opted-out", "http", "no-shim", "failed", ""])
def test_sh_a_tokenless_install_says_nothing(bash, tmp_path, token):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, token=token)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    assert proc.stdout.splitlines() == ["BLOCK-EXIT 0", "DONE"] and proc.stderr == ""


@BASH
@pytest.mark.parametrize("console_exit", [3, 4])
def test_sh_a_failed_console_warns_once_and_the_install_completes(bash, tmp_path, console_exit):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, console_exit=console_exit)
    assert proc.returncode == 0, proc.stderr
    warnings = [line for line in proc.stderr.splitlines() if "WARNING" in line]
    assert len(warnings) == 1 and COMMAND in warnings[0] and f"exited {console_exit}" in warnings[0]
    assert "BLOCK-EXIT 0" in proc.stdout and "DONE" in proc.stdout


@BASH
def test_sh_without_the_shim_it_says_so_and_goes_on(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, shim=False)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    warnings = [line for line in proc.stderr.splitlines() if "WARNING" in line]
    assert len(warnings) == 1 and COMMAND in warnings[0]
    assert "BLOCK-EXIT 0" in proc.stdout and "DONE" in proc.stdout


# -- install.ps1 ---------------------------------------------------------------

def _ps(ps: _PowerShell, *, tty: bool = True, token: str = "minted",
        console_exit: int = 0, shim: bool = True, board: bool = True, setup_done: bool = False):
    return ps.run(
        "function global:fakeshim {\n"
        f"    Add-Content -LiteralPath '{ps.calls}' -Value ('fakeshim|' + (@($args) -join ' '))\n"
        f"    Add-Content -LiteralPath '{ps.calls}' -Value ('token|' + $env:PSEUDOLIFE_MCP_TOKEN_FILE)\n"
        f"    $global:LASTEXITCODE = {console_exit}\n}}\n"
        + "$env:PSEUDOLIFE_MCP_TOKEN_FILE = $null\n"
        + f"$interactive = ${'true' if tty else 'false'}\n"
        + "function Read-BankAnswer([string]$Prompt) { Write-Output 'UNEXPECTED-QUESTION'; '' }\n"
        + ("function Install-ShimOnce { $script:shimInstallPath = 'fakeshim'; $true }\n" if shim
           else "function Install-ShimOnce { $script:shimInstallPath = $null; $false }\n")
        + f"$tokenState = '{token}'\n"
        + ("$boardFile = '/fixture/claude-code.token'\n" if board else "$boardFile = $null\n")
        + ("$script:maintainerSetupDone = $true\n" if setup_done else "")
        + _block("console sign-in", "install.ps1")
        + "\nInvoke-ConsoleSignin\n"
        + "Write-Output ('after|' + $env:PSEUDOLIFE_MCP_TOKEN_FILE)\nWrite-Output DONE\n")


def test_ps_interactive_opens_the_console_with_the_board_token(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == ["fakeshim|console", "token|/fixture/claude-code.token"]
    assert OPENING in _output(proc) and "DONE" in proc.stdout
    assert "UNEXPECTED-QUESTION" not in _output(proc) and "[y/N]" not in _output(proc)
    assert "WARNING" not in _output(proc)
    # the board token file is set for the one call only
    assert "after|" in proc.stdout.splitlines()


def test_ps_without_a_board_token_file_it_uses_the_environment_token(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, token="present", board=False)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == ["fakeshim|console", "token|"]


def test_ps_without_a_terminal_one_line_names_the_command(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, tty=False)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == []
    assert [line for line in _output(proc).splitlines() if COMMAND in line] == [UNATTENDED]
    assert OPENING not in _output(proc) and "WARNING" not in _output(proc)


@pytest.mark.parametrize("token", ["opted-out", "http", "no-shim", "failed", ""])
def test_ps_a_tokenless_install_says_nothing(tmp_path, token):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, token=token)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == []
    assert COMMAND not in _output(proc) and OPENING not in _output(proc)
    assert "WARNING" not in _output(proc) and "DONE" in proc.stdout


@pytest.mark.parametrize("console_exit", [3, 4])
def test_ps_a_failed_console_warns_once_and_the_install_completes(tmp_path, console_exit):
    proc = _ps(_PowerShell(tmp_path), console_exit=console_exit)
    assert proc.returncode == 0, _output(proc)
    warnings = [line for line in _output(proc).splitlines() if "WARNING" in line]
    assert len(warnings) == 1 and COMMAND in warnings[0] and f"exited {console_exit}" in warnings[0]
    assert "DONE" in proc.stdout


def test_ps_without_the_shim_it_says_so_and_goes_on(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, shim=False)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == []
    warnings = [line for line in _output(proc).splitlines() if "WARNING" in line]
    assert len(warnings) == 1 and COMMAND in warnings[0]
    assert "DONE" in proc.stdout


# -- client-only install -------------------------------------------------------

@pytest.mark.parametrize("script", ["install.sh", "install.ps1"])
def test_the_client_only_notes_name_the_console_command(script):
    notes = " ".join(" ".join(_heredoc_payload(
        _marker_block(_text(script), "client-only notes"))).split())
    assert "Open the Console signed in, on this machine: pseudolife-mcp console" in notes


@pytest.mark.parametrize("script,call", [
    ("install.sh", '\nif [ -n "$CLIENT_ONLY" ]; then show_client_only_notes; echo ""; fi\n'),
    ("install.ps1", '\nif ($ClientOnly) { Show-ClientOnlyNotes; Write-Host "" }\n'),
])
def test_a_client_only_install_shows_the_notes_and_runs_no_console(script, call):
    text = _text(script)
    assert text.count(call) == 1
    name = "open_console_signin" if script.endswith(".sh") else "Invoke-ConsoleSignin"
    [run] = [line for line in text.splitlines()
             if name in line and not line.lstrip().startswith(("#", "function ", f"{name}()"))]
    assert "CLIENT_ONLY" in run or "ClientOnly" in run
    assert run.startswith(('if [ -z "$CLIENT_ONLY" ]', "if (-not $ClientOnly)"))


@BASH
@pytest.mark.parametrize("tty", [True, False])
def test_sh_after_a_successful_passkey_setup_the_console_is_not_opened_twice(bash, tmp_path, tty):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, tty=tty, setup_done=True)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    assert "opened signed in by the passkey setup" in proc.stdout and "BLOCK-EXIT 0" in proc.stdout


@pytest.mark.parametrize("tty", [True, False])
def test_ps_after_a_successful_passkey_setup_the_console_is_not_opened_twice(tmp_path, tty):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, tty=tty, setup_done=True)
    assert proc.returncode == 0, _output(proc)
    assert ps.logged() == []
    assert "opened signed in by the passkey setup" in _output(proc)


@pytest.mark.parametrize("script,marker", [("install.sh", "MAINTAINER_SETUP_DONE=1"),
                                           ("install.ps1", "$script:maintainerSetupDone = $true")])
def test_only_a_successful_passkey_setup_sets_the_flag(script, marker):
    offer = _block("maintainer setup offer", script)
    assert offer.count(marker) == 1
    # set in the success branch, after the failure warning
    assert offer.index("maintainer setup exited") < offer.index(marker)
