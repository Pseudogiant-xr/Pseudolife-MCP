"""The installers' first question, and a client-only install's existing
registrations (spec: docs/superpowers/specs/
2026-09-30-connect-and-bank-location-design.md, "The installer's first
question").

An interactive run that does not already name the daemon asks where the
memory bank lives: on this machine (today's local install), on another
machine (the client-only install, with the daemon's URL and token asked
for), or on this machine shared with others (a local install that ends with
the exposure steps). A client-only install then re-points the registrations
the machine already has with ``pseudolife-mcp connect`` (a dry run first,
then ``--yes``), before the Codex credential setup and the registrars, and
does not run it at all when there are none.

Blocks run extracted from ops/install.sh and ops/install.ps1 with stub
commands and a fake shim in a disposable home, as the other installer tests
do; the answers come from an overridden reader, never a terminal.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

import pytest

from pseudolife_memory.credentials import CredentialProvider
from tests.test_client_install_ux import _heredoc_payload, _marker_block
from tests.test_installer_client_only import (
    BASH, CRED_READY, FIXTURE_TOKEN, HEALTH_ON, REMOTE, _Shell, _stub,
    _block as _sh_block, _q as _sh_q, _token as _sh_token,
)
from tests.test_installer_client_only_ps import (
    _PowerShell, _block as _ps_block, _output, _q as _ps_q, _token as _ps_token,
)
from tests.test_installer_existing_upgrade import _bash_fixture_path


ROOT = Path(__file__).resolve().parents[1]
SH = (ROOT / "ops" / "install.sh").read_text(encoding="utf-8")
PS = (ROOT / "ops" / "install.ps1").read_text(encoding="utf-8")
QUESTION = "Where does the memory bank live?"


# -- the question: ops/install.sh ------------------------------------------------

def _ask_sh(shell: _Shell, answers: list[str] | None, *, daemon_url: str = "",
            client_only: str = "", token_file: str = "", read_token: str = "",
            extractor: str = "", no_token: str = "", clients: str = "claude", then: str = "",
            extra_env: dict[str, str] | None = None, pairing_code: str | None = None):
    """The question, then the mode block, as the script runs them.
    ``answers``: the replies, one per question asked, at a stand-in
    terminal (None: the stock check, and stdin here is no terminal)."""
    body = "" if pairing_code is None else f"PAIRING_CODE='{_sh_q(pairing_code)}'\n"
    body += (f"DAEMON_URL='{_sh_q(daemon_url)}'\nCLIENT_ONLY='{client_only}'\n"
            f"TOKEN_FILE='{_sh_q(token_file)}'\nREAD_TOKEN='{read_token}'\n"
            f"EXTRACTOR='{extractor}'\nEXTRACTOR_URL='' MODEL='' SHIM_PORT=0 NO_TOKEN='{no_token}'\n"
            "TRANSPORT=shim\n" + _sh_block("bank location"))
    if answers is not None:
        replies = shell.tmp / "answers.txt"
        replies.write_bytes("".join(answer + "\n" for answer in answers).encode("utf-8"))
        body += (f"exec 3<'{shell.path(replies)}'\n"
                 "bank_can_ask() { return 0; }\n"
                 "bank_read() { printf 'ASKED: %s\\n' \"$1\"; BANK_REPLY=''\n"
                 "    IFS= read -r BANK_REPLY <&3 || [ -n \"$BANK_REPLY\" ] || return 1; }\n")
    body += ("choose_bank_location\n" + _sh_block("client-only mode")
             + f"CLIENTS='{clients}'\n" + then
             + "\nprintf 'BANK=%s\\nCLIENT_ONLY=%s\\nDAEMON_URL=%s\\nTOKEN_FILE=%s\\nREAD_TOKEN=%s\\n'"
               " \"$BANK_LOCATION\" \"$CLIENT_ONLY\" \"$DAEMON_URL\" \"$TOKEN_FILE\" \"$READ_TOKEN\"\n")
    return shell.run(body, extra_env=extra_env)


def _asked(proc) -> list[str]:
    return [line for line in proc.stdout.splitlines() if line.startswith("ASKED: ")]


@pytest.mark.parametrize("answer", ["", "1", " 1 "])
@BASH
def test_this_machine_is_the_default_answer(bash, tmp_path, answer):
    proc = _ask_sh(_Shell(bash, tmp_path), [answer])
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 1
    assert "BANK=local\nCLIENT_ONLY=\n" in proc.stdout


@BASH
def test_another_machine_asks_for_the_url_and_is_client_only(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["2", f" {REMOTE}/ "])
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 2
    assert "URL" in _asked(proc)[1]
    assert f"BANK=remote\nCLIENT_ONLY=1\nDAEMON_URL={REMOTE}\n" in proc.stdout


@BASH
def test_another_machine_through_a_loopback_tunnel_is_still_client_only(bash, tmp_path):
    """The answer says where the bank is: a loopback URL there is an SSH
    tunnel, as --client-only with a loopback --daemon-url is."""
    proc = _ask_sh(_Shell(bash, tmp_path), ["2", "http://127.0.0.1:18765"])
    assert proc.returncode == 0, proc.stderr
    assert "CLIENT_ONLY=1\nDAEMON_URL=http://127.0.0.1:18765\n" in proc.stdout


@BASH
def test_a_shared_bank_is_a_local_install(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["3"])
    assert proc.returncode == 0, proc.stderr
    assert "BANK=shared\nCLIENT_ONLY=\nDAEMON_URL=\n" in proc.stdout


@BASH
def test_an_answer_that_is_not_offered_is_asked_again(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["4", "3"])
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 2
    assert "please answer 1, 2 or 3" in proc.stdout + proc.stderr
    assert "BANK=shared\n" in proc.stdout


@BASH
def test_the_url_answer_is_validated_as_the_flag_is(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["2", "pl.example.invalid:8765"])
    assert proc.returncode == 2
    assert "invalid daemon URL" in proc.stderr


@BASH
def test_the_client_only_refusals_run_on_the_answer(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["2", REMOTE], extractor="sidecar")
    assert proc.returncode == 2
    assert "--extractor" in proc.stderr


@BASH
def test_without_a_terminal_nothing_is_asked(bash, tmp_path):
    """The stock reader: stdin here is the script, not a terminal."""
    proc = _ask_sh(_Shell(bash, tmp_path), None)
    assert proc.returncode == 0, proc.stderr
    assert QUESTION not in proc.stdout + proc.stderr
    assert proc.stdout.startswith("BANK=local\nCLIENT_ONLY=\n")


@pytest.mark.parametrize("kwargs", [
    {"daemon_url": REMOTE},
    {"daemon_url": "http://127.0.0.1:8765"},
    {"client_only": "1", "daemon_url": "http://127.0.0.1:18765"},
    {"extra_env": {"PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}},
], ids=["remote-flag", "loopback-flag", "client-only", "environment"])
@BASH
def test_a_run_that_names_the_daemon_is_not_asked(bash, tmp_path, kwargs):
    proc = _ask_sh(_Shell(bash, tmp_path), ["3"], **kwargs)
    assert proc.returncode == 0, proc.stderr
    assert _asked(proc) == []
    assert "BANK=local\n" in proc.stdout


# -- the token, for the other machine --------------------------------------------

@pytest.mark.parametrize("clients,name", [
    ("claude", "claude-code"), ("codex", "codex"), ("gemini", "gemini"),
    ("claude-desktop", "claude-desktop"), ("generic", "mcp-client"), ("claude codex", "shared")])
@BASH
def test_a_pasted_token_goes_to_a_new_file_named_for_the_client(bash, tmp_path, clients, name):
    shell = _Shell(bash, tmp_path)
    proc = _ask_sh(shell, ["2", REMOTE, "", ""], clients=clients, then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 4
    assert f"~/.pseudolife-mcp/{name}.token" in _asked(proc)[3]
    assert f"TOKEN_FILE={shell.path(shell.home)}/.pseudolife-mcp/{name}.token\nREAD_TOKEN=1\n" in proc.stdout


@BASH
def test_an_existing_token_file_is_used_as_it_is(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _ask_sh(shell, ["2", REMOTE, "2", "~/keys/remote.token"], then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert f"TOKEN_FILE={shell.path(shell.home)}/keys/remote.token\nREAD_TOKEN=\n" in proc.stdout


@BASH
def test_a_pasted_token_never_names_a_file_that_exists(bash, tmp_path):
    """The preflight refuses to overwrite a token file, so the question
    asks again rather than walking into that refusal."""
    shell = _Shell(bash, tmp_path)
    taken = shell.home / ".pseudolife-mcp" / "claude-code.token"
    taken.parent.mkdir()
    taken.write_text("fixture-old\n", encoding="utf-8")
    proc = _ask_sh(shell, ["2", REMOTE, "", "", "~/other.token"], then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 5
    assert "exists already" in proc.stdout
    assert f"TOKEN_FILE={shell.path(shell.home)}/other.token\nREAD_TOKEN=1\n" in proc.stdout


@BASH
def test_a_token_file_from_the_environment_is_named_and_used(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["2", REMOTE], then="choose_bank_token\n",
                   extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/remote.token"})
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 2
    [named] = [line for line in proc.stdout.splitlines() if "/fixture/remote.token" in line
               and not line.startswith("TOKEN_FILE=")]
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in named
    assert "READ_TOKEN=\n" in proc.stdout


@BASH
def test_a_token_file_flag_asks_only_for_the_url(bash, tmp_path):
    """--token-file already means "another machine": the three-way
    question is skipped, and only the daemon's URL is asked for."""
    proc = _ask_sh(_Shell(bash, tmp_path), [REMOTE], token_file="/fixture/remote.token",
                   then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 1 and "URL" in _asked(proc)[0]
    assert QUESTION not in proc.stdout
    assert (f"BANK=remote\nCLIENT_ONLY=1\nDAEMON_URL={REMOTE}\n"
            "TOKEN_FILE=/fixture/remote.token\nREAD_TOKEN=\n") in proc.stdout


@BASH
def test_read_token_without_a_file_asks_for_the_url_and_where_to_write_it(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _ask_sh(shell, [REMOTE, "~/new.token"], read_token="1", then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 2
    assert f"TOKEN_FILE={shell.path(shell.home)}/new.token\nREAD_TOKEN=1\n" in proc.stdout


@pytest.mark.parametrize("kwargs", [
    {"token_file": "/fixture/remote.token"}, {"read_token": "1"}], ids=["token-file", "read-token"])
@BASH
def test_token_flags_without_a_url_are_refused_without_a_terminal(bash, tmp_path, kwargs):
    proc = _ask_sh(_Shell(bash, tmp_path), None, **kwargs)
    assert proc.returncode == 2
    assert "--client-only --daemon-url" in proc.stderr


@BASH
def test_quoted_answers_are_unquoted(bash, tmp_path):
    """A path pasted from a file manager, or a URL copied with its quotes."""
    shell = _Shell(bash, tmp_path)
    proc = _ask_sh(shell, ["2", f"'{REMOTE}'", "2", '"~/keys/remote.token"'],
                   then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert (f"DAEMON_URL={REMOTE}\nTOKEN_FILE={shell.path(shell.home)}/keys/remote.token\n"
            in proc.stdout)


@BASH
def test_no_answer_at_a_terminal_is_refused(bash, tmp_path):
    """End of input at the question is not an answer: exit 2, as at the
    URL prompt, rather than a local install nobody chose."""
    proc = _ask_sh(_Shell(bash, tmp_path), [])
    assert proc.returncode == 2
    assert "no answer" in proc.stderr


@BASH
def test_a_shared_bank_without_a_token_is_refused(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), ["3"], no_token="1")
    assert proc.returncode == 2
    assert "--no-token" in proc.stderr and "never be exposed" in proc.stderr
    assert "docs/guide/remote-bank.md" in proc.stderr


@BASH
def test_the_question_is_shown_at_a_terminal(bash, tmp_path):
    proc = _ask_sh(_Shell(bash, tmp_path), [""])
    assert QUESTION in proc.stdout


@pytest.mark.parametrize("answer", ["1", "3"])
@BASH
def test_a_local_answer_never_asks_for_a_token(bash, tmp_path, answer):
    proc = _ask_sh(_Shell(bash, tmp_path), [answer], then="choose_bank_token\n")
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 1


@BASH
def test_the_other_machine_answer_runs_the_client_only_preflight(bash, tmp_path):
    """Answer 2, the URL, "paste", the default path: the preflight writes
    the token file from the pasted token and checks the daemon at the URL."""
    shell = _Shell(bash, tmp_path)
    preflight = (_sh_block("client-only preflight")
                 + "printf '%s\\n' \"$FIXTURE_FEED\" | client_only_preflight\n")
    proc = _ask_sh(shell, ["2", REMOTE, "1", ""], then="choose_bank_token\n" + preflight,
                   extra_env={"FAKE_HEALTH": HEALTH_ON, "FIXTURE_FEED": FIXTURE_TOKEN})
    assert proc.returncode == 0, proc.stderr
    target = shell.home / ".pseudolife-mcp" / "claude-code.token"
    assert CredentialProvider(path=target).snapshot().token == FIXTURE_TOKEN
    assert FIXTURE_TOKEN not in proc.stdout + proc.stderr
    assert f"use the daemon at {REMOTE}" in proc.stdout
    assert f"curl|-fsS --max-time 5 {REMOTE}/health" in shell.logged()


# -- the question: ops/install.ps1 -----------------------------------------------

def _ask_ps(ps: _PowerShell, answers: list[str] | None, *, daemon_url: str = "",
            client_only: bool = False, token_file: str = "", read_token: bool = False,
            extractor: str = "", no_token: bool = False, clients: tuple[str, ...] = ("claude",),
            then: str = "", extra_env: dict[str, str] | None = None,
            stdin_text: str | None = None, pairing_code: str | None = None):
    body = "" if pairing_code is None else f"$PairingCode = '{_ps_q(pairing_code)}'\n"
    body += (f"$DaemonUrl = '{_ps_q(daemon_url)}'\n"
            f"$ClientOnly = [switch]${'true' if client_only else 'false'}\n"
            f"$ReadToken = [switch]${'true' if read_token else 'false'}\n"
            f"$TokenFile = '{_ps_q(token_file)}'\n$Extractor = '{extractor}'\n"
            "$ExtractorUrl = ''; $Model = ''; $ShimPort = 0\n"
            f"$NoToken = [switch]${'true' if no_token else 'false'}\n"
            f"$Transport = 'shim'; $interactive = ${'false' if answers is None else 'true'}\n"
            + _ps_block("bank location"))
    if answers is not None:
        queued = ", ".join(f"'{_ps_q(answer)}'" for answer in answers)
        body += (f"$script:fixtureAnswers = [Collections.Queue]::new([string[]]@({queued}))\n"
                 "function Read-BankAnswer([string]$Prompt) {\n"
                 "    Write-Host \"ASKED: $Prompt\"\n"
                 "    if ($script:fixtureAnswers.Count -eq 0) { return $null }\n"
                 "    return $script:fixtureAnswers.Dequeue()\n}\n")
    client_list = ", ".join(f"'{client}'" for client in clients)
    body += ("Invoke-BankLocationQuestion\n" + _ps_block("client-only mode")
             + f"$clients = @({client_list})\n" + then
             + "\nWrite-Output \"BANK=$($script:bankLocation)\"\n"
               "Write-Output \"CLIENT_ONLY=$([bool]$ClientOnly)\"\n"
               "Write-Output \"DAEMON_URL=$DaemonUrl\"\n"
               "Write-Output \"TOKEN_FILE=$TokenFile\"\n"
               "Write-Output \"READ_TOKEN=$([bool]$ReadToken)\"\n")
    return ps.run(body, extra_env=extra_env, stdin_text=stdin_text)


def _lines(proc) -> list[str]:
    return proc.stdout.splitlines()


@pytest.mark.parametrize("answer", ["", "1", " 1 "])
def test_ps_this_machine_is_the_default_answer(tmp_path, answer):
    proc = _ask_ps(_PowerShell(tmp_path), [answer])
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 1
    assert "BANK=local" in _lines(proc) and "CLIENT_ONLY=False" in _lines(proc)


def test_ps_another_machine_asks_for_the_url_and_is_client_only(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["2", f" {REMOTE}/ "])
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 2 and "URL" in _asked(proc)[1]
    for line in ("BANK=remote", "CLIENT_ONLY=True", f"DAEMON_URL={REMOTE}"):
        assert line in _lines(proc), line


def test_ps_another_machine_through_a_loopback_tunnel_is_still_client_only(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["2", "http://127.0.0.1:18765"])
    assert proc.returncode == 0, _output(proc)
    assert "CLIENT_ONLY=True" in _lines(proc)


def test_ps_a_shared_bank_is_a_local_install(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["3"])
    assert proc.returncode == 0, _output(proc)
    assert "BANK=shared" in _lines(proc) and "CLIENT_ONLY=False" in _lines(proc)


def test_ps_an_answer_that_is_not_offered_is_asked_again(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["4", "3"])
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 2
    assert "please answer 1, 2 or 3" in _output(proc)
    assert "BANK=shared" in _lines(proc)


def test_ps_the_url_answer_is_validated_as_the_parameter_is(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["2", "pl.example.invalid:8765"])
    assert proc.returncode == 2
    assert "invalid daemon URL" in _output(proc)


def test_ps_the_client_only_refusals_run_on_the_answer(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["2", REMOTE], extractor="sidecar")
    assert proc.returncode == 2
    assert "-Extractor" in _output(proc)


def test_ps_without_a_terminal_nothing_is_asked(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), None)
    assert proc.returncode == 0, _output(proc)
    assert QUESTION not in _output(proc)
    assert _lines(proc)[:2] == ["BANK=local", "CLIENT_ONLY=False"]


@pytest.mark.parametrize("kwargs", [
    {"daemon_url": REMOTE},
    {"daemon_url": "http://127.0.0.1:8765"},
    {"client_only": True, "daemon_url": "http://127.0.0.1:18765"},
    {"extra_env": {"PSEUDOLIFE_MCP_DAEMON_URL": "http://127.0.0.1:8765"}},
], ids=["remote-flag", "loopback-flag", "client-only", "environment"])
def test_ps_a_run_that_names_the_daemon_is_not_asked(tmp_path, kwargs):
    proc = _ask_ps(_PowerShell(tmp_path), ["3"], **kwargs)
    assert proc.returncode == 0, _output(proc)
    assert _asked(proc) == []
    assert "BANK=local" in _lines(proc)


@pytest.mark.parametrize("clients,name", [
    (("claude",), "claude-code"), (("codex",), "codex"), (("gemini",), "gemini"),
    (("claude-desktop",), "claude-desktop"), (("generic",), "mcp-client"),
    (("claude", "codex"), "shared")])
def test_ps_a_pasted_token_goes_to_a_new_file_named_for_the_client(tmp_path, clients, name):
    ps = _PowerShell(tmp_path)
    proc = _ask_ps(ps, ["2", REMOTE, "", ""], clients=clients, then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 4
    assert f"~/.pseudolife-mcp/{name}.token" in _asked(proc)[3]
    [line] = [line for line in _lines(proc) if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == ps.home / ".pseudolife-mcp" / f"{name}.token"
    assert "READ_TOKEN=True" in _lines(proc)


def test_ps_an_existing_token_file_is_used_as_it_is(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ask_ps(ps, ["2", REMOTE, "2", "~/keys/remote.token"],
                   then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    [line] = [line for line in _lines(proc) if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == ps.home / "keys" / "remote.token"
    assert "READ_TOKEN=False" in _lines(proc)


def test_ps_a_pasted_token_never_names_a_file_that_exists(tmp_path):
    ps = _PowerShell(tmp_path)
    taken = ps.home / ".pseudolife-mcp" / "claude-code.token"
    taken.parent.mkdir()
    taken.write_text("fixture-old\n", encoding="utf-8")
    proc = _ask_ps(ps, ["2", REMOTE, "", "", "~/other.token"], then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 5
    assert "exists already" in _output(proc)
    [line] = [line for line in _lines(proc) if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == ps.home / "other.token"


def test_ps_a_token_file_parameter_asks_only_for_the_url(tmp_path):
    token = str(tmp_path / "t.token")
    proc = _ask_ps(_PowerShell(tmp_path), [REMOTE], token_file=token,
                   then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 1 and "URL" in _asked(proc)[0]
    assert QUESTION not in _output(proc)
    for line in ("BANK=remote", "CLIENT_ONLY=True", f"DAEMON_URL={REMOTE}",
                 f"TOKEN_FILE={token}", "READ_TOKEN=False"):
        assert line in _lines(proc), line


def test_ps_read_token_without_a_file_asks_for_the_url_and_where_to_write_it(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ask_ps(ps, [REMOTE, "~/new.token"], read_token=True,
                   then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 2
    [line] = [line for line in _lines(proc) if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == ps.home / "new.token"


@pytest.mark.parametrize("kwargs", [
    {"token_file": "/fixture/remote.token"}, {"read_token": True}], ids=["token-file", "read-token"])
def test_ps_token_parameters_without_a_url_are_refused_without_a_terminal(tmp_path, kwargs):
    proc = _ask_ps(_PowerShell(tmp_path), None, **kwargs)
    assert proc.returncode == 2
    assert "-ClientOnly -DaemonUrl" in _output(proc)


def test_ps_a_token_file_from_the_environment_is_named_and_used(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["2", REMOTE], then="Invoke-BankTokenQuestion\n",
                   extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/remote.token"})
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 2
    [named] = [line for line in _output(proc).splitlines() if "/fixture/remote.token" in line
               and not line.startswith("TOKEN_FILE=")]
    assert "PSEUDOLIFE_MCP_TOKEN_FILE" in named


def test_ps_quoted_answers_are_unquoted(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ask_ps(ps, ["2", f"'{REMOTE}'", "2", '"~/keys/remote.token"'],
                   then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    assert f"DAEMON_URL={REMOTE}" in _lines(proc)
    [line] = [line for line in _lines(proc) if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == ps.home / "keys" / "remote.token"


def test_ps_no_answer_at_a_terminal_is_refused(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), [])
    assert proc.returncode == 2
    assert "no answer" in _output(proc)


def test_ps_a_shared_bank_without_a_token_is_refused(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), ["3"], no_token=True)
    assert proc.returncode == 2
    assert "-NoToken" in _output(proc) and "never be exposed" in _output(proc)
    assert "docs/guide/remote-bank.md" in _output(proc)


def test_ps_the_question_is_shown_at_a_terminal(tmp_path):
    proc = _ask_ps(_PowerShell(tmp_path), [""])
    assert QUESTION in _output(proc)


@pytest.mark.parametrize("answer", ["1", "3"])
def test_ps_a_local_answer_never_asks_for_a_token(tmp_path, answer):
    proc = _ask_ps(_PowerShell(tmp_path), [answer], then="Invoke-BankTokenQuestion\n")
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 1


def test_ps_the_other_machine_answer_runs_the_client_only_preflight(tmp_path):
    """Answer 2, the URL, "paste", the default path: the preflight writes
    the token file from the pasted token (redirected stdin here) and checks
    the daemon at the URL."""
    ps = _PowerShell(tmp_path)
    stubs = "".join(ps.stub_function(name) for name in ("claude", "codex", "gemini"))
    preflight = (f"$env:PATH = '{_ps_q(ps.tmp)}'\n" + stubs
                 + ps.stub_function("Invoke-RestMethod",
                                    "return ($env:FAKE_HEALTH | ConvertFrom-Json)")
                 + _ps_block("client-only preflight") + "\nInvoke-ClientOnlyPreflight\n")
    proc = _ask_ps(ps, ["2", REMOTE, "1", ""], then="Invoke-BankTokenQuestion\n" + preflight,
                   extra_env={"FAKE_HEALTH": HEALTH_ON}, stdin_text=FIXTURE_TOKEN + "\n")
    assert proc.returncode == 0, _output(proc)
    target = ps.home / ".pseudolife-mcp" / "claude-code.token"
    assert CredentialProvider(path=target).snapshot().token == FIXTURE_TOKEN
    assert FIXTURE_TOKEN not in _output(proc)
    assert f"use the daemon at {REMOTE}" in _output(proc)
    assert any(call.startswith("Invoke-RestMethod|") and f"{REMOTE}/health" in call
               for call in ps.logged())


# -- shared helpers of both installers --------------------------------------------

def test_the_question_is_the_same_in_both_installers():
    sh = _heredoc_payload(_marker_block(SH, "bank location"))
    ps = _heredoc_payload(_marker_block(PS, "bank location"))
    assert sh == ps
    joined = "\n".join(sh)
    assert QUESTION in joined
    for option in ("1) On this machine (default)", "2) On another machine that already runs it",
                   "3) On this machine, and other machines will connect to it"):
        assert option in joined
    for line in sh:
        assert len(line) <= 78 and all(0x20 <= ord(c) <= 0x7E for c in line), line


def test_the_question_comes_after_the_banner_and_before_the_agents():
    """The mode is resolved on the answer: question, then the mode block,
    then the agent selection, then the token (named for the agent), then
    the preflight."""
    order = [SH.index("\nshow_banner\n"), SH.index("\nchoose_bank_location\n"),
             SH.index("# >>> client-only mode >>>"), SH.index("# ── 1. provider selection"),
             SH.index("\nchoose_bank_token\n"), SH.index("# ── 2. preflight")]
    assert order == sorted(order)
    order = [PS.index("\nShow-Banner\n"), PS.index("\nInvoke-BankLocationQuestion\n"),
             PS.index("# >>> client-only mode >>>"), PS.index("# -- 1. provider selection"),
             PS.index("\nInvoke-BankTokenQuestion\n"), PS.index("# -- 2. preflight")]
    assert order == sorted(order)


# -- option 3: the exposure steps ---------------------------------------------------

def test_a_shared_bank_ends_with_the_expose_and_invite_commands():
    """Two commands, expose then invite per joining machine, whose code the
    other machine gives to option 2 (or to pair); the hand steps stay in
    the guide."""
    sh = _heredoc_payload(_marker_block(SH, "shared bank notes"))
    ps = _heredoc_payload(_marker_block(PS, "shared bank notes"))
    assert sh == ps
    joined = "\n".join(sh)
    assert joined.index("pseudolife-mcp expose tailscale") < joined.index(
        "pseudolife-mcp invite <machine>")
    for needle in ("answer 2", "pseudolife-mcp pair <url> <code>", "docs/guide/remote-bank.md"):
        assert needle in joined, needle
    # The hand steps are the guide's now, not the summary's.
    for gone in ("tailscale serve --bg", "PSEUDOLIFE_MCP_TOKENS", "allowed_principals"):
        assert gone not in joined, gone
    for line in sh:
        assert len(line) <= 78 and all(0x20 <= ord(c) <= 0x7E for c in line), line


@BASH
def test_the_shared_bank_notes_print(bash, tmp_path):
    proc = _Shell(bash, tmp_path).run(_sh_block("shared bank notes") + "show_shared_bank_notes\n")
    assert proc.returncode == 0, proc.stderr
    assert "pseudolife-mcp expose tailscale" in proc.stdout
    assert "pseudolife-mcp invite <machine>" in proc.stdout


def test_ps_the_shared_bank_notes_print(tmp_path):
    proc = _PowerShell(tmp_path).run(_ps_block("shared bank notes") + "Show-SharedBankNotes\n")
    assert proc.returncode == 0, _output(proc)
    assert "pseudolife-mcp expose tailscale" in _output(proc)
    assert "pseudolife-mcp invite <machine>" in _output(proc)


def test_the_summary_prints_them_only_for_a_shared_bank():
    sh_summary = SH.split("# ── 13.", 1)[1]
    ps_summary = PS.split("# -- 13.", 1)[1]
    sh_notes = 'if [ "${BANK_LOCATION:-}" = shared ]; then show_shared_bank_notes; echo ""; fi'
    ps_notes = 'if ($script:bankLocation -eq "shared") { Show-SharedBankNotes; Write-Host "" }'
    assert sh_notes in sh_summary
    assert ps_notes in ps_summary
    # The offer to run expose follows the notes that explain it.
    sh_offer = 'if [ "${BANK_LOCATION:-}" = shared ]; then offer_shared_bank_expose; fi'
    ps_offer = 'if ($script:bankLocation -eq "shared") { Invoke-SharedBankExposeOffer }'
    assert sh_summary.index(sh_notes) < sh_summary.index(sh_offer)
    assert ps_summary.index(ps_notes) < ps_summary.index(ps_offer)


EXPOSE_SHIM = ("printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
               "exit \"${FAKE_EXPOSE_EXIT:-0}\"\n")


def _offer_sh(shell: _Shell, answers: list[str] | None, *, shim_ok: bool = True,
              expose_exit: int = 0):
    """The expose offer at the end of an option-3 install. ``answers``: the
    replies at a stand-in terminal (None: the stock check, no terminal)."""
    shim = shell.bin / "fake-shim"
    _stub(shim, EXPOSE_SHIM)
    body = (_sh_block("bank location") + _sh_block("shared bank notes")
            + (f"ensure_shim() {{ SHIM_OK=1; SHIM_PATH='{shell.path(shim)}'; }}\n" if shim_ok
               else "ensure_shim() { SHIM_OK=''; SHIM_PATH=''; }\n"))
    if answers is not None:
        replies = shell.tmp / "answers.txt"
        replies.write_bytes("".join(answer + "\n" for answer in answers).encode("utf-8"))
        body += (f"exec 3<'{shell.path(replies)}'\n"
                 "bank_can_ask() { return 0; }\n"
                 "bank_read() { printf 'ASKED: %s\\n' \"$1\"; BANK_REPLY=''\n"
                 "    IFS= read -r BANK_REPLY <&3 || [ -n \"$BANK_REPLY\" ] || return 1; }\n")
    proc = shell.run(body + "offer_shared_bank_expose\necho CONTINUED\n",
                     extra_env={"FAKE_EXPOSE_EXIT": str(expose_exit)})
    return proc, [call for call in shell.logged() if call.startswith("shim|")]


@pytest.mark.parametrize("answer", ["", "n", "no", "N", "later"])
@BASH
def test_the_expose_offer_defaults_to_no(bash, tmp_path, answer):
    proc, calls = _offer_sh(_Shell(bash, tmp_path), [answer])
    assert proc.returncode == 0, proc.stderr
    [asked] = _asked(proc)
    assert "pseudolife-mcp expose tailscale" in asked and "[y/N]" in asked
    assert "tailnet" in asked
    assert calls == []
    assert "CONTINUED" in proc.stdout


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " yes "])
@BASH
def test_a_yes_runs_expose_which_asks_for_itself(bash, tmp_path, answer):
    """Without --yes: expose shows its own plan and asks before it changes
    the host's tailnet serve."""
    proc, calls = _offer_sh(_Shell(bash, tmp_path), [answer])
    assert proc.returncode == 0, proc.stderr
    assert calls == ["shim|expose tailscale"]
    assert "CONTINUED" in proc.stdout


@BASH
def test_without_a_terminal_expose_is_not_offered(bash, tmp_path):
    proc, calls = _offer_sh(_Shell(bash, tmp_path), None)
    assert proc.returncode == 0, proc.stderr
    assert "expose tailscale now" not in proc.stdout + proc.stderr
    assert calls == []
    assert "CONTINUED" in proc.stdout


@BASH
def test_end_of_input_at_the_expose_offer_is_a_no(bash, tmp_path):
    proc, calls = _offer_sh(_Shell(bash, tmp_path), [])
    assert proc.returncode == 0, proc.stderr
    assert calls == []


@BASH
def test_a_failed_expose_does_not_fail_the_finished_install(bash, tmp_path):
    proc, calls = _offer_sh(_Shell(bash, tmp_path), ["y"], expose_exit=4)
    assert proc.returncode == 0, proc.stderr
    assert calls == ["shim|expose tailscale"]
    assert "expose tailscale exited 4" in proc.stderr
    assert "CONTINUED" in proc.stdout


@BASH
def test_expose_without_a_shim_says_how_to_run_it_later(bash, tmp_path):
    proc, calls = _offer_sh(_Shell(bash, tmp_path), ["y"], shim_ok=False)
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert "pseudolife-mcp expose tailscale" in proc.stderr
    assert "CONTINUED" in proc.stdout


PS_EXPOSE_SHIM = """function global:fake-shim {{
    Add-Content -LiteralPath '{calls}' -Value ('shim|' + (@($args) -join ' '))
    $global:LASTEXITCODE = [int]$env:FAKE_EXPOSE_EXIT
}}
"""


def _offer_ps(ps: _PowerShell, answers: list[str] | None, *, shim_ok: bool = True,
              expose_exit: int = 0):
    ensure = ("function Install-ShimOnce { $script:shimInstallPath = 'fake-shim'; return $true }\n"
              if shim_ok else
              "function Install-ShimOnce { $script:shimInstallPath = $null; return $false }\n")
    body = (f"$interactive = ${'false' if answers is None else 'true'}\n"
            + PS_EXPOSE_SHIM.format(calls=_ps_q(ps.calls)) + _ps_block("bank location")
            + _ps_block("shared bank notes") + ensure)
    if answers is not None:
        queued = ", ".join(f"'{_ps_q(answer)}'" for answer in answers)
        body += (f"$script:fixtureAnswers = [Collections.Queue]::new([string[]]@({queued}))\n"
                 "function Read-BankAnswer([string]$Prompt) {\n"
                 "    Write-Host \"ASKED: $Prompt\"\n"
                 "    if ($script:fixtureAnswers.Count -eq 0) { return $null }\n"
                 "    return $script:fixtureAnswers.Dequeue()\n}\n")
    proc = ps.run(body + "Invoke-SharedBankExposeOffer\nWrite-Output 'CONTINUED'\n",
                  extra_env={"FAKE_EXPOSE_EXIT": str(expose_exit)})
    return proc, [call for call in ps.logged() if call.startswith("shim|")]


@pytest.mark.parametrize("answer", ["", "n", "no", "N", "later"])
def test_ps_the_expose_offer_defaults_to_no(tmp_path, answer):
    proc, calls = _offer_ps(_PowerShell(tmp_path), [answer])
    assert proc.returncode == 0, _output(proc)
    [asked] = _asked(proc)
    assert "pseudolife-mcp expose tailscale" in asked and "[y/N]" in asked
    assert "tailnet" in asked
    assert calls == []
    assert "CONTINUED" in _lines(proc)


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " yes "])
def test_ps_a_yes_runs_expose_which_asks_for_itself(tmp_path, answer):
    proc, calls = _offer_ps(_PowerShell(tmp_path), [answer])
    assert proc.returncode == 0, _output(proc)
    assert calls == ["shim|expose tailscale"]
    assert "CONTINUED" in _lines(proc)


def test_ps_without_a_terminal_expose_is_not_offered(tmp_path):
    proc, calls = _offer_ps(_PowerShell(tmp_path), None)
    assert proc.returncode == 0, _output(proc)
    assert "expose tailscale now" not in _output(proc)
    assert calls == []
    assert "CONTINUED" in _lines(proc)


def test_ps_end_of_input_at_the_expose_offer_is_a_no(tmp_path):
    proc, calls = _offer_ps(_PowerShell(tmp_path), [])
    assert proc.returncode == 0, _output(proc)
    assert calls == []


def test_ps_a_failed_expose_does_not_fail_the_finished_install(tmp_path):
    proc, calls = _offer_ps(_PowerShell(tmp_path), ["y"], expose_exit=4)
    assert proc.returncode == 0, _output(proc)
    assert calls == ["shim|expose tailscale"]
    assert "expose tailscale exited 4" in _output(proc)
    assert "CONTINUED" in _lines(proc)


def test_ps_expose_without_a_shim_says_how_to_run_it_later(tmp_path):
    proc, calls = _offer_ps(_PowerShell(tmp_path), ["y"], shim_ok=False)
    assert proc.returncode == 0, _output(proc)
    assert calls == []
    assert "pseudolife-mcp expose tailscale" in _output(proc)
    assert "CONTINUED" in _lines(proc)


# -- option 2: a pairing code in place of the token ----------------------------------

def test_the_token_menu_offers_a_pairing_code_in_both_installers():
    for text in (_marker_block(SH, "bank location"), _marker_block(PS, "bank location")):
        joined = "\n".join(text)
        assert "The daemon's bearer token, or a pairing code from pseudolife-mcp invite:" in joined
        assert "1) Paste it now: it makes a new owner-only token file (default)" in joined
        assert "2) The token is already in a token file on this machine" in joined


@BASH
def test_a_pairing_code_flag_asks_only_for_the_url_and_where_to_write(bash, tmp_path):
    """--pairing-code means "another machine", as --token-file does, and its
    token goes to a new file: the URL, then the file (Enter = the default),
    never the paste-or-file menu."""
    shell = _Shell(bash, tmp_path)
    proc = _ask_sh(shell, [REMOTE, ""], then="choose_bank_token\n",
                   extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/other.token"},
                   pairing_code="7KQ2-MX4P-9TZC")
    assert proc.returncode == 0, proc.stderr
    assert len(_asked(proc)) == 2
    assert "URL" in _asked(proc)[0] and "~/.pseudolife-mcp/claude-code.token" in _asked(proc)[1]
    assert QUESTION not in proc.stdout
    assert (f"BANK=remote\nCLIENT_ONLY=1\nDAEMON_URL={REMOTE}\n"
            f"TOKEN_FILE={shell.path(shell.home)}/.pseudolife-mcp/claude-code.token\n"
            "READ_TOKEN=\n") in proc.stdout
    assert "7KQ2" not in proc.stdout + proc.stderr


def test_ps_a_pairing_code_parameter_asks_only_for_the_url_and_where_to_write(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ask_ps(ps, [REMOTE, ""], then="Invoke-BankTokenQuestion\n",
                   extra_env={"PSEUDOLIFE_MCP_TOKEN_FILE": "/fixture/other.token"},
                   pairing_code="7KQ2-MX4P-9TZC")
    assert proc.returncode == 0, _output(proc)
    assert len(_asked(proc)) == 2
    assert "URL" in _asked(proc)[0] and "~/.pseudolife-mcp/claude-code.token" in _asked(proc)[1]
    assert QUESTION not in _output(proc)
    [line] = [line for line in _lines(proc) if line.startswith("TOKEN_FILE=")]
    assert Path(line[len("TOKEN_FILE="):]) == ps.home / ".pseudolife-mcp" / "claude-code.token"
    assert "READ_TOKEN=False" in _lines(proc)
    assert "7KQ2" not in _output(proc)


# -- client-only: connect re-points existing registrations ----------------------------

PLAN_EXISTING = json.dumps({"url": REMOTE, "exit": 0, "rows": [
    {"client": "codex", "place": "registration", "file": "/fixture/codex/config.toml",
     "key": "pseudolife-memory", "state": "change",
     "changes": {"PSEUDOLIFE_MCP_DAEMON_URL": ["http://127.0.0.1:8765", REMOTE]},
     "detail": "", "notes": []},
    {"client": "codex", "place": "connection", "file": "/fixture/codex/pseudolife/connection.json",
     "key": None, "state": "change", "changes": {"daemon_url": ["http://127.0.0.1:8765", REMOTE]},
     "detail": "", "notes": []},
    {"client": "gemini", "place": "registration", "file": "/fixture/gemini/settings.json",
     "key": "pseudolife-memory", "state": "current", "changes": {}, "detail": "", "notes": []},
    {"client": "claude-code", "place": "registration", "file": None, "key": None,
     "state": "absent", "changes": {},
     "detail": "no registration; connect never creates one. To register: fixture-register",
     "notes": []}]})
PLAN_MANUAL_ONLY = json.dumps({"url": REMOTE, "exit": 3, "rows": [
    {"client": "claude-code", "place": "project", "file": "/fixture/.claude.json",
     "key": 'projects["/fixture"].mcpServers.pseudolife-memory', "state": "manual",
     "changes": {}, "detail": "a project-scoped registration: fixture-manual-detail",
     "notes": []}]})
NO_REGISTRATION = json.dumps({"url": REMOTE, "exit": 3, "rows": [], "error": "fixture-none"})


def _refused(code: int) -> str:
    """connect's --json report of a dry run that failed: the reason is only
    in its ``error`` field (stderr stays empty under --json)."""
    return json.dumps({"url": REMOTE, "exit": code, "rows": [],
                       "error": f"fixture-refusal-{code}: the daemon did not answer"})


SHIM_STUB = ("printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
             "case \" $* \" in *' --dry-run '*)\n"
             "    printf '%s\\n' \"$FAKE_PLAN\"; exit \"${FAKE_PLAN_EXIT:-0}\" ;; esac\n"
             "echo 'fixture connect: applied'\n"
             "exit \"${FAKE_APPLY_EXIT:-0}\"\n")


def _connect_sh(shell: _Shell, *, clients: str = "codex", plan: str = PLAN_EXISTING,
                plan_exit: int = 0, apply_exit: int = 0, shim_ok: bool = True,
                shim_path: Path | None = None, held: str = ""):
    token = _sh_token(shell)
    shim = shell.bin / "fake-shim"
    _stub(shim, SHIM_STUB)
    shim = shim_path or shim
    ensure = (f"ensure_shim() {{ SHIM_OK=1; SHIM_PATH='{shell.path(shim)}'; }}\n" if shim_ok
              else "ensure_shim() { SHIM_OK=''; SHIM_PATH=''; }\n")
    proc = shell.run(
        f"DAEMON_URL='{REMOTE}'\nCLIENT_ONLY=1\nTOKEN_FILE='{shell.path(token)}'\n"
        f"SHIM_HELD='{_sh_q(held)}'\n"
        f"CLIENTS='{clients}'\n" + ensure + _sh_block("client-only connect")
        + "connect_existing_registrations\nprintf 'CONNECTED=%s\\n' \"$CONNECTED_CLIENTS\"\n",
        strict="set -euo pipefail",
        extra_env={"FAKE_PLAN": plan, "FAKE_PLAN_EXIT": str(plan_exit),
                   "FAKE_APPLY_EXIT": str(apply_exit)})
    calls = [call for call in shell.logged() if call.startswith("shim|")]
    return proc, calls, shell.path(token)


@BASH
def test_no_existing_registration_runs_only_the_dry_run(bash, tmp_path):
    proc, calls, token = _connect_sh(_Shell(bash, tmp_path), plan=NO_REGISTRATION, plan_exit=3)
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"shim|connect {REMOTE} --token-file {token} --client codex --dry-run --json"]
    assert "CONNECTED=\n" in proc.stdout


@BASH
def test_existing_registrations_are_shown_then_re_pointed(bash, tmp_path):
    proc, calls, token = _connect_sh(_Shell(bash, tmp_path), clients="codex gemini")
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        f"shim|connect {REMOTE} --token-file {token} --client codex,gemini --dry-run --json",
        f"shim|connect {REMOTE} --token-file {token} --client codex,gemini --yes"]
    out = proc.stdout
    assert re.search(r"change\s+codex registration: /fixture/codex/config.toml", out)
    assert f"PSEUDOLIFE_MCP_DAEMON_URL: http://127.0.0.1:8765 -> {REMOTE}" in out
    assert re.search(r"current\s+gemini registration", out)
    # An absent client is the registrars' to create, not a connect action.
    assert "fixture-register" not in out
    assert out.index("codex registration") < out.index("fixture connect: applied")
    assert "CONNECTED=codex gemini\n" in out


@pytest.mark.parametrize("clients,names", [
    ("claude claude-desktop codex gemini generic", "claude-code,claude-desktop,codex,gemini"),
    ("claude", "claude-code")])
@BASH
def test_connect_is_given_its_own_client_names(bash, tmp_path, clients, names):
    proc, calls, token = _connect_sh(_Shell(bash, tmp_path), clients=clients, plan=NO_REGISTRATION,
                                     plan_exit=3)
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"shim|connect {REMOTE} --token-file {token} --client {names} --dry-run --json"]


@BASH
def test_a_generic_agent_alone_runs_no_connect(bash, tmp_path):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), clients="generic")
    assert proc.returncode == 0, proc.stderr
    assert calls == []


@pytest.mark.parametrize("apply_exit,says", [
    (1, "rolled back"), (4, "nothing was written"), (5, "backups"), (2, "see its message")])
@BASH
def test_a_failed_connect_stops_the_installer(bash, tmp_path, apply_exit, says):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), apply_exit=apply_exit)
    assert proc.returncode == 1
    assert len(calls) == 2
    assert f"pseudolife-mcp connect exited {apply_exit}" in proc.stderr
    assert says in proc.stderr
    assert "CONNECTED=" not in proc.stdout


@pytest.mark.parametrize("plan_exit", [1, 2, 4])
@BASH
def test_a_dry_run_that_fails_stops_before_anything_is_changed(bash, tmp_path, plan_exit):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), plan=_refused(plan_exit),
                                 plan_exit=plan_exit)
    assert proc.returncode == 1
    assert len(calls) == 1
    assert "could not check" in proc.stderr
    # --json keeps connect's own reason in the report: it must reach the user.
    assert f"fixture-refusal-{plan_exit}: the daemon did not answer" in proc.stderr


@pytest.mark.parametrize("held", ["", "fixture-held: 1 session is running the shim"])
@BASH
def test_a_shim_without_connect_leaves_the_install_as_before(bash, tmp_path, held):
    """A held shim from before connect answers "unknown mode": no report,
    so nothing is trusted, nothing is stopped, and the registrars run with
    their earlier advice."""
    shell = _Shell(bash, tmp_path)
    older = shell.bin / "older-shim"
    _stub(older, "printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
                 "echo \"unknown mode 'connect'\"; exit 2\n")
    proc, calls, _ = _connect_sh(shell, shim_path=older, held=held)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1
    assert "not checked" in proc.stderr
    assert "CONNECTED=\n" in proc.stdout
    if held:
        assert held in proc.stderr


@BASH
def test_a_dry_run_that_succeeds_without_a_report_applies_nothing(bash, tmp_path):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), plan="fixture: not a report")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1 and "--dry-run" in calls[0]
    assert "not checked" in proc.stderr


@BASH
def test_a_shim_that_cannot_start_is_a_warning_not_a_failure(bash, tmp_path):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), shim_path=tmp_path / "absent-shim")
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert "not checked" in proc.stderr


