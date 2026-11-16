"""Windows scheduled tasks the product registers need no elevation.

Users were told to open an elevated PowerShell to register the extractor
shim autostart. That was needed only because the Python-era scripts used a
logon trigger for ANY user. A logon trigger for this user, with this user
as an Interactive, Limited principal, registers without elevation: the
maintainer checked that on a real desktop in a non-elevated Windows
PowerShell on 16 November 2026, and a Windows dev VM probe agrees. What still
needs elevation is changing or removing a task registered from an elevated
shell, which Administrators own. Those tasks are now detected read-only
(``Get-TaskLock``), left exactly as they are, and reported in one line with
the one elevated command that removes them.

The PowerShell cases run the real functions with ``pwsh`` and replace only
the thin readers of Windows state (the task's owner, this token's SID,
whether this shell is elevated), so no test registers, reads or removes a
real task.
"""

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
        pytest.skip("pwsh not available")
    return pwsh


def _mocks(sddl: str, *, elevated: bool = False) -> str:
    """Replace the three readers of Windows state; everything else is real."""
    quoted = sddl.replace("'", "''")
    return (
        f"function Get-TaskOwnerSddl([string]$TaskName) {{ $global:sddlReads += $TaskName; '{quoted}' }}\n"
        f"function Test-TaskShellElevated {{ ${'true' if elevated else 'false'} }}\n"
        f"function Get-TaskShellUserSid {{ '{ME}' }}\n"
        # SID-to-name translation reads the account database; keep it out.
        "function Get-TaskOwnerName([string]$Owner) { if ($Owner -eq 'BA') { 'Administrators' } else { \"account $Owner\" } }\n"
        "$global:sddlReads = @()\n"
    )


