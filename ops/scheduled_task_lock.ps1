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
function Get-AccountSid([string]$Account) {
    # An account name or SID as a SID, or '' when it does not resolve.
    if (-not $Account) { return '' }
    if ($Account -match '^S-1-') { return $Account }
    try { (New-Object Security.Principal.NTAccount $Account).Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { '' }
}
function Get-TaskPrincipalSid([string]$TaskName) {
    # The account the task runs as, as a SID, or '' when it names none
    # (a group principal) or the name does not resolve.
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Get-AccountSid "$($task.Principal.UserId)"
}
function Get-TaskLogonMismatch($task) {
    # '' when an enabled logon trigger fires at this user's logon (one for
    # this user, or the older one for any user's logon), else why not.
    $logon = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' })
    if (-not $logon.Count) { return 'has no logon trigger, so it does not start the shim at logon' }
    $me = Get-TaskShellUserSid
    $usable = @($logon | Where-Object { $_.Enabled -ne $false -and (-not "$($_.UserId)" -or (Get-AccountSid "$($_.UserId)") -eq $me) })
    if (-not $usable.Count) { return 'has no enabled logon trigger for this user, so it does not start the shim at your logon' }
    ''
}
function Get-TaskOwnerName([string]$Owner) {
    if ($Owner -eq 'BA') { return 'Administrators' }
    if ($Owner -eq 'SY') { return 'SYSTEM' }
    if (-not $Owner) { return 'an unknown account' }
    try {
        (New-Object Security.Principal.SecurityIdentifier $Owner).Translate([Security.Principal.NTAccount]).Value
    } catch { $Owner }
}
function Get-TaskLock([string]$TaskName) {
    # $null when this shell may change the task: there is none, this user
    # owns it, or Administrators or SYSTEM own it, it runs as this user and
    # this shell is elevated. Otherwise an object saying why not. Another
    # account's task is never this shell's to change, elevated or not: one
    # owned by another user, or one Administrators own that runs as another
    # account (an administrator who registered it elevated for themselves).
    $sddl = Get-TaskOwnerSddl $TaskName
    if ($sddl -eq 'absent') { return $null }
    if ($sddl -eq 'hidden') {
        return [pscustomobject]@{ TaskName = $TaskName; Owner = 'another account'; Hidden = $true; OtherUser = $true }
    }
    $owner = if ("$sddl" -match '^O:(.+?)(?=[GDS]:|$)') { $Matches[1] } else { '' }
    $me = Get-TaskShellUserSid
    if ($owner -and $owner -eq $me) { return $null }
    $machine = $owner -in @('BA', 'SY', 'S-1-5-32-544', 'S-1-5-18')
    if (-not $machine) {
        return [pscustomobject]@{ TaskName = $TaskName; Owner = (Get-TaskOwnerName $owner); Hidden = $false; OtherUser = $true }
    }
    $runsAs = Get-TaskPrincipalSid $TaskName
    if ($runsAs -ne $me) {
        $who = "$(Get-TaskOwnerName $owner) own it and it runs as $(if ($runsAs) { Get-TaskOwnerName $runsAs } else { 'no single account' })"
        return [pscustomobject]@{ TaskName = $TaskName; Owner = $who; Hidden = $false; OtherUser = $true }
    }
    if (Test-TaskShellElevated) { return $null }
    [pscustomobject]@{ TaskName = $TaskName; Owner = (Get-TaskOwnerName $owner); Hidden = $false; OtherUser = $false }
}
function Format-TaskLock($lock, [switch]$Removal, [string]$Follows, [string]$Reason) {
    # One line: the true state, then the one command that changes it, last,
    # so a copy to the end of the line pastes only the command.
    # -Removal words it for a task this run meant to remove, not replace.
    # -Follows names the settings file a task this shell cannot change still
    # reads at every start: the command is then only for a user who wants a
    # task of their own. -Reason says why the task cannot be followed.
    $quoted = $lock.TaskName.Replace("'", "''")
    if ($lock.Hidden) {
        return ("a scheduled task named '$($lock.TaskName)' exists but this account cannot read it, " +
                "so it belongs to another account on this machine; nothing was changed. Ask that account to remove it")
    }
    $remove = "Stop-ScheduledTask -TaskName '$quoted'; Unregister-ScheduledTask -TaskName '$quoted' -Confirm:`$false"
    if ($lock.OtherUser) {
        return ("the scheduled task '$($lock.TaskName)' belongs to another account ($($lock.Owner)), " +
                "and this run never changes another account's task; nothing was changed. Ask that account to remove it: $remove")
    }
    $why = if ($lock.Owner -eq 'Administrators') {
        "was registered from an elevated PowerShell, so Administrators own it"
    } else { "is owned by $($lock.Owner)" }
    $elevated = ("run this once in Windows PowerShell opened with Run as administrator from the Start menu " +
                 "(not from a shell inside Claude Desktop: anthropics/claude-code#61635)")
    if ($Follows) {
        return ("the scheduled task '$($lock.TaskName)' $why and only an elevated shell can change it, " +
                "but it reads $Follows at every start, so it keeps following these settings. " +
                "Only if you want a task of your own instead, $elevated, then rerun this command in a normal window: $remove")
    }
    if ($Removal) {
        return ("the scheduled task '$($lock.TaskName)' $why, and this unelevated shell cannot remove it; " +
                "nothing was changed. To remove it, ${elevated}: $remove")
    }
    $state = if ($Reason) { "it $Reason, so the new settings were not applied and nothing was changed" }
             else { "nothing was changed and it keeps running as registered" }
    ("the scheduled task '$($lock.TaskName)' $why, and this unelevated shell cannot change it; $state. " +
     "To replace it with a task of your own, $elevated, then rerun this command in a normal window: $remove")
}
function New-TaskLockError([string]$Line) {
    # A refusal over a task this shell may not change, marked so a caller
    # can print only its line ('PseudolifeTaskLocked').
    [Management.Automation.ErrorRecord]::new([InvalidOperationException]::new($Line), 'PseudolifeTaskLocked',
        [Management.Automation.ErrorCategory]::PermissionDenied, $null)
}
function Get-CanonicalPath([string]$Path) {
    # The path as ops/shim_autostart.py's Path.resolve() spells it: links
    # resolved and every component in its on-disk casing, so a checkout
    # reached through another spelling names the same PID file. The full
    # path where the file system cannot be asked (or off Windows).
    $full = [IO.Path]::GetFullPath($Path)
    if (-not ('PseudolifeFinalPath' -as [type])) {
        try {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
public static class PseudolifeFinalPath {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, StringBuilder path, uint length, uint flags);
    public static string Of(string path) {
        // No access rights, every share mode, OPEN_EXISTING, and
        // FILE_FLAG_BACKUP_SEMANTICS so a directory opens too.
        using (SafeFileHandle handle = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
            if (handle.IsInvalid) { return null; }
            StringBuilder buffer = new StringBuilder(32768);
            uint length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
            if (length == 0 || length >= buffer.Capacity) { return null; }
            return buffer.ToString();
        }
    }
}
'@
        } catch { return $full }
    }
    $final = try { [PseudolifeFinalPath]::Of($full) } catch { $null }
    if (-not $final) { return $full }
    if ($final.StartsWith('\\?\UNC\')) { return '\\' + $final.Substring(8) }
    if ($final.StartsWith('\\?\')) { return $final.Substring(4) }
    $final
}
# <<< task lock <<<
function Get-TaskRunnerMismatch([string]$TaskName, [string]$Repo, [string]$Kind) {
    # Why the task cannot be followed by writing ops\.env, or '' when it can:
    # its one action is the spawner the autostart scripts in this folder
    # register since 29 September 2026, starting
    # "<Repo>\ops\shim_autostart.py" run <Kind> (which reads ops\.env at every
    # start) with an interpreter that still exists, at this user's logon, and
    # it is enabled. Read only.
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    # A disabled task never starts at logon, and this shell cannot enable it.
    if ($task.Settings -and -not $task.Settings.Enabled) { return 'is disabled, so it never starts at logon' }
    $logon = Get-TaskLogonMismatch $task
    if ($logon) { return $logon }
    $older = "runs an older command that carries its settings on its own command line instead of reading ops\.env"
    $actions = @($task.Actions)
    if ($actions.Count -ne 1 -or [IO.Path]::GetFileName("$($actions[0].Execute)") -notin @('powershell.exe', 'pwsh.exe') -or
        "$($actions[0].Arguments)" -notmatch '-EncodedCommand ([A-Za-z0-9+/=]+)') { return $older }
    try { $code = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1])) } catch { return $older }
    $lines = @($code -split "`r?`n")
    $runner = ('"' + $Repo + '\ops\shim_autostart.py" run ' + $Kind).Replace("'", "''")
    if (-not ($lines -contains "`$psi.Arguments = '$runner'")) {
        if ($code -match 'shim_autostart\.py') { return "runs the runner of another checkout or of the other shim" }
        return $older
    }
    $file = @($lines | Where-Object { $_ -match "^\`$psi\.FileName = '((?:[^']|'')*)'$" })
    if ($file.Count -ne 1 -or $file[0] -notmatch "^\`$psi\.FileName = '((?:[^']|'')*)'$") { return $older }
    $python = $Matches[1].Replace("''", "'")
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { return "runs an interpreter that no longer exists ($python)" }
    ''
}
function Get-RecordedShim([string]$Repo, [string]$Kind) {
    # The shim the runner started last, proved by its PID file rather than by
    # a command-line pattern: the PID it recorded, a process started no later
    # than the file was written (a reused PID starts after it), whose command
    # line runs this checkout's evals\<kind>_shim.py on the port the file
    # records (which may differ from the port asked for now); plus its
    # descendants. An empty list when there is none to prove.
    $full = Get-CanonicalPath $Repo
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($full)) | ForEach-Object { $_.ToString('x2') }) }
    finally { $sha.Dispose() }
    $pidFile = Join-Path $env:USERPROFILE ('.pseudolife-mcp\' + $Kind + '-shim-' + $digest.Substring(0, 8) + '.pid')
    if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) { return @() }
    $recorded = Get-Content -Raw -LiteralPath $pidFile
    if ($recorded -notmatch '^(\d+)\s+(\d+)\s*$') { return @() }
    $id = [int]$Matches[1]
    $port = [int]$Matches[2]
    $written = (Get-Item -LiteralPath $pidFile).LastWriteTime
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction Stop
    $script = [regex]::Escape((Join-Path $full "evals\${Kind}_shim.py"))
    if (-not $process -or $process.CreationDate -gt $written -or
        "$($process.CommandLine)" -notmatch ($script + '"?\s.*--port\s+"?' + $port + '"?(\s|$)')) { return @() }
    # Every descendant started after its parent: the base interpreter a venv
    # launcher starts, and the provider CLI a request in flight runs, which
    # would otherwise outlive the shim that supervises its timeout.
    $snapshot = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $tree = @($process)
    do {
        $added = @($snapshot | Where-Object {
            $child = $_
            $child.ProcessId -notin @($tree | ForEach-Object ProcessId) -and @($tree | Where-Object {
                $_.ProcessId -eq $child.ParentProcessId -and $_.CreationDate -le $child.CreationDate
            }).Count -gt 0
        })
        $tree += $added
    } while ($added.Count)
    $tree
}
function Stop-RecordedShim($processes) {
    # Stop exactly those processes, children first. Each is re-read by PID
    # (same start time and command line), then stopped through a handle held
    # across the kill whose start time must equal the recorded one at the
    # precision CIM keeps (microseconds), so a reused PID is never stopped.
    foreach ($old in @($processes | Sort-Object CreationDate -Descending)) {
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($old.ProcessId)" -ErrorAction Stop
        if (-not $current -or $current.CreationDate -ne $old.CreationDate -or $current.CommandLine -ne $old.CommandLine) { continue }
        $handle = try { Get-Process -Id $old.ProcessId -ErrorAction Stop } catch { $null }
        if (-not $handle) { continue }
        try {
            $null = $handle.get_Handle()
            $started = $handle.get_StartTime().ToUniversalTime().Ticks
            $captured = ([datetime]$old.CreationDate).ToUniversalTime().Ticks
            if (($started - $started % 10) -ne ($captured - $captured % 10)) { continue }
            if (-not $handle.get_HasExited()) { $handle.Kill(); $null = $handle.WaitForExit(15000) }
        } finally { $handle.Dispose() }
    }
}