@BASH
def test_manual_entries_are_named_when_nothing_can_be_re_pointed(bash, tmp_path):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), clients="claude",
                                 plan=PLAN_MANUAL_ONLY, plan_exit=3)
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 1
    assert "fixture-manual-detail" in proc.stdout + proc.stderr


@BASH
def test_without_a_shim_existing_registrations_are_left_with_a_warning(bash, tmp_path):
    proc, calls, _ = _connect_sh(_Shell(bash, tmp_path), shim_ok=False)
    assert proc.returncode == 0, proc.stderr
    assert calls == []
    assert "not checked" in proc.stderr


def _connect_stages_sh(shell: _Shell, *, plan: str, plan_exit: int = 0, apply_exit: int = 0,
                       cred_url: str = REMOTE, shim: str = SHIM_STUB):
    """Sections 9 to 11 for a Codex-only client-only install: the installed
    shim, python, pipx and codex all log to one file, in order."""
    token = _sh_token(shell)
    installed = shell.tmp / "installed-bin"
    installed.mkdir()
    _stub(installed / "pseudolife-mcp", shim)
    for name in ("python3", "python"):
        _stub(shell.bin / name,
              "case \"$1\" in\n"
              "  -c) exec \"$FIXTURE_PYTHON\" \"$@\" ;;\n"
              "  *setup-codex-coordination.py)\n"
              "    printf 'python|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
              "    if [ \"$2\" = --runtime-defaults ]; then\n"
              "      printf '{\"status\":\"ready\",\"runtime_defaults\":\"configured\"}\\n'; exit 0; fi\n"
              "    cat >/dev/null; printf '%s\\n' \"$FAKE_CRED\" ;;\n"
              "  *setup-codex-hooks.py)\n"
              "    printf '{\"status\":\"ready\",\"source\":\"manual\","
              "\"instructions\":\"covered-by-hooks\",\"recovery\":null}\\n' ;;\n"
              "esac\n")
    _stub(shell.bin / "pipx",
          "if [ \"$1\" = environment ]; then\n"
          "  [ \"$3\" = PIPX_BIN_DIR ] && printf '%s\\n' \"$FAKE_INSTALL_BIN\"; exit 0; fi\nexit 0\n")
    _stub(shell.bin / "codex",
          "printf 'codex|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "if [ \"$1 $2\" = 'mcp get' ]; then exit 1; fi\n"
          "if [ \"$1 $2 $3\" = 'mcp add --help' ]; then echo '  --env <KEY=VALUE>'; exit 0; fi\n"
          "exit 0\n")
    stages = re.search(r"(?ms)^# [^\n]*9\. session lifecycle hooks[^\n]*\n(.*?)"
                       r"^# [^\n]*12\. health", SH)[1]
    from tests.test_installer_existing_upgrade import _between
    proc = shell.run(
        f"env_file='{shell.path(shell.tmp / 'absent.env')}'\n"
        f"CLIENT_ONLY=1\nDAEMON_URL='{REMOTE}'\nTOKEN_FILE='{shell.path(token)}'\n"
        "CLIENTS=codex CODEX_HOOKS=auto CODEX_HOOK_TRUST=yes INSTRUCTIONS=auto CLAUDE_MD=''\n"
        "AGENTS_FILE='' TRANSPORT=shim\n"
        + _between("ops/install.sh", "get_env() {", "\n") + "\n" + stages
        + "\nprintf 'MCP_CODEX=%s\\n' \"$MCP_CODEX\"\n",
        strict="set -euo pipefail",
        extra_env={"FIXTURE_PYTHON": _bash_fixture_path(shell.bash, Path(sys.executable),
                                                        shell.env),
                   "FAKE_CRED": CRED_READY % cred_url, "FAKE_PLAN": plan,
                   "FAKE_PLAN_EXIT": str(plan_exit), "FAKE_APPLY_EXIT": str(apply_exit),
                   "FAKE_INSTALL_BIN": _bash_fixture_path(shell.bash, installed, shell.env)})
    return proc, shell.logged()


def _first(calls: list[str], prefix: str) -> int:
    return next(i for i, call in enumerate(calls) if call.startswith(prefix))


@BASH
def test_connect_runs_before_the_codex_setup_and_the_registrars(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=PLAN_EXISTING)
    assert proc.returncode == 0, proc.stderr
    shim = [i for i, call in enumerate(calls) if call.startswith("shim|")]
    assert len(shim) == 2
    assert "--dry-run" in calls[shim[0]] and "--yes" in calls[shim[1]]
    assert shim[1] < _first(calls, "python|") < _first(calls, "codex|")
    assert "MCP_CODEX=shim-env" in proc.stdout


@BASH
def test_without_a_registration_the_codex_setup_follows_the_dry_run(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=NO_REGISTRATION, plan_exit=3)
    assert proc.returncode == 0, proc.stderr
    shim = [call for call in calls if call.startswith("shim|")]
    assert len(shim) == 1 and "--dry-run" in shim[0]
    assert _first(calls, "shim|") < _first(calls, "python|")
    assert "MCP_CODEX=shim-env" in proc.stdout


@BASH
def test_a_failed_connect_stops_before_the_codex_setup(bash, tmp_path):
    proc, calls = _connect_stages_sh(_Shell(bash, tmp_path), plan=PLAN_EXISTING, apply_exit=4)
    assert proc.returncode == 1
    assert not any(call.startswith(("python|", "codex|")) for call in calls)


OLDER_SHIM = ("printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
              "echo \"unknown mode 'connect'\"; exit 2\n")


@BASH
def test_a_codex_mismatch_connect_left_says_connect_did_not_re_point_it(bash, tmp_path):
    """connect checked (a report came back) and the Codex registration still
    names another daemon: one connect could not write."""
    proc, _ = _connect_stages_sh(_Shell(bash, tmp_path), plan=NO_REGISTRATION, plan_exit=3,
                                 cred_url="http://127.0.0.1:8765")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert f"not the daemon at {REMOTE}, and pseudolife-mcp connect did not re-point it" in out
    assert "Edit it in place" not in out
    assert "MCP_CODEX=failed" in proc.stdout


@BASH
def test_a_codex_mismatch_without_connect_keeps_the_earlier_advice(bash, tmp_path):
    proc, _ = _connect_stages_sh(_Shell(bash, tmp_path), plan="", cred_url="http://127.0.0.1:8765",
                                 shim=OLDER_SHIM)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout + proc.stderr
    assert "Edit it in place in the Codex config.toml" in out
    assert "did not re-point" not in out


# -- client-only connect: ops/install.ps1 ----------------------------------------------

PS_SHIM = """function global:fake-shim {{
    Add-Content -LiteralPath '{calls}' -Value ('shim|' + (@($args) -join ' '))
    if ($args -contains '--dry-run') {{
        $global:LASTEXITCODE = [int]$env:FAKE_PLAN_EXIT
        return $env:FAKE_PLAN
    }}
    Write-Host 'fixture connect: applied'
    $global:LASTEXITCODE = [int]$env:FAKE_APPLY_EXIT
}}
"""


def _connect_ps(ps: _PowerShell, *, clients: tuple[str, ...] = ("codex",),
                plan: str = PLAN_EXISTING, plan_exit: int = 0, apply_exit: int = 0,
                shim_ok: bool = True, shim: str = "fake-shim", held: str = "",
                extra: str = ""):
    token = _ps_token(ps)
    client_list = ", ".join(f"'{client}'" for client in clients)
    ensure = (f"function Install-ShimOnce {{ $script:shimInstallPath = '{_ps_q(shim)}'; return $true }}\n"
              if shim_ok else
              "function Install-ShimOnce { $script:shimInstallPath = $null; return $false }\n")
    proc = ps.run(
        f"$DaemonUrl = '{REMOTE}'; $ClientOnly = [switch]$true; $TokenFile = '{_ps_q(token)}'\n"
        f"$clients = @({client_list})\n$script:shimUpgradeHeld = '{_ps_q(held)}'\n"
        + PS_SHIM.format(calls=_ps_q(ps.calls)) + extra + ensure
        + _ps_block("client-only connect")
        + "Invoke-ClientOnlyConnect\n"
          "Write-Output (\"CONNECTED=\" + (@($script:connectedClients) -join ' '))\n",
        extra_env={"FAKE_PLAN": plan, "FAKE_PLAN_EXIT": str(plan_exit),
                   "FAKE_APPLY_EXIT": str(apply_exit)})
    calls = [call for call in ps.logged() if call.startswith("shim|")]
    return proc, calls, str(token)


def test_ps_no_existing_registration_runs_only_the_dry_run(tmp_path):
    proc, calls, token = _connect_ps(_PowerShell(tmp_path), plan=NO_REGISTRATION, plan_exit=3)
    assert proc.returncode == 0, _output(proc)
    assert calls == [f"shim|connect {REMOTE} --token-file {token} --client codex --dry-run --json"]
    assert "CONNECTED=" in _lines(proc)


def test_ps_existing_registrations_are_shown_then_re_pointed(tmp_path):
    proc, calls, token = _connect_ps(_PowerShell(tmp_path), clients=("codex", "gemini"))
    assert proc.returncode == 0, _output(proc)
    assert calls == [
        f"shim|connect {REMOTE} --token-file {token} --client codex,gemini --dry-run --json",
        f"shim|connect {REMOTE} --token-file {token} --client codex,gemini --yes"]
    out = _output(proc)
    assert re.search(r"change\s+codex registration: /fixture/codex/config.toml", out)
    assert f"PSEUDOLIFE_MCP_DAEMON_URL: http://127.0.0.1:8765 -> {REMOTE}" in out
    assert "fixture-register" not in out
    assert out.index("codex registration") < out.index("fixture connect: applied")
    assert "CONNECTED=codex gemini" in _lines(proc)


def test_ps_connect_is_given_its_own_client_names(tmp_path):
    proc, calls, token = _connect_ps(
        _PowerShell(tmp_path), clients=("claude", "claude-desktop", "codex", "gemini", "generic"),
        plan=NO_REGISTRATION, plan_exit=3)
    assert proc.returncode == 0, _output(proc)
    assert calls == [f"shim|connect {REMOTE} --token-file {token} "
                     "--client claude-code,claude-desktop,codex,gemini --dry-run --json"]


def test_ps_a_generic_agent_alone_runs_no_connect(tmp_path):
    proc, calls, _ = _connect_ps(_PowerShell(tmp_path), clients=("generic",))
    assert proc.returncode == 0, _output(proc)
    assert calls == []


@pytest.mark.parametrize("apply_exit,says", [
    (1, "rolled back"), (4, "nothing was written"), (5, "backups"), (2, "see its message")])
def test_ps_a_failed_connect_stops_the_installer(tmp_path, apply_exit, says):
    proc, calls, _ = _connect_ps(_PowerShell(tmp_path), apply_exit=apply_exit)
    assert proc.returncode == 1
    assert len(calls) == 2
    assert f"pseudolife-mcp connect exited {apply_exit}" in _output(proc)
    assert says in _output(proc)
    assert not any(line.startswith("CONNECTED=") for line in _lines(proc))


@pytest.mark.parametrize("plan_exit", [1, 2, 4])
def test_ps_a_dry_run_that_fails_stops_before_anything_is_changed(tmp_path, plan_exit):
    proc, calls, _ = _connect_ps(_PowerShell(tmp_path), plan=_refused(plan_exit),
                                 plan_exit=plan_exit)
    assert proc.returncode == 1
    assert len(calls) == 1
    assert "could not check" in _output(proc)
    assert f"fixture-refusal-{plan_exit}: the daemon did not answer" in _output(proc)


PS_OLDER_SHIM = """function global:older-shim {{
    Add-Content -LiteralPath '{calls}' -Value ('shim|' + (@($args) -join ' '))
    $global:LASTEXITCODE = 2
    return "unknown mode 'connect'"
}}
"""


@pytest.mark.parametrize("held", ["", "fixture-held: 1 session is running the shim"])
def test_ps_a_shim_without_connect_leaves_the_install_as_before(tmp_path, held):
    ps = _PowerShell(tmp_path)
    proc, calls, _ = _connect_ps(ps, shim="older-shim", held=held,
                                 extra=PS_OLDER_SHIM.format(calls=_ps_q(ps.calls)))
    assert proc.returncode == 0, _output(proc)
    assert len(calls) == 1
    assert "not checked" in _output(proc)
    assert "CONNECTED=" in _lines(proc)
    if held:
        assert held in _output(proc)


def test_ps_a_dry_run_that_succeeds_without_a_report_applies_nothing(tmp_path):
    proc, calls, _ = _connect_ps(_PowerShell(tmp_path), plan="fixture: not a report")
    assert proc.returncode == 0, _output(proc)
    assert len(calls) == 1 and "--dry-run" in calls[0]
    assert "not checked" in _output(proc)


def test_ps_manual_entries_are_named_when_nothing_can_be_re_pointed(tmp_path):
    proc, calls, _ = _connect_ps(_PowerShell(tmp_path), clients=("claude",),
                                 plan=PLAN_MANUAL_ONLY, plan_exit=3)
    assert proc.returncode == 0, _output(proc)
    assert len(calls) == 1
    assert "fixture-manual-detail" in _output(proc)


def test_ps_without_a_shim_existing_registrations_are_left_with_a_warning(tmp_path):
    proc, calls, _ = _connect_ps(_PowerShell(tmp_path), shim_ok=False)
    assert proc.returncode == 0, _output(proc)
    assert calls == []
    assert "not checked" in _output(proc)


def test_ps_a_shim_that_cannot_start_is_a_warning_not_a_failure(tmp_path):
    ps = _PowerShell(tmp_path)
    token = _ps_token(ps)
    proc = ps.run(
        f"$DaemonUrl = '{REMOTE}'; $ClientOnly = [switch]$true; $TokenFile = '{_ps_q(token)}'\n"
        "$clients = @('codex')\n"
        f"function Install-ShimOnce {{ $script:shimInstallPath = '{_ps_q(tmp_path / 'absent-shim')}'; return $true }}\n"
        + _ps_block("client-only connect") + "Invoke-ClientOnlyConnect\nWrite-Output 'CONTINUED'\n")
    assert proc.returncode == 0, _output(proc)
    assert "CONTINUED" in _lines(proc)
    assert "not checked" in _output(proc)


def test_connect_runs_before_the_codex_credential_setup_in_both_installers():
    """Static half of the order contract (the bash stages test above runs
    it): the call sits before the Codex credential setup and the registrars."""
    assert (SH.index("\n[ -z \"${CLIENT_ONLY:-}\" ] || connect_existing_registrations\n")
            < SH.index("setup-codex-coordination.py\" --credentials")
            < SH.index("# ── 11. wire into selected MCP clients"))
    assert (PS.index("\nif ($ClientOnly) { Invoke-ClientOnlyConnect }\n")
            < PS.index('(Join-Path $repo "ops/setup-codex-coordination.py"),')
            < PS.index("# -- 11. wire into selected MCP clients"))


def test_pairing_runs_after_the_preflight_and_right_before_connect_in_both_installers():
    """A pairing code is redeemed once the preflight has checked the daemon,
    and immediately before connect, the first step that reads the token file
    pair creates; it runs whether or not connect has a client to check."""
    sh_pair = "\n[ -z \"${PAIRING_CODE:-}\" ] || pair_with_code\n"
    sh_connect = "[ -z \"${CLIENT_ONLY:-}\" ] || connect_existing_registrations\n"
    assert SH.index("\n    client_only_preflight\n") < SH.index(sh_pair)
    assert sh_pair + sh_connect in SH
    ps_pair = "\nif ($PairingCode) { Invoke-ClientOnlyPair }\n"
    ps_connect = "if ($ClientOnly) { Invoke-ClientOnlyConnect }\n"
    assert PS.index("\n    Invoke-ClientOnlyPreflight\n") < PS.index(ps_pair)
    assert ps_pair + ps_connect in PS


CLAUDE_EXISTING ="Type: stdio\nCommand: pseudolife-mcp\nEnvironment:\n  PSEUDOLIFE_MCP_NO_SPAWN=1"


@pytest.mark.parametrize("checked,says", [
    ("1", "and pseudolife-mcp connect did not re-point it"),
    ("", "Edit it in place and set PSEUDOLIFE_MCP_DAEMON_URL")], ids=["checked", "unchecked"])
@BASH
def test_a_claude_code_mismatch_names_what_happened(bash, tmp_path, checked, says):
    """After connect checked, a registration still naming another daemon is
    one it could not write; without connect (an older shim), the earlier
    advice stands."""
    from tests.test_installer_board_token import _Shell as _WireShell, _wire
    shell = _WireShell(bash, tmp_path)
    claude_json = shell.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"pseudolife-memory": {
        "type": "stdio", "command": "pseudolife-mcp", "args": [],
        "env": {"PSEUDOLIFE_WRITER_ID": "claude-code", "PSEUDOLIFE_MCP_NO_SPAWN": "1"}}}}),
        encoding="utf-8")
    run = shell.run
    shell.run = lambda body: run(f"CLIENT_ONLY=1\nCONNECT_CHECKED='{checked}'\n" + body)
    proc, _ = _wire(shell, "claude", token_file="/fixture/claude-code.token",
                    existing=CLAUDE_EXISTING, claude_json=claude_json)
    assert proc.returncode == 0, proc.stderr
    assert "does not name the daemon at http://127.0.0.1:8765" in proc.stderr
    assert says in proc.stderr


