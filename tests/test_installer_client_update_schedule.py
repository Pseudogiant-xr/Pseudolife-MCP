"""The installers offer the client update schedule on a client-only install.

`pseudolife-mcp update --schedule HH:MM --daemon-url <url>` on a client-only
machine registers a daily job (the Windows task "Pseudolife Client Update",
the systemd --user timer pseudolife-client-update.timer) that moves the shim
and plugin to the daemon's release, never past it. Before this, nothing but
the user running that command created it, a manual step that lived only in
the docs. The installers now offer it once, default no (a persistent job the
user did not choose by running the installer): answerable by
--client-update-schedule ask|yes|no|HH:MM / -ClientUpdateSchedule, skipped
with one line naming the command when nothing can ask, quiet when the job
already exists, a refusal from --schedule reported without failing the
install, and never offered on a daemon host. Blocks run extracted from
ops/install.sh and ops/install.ps1 with stub commands; nothing is registered.
"""
from __future__ import annotations

import re

import pytest

from pseudolife_memory import unattended_update
from tests.test_installer_client_only import BASH, ROOT, _mode, _q, _Shell, _stub
from tests.test_installer_client_only_ps import _PowerShell, _output
from tests.test_installer_client_only_ps import _q as _psq

URL = "http://100.64.0.2:8765"
OTHER = "http://100.64.0.9:8765"
DEFAULT = "04:30"
BLOCK = "client update schedule offer"
FLAG_BLOCK = "client update schedule flag"


def _text(name: str) -> str:
    return (ROOT / "ops" / name).read_text(encoding="utf-8")


def _block(name: str, script: str) -> str:
    text = _text(script)
    assert f"# >>> {name} >>>\n" in text, f"{script} has no '{name}' marker block"
    return text.split(f"# >>> {name} >>>\n", 1)[1].split(f"# <<< {name} <<<", 1)[0]


def _schedule_command(time: str = DEFAULT, url: str = URL) -> str:
    return f"update --schedule {time} --daemon-url {url}"


# -- where it is offered -------------------------------------------------------

@pytest.mark.parametrize("script,call", [
    ("install.sh", '\nif [ -n "$CLIENT_ONLY" ]; then offer_client_update_schedule; fi\n'),
    ("install.ps1", "\nif ($ClientOnly) { Invoke-ClientUpdateScheduleOffer }\n"),
])
def test_it_is_offered_once_on_a_client_only_install_before_the_update_line(script, call):
    text = _text(script)
    assert text.count(call) == 1, f"{script} does not offer the schedule on a client-only install"
    name = "offer_client_update_schedule" if script.endswith(".sh") else "Invoke-ClientUpdateScheduleOffer"
    calls = [line for line in text.splitlines()
             if name in line and not line.lstrip().startswith(("#", "function ", f"{name}()"))]
    assert len(calls) == 1, calls
    called = text.index(call)
    update_call = "\nprint_update_line\n" if script.endswith(".sh") else "\nWrite-UpdateLine "
    assert text.index("# <<< board line <<<") < called < text.index(update_call)


def test_the_job_names_are_the_schedulers_own():
    """The installers look for the job --schedule registers, by its names."""
    assert f'"{unattended_update.CLIENT_TASK_NAME}"' in _block(BLOCK, "install.ps1")
    assert unattended_update.CLIENT_UNIT_NAME in _block(BLOCK, "install.sh")


def test_both_installers_offer_the_same_default_time():
    assert f"CLIENT_SCHEDULE_DEFAULT={DEFAULT}\n" in _block(BLOCK, "install.sh")
    assert f'$ClientScheduleDefault = "{DEFAULT}"\n' in _block(BLOCK, "install.ps1")


def test_the_help_and_the_guides_name_the_offer():
    assert "--client-update-schedule ask|yes|no|HH:MM" in _block("usage", "install.sh")
    assert "-ClientUpdateSchedule ask|yes|no|HH:MM" in _text("install.ps1").split("param(", 1)[0]
    config = (ROOT / "docs" / "guide" / "configuration.md").read_text(encoding="utf-8")
    assert "does not offer the schedule yet" not in config
    assert "--client-update-schedule" in " ".join(config.split())
    remote = (ROOT / "docs" / "guide" / "remote-bank.md").read_text(encoding="utf-8")
    assert "--client-update-schedule" in remote and "-ClientUpdateSchedule" in remote


