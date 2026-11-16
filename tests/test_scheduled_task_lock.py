"""Windows scheduled tasks the product registers need no elevation.

Users were told to open an elevated PowerShell to register the extractor
shim autostart. That was needed only because the Python-era scripts used a
logon trigger for ANY user. A logon trigger for this user, with this user
as an Interactive, Limited principal, registers without elevation: the
maintainer checked that on a real desktop in a non-elevated Windows
PowerShell on 16 November 2026, and a Windows dev VM probe agrees. What still
needs elevation is changing or removing a task registered from an elevated
shell, which Administrators own. Those tasks are now detected read-only
(``Get-TaskLock``) and never re-registered: one that runs the same action
follows its settings file, any other is refused before anything changes,
with one line that ends in the one elevated command that removes it.
Another account's task is never touched, elevated or not.

The PowerShell cases run the real functions with ``pwsh`` (and, on Windows,
with Windows PowerShell 5.1 too, which runs the embedded native script) and
replace only the thin readers of Windows state: the task's owner and
principal, this token's SID, whether this shell is elevated, the process
table. No test registers, reads, stops or removes a real task or process.
"""

import base64
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "ops" / "scheduled_task_lock.ps1"
NATIVE = ROOT / "rust" / "shim" / "src" / "extractor_shim" / "autostart.ps1"
# The synthetic account SID the tracked-tree identifier guard allows, and a
# neighbour built from it, so no literal SID of another shape is tracked.
ME = "S-1-5-21-100-200-300-1001"
OTHER = ME[:-1] + "2"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(text: str, name: str) -> str:
    start = text.index(f"# >>> {name} >>>")
    end = text.index(f"# <<< {name} <<<")
    return text[start:end]


def _pwsh() -> str:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        # In CI a missing pwsh would turn every case here into a silent pass.
        if os.environ.get("CI"):
            pytest.fail("pwsh is required in CI for the scheduled-task tests")
        pytest.skip("pwsh not available")
    return pwsh


def _shells() -> list[str]:
    """pwsh everywhere, and Windows PowerShell 5.1 where it exists: the
    embedded native script and the daily-task script run under 5.1."""
    shells = [_pwsh()]
    if os.name == "nt":
        legacy = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if legacy.is_file():
            shells.append(str(legacy))
    return shells


def _mocks(sddl: str, *, elevated: bool = False, runs_as: str = ME) -> str:
    """Replace the readers of Windows state; everything else is real."""
    quoted = sddl.replace("'", "''")
    return (
        f"function Get-TaskOwnerSddl([string]$TaskName) {{ $global:sddlReads += $TaskName; '{quoted}' }}\n"
        f"function Get-TaskPrincipalSid([string]$TaskName) {{ '{runs_as}' }}\n"
        f"function Test-TaskShellElevated {{ ${'true' if elevated else 'false'} }}\n"
        f"function Get-TaskShellUserSid {{ '{ME}' }}\n"
        f"function Get-AccountSid([string]$Account) {{ if (-not $Account) {{ '' }} elseif ($Account -eq 'this-user') {{ '{ME}' }} else {{ '{OTHER}' }} }}\n"
        # SID-to-name translation reads the account database; keep it out.
        "function Get-TaskOwnerName([string]$Owner) { if ($Owner -eq 'BA') { 'Administrators' } else { \"account $Owner\" } }\n"
        "$global:sddlReads = @()\n"
    )