@pytest.mark.parametrize("checked,says", [
    ("$true", "and pseudolife-mcp connect did not re-point it"),
    ("$false", "Edit it in place and set PSEUDOLIFE_MCP_DAEMON_URL")], ids=["checked", "unchecked"])
def test_ps_a_claude_code_mismatch_names_what_happened(tmp_path, checked, says):
    from tests.test_installer_board_token_ps import _existing_claude_json, _wire
    ps = _PowerShell(tmp_path)
    claude_json = _existing_claude_json(ps)
    run = ps.run
    ps.run = lambda body, **kwargs: run(
        f"$ClientOnly = [switch]$true\n$script:connectChecked = {checked}\n" + body, **kwargs)
    proc, _ = _wire(ps, "claude", token_file=r"C:\fixture\claude-code.token",
                    existing=CLAUDE_EXISTING, claude_json=claude_json)
    assert proc.returncode == 0, _output(proc)
    assert "does not name the daemon at http://127.0.0.1:8765" in _output(proc)
    assert says in _output(proc)


PS_CONNECT_NONE = f"""Add-Content -LiteralPath $env:FIXTURE_CALLS -Value ('shim|' + (@($args) -join ' '))
Write-Output '{NO_REGISTRATION}'
exit 3
"""


@pytest.mark.parametrize("checked", [True, False], ids=["checked", "unchecked"])
def test_ps_a_codex_mismatch_names_what_happened(tmp_path, checked):
    from tests.test_installer_client_only_ps import CRED_READY as PS_CRED, _codex_wiring
    ps = _PowerShell(tmp_path)
    if checked:
        shim_bin = tmp_path / "shim-bin"
        shim_bin.mkdir()
        (shim_bin / "pseudolife-mcp.ps1").write_text(PS_CONNECT_NONE, encoding="utf-8")
        ps.env.update({"FAKE_INSTALL_BIN": str(shim_bin), "FIXTURE_CALLS": str(ps.calls)})
    proc, adds = _codex_wiring(
        ps, cred=PS_CRED % "http://127.0.0.1:8765",
        existing="pseudolife-memory\n  command: pseudolife-mcp\n"
                 "  env: PSEUDOLIFE_MCP_DAEMON_URL=http://127.0.0.1:8765")
    assert proc.returncode == 0, _output(proc)
    assert adds == []
    out = _output(proc)
    assert f"not the daemon at {REMOTE}" in out
    if checked:
        assert any(call.startswith("shim|connect") for call in ps.logged())
        assert "and pseudolife-mcp connect did not re-point it" in out
        assert "Edit it in place" not in out
    else:
        assert "Edit it in place in the Codex config.toml" in out