# -- install.sh ----------------------------------------------------------------

def _sh(shell: _Shell, *, mode: str = "ask", tty: bool = True, reply: str = "",
        platform: str = "Linux", manager: bool = True, enabled: bool = True,
        existing: str | None = None, existing_time: str = "05:10",
        schedule_exit: int = 0, shim: bool = True, url: str = URL):
    """``existing``: None (no job), a URL its unit follows, or "" (a unit
    with no --daemon-url, which follows the default URL)."""
    _stub(shell.bin / "uname", f"echo {platform}\n")
    _stub(shell.bin / "systemctl",
          "printf 'systemctl|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "case \"$*\" in *show-environment*) exit \"$FAKE_MANAGER_EXIT\" ;; "
          "*is-enabled*) exit \"$FAKE_ENABLED_EXIT\" ;; esac\nexit 0\n")
    _stub(shell.bin / "fakeshim",
          "printf 'shim|%s\\n' \"$*\" >>\"$CALL_LOG\"\n"
          "[ \"$FAKE_SCHEDULE_EXIT\" = 0 ] || echo 'pseudolife-mcp update: fixture refusal: "
          "the daemon did not answer /health. Nothing was installed' >&2\n"
          "exit \"$FAKE_SCHEDULE_EXIT\"\n")
    config = shell.home / "config"
    if existing is not None:
        units = config / "systemd" / "user"
        units.mkdir(parents=True)
        flag = f" --daemon-url {existing}" if existing else ""
        (units / "pseudolife-client-update.service").write_text(
            f"[Service]\nType=oneshot\nExecStart=/fixture/bin/pseudolife-mcp update --unattended{flag}\n",
            encoding="utf-8")
        (units / "pseudolife-client-update.timer").write_text(
            f"[Timer]\nOnCalendar=*-*-* {existing_time}:00\nPersistent=true\n", encoding="utf-8")
    shim_path = shell.path(shell.bin / "fakeshim")
    return shell.run(
        f"XDG_CONFIG_HOME='{shell.path(config)}'\n"
        f"bank_can_ask() {{ {'return 0' if tty else 'return 1'}; }}\n"
        "bank_read() { printf '%s' \"$1\"; BANK_REPLY=\"$FAKE_REPLY\"; }\n"
        "bank_trimmed() { printf '%s' \"$1\"; }\n"
        "ensure_shim() { "
        + (f"SHIM_OK=1; SHIM_PATH='{shim_path}'; }}\n" if shim else "SHIM_OK=; SHIM_PATH=; }\n")
        + f"CLIENT_ONLY=1\nCLIENT_DAEMON_URL='{url}'\nCLIENT_UPDATE_SCHEDULE='{mode}'\n"
        + _block("update line", "install.sh")
        + _block(BLOCK, "install.sh")
        + "\noffer_client_update_schedule\necho \"STATE=${CLIENT_SCHEDULE_STATE:-}\"\n"
        + "print_update_line\necho DONE\n",
        extra_env={"FAKE_REPLY": reply, "FAKE_SCHEDULE_EXIT": str(schedule_exit),
                   "FAKE_MANAGER_EXIT": "0" if manager else "1",
                   "FAKE_ENABLED_EXIT": "0" if enabled else "1"})


def _shim_calls(shell: _Shell) -> list[str]:
    return [line for line in shell.logged() if line.startswith("shim|")]


def _lines_with(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


@BASH
@pytest.mark.parametrize("reply", ["y", "Yes"])
def test_sh_yes_schedules_through_the_installed_shim(bash, tmp_path, reply):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply=reply)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [f"shim|{_schedule_command()}"]
    [prompt] = _lines_with(proc.stdout, "[y/N]")
    # one plain sentence on what and why, then the question
    assert "daemon's release" in prompt and DEFAULT in prompt and URL in prompt
    assert "STATE=scheduled" in proc.stdout and "DONE" in proc.stdout
    [update_line] = _lines_with(proc.stdout, "update --clients-only")
    assert "daily" in update_line