def _run(script: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([_pwsh(), "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120, cwd=cwd)


def _lock_of(sddl: str, *, elevated: bool = False) -> str:
    script = (_read(LOCK) + _mocks(sddl, elevated=elevated) +
              "$lock = Get-TaskLock 'Pseudolife Claude Shim'\n"
              "if ($lock) { 'LOCKED|' + $lock.Owner + '|' + $lock.Hidden + '|' + (Format-TaskLock $lock) } else { 'FREE' }\n")
    proc = _run(script)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


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
    out = _lock_of(f"O:BAG:SYD:(A;ID;FA;;;BA)(A;;FR;;;{ME})")
    state, owner, hidden, line = out.split("|", 3)
    assert (state, owner, hidden) == ("LOCKED", "Administrators", "False")
    assert "registered from an elevated PowerShell, so Administrators own it" in line
    assert "nothing was changed" in line
    assert ("Stop-ScheduledTask -TaskName 'Pseudolife Claude Shim'; "
            "Unregister-ScheduledTask -TaskName 'Pseudolife Claude Shim' -Confirm:$false") in line
    assert "Run as administrator from the Start menu" in line
    assert "inside Claude Desktop" in line and "claude-code#61635" in line
    assert "\n" not in line


def test_an_elevated_shell_may_change_an_administrators_task_but_never_another_users() -> None:
    assert _lock_of("O:BAG:SYD:(A;ID;FA;;;BA)", elevated=True) == "FREE"
    assert _lock_of("O:SYG:SYD:(A;ID;FA;;;SY)", elevated=True) == "FREE"
    # Task names are machine-wide: an elevated run must not take over
    # another user's per-user task of the same name.
    state, owner, hidden, line = _lock_of(f"O:{OTHER}D:(A;;FA;;;{OTHER})", elevated=True).split("|", 3)
    assert (state, owner) == ("LOCKED", f"account {OTHER}")
    assert "never changes another account's task" in line and "nothing was changed" in line


def test_a_task_owned_by_another_user_is_never_this_users() -> None:
    """The near miss: same name, a different owner SID."""
    state, owner, hidden, line = _lock_of(f"O:{OTHER}D:(A;;FA;;;{OTHER})").split("|", 3)
    assert (state, owner, hidden) == ("LOCKED", f"account {OTHER}", "False")
    assert f"is owned by another account (account {OTHER})" in line and "nothing was changed" in line
    assert "Ask that account to remove it" in line and "Run as administrator" not in line
    # A SID that merely starts with this user's is someone else's.
    assert _lock_of(f"O:{ME}0D:(A;;FA;;;{ME}0)").startswith("LOCKED|")


def test_a_task_hidden_from_this_account_is_another_accounts() -> None:
    state, owner, hidden, line = _lock_of("hidden").split("|", 3)
    assert (state, hidden) == ("LOCKED", "True")
    assert "belongs to another account on this machine" in line and "nothing was changed" in line


def test_the_removal_wording_says_remove_not_replace() -> None:
    script = (_read(LOCK) + _mocks("O:BA") +
              "Format-TaskLock (Get-TaskLock 'Pseudolife Codex Shim') -Removal\n")
    proc = _run(script)
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip()
    assert "To remove it, run once" in line and "rerun this command" not in line


def _sandboxed_lock(tmp_path: Path, sddl_by_task: dict[str, str]) -> Path:
    """A scheduled_task_lock.ps1 beside a copied script: the real functions,
    then readers that answer per task name."""
    cases = "".join(f"        '{name}' {{ '{sddl}' }}\n" for name, sddl in sddl_by_task.items())
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "scheduled_task_lock.ps1").write_text(
        _read(LOCK) + _mocks("absent") +
        "function Get-TaskOwnerSddl([string]$TaskName) {\n"
        "    $global:sddlReads += $TaskName\n"
        "    switch ($TaskName) {\n" + cases + "        default { 'absent' }\n    }\n}\n",
        encoding="utf-8")
    return tmp_path


def _spawner(python: str, repo: str, kind: str) -> str:
    """The -EncodedCommand argument the autostart scripts register since 29
    September 2026, built the way they build it."""
    import base64
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
    """The lock check runs before ops/.env is written and before any task is
    unregistered. A locked task that runs this checkout's runner follows
    ops/.env and is left registered; a locked task of any other shape, and a
    locked legacy task (which would otherwise start a second shim beside the
    new one), is refused with nothing changed."""
    text = _read(ROOT / "ops" / script)
    check = _block(text, "task lock check")
    for later in ('ops\\shim_autostart.py") config', "ops\\shim_autostart.py\") @configArgs",
                  "Unregister-ScheduledTask", "Register-ScheduledTask", r'ops\shim_autostart.py") restart ' + kind):
        if later in text:
            assert text.index(check) < text.index(later), later
    follow = text.index("if ($followsFile) {")
    assert text.index("config " + kind if kind == "claude" else "@configArgs") < follow
    assert follow < text.index("Unregister-ScheduledTask") and follow < text.index("\nRegister-ScheduledTask")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    repo = "C:\\checkout with 'quote"
    task = names[0]

    def run(sddl_by_task: dict[str, str], arguments: str, sandbox: str, enabled: bool = True) -> str:
        box = _sandboxed_lock(tmp_path / sandbox, sddl_by_task)
        body = box / "check.ps1"
        quoted = arguments.replace("'", "''")
        body.write_text(
            f"$taskName = '{task}'\n$legacyTaskName = 'Pseudolife Sonnet Shim'\n"
            f"$repo = '{repo.replace(chr(39), chr(39) * 2)}'\n"
            "function Get-ScheduledTask { param($TaskName, $ErrorAction) "
            f"[pscustomobject]@{{ TaskName = $TaskName; Settings = [pscustomobject]@{{ Enabled = ${'true' if enabled else 'false'} }}; "
            f"Actions = @([pscustomobject]@{{ Execute = 'powershell.exe'; Arguments = '{quoted}' }}) }} }}\n"
            + check + "'FOLLOWS ' + $followsFile\n", encoding="utf-8")
        proc = _run(f"try {{ & '{body}' }} catch {{ 'REFUSED ' + $_.Exception.Message }}")
        assert proc.returncode == 0, proc.stderr
        return proc.stdout

    runner = _spawner(str(python), repo, kind)
    # Locked, and it runs this checkout's runner with an interpreter that exists.
    out = run({task: "O:BA"}, runner, "follows")
    assert "FOLLOWS True" in out and "REFUSED" not in out, out
    # Locked, and its action is anything else: an older form, another kind,
    # another checkout, or an interpreter that is gone.
    others = [
        "/c \"\"C:\\py\\python.exe\" \"C:\\repo\\evals\\claude_shim.py\" --port 8082 --model m >> \"\"log\"\" 2>&1\"",
        _spawner(str(python), repo, "codex" if kind == "claude" else "claude"),
        _spawner(str(python), "C:\\another checkout", kind),
        _spawner(str(tmp_path / "gone.exe"), repo, kind),
    ]
    for at, arguments in enumerate(others):
        out = run({task: "O:BA"}, arguments, f"other{at}")
        assert f"REFUSED the scheduled task '{task}' was registered from an elevated PowerShell" in out, (at, out)
        assert "FOLLOWS" not in out, (at, out)
    # Locked and disabled: it never starts at logon and this shell cannot
    # enable it, so following it would report an autostart that is not.
    out = run({task: "O:BA"}, runner, "disabled", enabled=False)
    assert f"REFUSED the scheduled task '{task}' was registered from an elevated PowerShell" in out, out
    # A locked legacy task is refused even when the selected task is free.
    for locked in names[1:]:
        out = run({locked: "O:BA"}, runner, "legacy")
        assert f"REFUSED the scheduled task '{locked}' was registered from an elevated PowerShell" in out, out
        assert "To remove it, run once" in out, out
    # Free (this user's own task): no lock, normal registration follows.
    out = run({task: f"O:{ME}"}, runner, "free")
    assert "FOLLOWS False" in out and "REFUSED" not in out, out


def test_a_task_that_follows_its_settings_file_says_so_and_offers_the_command_as_optional() -> None:
    script = (_read(LOCK) + _mocks("O:BA") +
              "Format-TaskLock (Get-TaskLock 'Pseudolife Claude Shim') -Follows 'ops\\.env'\n")
    proc = _run(script)
    assert proc.returncode == 0, proc.stderr
    line = proc.stdout.strip()
    assert "Administrators own it" in line and "it reads ops\\.env at every start" in line
    assert "Only if you want a task of your own instead" in line
    assert "nothing was changed" not in line


def _remove_shim_task(installer: str) -> str:
    text = _read(ROOT / "ops" / installer)
    start = text.index("function Remove-ShimTask($name, $family) {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


@pytest.mark.parametrize("installer", ["install.ps1", "install-native.ps1"])
def test_a_mode_switch_leaves_a_locked_task_alone_and_says_how_to_remove_it(tmp_path: Path, installer: str) -> None:
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
    for sddl, removed in (("O:BA", False), (f"O:{ME}", True), (f"O:{OTHER}", None)):
        script = (_read(LOCK) + _mocks(sddl) + common + function +
                  "Remove-ShimTask 'Pseudolife Codex Shim' 'Codex' 3>&1 | ForEach-Object { \"OUT $_\" }\n"
                  "'UNREGISTERED ' + ($global:unregistered -join ',')\n")
        proc = _run(script)
        assert proc.returncode == 0, proc.stderr
        if removed:
            assert "UNREGISTERED Pseudolife Codex Shim" in proc.stdout, proc.stdout
            assert "Removed autostart task 'Pseudolife Codex Shim'" in proc.stdout
        else:
            assert re.search(r"^UNREGISTERED\s*$", proc.stdout, re.M), proc.stdout
            assert "nothing was changed" in proc.stdout, proc.stdout
            fix = "To remove it, run once" if removed is False else "Ask that account to remove it"
            assert fix in proc.stdout, proc.stdout
            assert "its Codex shim keeps starting at logon" in proc.stdout


def test_the_native_script_checks_locks_before_it_changes_anything() -> None:
    """Order in the embedded script: the legacy and sibling checks, and the
    refusal of a locked selected task that runs another action, all come
    before the settings file is written or anything is stopped."""
    text = _read(NATIVE)
    legacy_check = text.index("$lock = Get-TaskLock $other")
    selected = text.index("$selectedLock = if ($registered) { Get-TaskLock $name }")
    first_mutation = text.index("& $native @configArgs")
    assert legacy_check < selected < first_mutation
    refusal = text.index("throw (Format-TaskLock $selectedLock)")
    assert selected < refusal < first_mutation
    assert refusal < text.index("$mutated = $true") and refusal < text.index("Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue")
    # A failed restart of a locked task stops the instance it started before
    # the original restarts on the restored settings.
    catch = text.index("} catch {\n    $failure = $_")
    stop_started = text.index("if ($followsFile -and $mutated) {", catch)
    assert stop_started < text.index("try { Restart-UnchangedTask $name $originalOwnership $wasActive }", catch)
    remove = text.index("if ($c.Remove) {")
    removal_check = text.index("throw (Format-TaskLock $lock -Removal)", remove)
    assert removal_check < text.index("$null = Remove-OwnedTask $task", remove)
    # A rerun that changes nothing says so in one line.
    assert "nothing changed." in text.split("if (-not $unchanged) {")[-1]


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


def test_the_claude_follow_branch_removes_the_legacy_task_and_verifies_the_bind() -> None:
    """Following a locked runner task still removes a legacy task this user
    owns (or two shims start at logon), and `restart` exiting 0 is not taken
    as a working shim: a claude_shim.py must own the port."""
    text = _read(ROOT / "ops" / "install-shim-autostart.ps1")
    branch = text[text.index("if ($followsFile) {"):text.index("$inner = @\"")]
    assert branch.index("Unregister-ScheduledTask -TaskName $legacyTaskName") < branch.index("restart claude")
    restart = branch.index("restart claude")
    assert branch.index("Get-PortListener -ShimPort $Port", restart) < branch.index("Restarted the Claude shim")
    assert "claude_shim\\.py" in branch and "throw (" in branch[restart:]