def _run(script: str, shell: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([shell or _pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120, env=env)


def _lock_of(sddl: str, *, elevated: bool = False, runs_as: str = ME, **fmt: str) -> str:
    """The lock and its line, identical under every shell present."""
    args = " ".join(f"-{key} '{value}'" for key, value in fmt.items())
    script = (_read(LOCK) + _mocks(sddl, elevated=elevated, runs_as=runs_as) +
              "$lock = Get-TaskLock 'Pseudolife Claude Shim'\n"
              f"if ($lock) {{ 'LOCKED|' + $lock.Owner + '|' + $lock.Hidden + '|' + (Format-TaskLock $lock {args}) }} else {{ 'FREE' }}\n")
    results = set()
    for shell in _shells():
        proc = _run(script, shell)
        assert proc.returncode == 0, (shell, proc.stderr)
        results.add(proc.stdout.strip())
    assert len(results) == 1, results
    return results.pop()


def _ends_with_the_command(line: str, task: str) -> None:
    """A copy from the colon to the end of the line pastes only the command."""
    assert line.endswith(f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false"), line


def test_the_two_copies_of_the_task_lock_functions_are_identical() -> None:
    """The native client embeds autostart.ps1, so it carries its own copy;
    `update --schedule` reads the functions from that copy."""
    assert _block(_read(LOCK), "task lock") == _block(_read(NATIVE), "task lock")
    schedule = _read(ROOT / "rust/shim/src/cli/update/unattended/schedule.rs")
    assert 'include_str!("../../../extractor_shim/autostart.ps1")' in schedule


def test_every_logon_trigger_is_for_this_user_with_a_limited_principal() -> None:
    """A trigger for any user's logon is what made registration need
    elevation. Every logon trigger the product writes names the user, and
    every registration passes an explicit Interactive, Limited principal:
    the one shape checked unelevated on a real desktop."""
    scripts = [ROOT / "ops" / name for name in
               ("install-shim-autostart.ps1", "install-codex-shim-autostart.ps1", "install-autostart.ps1")]
    scripts.append(NATIVE)
    for path in scripts:
        text = _read(path)
        triggers = re.findall(r"New-ScheduledTaskTrigger -AtLogOn[^\n]*", text)
        assert triggers, path
        for trigger in triggers:
            assert re.search(r"-AtLogOn -User \$user\b", trigger), f"{path.name}: {trigger}"
        principals = re.findall(r"New-ScheduledTaskPrincipal [^\n]*", text)
        assert principals and all("-RunLevel Limited" in p for p in principals), path.name
        for call in re.findall(r"^\s*(?:\$\w+ = )?Register-ScheduledTask -TaskName [^\n]*", text, re.M):
            if "-Xml" in call:
                continue  # the native rollback restores the exported task as it was
            assert "-Principal $principal" in call, f"{path.name}: {call}"
        assert "-RunLevel Highest" not in text, path.name
    # The same guard over every .ps1 the product ships, so a new script
    # cannot bring the any-user trigger back.
    for path in list((ROOT / "ops").glob("*.ps1")) + [NATIVE]:
        for trigger in re.findall(r"New-ScheduledTaskTrigger -AtLogOn[^\n]*", _read(path)):
            assert "-User" in trigger, f"{path.name}: {trigger}"


def test_an_absent_task_or_one_this_user_owns_is_free() -> None:
    assert _lock_of("absent") == "FREE"
    assert _lock_of(f"O:{ME}") == "FREE"
    assert _lock_of(f"O:{ME}G:{ME}D:(A;;FA;;;{ME})") == "FREE"


def test_a_task_registered_from_an_elevated_shell_is_locked_with_the_one_elevated_command() -> None:
    out = _lock_of(f"O:BAG:SYD:(A;ID;FA;;;BA)(A;;FR;;;{ME})", Reason="is disabled, so it never starts at logon")
    state, owner, hidden, line = out.split("|", 3)
    assert (state, owner, hidden) == ("LOCKED", "Administrators", "False")
    assert "registered from an elevated PowerShell, so Administrators own it" in line
    # The true cause, and that the requested settings did not apply.
    assert "it is disabled, so it never starts at logon, so the new settings were not applied and nothing was changed" in line
    assert "keeps running as registered" not in line
    assert "Run as administrator from the Start menu" in line
    assert "inside Claude Desktop" in line and "claude-code#61635" in line
    assert "then rerun this command in a normal window: Stop-ScheduledTask -TaskName 'Pseudolife Claude Shim'; " in line
    _ends_with_the_command(line, "Pseudolife Claude Shim")
    assert "\n" not in line


def test_an_elevated_shell_may_change_an_administrators_task_but_never_another_accounts() -> None:
    assert _lock_of("O:BAG:SYD:(A;ID;FA;;;BA)", elevated=True) == "FREE"
    assert _lock_of("O:SYG:SYD:(A;ID;FA;;;SY)", elevated=True) == "FREE"
    # Task names are machine-wide: an elevated run must not take over
    # another user's per-user task of the same name...
    state, owner, hidden, line = _lock_of(f"O:{OTHER}D:(A;;FA;;;{OTHER})", elevated=True).split("|", 3)
    assert (state, owner) == ("LOCKED", f"account {OTHER}")
    assert "never changes another account's task" in line and "nothing was changed" in line
    # ...nor a task Administrators own that runs as another administrator
    # (registered elevated for themselves): owner and principal both count.
    state, owner, hidden, line = _lock_of("O:BAG:SYD:(A;ID;FA;;;BA)", elevated=True, runs_as=OTHER).split("|", 3)
    assert state == "LOCKED" and f"runs as account {OTHER}" in owner, owner
    assert "never changes another account's task" in line
    # Unelevated, the same task is another account's too, not this user's
    # to follow.
    assert "never changes another account's task" in _lock_of("O:BA", runs_as=OTHER)
    assert "never changes another account's task" in _lock_of("O:BA", runs_as="")


def test_a_task_owned_by_another_user_is_never_this_users() -> None:
    """The near miss: same name, a different owner SID."""
    state, owner, hidden, line = _lock_of(f"O:{OTHER}D:(A;;FA;;;{OTHER})").split("|", 3)
    assert (state, owner, hidden) == ("LOCKED", f"account {OTHER}", "False")
    assert f"belongs to another account (account {OTHER})" in line and "nothing was changed" in line
    assert "Ask that account to remove it: " in line and "Run as administrator" not in line
    _ends_with_the_command(line, "Pseudolife Claude Shim")
    # A SID that merely starts with this user's is someone else's.
    assert _lock_of(f"O:{ME}0D:(A;;FA;;;{ME}0)").startswith("LOCKED|")


def test_a_task_hidden_from_this_account_is_another_accounts() -> None:
    state, owner, hidden, line = _lock_of("hidden").split("|", 3)
    assert (state, hidden) == ("LOCKED", "True")
    assert "belongs to another account on this machine" in line and "nothing was changed" in line
    assert "pick another task name" not in line


def test_the_removal_and_follow_wordings_end_with_the_command() -> None:
    script = (_read(LOCK) + _mocks("O:BA") +
              "$lock = Get-TaskLock 'Pseudolife Codex Shim'\n"
              "Format-TaskLock $lock -Removal\n"
              "Format-TaskLock $lock -Follows 'ops\\.env'\n")
    for shell in _shells():
        proc = _run(script, shell)
        assert proc.returncode == 0, proc.stderr
        removal, follows = proc.stdout.strip().splitlines()
        assert "To remove it, run this once" in removal and "rerun this command" not in removal
        assert "it reads ops\\.env at every start, so it keeps following these settings" in follows
        assert "Only if you want a task of your own instead" in follows and "nothing was changed" not in follows
        for line in (removal, follows):
            _ends_with_the_command(line, "Pseudolife Codex Shim")


def test_a_lock_refusal_is_marked_for_its_callers() -> None:
    """The installers print only the line of a refusal they can recognise."""
    script = (_read(LOCK) + "try { throw (New-TaskLockError 'the line') } "
              "catch { $_.FullyQualifiedErrorId + '|' + $_.Exception.Message }\n")
    for shell in _shells():
        proc = _run(script, shell)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "PseudolifeTaskLocked|the line", proc.stdout


def _sandboxed_lock(tmp_path: Path, sddl_by_task: dict[str, str], runs_as: str = ME) -> Path:
    """A scheduled_task_lock.ps1 beside a copied script: the real functions,
    then readers that answer per task name."""
    cases = "".join(f"        '{name}' {{ '{sddl}' }}\n" for name, sddl in sddl_by_task.items())
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scheduled_task_lock.ps1").write_text(
        _read(LOCK) + _mocks("absent", runs_as=runs_as) +
        "function Get-TaskOwnerSddl([string]$TaskName) {\n"
        "    $global:sddlReads += $TaskName\n"
        "    switch ($TaskName) {\n" + cases + "        default { 'absent' }\n    }\n}\n",
        encoding="utf-8")
    return tmp_path


def _spawner(python: str, repo: str, kind: str) -> str:
    """The -EncodedCommand argument the autostart scripts register since 29
    September 2026, built the way they build it."""
    runner = f'"{repo}\\ops\\shim_autostart.py" run {kind}'.replace("'", "''")
    double = lambda text: text.replace("'", "''")  # noqa: E731
    inner = (f"$psi = New-Object System.Diagnostics.ProcessStartInfo\n"
             f"$psi.FileName = '{double(python)}'\n"
             f"$psi.Arguments = '{runner}'\n"
             "$psi.UseShellExecute = $false\n$psi.CreateNoWindow = $true\n"
             f"$psi.WorkingDirectory = '{double(repo)}'\n"
             "[System.Diagnostics.Process]::Start($psi) | Out-Null\n")
    return "-NoProfile -WindowStyle Hidden -EncodedCommand " + base64.b64encode(inner.encode("utf-16-le")).decode()


@pytest.mark.parametrize("script,kind,names", [
    ("install-shim-autostart.ps1", "claude", ["Pseudolife Claude Shim", "Pseudolife Sonnet Shim"]),
    ("install-codex-shim-autostart.ps1", "codex", ["Pseudolife Codex Shim"]),
])
def test_the_autostart_scripts_decide_before_changing_anything(tmp_path: Path, script: str, kind: str,
                                                               names: list[str]) -> None:
    """The lock check runs before the interpreter pick (which may build a
    venv), the log directory, ops/.env and any task change. A locked task
    that is enabled, starts at this user's logon and runs this checkout's
    runner follows ops/.env; any other locked task, another account's task
    even when it runs the same runner, and a locked legacy task (which would
    start a second shim beside the new one) are refused with their reason,
    marked so the installer prints only that line."""
    text = _read(ROOT / "ops" / script)
    check = _block(text, "task lock check")
    config = 'ops\\shim_autostart.py") config claude' if kind == "claude" else 'ops\\shim_autostart.py") @configArgs'
    for later in (config, "Unregister-ScheduledTask", "Register-ScheduledTask", "shim_python.py", "New-Item -ItemType Directory",
                  r'ops\shim_autostart.py") run ' + kind):
        assert later in text, later
        assert text.index(check) < text.index(later), later
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    repo = "C:\\checkout with 'quote"
    task = names[0]

    def run(sddl_by_task: dict[str, str], arguments: str, sandbox: str, enabled: bool = True,
            logon: bool = True, runs_as: str = ME, trigger_user: str = "this-user", trigger_enabled: bool = True) -> str:
        box = _sandboxed_lock(tmp_path / sandbox, sddl_by_task, runs_as)
        body = box / "check.ps1"
        quoted = arguments.replace("'", "''")
        trigger = "MSFT_TaskLogonTrigger" if logon else "MSFT_TaskTimeTrigger"
        body.write_text(
            # The scripts dot-source the lock file before the check block.
            ". (Join-Path $PSScriptRoot 'scheduled_task_lock.ps1')\n"
            f"$taskName = '{task}'\n$legacyTaskName = 'Pseudolife Sonnet Shim'\n"
            f"$repo = '{repo.replace(chr(39), chr(39) * 2)}'\n"
            "function Get-ScheduledTask { param($TaskName, $ErrorAction) "
            f"[pscustomobject]@{{ TaskName = $TaskName; Settings = [pscustomobject]@{{ Enabled = ${'true' if enabled else 'false'} }}; "
            f"Triggers = @([pscustomobject]@{{ CimClass = [pscustomobject]@{{ CimClassName = '{trigger}' }}; "
            f"Enabled = ${'true' if trigger_enabled else 'false'}; UserId = '{trigger_user}' }}); "
            f"Actions = @([pscustomobject]@{{ Execute = 'powershell.exe'; Arguments = '{quoted}' }}) }} }}\n"
            + check + "'FOLLOWS ' + $followsFile\n", encoding="utf-8")
        proc = _run(f"try {{ & '{body}' }} catch {{ 'REFUSED ' + $_.FullyQualifiedErrorId + ' ' + $_.Exception.Message }}")
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    runner = _spawner(str(python), repo, kind)
    locked = f"REFUSED PseudolifeTaskLocked the scheduled task '{task}' was registered from an elevated PowerShell"
    # Locked, and it runs this checkout's runner with an interpreter that exists.
    out = run({task: "O:BA"}, runner, "follows")
    assert "FOLLOWS True" in out and "REFUSED" not in out, out
    # Locked, and it is anything else: each refusal says why.
    others = {
        "older": ("/c \"\"C:\\py\\python.exe\" \"C:\\repo\\evals\\claude_shim.py\" --port 8082 --model m >> \"\"log\"\" 2>&1\"",
                  "runs an older command that carries its settings on its own command line"),
        "kind": (_spawner(str(python), repo, "codex" if kind == "claude" else "claude"), "runs the runner of another checkout"),
        "checkout": (_spawner(str(python), "C:\\another checkout", kind), "runs the runner of another checkout"),
        "gone": (_spawner(str(tmp_path / "gone.exe"), repo, kind), "runs an interpreter that no longer exists"),
    }
    for sandbox, (arguments, reason) in others.items():
        out = run({task: "O:BA"}, arguments, sandbox)
        assert locked in out and reason in out and "the new settings were not applied" in out, (sandbox, out)
        assert "FOLLOWS" not in out, (sandbox, out)
    out = run({task: "O:BA"}, runner, "disabled", enabled=False)
    assert locked in out and "is disabled, so it never starts at logon" in out, out
    out = run({task: "O:BA"}, runner, "nologon", logon=False)
    assert locked in out and "has no logon trigger" in out, out
    # A logon trigger that is disabled, or fires at another account's logon,
    # does not start the shim at this user's logon either.
    for sandbox, kwargs in (("triggeroff", {"trigger_enabled": False}), ("triggerother", {"trigger_user": "someone-else"})):
        out = run({task: "O:BA"}, runner, sandbox, **kwargs)
        assert locked in out and "has no enabled logon trigger for this user" in out, (sandbox, out)
    # The older any-user logon trigger (no user) does fire at this user's logon.
    out = run({task: "O:BA"}, runner, "anyuser", trigger_user="")
    assert "FOLLOWS True" in out, out
    # The near misses: another account's task running this very runner is
    # never followed, whether another user owns it or Administrators own it
    # and it runs as another account (an elevated run sees both).
    out = run({task: f"O:{OTHER}"}, runner, "otheruser")
    assert "REFUSED PseudolifeTaskLocked" in out and "never changes another account's task" in out, out
    out = run({task: "O:BA"}, runner, "otherprincipal", runs_as=OTHER)
    assert "REFUSED PseudolifeTaskLocked" in out and "never changes another account's task" in out, out
    # A locked legacy task is refused even when the selected task is free.
    for legacy in names[1:]:
        out = run({legacy: "O:BA"}, runner, "legacy")
        assert f"REFUSED PseudolifeTaskLocked the scheduled task '{legacy}' was registered from an elevated PowerShell" in out, out
        assert "To remove it, run this once" in out, out
    # Free (this user's own task): no lock, normal registration follows.
    out = run({task: f"O:{ME}"}, runner, "free")
    assert "FOLLOWS False" in out and "REFUSED" not in out, out


def test_the_follow_branches_stop_only_the_recorded_shim_and_verify_the_bind() -> None:
    """Following a locked runner task stops only the shim the runner recorded
    (proved by its PID file), never by a command-line pattern; refuses a
    port held by anything else before ops/.env is written; removes a legacy
    task this user owns (Claude); and does not take the runner's zero exit
    as a working shim: a <kind>_shim.py must own the port."""
    for script, kind in (("install-shim-autostart.ps1", "claude"), ("install-codex-shim-autostart.ps1", "codex")):
        text = _read(ROOT / "ops" / script)
        proof = text.index(f"$recorded = @(Get-RecordedShim $repo '{kind}')")
        assert "$full = Get-CanonicalPath $Repo" in _read(LOCK)
        config = text.index('shim_autostart.py") config claude' if kind == "claude" else "@configArgs 2>&1")
        assert proof < config, script
        assert "cannot prove is the shim it started" in text[proof:config], script
        branch = text[text.index("if ($followsFile) {", config):]
        branch = branch[:branch.index("\n}\n")]
        assert "restart " + kind not in branch and "Stop-Process" not in branch, script
        assert branch.index("Stop-RecordedShim $recorded") < branch.index(f'shim_autostart.py") run {kind}'), script
        start = branch.index(f'shim_autostart.py") run {kind}')
        assert branch.index("Get-PortListener -ShimPort $Port", start) < branch.index(f"Restarted the {kind.capitalize()} shim"), script
        assert f"{kind}_shim\\.py" in branch and f'throw "no {kind}_shim.py listens' in branch[start:], script
        if kind == "claude":
            # Removed before ops/.env is written, so a refused removal changes nothing.
            assert proof < text.index("Unregister-ScheduledTask -TaskName $legacyTaskName", proof) < config
        # A failed restart puts the settings back and starts the previous
        # shim again, after stopping whatever the failed start left.
        assert text.index("$envBefore = ") < config, script
        rollback = branch[branch.index("if ($failure) {"):]
        assert rollback.index(f"Stop-RecordedShim @(Get-RecordedShim $repo '{kind}')") < rollback.index("WriteAllBytes($envFile, $envBefore)")
        assert rollback.index("WriteAllBytes($envFile, $envBefore)") < rollback.index(f'shim_autostart.py") run {kind}')


def _recorded(tmp_path: Path, processes: list[dict], *, written_after: bool = True, port: int = 8082,
              recorded_pid: int = 4242) -> str:
    """Get-RecordedShim over a PID file this test writes and a mocked
    process table, under a throwaway profile."""
    home = tmp_path / "home"
    (home / ".pseudolife-mcp").mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "checkout"
    repo.mkdir(exist_ok=True)
    rows = []
    for row in processes:
        rows.append("[pscustomobject]@{ ProcessId = %d; ParentProcessId = %d; CreationDate = $(%s); "
                    "CommandLine = ('%s').Replace('{SCRIPT}', $global:fixtureScript).Replace('{OTHER}', $global:fixtureOther) }"
                    % (row["pid"], row.get("parent", 1), row["created"], row["command"].replace("'", "''")))
    script = (
        _read(LOCK) +
        f"$repo = '{str(repo)}'\n"
        "$full = [IO.Path]::GetFullPath($repo)\n"
        "$sha = [Security.Cryptography.SHA256]::Create()\n"
        "$digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($full)) | ForEach-Object { $_.ToString('x2') })\n"
        "$pidFile = Join-Path $env:USERPROFILE ('.pseudolife-mcp\\claude-shim-' + $digest.Substring(0, 8) + '.pid')\n"
        f"[IO.File]::WriteAllText($pidFile, '{recorded_pid} {port}' + [Environment]::NewLine)\n"
        f"$written = [datetime]'2026-01-01T00:00:{'10' if written_after else '00'}'\n"
        "(Get-Item -LiteralPath $pidFile).LastWriteTime = $written\n"
        "$global:fixtureScript = Join-Path $full 'evals\\claude_shim.py'\n"
        f"$global:fixtureOther = Join-Path '{tmp_path / 'other'}' 'evals\\claude_shim.py'\n"
        "function Get-CimInstance { param($ClassName, $Filter, $ErrorAction)\n"
        f"    $all = @({', '.join(rows) if rows else ''})\n"
        "    if ($Filter -match '^ProcessId = (\\d+)$') { $all | Where-Object ProcessId -eq ([int]$Matches[1]) }\n"
        "    elseif (-not $Filter) { $all }\n"
        "}\n"
        "(@(Get-RecordedShim $repo 'claude') | ForEach-Object { $_.ProcessId }) -join ','\n"
    )
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
    # The PID file name is the one ops/shim_autostart.py writes.
    expected = hashlib.sha256(str(repo.resolve()).encode("utf-8")).hexdigest()[:8]
    proc = _run(script, env=env)
    assert proc.returncode == 0, proc.stderr
    assert (home / ".pseudolife-mcp" / f"claude-shim-{expected}.pid").is_file()
    return proc.stdout.strip()


def test_the_recorded_shim_is_proved_by_its_pid_file_not_by_a_pattern(tmp_path: Path) -> None:
    created = "[datetime]'2026-01-01T00:00:05'"
    later = "[datetime]'2026-01-01T00:00:20'"
    own = '"python.exe" {SCRIPT} --host 127.0.0.1 --port 8082 --model m'
    # The recorded PID, started before the file was written, running this
    # checkout's shim on that port, and all its descendants started after
    # their parents: the base interpreter, and the provider CLI a request in
    # flight runs (a grandchild), but not an older process that merely
    # carries a reused parent PID, nor an unrelated one.
    assert _recorded(tmp_path, [{"pid": 4242, "created": created, "command": own},
                                {"pid": 4343, "parent": 4242, "created": later, "command": own},
                                {"pid": 4545, "parent": 4343, "created": later, "command": "claude.exe -p"},
                                {"pid": 4646, "parent": 4242, "created": "[datetime]'2025-12-31'", "command": "stale.exe"},
                                {"pid": 4747, "parent": 1, "created": later, "command": "unrelated.exe"}]) == "4242,4343,4545"
    # A PID reused after the file was written is not the recorded shim.
    assert _recorded(tmp_path, [{"pid": 4242, "created": later, "command": own}]) == ""
    # Another checkout's shim, or a process whose port is not the one the
    # file records.
    other = '"python.exe" {OTHER} --port 8082'
    assert _recorded(tmp_path, [{"pid": 4242, "created": created, "command": other}]) == ""
    assert _recorded(tmp_path, [{"pid": 4242, "created": created, "command": own}], port=8083) == ""
    # The port the file records, not the one asked for now: a rerun that
    # moves the shim to another port still proves, and stops, the old one.
    moved = '"python.exe" {SCRIPT} --host 127.0.0.1 --port 9090 --model m'
    assert _recorded(tmp_path, [{"pid": 4242, "created": created, "command": moved}], port=9090) == "4242"
    # No such process any more.
    assert _recorded(tmp_path, [], recorded_pid=4242) == ""


def _remove_shim_task(installer: str) -> str:
    text = _read(ROOT / "ops" / installer)
    start = text.index("function Remove-ShimTask($name, $family) {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


@pytest.mark.parametrize("installer", ["install.ps1", "install-native.ps1"])
def test_a_mode_switch_leaves_a_locked_task_alone_and_says_how_to_remove_it(installer: str) -> None:
    function = _remove_shim_task(installer)
    assert '. (Join-Path $PSScriptRoot "scheduled_task_lock.ps1")' in _read(ROOT / "ops" / installer)
    common = (
        "$global:unregistered = @()\n"
        "$global:present = @{ 'Pseudolife Codex Shim' = $true }\n"
        "function Get-ScheduledTask { param($TaskName, $ErrorAction) if ($global:present[$TaskName]) { [pscustomobject]@{ TaskName = $TaskName } } }\n"
        "function Unregister-ScheduledTask { param($TaskName, $Confirm, $ErrorAction) $global:unregistered += $TaskName; $global:present[$TaskName] = $false }\n"
        "function Step($text) { Write-Host \"STEP $text\" }\n"
        "$Extractor = 'claude-only'\n"
    )
    unreadable = "function Get-TaskOwnerSddl([string]$TaskName) { throw 'fixture COM failure' }\n"
    for case, sddl, extra, removed in (("admins", "O:BA", "", False), ("own", f"O:{ME}", "", True),
                                       ("other", f"O:{OTHER}", "", False), ("unread", "O:BA", unreadable, False)):
        script = (_read(LOCK) + _mocks(sddl) + extra + common + function +
                  "Remove-ShimTask 'Pseudolife Codex Shim' 'Codex' 3>&1 | ForEach-Object { \"OUT $_\" }\n"
                  "'UNREGISTERED ' + ($global:unregistered -join ',')\n")
        proc = _run(script)
        assert proc.returncode == 0, proc.stderr
        if removed:
            assert "UNREGISTERED Pseudolife Codex Shim" in proc.stdout, proc.stdout
            assert "Removed autostart task 'Pseudolife Codex Shim'" in proc.stdout
            continue
        # Locked, another account's, or an owner that could not be read: a
        # failed read is a refusal, never a fall-through to the delete.
        assert re.search(r"^UNREGISTERED\s*$", proc.stdout, re.M), (case, proc.stdout)
        assert "nothing was changed" in proc.stdout and "its Codex shim keeps starting at logon" in proc.stdout, (case, proc.stdout)
        fix = {"admins": "To remove it, run this once", "other": "Ask that account to remove it",
               "unread": "could not read the owner of autostart task 'Pseudolife Codex Shim' (fixture COM failure)"}[case]
        assert fix in proc.stdout, (case, proc.stdout)
        if case != "unread":
            assert proc.stdout.rstrip().splitlines()[-2].endswith("-Confirm:$false"), proc.stdout


def test_the_installers_print_only_the_line_of_a_lock_refusal() -> None:
    """A refusal leaves the locked task's shim serving; the installers must
    not call it a failure or suggest starting a second shim on its port."""
    install = _read(ROOT / "ops" / "install.ps1")
    block = install.split("# >>> shim autostart >>>", 1)[1].split("# <<< shim autostart <<<", 1)[0]
    for kind in ("Claude", "Codex"):
        locked = block.index(f"""if ($_.FullyQualifiedErrorId -like 'PseudolifeTaskLocked*') {{
            Write-Warning "{kind} shim autostart: $($_.Exception.Message)"
        }} else {{""")
        rest = block[locked:]
        assert rest.index("} else {") < rest.index("Or start it manually")
    native = _read(ROOT / "ops" / "install-native.ps1")
    block = native.split("# >>> shim autostart >>>", 1)[1].split("# <<< shim autostart <<<", 1)[0]
    assert block.index("if ($autostartCode -eq 3) {") < block.index("} elseif ($autostartCode -ne 0) {")
    assert "Write-" not in block[block.index("if ($autostartCode -eq 3) {"):block.index("} elseif ($autostartCode -ne 0) {")]
    for script in ("install-shim-autostart.ps1", "install-codex-shim-autostart.ps1"):
        assert "if ($LASTEXITCODE -eq 3) { throw (New-TaskLockError" in _read(ROOT / "ops" / script), script
    rs = _read(ROOT / "rust/shim/src/extractor_shim/autostart.rs")
    assert '"LockExitCode":TASK_LOCKED_EXIT' in rs and "pub(super) const TASK_LOCKED_EXIT: u8 = 3;" in rs


def test_the_native_script_checks_locks_before_it_changes_anything() -> None:
    """Order in the embedded script: the legacy and sibling checks, and the
    refusal of a locked selected task, all come before the settings file is
    written or anything is stopped; every name's owner is read whether or
    not Get-ScheduledTask lists it; and the follow decision ignores the
    runtime line each client update changes."""
    text = _read(NATIVE)
    legacy_check = text.index("$lock = Get-TaskLock $other")
    selected = text.index("$selectedLock = Get-TaskLock $name\n")
    first_mutation = text.index("& $native @configArgs")
    assert legacy_check < selected < first_mutation
    refusal = text.index("Stop-TaskLocked (Format-TaskLock $selectedLock -Reason $why)")
    assert selected < refusal < first_mutation
    assert refusal < text.index("$mutated = $true") and refusal < text.index("Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue")
    assert "$selectedLock.OtherUser" in text[selected:refusal]
    assert "-replace '^\\$runtimeExe = " in text
    gate = text[text.index("if ($selectedLock) {", selected):refusal]
    assert "-not $sameLauncher" in gate and "-not $registered.Settings.Enabled" in gate and "Get-TaskLogonMismatch $registered" in gate
    # A failed restart of a locked task stops the instance it started before
    # the original restarts on the restored settings.
    catch = text.index("} catch {\n    $failure = $_")
    stop_started = text.index("if ($followsFile -and $mutated) {", catch)
    assert stop_started < text.index("try { Restart-UnchangedTask $name $originalOwnership $wasActive }", catch)
    remove = text.index("if ($c.Remove) {")
    removal_check = text.index("Stop-TaskLocked (Format-TaskLock $lock -Removal)", remove)
    assert removal_check < text.index("$null = Remove-OwnedTask $task", remove)
    # A rerun that changes nothing says so in one line.
    assert "nothing changed." in text.split("if (-not $unchanged -and -not $followsFile) {")[-1]


def test_no_product_text_asks_for_elevation_to_register_these_tasks() -> None:
    """Elevation is named only where it is still needed: removing a task an
    elevated shell registered, and an S4U principal."""
    banned = re.compile(r"needs an ELEVATED|admin pwsh|usually elevation|from an elevated PowerShell fresh|"
                        r"run --unschedule again from an elevated|or an elevated PowerShell on Windows", re.I)
    for rel in ("ops/install.ps1", "ops/install-native.ps1", "ops/install-shim-autostart.ps1",
                "ops/install-codex-shim-autostart.ps1", "ops/install-autostart.ps1", "ops/shim_autostart.py",
                "rust/shim/src/extractor_shim/autostart.rs", "rust/shim/src/extractor_shim/autostart.ps1",
                "rust/shim/src/cli/update/unattended/schedule.rs",
                "docs/guide/dreaming.md", "docs/guide/configuration.md"):
        hits = banned.findall(_read(ROOT / rel))
        assert not hits, f"{rel}: {hits}"
    assert "(elevated)" not in _read(ROOT / "ops/shim_autostart.py")
    # A client file an elevated shell wrote is taken back from a normal
    # window (takeown exited 0 under a filtered token on the dev VM), not by
    # "running this as its owner", which for Administrators means elevating.
    config_file = _read(ROOT / "rust/shim/src/runtime/config_file.rs")
    assert "run this as its owner, or take ownership" not in config_file
    assert "from a normal PowerShell, with no elevation" in config_file


def test_the_rerun_hints_say_what_a_locked_task_needs() -> None:
    """`show` and `restart` tell the user to rerun the installer for a
    pre-runner or disabled task, which Administrators own on Windows: the
    hint names the elevated line the installer prints."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("shim_autostart_for_hints", ROOT / "ops" / "shim_autostart.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "run the one elevated line it prints" in module._ELEVATED_FIRST
    source = _read(ROOT / "ops" / "shim_autostart.py")
    note = source[source.index("def registration_note"):source.index("# ── run / restart")]
    assert note.count("_ELEVATED_FIRST") == 2
    guide = _read(ROOT / "docs" / "guide" / "dreaming.md")
    assert guide.count("run the one elevated line it prints") >= 2


def test_the_recorded_shim_is_stopped_only_at_its_exact_start_time() -> None:
    """A PID reused within a second is not the recorded shim: the handle's
    start time must equal the recorded one at CIM's microsecond precision,
    and the process is re-read by PID first."""
    def stop(handle_offset_ms: float, cim_offset_s: int = 0) -> str:
        script = (
            _read(LOCK) +
            "$created = [datetime]'2026-01-01T00:00:05'\n"
            "$old = [pscustomobject]@{ ProcessId = 4242; CreationDate = $created; CommandLine = 'python.exe shim' }\n"
            f"function Get-CimInstance {{ param($ClassName, $Filter, $ErrorAction) "
            f"[pscustomobject]@{{ ProcessId = 4242; CreationDate = $created.AddSeconds({cim_offset_s}); CommandLine = 'python.exe shim' }} }}\n"
            "$global:killed = $false\n"
            "function Get-Process { param($Id, $ErrorAction)\n"
            "    $h = [pscustomobject]@{}\n"
            "    $h | Add-Member ScriptMethod get_Handle { [IntPtr]1 }\n"
            f"    $h | Add-Member ScriptMethod get_StartTime {{ $created.AddMilliseconds({handle_offset_ms}) }}\n"
            "    $h | Add-Member ScriptMethod get_HasExited { $false }\n"
            "    $h | Add-Member ScriptMethod Kill { $global:killed = $true }\n"
            "    $h | Add-Member ScriptMethod WaitForExit { param($t) $true }\n"
            "    $h | Add-Member ScriptMethod Dispose { }\n"
            "    $h\n"
            "}\n"
            "Stop-RecordedShim @($old)\n"
            "'KILLED ' + $global:killed\n")
        proc = _run(script)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()
    assert stop(0) == "KILLED True"
    assert stop(0.0004) == "KILLED True"       # below CIM's microsecond precision
    assert stop(500) == "KILLED False"         # a PID reused 500 ms later
    assert stop(0, cim_offset_s=1) == "KILLED False"   # the PID now names another process


@pytest.mark.skipif(os.name != "nt", reason="the on-disk spelling is a Windows file-system question")
def test_the_checkout_is_named_as_the_runner_names_it(tmp_path: Path) -> None:
    """ops/shim_autostart.py names its PID file after Path.resolve(); a
    rerun through another spelling of the same checkout (other casing)
    must find the same file, or it refuses its own running shim."""
    checkout = tmp_path / "CheckOut Dir"
    checkout.mkdir()
    other = str(checkout).replace("CheckOut Dir", "checkout dir")
    for shell in _shells():
        proc = _run(_read(LOCK) + f"Get-CanonicalPath '{other}'\n", shell)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(Path(other).resolve()), (shell, proc.stdout)