@BASH
@pytest.mark.parametrize("reply", ["", "n", "no"])
def test_sh_enter_or_no_declines_with_the_command_named(bash, tmp_path, reply):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply=reply)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    assert _lines_with(proc.stdout, _schedule_command())
    assert "STATE=declined" in proc.stdout
    [update_line] = _lines_with(proc.stdout, "update --clients-only")
    assert "daily" not in update_line


@BASH
@pytest.mark.parametrize("mode,time", [("yes", DEFAULT), ("06:15", "06:15"), ("7:05", "7:05")])
@pytest.mark.parametrize("tty", [True, False])
def test_sh_the_flag_answers_without_asking(bash, tmp_path, mode, time, tty):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, mode=mode, tty=tty)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [f"shim|{_schedule_command(time)}"]
    assert "[y/N]" not in proc.stdout and "STATE=scheduled" in proc.stdout


@BASH
@pytest.mark.parametrize("tty", [True, False])
def test_sh_the_flag_no_declines_in_one_line(bash, tmp_path, tty):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, mode="no", tty=tty)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    [line] = _lines_with(proc.stdout, "--client-update-schedule no")
    assert _schedule_command() in line


@BASH
def test_sh_without_a_terminal_one_line_names_the_command(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, tty=False)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    [line] = _lines_with(proc.stdout, _schedule_command())
    assert "--client-update-schedule yes" in line
    assert "STATE=skipped" in proc.stdout


@BASH
@pytest.mark.parametrize("mode,tty", [("ask", True), ("ask", False), ("yes", False), ("06:15", True)])
def test_sh_a_job_in_place_is_left_alone_in_one_line(bash, tmp_path, mode, tty):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, mode=mode, tty=tty, existing=URL)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    [line] = _lines_with(proc.stdout, "already scheduled")
    assert "nothing changed" in line and "05:10" in line
    assert "STATE=scheduled" in proc.stdout


@BASH
def test_sh_a_job_following_another_daemon_names_the_command_that_moves_it(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, existing=OTHER)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    [line] = _lines_with(proc.stdout, "already scheduled")
    assert OTHER in line and _schedule_command("05:10") in line and "nothing changed" in line.lower()


@BASH
def test_sh_a_job_with_no_url_follows_the_default_daemon(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, existing="", url="http://127.0.0.1:8765")
    assert proc.returncode == 0, proc.stderr
    [line] = _lines_with(proc.stdout, "already scheduled")
    assert "nothing changed" in line and "not " not in line


@BASH
def test_sh_unit_files_without_an_enabled_timer_are_offered_again(bash, tmp_path):
    """A half-installed unit (systemctl enable failed) is no job."""
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, reply="y", existing=URL, enabled=False)
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [f"shim|{_schedule_command()}"]
    assert "already scheduled" not in proc.stdout


@BASH
@pytest.mark.parametrize("mode", ["yes", "ask"])
def test_sh_a_refusal_warns_and_the_install_completes(bash, tmp_path, mode):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, mode=mode, reply="y", schedule_exit=2)
    assert proc.returncode == 0, proc.stderr
    assert "fixture refusal" in proc.stderr
    [warning] = _lines_with(proc.stderr, "WARNING")
    assert _schedule_command() in warning and "install itself is complete" in warning
    assert "STATE=failed" in proc.stdout and "DONE" in proc.stdout
    [update_line] = _lines_with(proc.stdout, "update --clients-only")
    assert "daily" not in update_line


@BASH
def test_sh_without_the_shim_it_says_so_and_asks_nothing(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, shim=False)
    assert proc.returncode == 0, proc.stderr
    assert "[y/N]" not in proc.stdout and "WARNING" in proc.stderr
    assert "update --schedule" in proc.stderr and "DONE" in proc.stdout


@BASH
def test_sh_macos_is_not_offered_a_scheduler(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, platform="Darwin")
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    assert "update --schedule" not in proc.stdout + proc.stderr


@BASH
def test_sh_macos_says_why_when_the_flag_asked(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, platform="Darwin", mode="yes")
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == []
    [line] = _lines_with(proc.stdout + proc.stderr, "--client-update-schedule")
    assert "update --clients-only" in line