@BASH
def test_a_gemini_registration_connect_re_pointed_gets_no_manual_token_fix(bash, tmp_path):
    from tests.test_installer_board_token import _Shell as _WireShell, _wire
    shell = _WireShell(bash, tmp_path)
    run = shell.run
    shell.run = lambda body: run("CONNECTED_CLIENTS=gemini\n" + body)
    proc, _ = _wire(shell, "gemini", token_file="/fixture/claude-code.token",
                    existing="pseudolife-memory: pseudolife-mcp (stdio) - Connected")
    assert proc.returncode == 0, proc.stderr
    assert "Edit it in place in ~/.gemini/settings.json" not in proc.stderr


def test_ps_a_gemini_registration_connect_re_pointed_gets_no_manual_token_fix(tmp_path):
    from tests.test_installer_board_token_ps import _wire
    ps = _PowerShell(tmp_path)
    run = ps.run
    ps.run = lambda body, **kwargs: run("$script:connectedClients = @('gemini')\n" + body, **kwargs)
    proc, _ = _wire(ps, "gemini", token_file=r"C:\fixture\claude-code.token",
                    existing="pseudolife-memory: pseudolife-mcp (stdio) - Connected")
    assert proc.returncode == 0, _output(proc)
    assert "Edit it in place in ~/.gemini/settings.json" not in _output(proc)
