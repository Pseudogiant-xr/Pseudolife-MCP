# >>> task lock >>>
# Whether this shell may change or remove a scheduled task, read without
# changing anything. Valid for Windows PowerShell 5.1 and PowerShell 7.
#
# An unelevated user can register, replace, disable and remove a task they
# own. A task registered from an elevated shell is owned by Administrators
# and grants its user only read access, so an unelevated shell can neither
# replace nor remove it (Windows dev VM probe, 16 November 2026, with a
# UAC-style filtered token: the owner in the task's security descriptor is
# the signal). The copies of this block in ops/scheduled_task_lock.ps1 and
# rust/shim/src/extractor_shim/autostart.ps1 are kept identical by
# tests/test_scheduled_task_lock.py.
function Test-TaskShellElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-TaskShellUserSid {
    [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
}
function Get-TaskOwnerSddl([string]$TaskName) {
    # The owner part of the task's security descriptor (SDDL "O:..."), or
    # 'absent' when no task of that name exists in the root folder, or
    # 'hidden' when Task Scheduler refuses to show it to this account.
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    try { $task = $service.GetFolder('\').GetTask($TaskName) }
    catch {
        # PowerShell wraps the COM error; its HRESULT is the COMException's.
        $code = $_.Exception.HResult
        for ($inner = $_.Exception; $inner; $inner = $inner.InnerException) {
            if ($inner -is [Runtime.InteropServices.COMException]) { $code = $inner.HResult; break }
        }
        # 0x80070002 file not found, 0x80070003 path not found.
        if ($code -eq -2147024894 -or $code -eq -2147024893) { return 'absent' }
        # 0x80070005 access denied: a task this account may not even read.
        if ($code -eq -2147024891) { return 'hidden' }
        throw
    }
    # 1 = OWNER_SECURITY_INFORMATION: reading it needs only read access.
    $task.GetSecurityDescriptor(1)
}
function Get-TaskOwnerName([string]$Owner) {
    if ($Owner -eq 'BA') { return 'Administrators' }
    if ($Owner -eq 'SY') { return 'SYSTEM' }
    try {
        (New-Object Security.Principal.SecurityIdentifier $Owner).Translate([Security.Principal.NTAccount]).Value
    } catch { $Owner }
}
function Get-TaskLock([string]$TaskName) {
    # $null when this shell may change the task: there is none, this user
    # owns it, or Administrators or SYSTEM own it and this shell is
    # elevated. Otherwise an object naming who owns it ('another account'
    # when the task is hidden from this one). Another user's task is never
    # this shell's to change, elevated or not.
    $sddl = Get-TaskOwnerSddl $TaskName
    if ($sddl -eq 'absent') { return $null }
    if ($sddl -eq 'hidden') {
        return [pscustomobject]@{ TaskName = $TaskName; Owner = 'another account'; Hidden = $true; OtherUser = $true }
    }
    $owner = if ("$sddl" -match '^O:(.+?)(?=[GDS]:|$)') { $Matches[1] } else { '' }
    if ($owner -and $owner -eq (Get-TaskShellUserSid)) { return $null }
    $machine = $owner -in @('BA', 'SY', 'S-1-5-32-544', 'S-1-5-18')
    if ($machine -and (Test-TaskShellElevated)) { return $null }
    $name = if ($owner) { Get-TaskOwnerName $owner } else { 'an unknown owner' }
    [pscustomobject]@{ TaskName = $TaskName; Owner = $name; Hidden = $false; OtherUser = -not $machine }
}
function Format-TaskLock($lock, [switch]$Removal, [string]$Follows) {
    # One line: the true state and the one command that changes it.
    # -Removal words it for a task this run meant to remove, not replace.
    # -Follows names the settings file a task this shell cannot change still
    # reads at every start: the command is then only for a user who wants a
    # task of their own.
    $quoted = $lock.TaskName.Replace("'", "''")
    if ($lock.Hidden) {
        return ("a scheduled task named '$($lock.TaskName)' exists but this account cannot read it, " +
                "so it belongs to another account on this machine; nothing was changed. " +
                "Ask that account to remove it, or pick another task name")
    }
    $remove = "Stop-ScheduledTask -TaskName '$quoted'; Unregister-ScheduledTask -TaskName '$quoted' -Confirm:`$false"
    if ($lock.OtherUser) {
        return ("the scheduled task '$($lock.TaskName)' is owned by another account ($($lock.Owner)), " +
                "and this run never changes another account's task; nothing was changed and it keeps running as registered. " +
                "Ask that account to remove it ($remove), or pick another task name")
    }
    $why = if ($lock.Owner -eq 'Administrators') {
        "was registered from an elevated PowerShell, so Administrators own it"
    } else { "is owned by $($lock.Owner)" }
    $command = ("run once in Windows PowerShell opened with Run as administrator from the Start menu " +
                "(not from a shell inside Claude Desktop: anthropics/claude-code#61635): $remove")
    if ($Follows) {
        return ("the scheduled task '$($lock.TaskName)' $why and only an elevated shell can change it, " +
                "but it reads $Follows at every start, so it keeps following these settings. " +
                "Only if you want a task of your own instead, $command - then rerun this command in a normal window")
    }
    $goal = if ($Removal) { "To remove it" } else { "To replace it with a task of your own" }
    $then = if ($Removal) { "" } else { " - then rerun this command in a normal window" }
    ("the scheduled task '$($lock.TaskName)' $why, and this unelevated shell cannot change or remove it; " +
     "nothing was changed and it keeps running as registered. $goal, $command" + $then)
}
# <<< task lock <<<
function Test-TaskRunsRunner([string]$TaskName, [string]$Repo, [string]$Kind) {
    # Whether the task's one action is the spawner the autostart scripts in
    # this folder register since 29 September 2026: it starts
    # "<Repo>\ops\shim_autostart.py" run <Kind>, which reads ops\.env at every
    # start, with an interpreter that still exists, and is enabled. Read
    # only; an older task
    # carries the model and port on its own command line instead.
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    # A disabled task never starts at logon, and this shell cannot enable it.
    if ($task.Settings -and -not $task.Settings.Enabled) { return $false }
    $actions = @($task.Actions)
    if ($actions.Count -ne 1 -or "$($actions[0].Arguments)" -notmatch '-EncodedCommand ([A-Za-z0-9+/=]+)') { return $false }
    try { $code = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1])) } catch { return $false }
    $lines = @($code -split "`r?`n")
    $runner = ('"' + $Repo + '\ops\shim_autostart.py" run ' + $Kind).Replace("'", "''")
    if (-not ($lines -contains "`$psi.Arguments = '$runner'")) { return $false }
    $file = @($lines | Where-Object { $_ -match "^\`$psi\.FileName = '((?:[^']|'')*)'$" })
    if ($file.Count -ne 1 -or $file[0] -notmatch "^\`$psi\.FileName = '((?:[^']|'')*)'$") { return $false }
    Test-Path -LiteralPath $Matches[1].Replace("''", "'") -PathType Leaf
}