@BASH
def test_sh_no_systemd_user_manager_skips_with_the_command(bash, tmp_path):
    shell = _Shell(bash, tmp_path)
    proc = _sh(shell, manager=False, mode="yes")
    assert proc.returncode == 0, proc.stderr
    assert _shim_calls(shell) == [] and "[y/N]" not in proc.stdout
    [line] = _lines_with(proc.stdout + proc.stderr, "no systemd user manager")
    assert _schedule_command() in line


@BASH
@pytest.mark.parametrize("value", ["yes", "06:15"])
def test_sh_the_flag_asking_for_a_job_on_a_daemon_host_is_refused(bash, tmp_path, value):
    proc = _mode(_Shell(bash, tmp_path), daemon_url="http://127.0.0.1:8765",
                 prelude=f"CLIENT_UPDATE_SCHEDULE='{value}'\n")
    assert proc.returncode == 2
    assert "--client-update-schedule" in proc.stderr and "client-only" in proc.stderr
    assert "update --schedule" in proc.stderr


@BASH
@pytest.mark.parametrize("value,daemon_url", [("no", "http://127.0.0.1:8765"),
                                              ("ask", "http://127.0.0.1:8765"),
                                              ("yes", URL), ("06:15", URL)])
def test_sh_near_misses_pass_the_mode_check(bash, tmp_path, value, daemon_url):
    proc = _mode(_Shell(bash, tmp_path), daemon_url=daemon_url,
                 prelude=f"CLIENT_UPDATE_SCHEDULE='{value}'\n")
    assert proc.returncode == 0, proc.stderr


@BASH
def test_sh_the_mode_check_holds_with_the_variable_unset(bash, tmp_path):
    proc = _mode(_Shell(bash, tmp_path), daemon_url="http://127.0.0.1:8765")
    assert proc.returncode == 0, proc.stderr


@BASH
@pytest.mark.parametrize("value,ok", [("ask", True), ("yes", True), ("no", True), ("04:30", True),
                                      ("4:30", True), ("23:59", True), ("24:00", False),
                                      ("4:3", False), ("soon", False), ("", False)])
def test_sh_the_flag_value_is_checked_up_front(bash, tmp_path, value, ok):
    shell = _Shell(bash, tmp_path)
    proc = shell.run(f"CLIENT_UPDATE_SCHEDULE='{_q(value)}'\n" + _block(FLAG_BLOCK, "install.sh")
                     + "\necho PASSED\n")
    if ok:
        assert proc.returncode == 0 and "PASSED" in proc.stdout, proc.stderr
    else:
        assert proc.returncode == 2 and "PASSED" not in proc.stdout
        assert "--client-update-schedule" in proc.stderr and "HH:MM" in proc.stderr


# -- install.ps1 ---------------------------------------------------------------

_XML = ("<?xml version=\"1.0\"?><Task><Triggers><CalendarTrigger>"
        "<StartBoundary>2026-10-31T{time}:00</StartBoundary></CalendarTrigger></Triggers>"
        "<Actions><Exec><Command>C:\\fixture\\bin\\pseudolife-mcp.exe</Command>"
        "<Arguments>update --unattended{flag}</Arguments></Exec></Actions></Task>")


def _ps(ps: _PowerShell, *, mode: str = "ask", tty: bool = True, reply: str = "",
        windows: bool = True, existing: str | None = None, existing_time: str = "05:10",
        schedule_exit: int = 0, shim: bool = True, url: str = URL):
    xml = "" if existing is None else _XML.format(
        time=existing_time, flag=f" --daemon-url {existing}" if existing else "")
    return ps.run(
        "function global:schtasks {\n"
        f"    Add-Content -LiteralPath '{_psq(ps.calls)}' -Value ('schtasks|' + (@($args) -join ' '))\n"
        + (f"    $global:LASTEXITCODE = 0\n    '{_psq(xml)}'\n" if existing is not None
           else "    [Console]::Error.WriteLine('ERROR: The system cannot find the file specified.')\n"
                "    $global:LASTEXITCODE = 1\n")
        + "}\n"
        "function global:fakeshim {\n"
        f"    Add-Content -LiteralPath '{_psq(ps.calls)}' -Value ('fakeshim|' + (@($args) -join ' '))\n"
        + (f"    Write-Host 'pseudolife-mcp update: fixture refusal: the daemon did not answer /health. "
           f"Nothing was installed'\n" if schedule_exit else "")
        + f"    $global:LASTEXITCODE = {schedule_exit}\n}}\n"
        + f"$interactive = ${'true' if tty else 'false'}\n"
        + f"function Read-BankAnswer([string]$Prompt) {{ Write-Output \"$Prompt\" | Out-Host; '{reply}' }}\n"
        + "function Get-BankReply($reply) { \"$reply\".Trim() }\n"
        + ("function Install-ShimOnce { $script:shimInstallPath = 'fakeshim'; $true }\n" if shim
           else "function Install-ShimOnce { $script:shimInstallPath = $null; $false }\n")
        + f"$ClientOnly = [switch]$true\n$clientDaemonUrl = '{url}'\n$ClientUpdateSchedule = '{mode}'\n"
        + _block("update line", "install.ps1")
        + _block(BLOCK, "install.ps1")
        + "\nInvoke-ClientUpdateScheduleOffer\nWrite-Output \"STATE=$($script:clientScheduleState)\"\n"
        + "Write-UpdateLine -ShimPath 'fakeshim' -ClientOnly -Scheduled:($script:clientScheduleState -eq 'scheduled')\n"
        + "Write-Output DONE\n",
        extra_env={"OS": "Windows_NT" if windows else "Linux-fixture"})


def _ps_calls(ps: _PowerShell, prefix: str = "fakeshim|") -> list[str]:
    return [line for line in ps.logged() if line.startswith(prefix)]


@pytest.mark.parametrize("reply", ["y", "Yes"])
def test_ps_yes_schedules_through_the_installed_shim(tmp_path, reply):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, reply=reply)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == [f"fakeshim|{_schedule_command()}"]
    [prompt] = _lines_with(out, "[y/N]")
    assert "daemon's release" in prompt and DEFAULT in prompt and URL in prompt
    assert "STATE=scheduled" in out and "DONE" in out
    [update_line] = _lines_with(out, "update --clients-only")
    assert "daily" in update_line


@pytest.mark.parametrize("reply", ["", "n"])
def test_ps_enter_or_no_declines_with_the_command_named(tmp_path, reply):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, reply=reply)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == []
    assert _lines_with(out, _schedule_command()) and "STATE=declined" in out
    [update_line] = _lines_with(out, "update --clients-only")
    assert "daily" not in update_line


@pytest.mark.parametrize("mode,time", [("yes", DEFAULT), ("06:15", "06:15")])
@pytest.mark.parametrize("tty", [True, False])
def test_ps_the_flag_answers_without_asking(tmp_path, mode, time, tty):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, mode=mode, tty=tty)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == [f"fakeshim|{_schedule_command(time)}"]
    assert "[y/N]" not in out and "STATE=scheduled" in out


def test_ps_the_flag_no_declines_in_one_line(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, mode="no")
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == [] and "[y/N]" not in out
    [line] = _lines_with(out, "-ClientUpdateSchedule no")
    assert _schedule_command() in line


def test_ps_without_a_terminal_one_line_names_the_command(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, tty=False)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == [] and "[y/N]" not in out
    [line] = _lines_with(out, _schedule_command())
    assert "-ClientUpdateSchedule yes" in line and "STATE=skipped" in out


@pytest.mark.parametrize("mode,tty", [("ask", True), ("ask", False), ("yes", False)])
def test_ps_a_job_in_place_is_left_alone_in_one_line(tmp_path, mode, tty):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, mode=mode, tty=tty, existing=URL)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == [] and "[y/N]" not in out
    assert _ps_calls(ps, "schtasks|") == [
        f"schtasks|/Query /TN {unattended_update.CLIENT_TASK_NAME} /XML"]
    [line] = _lines_with(out, "already scheduled")
    assert "nothing changed" in line and "05:10" in line and "STATE=scheduled" in out


def test_ps_a_job_following_another_daemon_names_the_command_that_moves_it(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, existing=OTHER)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert _ps_calls(ps) == [] and "[y/N]" not in out
    [line] = _lines_with(out, "already scheduled")
    assert OTHER in line and _schedule_command("05:10") in line and "nothing changed" in line.lower()


def test_ps_a_job_with_no_url_follows_the_default_daemon(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, existing="", url="http://127.0.0.1:8765")
    out = _output(proc)
    assert proc.returncode == 0, out
    [line] = _lines_with(out, "already scheduled")
    assert "nothing changed" in line and "not " not in line


@pytest.mark.parametrize("mode", ["yes", "ask"])
def test_ps_a_refusal_warns_and_the_install_completes(tmp_path, mode):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, mode=mode, reply="y", schedule_exit=2)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert "fixture refusal" in out
    [warning] = _lines_with(out, "WARNING")
    assert _schedule_command() in warning and "install itself is complete" in warning
    assert "STATE=failed" in out and "DONE" in out


def test_ps_without_the_shim_it_says_so_and_asks_nothing(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, shim=False)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert "[y/N]" not in out and "WARNING" in out and "update --schedule" in out and "DONE" in out


def test_ps_off_windows_it_is_not_offered(tmp_path):
    ps = _PowerShell(tmp_path)
    proc = _ps(ps, windows=False)
    out = _output(proc)
    assert proc.returncode == 0, out
    assert ps.logged() == [] and "[y/N]" not in out and "update --schedule" not in out


def _ps_mode(ps: _PowerShell, value: str | None, daemon_url: str):
    setting = "" if value is None else f"$ClientUpdateSchedule = '{value}'\n"
    return ps.run(
        setting + "$PairingCode = ''\n"
        f"$DaemonUrl = '{daemon_url}'\n$ReadToken = [switch]$false\n$ClientOnly = [switch]$false\n"
        "$TokenFile = ''\n$Extractor = ''\n$Model = ''\n$ShimPort = 0\n$NoToken = [switch]$false\n"
        "$Transport = 'shim'\n"
        + _block("client-only mode", "install.ps1") + "\nWrite-Output PASSED\n")


@pytest.mark.parametrize("value", ["yes", "06:15"])
def test_ps_the_flag_asking_for_a_job_on_a_daemon_host_is_refused(tmp_path, value):
    proc = _ps_mode(_PowerShell(tmp_path), value, "http://127.0.0.1:8765")
    out = _output(proc)
    assert proc.returncode == 2, out
    assert "-ClientUpdateSchedule" in out and "client-only" in out and "update --schedule" in out
    assert "PASSED" not in out


@pytest.mark.parametrize("value,daemon_url", [("no", "http://127.0.0.1:8765"),
                                              ("ask", "http://127.0.0.1:8765"),
                                              (None, "http://127.0.0.1:8765"),
                                              ("yes", URL)])
def test_ps_near_misses_pass_the_mode_check(tmp_path, value, daemon_url):
    proc = _ps_mode(_PowerShell(tmp_path), value, daemon_url)
    assert proc.returncode == 0, _output(proc)
    assert "PASSED" in proc.stdout


@pytest.mark.parametrize("value,ok", [("ask", True), ("yes", True), ("no", True), ("04:30", True),
                                      ("4:30", True), ("24:00", False), ("soon", False), ("", False)])
def test_ps_the_flag_value_is_checked_up_front(tmp_path, value, ok):
    ps = _PowerShell(tmp_path)
    proc = ps.run(f"$ClientUpdateSchedule = '{_psq(value)}'\n" + _block(FLAG_BLOCK, "install.ps1")
                  + "\nWrite-Output PASSED\n")
    out = _output(proc)
    if ok:
        assert proc.returncode == 0 and "PASSED" in out, out
    else:
        assert proc.returncode == 2 and "PASSED" not in out, out
        assert "-ClientUpdateSchedule" in out and "HH:MM" in out


def test_the_ps1_parameter_defaults_to_ask():
    params = _text("install.ps1").split("param(", 1)[1].split("\n)", 1)[0]
    assert re.search(r'\[string\]\$ClientUpdateSchedule = "ask"', params), params
