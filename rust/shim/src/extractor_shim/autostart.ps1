param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$c = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
$native = [string]$c.Native
$ownerExe = [string]$c.OwnerExe
$kind = [string]$c.Kind
$port = [int]$c.Port
$name = [string]$c.Name
function Test-SamePath([string]$left, [string]$right) {
    if (-not $left -or -not $right) { return $false }
    if ($left -eq $right) { return $true }
    try { return (Get-Item -LiteralPath $left -ErrorAction Stop).FullName -eq (Get-Item -LiteralPath $right -ErrorAction Stop).FullName }
    catch { return $false }
}
function Test-NativeInvocation([string]$command, [string]$provider, [string]$settingsFile) {
    $pattern = '(?:^|\s)"?extractor-shim"?\s+"?' + [regex]::Escape($provider) + '"?\s+"?--env-file"?\s+(?:"([^"]+)"|(\S+))(?=\s|$)'
    $match = [regex]::Match($command, $pattern, 'IgnoreCase')
    if (-not $match.Success) { return $false }
    $path = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
    return Test-SamePath $path $settingsFile
}
function Format-ForeignCommand([string]$command) {
    # Once a credential-like argument occurs, omit the remaining arguments;
    # quoting and multi-token authorization values must not defeat redaction.
    $sensitive = '(?i)(?:^|\s)["'']?(?:--?|/)?[\w-]*(?:token|password|secret|credential|api[-_]?key|authorization|cookie|encodedcommand)[\w-]*["'']?(?:\s|[:=])|\bBearer\s+'
    $match = [regex]::Match($command, $sensitive)
    if ($match.Success) { return $command.Substring(0, $match.Index) + ' [redacted sensitive arguments]' }
    return $command
}
function Get-NativeTaskCode([string]$executable, [string]$arguments, [string]$runtimeExe) {
    $metadata = if ($runtimeExe) { "`$runtimeExe = '$($runtimeExe.Replace("'", "''"))'`n" } else { '' }
    $metadata + @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = '$($executable.Replace("'", "''"))'
`$psi.Arguments = '$($arguments.Replace("'", "''"))'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$process = [System.Diagnostics.Process]::Start(`$psi)
`$process.WaitForExit()
exit `$process.ExitCode
"@
}
function Get-RegisteredInvocation($task) {
    foreach ($item in $task.Actions) {
        $exe = $item.Execute
        $arguments = $item.Arguments
        $nativeWrapper = $false
        $encodedWrapper = $false
        $runtimeExe = ''
        if ($arguments -match '-EncodedCommand ([A-Za-z0-9+/=]+)') {
            $encodedWrapper = $true
            if ([IO.Path]::GetFileName($exe) -notin @('powershell.exe', 'pwsh.exe')) { continue }
            try {
                $code = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
                # The original Sonnet installer invoked Python directly from
                # an encoded PowerShell command. Parse its literal template;
                # never evaluate a registered task's code to identify it.
                if ($code -match '^& ''([^'']+)'' ''([^'']+[\\/]evals[\\/]sonnet_shim\.py)'' --port (\d+) --system-prompt-file ''[^'']+'' \*>> ''[^'']+''\s*$') {
                    $script = $Matches[2]
                    $scriptPort = [int]$Matches[3]
                    if ([IO.Path]::GetFileName($Matches[1]) -match '^python(?:w|\d+(?:\.\d+)*)?\.exe$') {
                        [PSCustomObject]@{ PythonScript=$script; Ports=@($scriptPort) }
                    }
                    continue
                }
                if ($code -notmatch '(?m)^\$psi\.FileName = ''((?:[^'']|'''')*)''\r?$') { continue }
                $exe = $Matches[1].Replace("''", "'")
                if ($code -notmatch '(?m)^\$psi\.Arguments = ''((?:[^'']|'''')*)''\r?$') { continue }
                $arguments = $Matches[1].Replace("''", "'")
                # --native-exe can name a custom waiting launcher. Its exact
                # generated wrapper identifies that registration after restart;
                # arbitrary encoded code with similar arguments does not.
                if ($code -match '^\$runtimeExe = ''((?:[^'']|'''')*)''\r?\n') { $runtimeExe = $Matches[1].Replace("''", "'") }
                $expected = Get-NativeTaskCode $exe $arguments $runtimeExe
                $nativeWrapper = $code.Replace("`r`n", "`n").TrimEnd() -ceq $expected.Replace("`r`n", "`n").TrimEnd()
            } catch { continue }
        }
        if ([IO.Path]::GetFileName($exe) -eq 'cmd.exe') {
            # The later Sonnet, Claude and Codex installers detached cmd.exe
            # with these quoted flags/redirection templates. Only the recorded
            # Python script and port own a child; model/CLI flags are not run.
            # 2f0edc4ce's BEL-corrupted evals/anthropic_shim.py path never ran
            # and ccb6f9a07 fixed it; that invalid action grants no ownership.
            if ($arguments -match '^/c ""([^"]+)" "([^"]+[\\/]evals[\\/](sonnet|claude|codex)_shim\.py)" --port (\d+) (.+) >> ""[^"]+"" 2>&1"$') {
                $script = $Matches[2]
                $provider = $Matches[3]
                $scriptPort = [int]$Matches[4]
                $tail = $Matches[5]
                $python = [IO.Path]::GetFileName($Matches[1]) -match '^python(?:w|\d+(?:\.\d+)*)?\.exe$'
                $tailPattern = switch ($provider) {
                    'sonnet' { '^--system-prompt-file "[^"]+"$' }
                    'claude' { '^--model [^"\s&|<>]+ --system-prompt-file "[^"]+"$' }
                    'codex' { '^--model [^"\s&|<>]+ --health-ttl \d+(?: --cli "[^"]+")?$' }
                }
                if ($python -and $tail -match $tailPattern) {
                    [PSCustomObject]@{ PythonScript=$script; Ports=@($scriptPort) }
                }
            }
            continue
        }
        if ($arguments -match '^"?extractor-shim"?\s+"?(claude|codex)"?\s+"?--env-file"?\s+"([^"]+)"') {
            $provider = $Matches[1]
            $settingsFile = $Matches[2]
            if ($nativeWrapper -or (-not $encodedWrapper -and
                ((Test-SamePath $exe $native) -or (Test-SamePath $exe $ownerExe)))) {
                [PSCustomObject]@{ Exe=$exe; Kind=$provider; EnvFile=$settingsFile; RuntimeExe=$runtimeExe }
            }
        } elseif ($arguments -match '^"?(.+?[\\/]ops[\\/]shim_autostart\.py)"?\s+run\s+(claude|codex)(\s|$)') {
            $repo = Split-Path -Parent (Split-Path -Parent $Matches[1])
            $provider = $Matches[2]
            $ports = @(if ($provider -eq 'claude') { 8082 } else { 8086 })
            $key = 'PSEUDOLIFE_' + $provider.ToUpperInvariant() + '_SHIM_PORT'
            foreach ($line in @(Get-Content -LiteralPath (Join-Path $repo 'ops/.env') -ErrorAction SilentlyContinue)) {
                if ($line -match ('^\s*(?:export\s+)?' + $key + '\s*=\s*["'']?(\d+)["'']?\s*(?:#.*)?$')) { $ports = @([int]$Matches[1]) }
            }
            # The runner records its last port as well, since config may have
            # changed since the detached process started.
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($repo))) | ForEach-Object { $_.ToString('x2') }) }
            finally { $sha.Dispose() }
            $pidFile = Join-Path $env:USERPROFILE ('.pseudolife-mcp/' + $provider + '-shim-' + $digest.Substring(0,8) + '.pid')
            $previous = Get-Content -Raw -LiteralPath $pidFile -ErrorAction SilentlyContinue
            if ($previous -match '^\d+\s+(\d+)\s*$') { $ports += [int]$Matches[1] }
            [PSCustomObject]@{ PythonScript=(Join-Path $repo "evals/${provider}_shim.py"); Ports=$ports }
        } elseif ($arguments -match '^"?(.+?[\\/]evals[\\/](?:claude|codex|sonnet)_shim\.py)"?\s+') {
            $script = $Matches[1]
            if ($arguments -match '--port\s+"?(\d+)"?(\s|$)') {
                [PSCustomObject]@{ PythonScript=$script; Ports=@([int]$Matches[1]) }
            }
        }
    }
}
function Stop-CapturedProcess($old) {
    $filter = "ProcessId = $($old.ProcessId)"
    $current = try { Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop }
        catch { throw "could not verify process $($old.ProcessId): $($_.Exception.Message); restore access to Get-CimInstance Win32_Process -Filter '$filter', then retry the autostart command" }
    if (-not $current -or $current.CreationDate -ne $old.CreationDate -or $current.CommandLine -ne $old.CommandLine) { return }
    $process = $null
    try {
        try { $process = Get-Process -Id $old.ProcessId -ErrorAction Stop }
        catch {
            if ($_.FullyQualifiedErrorId -eq 'NoProcessFoundForGivenId,Microsoft.PowerShell.Commands.GetProcessCommand') { return }
            throw
        }
        # Handle opens the long-lived .NET process handle. StartTime and Kill
        # then use that same handle even if the original PID is reused.
        # Invoke getters explicitly: PowerShell property syntax can suppress
        # getter exceptions and silently produce null instead.
        $null = $process.get_Handle()
        $started = $process.get_StartTime().ToUniversalTime().Ticks
        $captured = ([datetime]$old.CreationDate).ToUniversalTime().Ticks
        # CIM's datetime has microsecond precision; .NET retains 100ns ticks.
        if (($started - $started % 10) -ne ($captured - $captured % 10)) { return }
        $process.Kill()
        if (-not $process.WaitForExit(15000)) { throw "process $($old.ProcessId) did not exit after termination; inspect Stop-Process -Id $($old.ProcessId) -Force after confirming its identity, then retry the autostart command" }
    } catch {
        $failure = $_
        $exited = $false
        # Only a confirmed exit on the pinned process makes a race benign;
        # an unreadable handle or a permission failure still propagates.
        if ($process) { try { $exited = $process.get_HasExited() } catch { } }
        if (-not $exited) { throw "could not stop process $($old.ProcessId): $($failure.Exception.Message); inspect Get-Process -Id $($old.ProcessId), restore permission to inspect and terminate that process, then retry the autostart command" }
    } finally {
        if ($process) { $process.Dispose() }
    }
}
function Assert-ShimRegistration($task) {
    if ($task -and (@($task.Actions).Count -ne 1 -or @(Get-RegisteredInvocation $task).Count -ne 1)) {
        throw "task $($task.TaskName) is not a recognized shim registration; choose another --name or inspect its action"
    }
}
function Get-OwnedTaskProcesses($task, $previous = @(), [switch]$RequireSettled) {
    if (-not $task) { return }
    $captured = @()
    $snapshot = try { @(Get-CimInstance Win32_Process -ErrorAction Stop) }
        catch { throw "could not inspect shim processes: $($_.Exception.Message); restore access to Get-CimInstance Win32_Process, then retry the autostart command" }
    foreach ($invocation in @(Get-RegisteredInvocation $task)) {
        if ($invocation.PythonScript) {
            $ports = '--port\s+"?(' + ($invocation.Ports -join '|') + ')"?(\s|$)'
            $captured += @($snapshot | Where-Object {
                $command = [regex]::Match($_.CommandLine, '^(?:"[^"]+"|\S+)\s+(?:"([^"]+)"|(\S+))(?=\s|$)')
                $script = if ($command.Groups[1].Success) { $command.Groups[1].Value } else { $command.Groups[2].Value }
                $command.Success -and (Test-SamePath $script $invocation.PythonScript) -and $_.CommandLine -match $ports
            })
            continue
        }
        foreach ($process in @($snapshot | Where-Object { Test-NativeInvocation $_.CommandLine $invocation.Kind $invocation.EnvFile })) {
            # A waiting launcher may already have exited. The exact runtime
            # image and this registration's provider/settings still identify
            # its native child; a basename or matching port never does.
            # New registrations retain the exact runtime image across upgrades.
            # Metadata-free launchers can only reuse runtime paths whose
            # ownership was already proved in the first snapshot.
            $runtime = if ($invocation.RuntimeExe) { $invocation.RuntimeExe } else { $ownerExe }
            $owned = (Test-SamePath $process.ExecutablePath $invocation.Exe) -or
                (Test-SamePath $process.ExecutablePath $runtime) -or
                (Test-SamePath $process.ExecutablePath $ownerExe) -or
                @($previous | Where-Object {
                    (Test-SamePath $_.ExecutablePath $process.ExecutablePath) -and
                    (Test-NativeInvocation $_.CommandLine $invocation.Kind $invocation.EnvFile)
                }).Count -gt 0
            if (-not $owned) {
                $filter = "ProcessId = $($process.ParentProcessId)"
                $parent = try { Get-CimInstance Win32_Process -Filter $filter -ErrorAction Stop }
                    catch { throw "could not verify launcher for process $($process.ProcessId): $($_.Exception.Message); restore access to Get-CimInstance Win32_Process -Filter '$filter', then retry the autostart command" }
                $owned = $parent -and (Test-SamePath $parent.ExecutablePath $invocation.Exe) -and
                    $parent.CreationDate -le $process.CreationDate -and (Test-NativeInvocation $parent.CommandLine $invocation.Kind $invocation.EnvFile)
            }
            if (-not $owned -and $RequireSettled -and -not $invocation.RuntimeExe) {
                # An absent launcher or reused parent PID cannot prove this
                # image belongs to a metadata-free task. Preserve its registration
                # while any unverified exact provider/settings candidate remains.
                throw "task $($task.TaskName) has an unverified runtime (pid $($process.ProcessId), image: $($process.ExecutablePath)); inspect Get-CimInstance Win32_Process -Filter 'ProcessId = $($process.ProcessId)' and stop its confirmed owner, then retry the autostart command; reinstall the task to record its runtime image"
            }
            if ($owned) { $captured += $process }
        }
    }
    do {
        $added = @($snapshot | Where-Object {
            $child = $_
            $child.ProcessId -notin @($captured.ProcessId) -and @($captured | Where-Object {
                $_.ProcessId -eq $child.ParentProcessId -and $_.CreationDate -le $child.CreationDate
            }).Count -gt 0
        })
        $captured += $added
    } while ($added.Count)
    $captured
}
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
function Get-TaskRegistration([string]$taskName, [datetime]$deadline = (Get-Date).AddSeconds(15)) {
    # The 2026-11-03 VM trace saw four intermittent provider file-not-found
    # errors in 80 autostart proofs. They are not the named-task absence result.
    # One-shot reads get a 15-second retry budget. Stop/delete polling passes
    # its existing deadline so a read cannot restart that settlement budget.
    # Persistent failures and all other errors remain visible to callers.
    while ($true) {
        try { return Get-ScheduledTask -TaskName $taskName -ErrorAction Stop }
        catch {
            if ($_.FullyQualifiedErrorId -eq 'CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask') { return }
            if ($_.FullyQualifiedErrorId -ne 'HRESULT 0x80070002,Get-ScheduledTask' -or (Get-Date) -ge $deadline) { throw }
        }
        Start-Sleep -Milliseconds 200
    }
}
function Get-TaskOwnership($task) {
    # State changes while the scheduler settles; execution identity must not.
    [ordered]@{ Actions=@($task.Actions | Select-Object Execute, Arguments, WorkingDirectory);
       Principal=($task.Principal | Select-Object UserId, GroupId, LogonType, RunLevel) } | ConvertTo-Json -Depth 4 -Compress
}
function Wait-TaskStopped([string]$taskName, [string]$ownership) {
    # Windows autostart stress (2026-11-02) left a terminated task visible;
    # reuse the existing 15-second port-release budget for scheduler settlement.
    $deadline = (Get-Date).AddSeconds(15)
    while ($true) {
        $task = try { Get-TaskRegistration $taskName $deadline }
                catch { throw "could not confirm task $taskName stopped: $($_.Exception.Message); inspect its scheduler state" }
        if ($task -and $ownership -and (Get-TaskOwnership $task) -ne $ownership) { throw "task $taskName registration changed; refusing to change it" }
        if (-not $task -or $task.State -notin @('Running', 'Queued')) { return }
        if ((Get-Date) -ge $deadline) { throw "task $taskName did not stop; inspect its scheduler state" }
        Start-Sleep -Milliseconds 200
    }
}
function Remove-OwnedTask([string]$taskName, [string]$expectedOwnership, [switch]$RequireExpectedOwnership) {
    $task = Get-TaskRegistration $taskName
    # False means another operation already removed the registration; callers
    # must not restore previous XML in response to that concurrent deletion.
    if (-not $task) { return $false }
    if ($RequireExpectedOwnership -and (-not $expectedOwnership -or (Get-TaskOwnership $task) -ne $expectedOwnership)) {
        throw "task $taskName registration changed; inspect its action before retrying; refusing to remove it"
    }
    Assert-ShimRegistration $task
    $ownership = Get-TaskOwnership $task
    $captured = @(Get-OwnedTaskProcesses $task -RequireSettled)
    # Capture children before stopping the wrapper: its exit can orphan them.
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    foreach ($process in @($captured | Sort-Object CreationDate -Descending)) { Stop-CapturedProcess $process }
    Wait-TaskStopped $taskName $ownership
    # A launcher can spawn between the first snapshot and scheduler stop.
    # Once the wrapper has stopped, collect that last child before removing
    # the registration or restoring a previous listener on the same port.
    foreach ($process in @(Get-OwnedTaskProcesses $task $captured -RequireSettled)) { Stop-CapturedProcess $process }
    $deadline = (Get-Date).AddSeconds(15)
    $accepted = $false
    $lastDeleteError = 'registration is still present'
    while ($true) {
        $task = try { Get-TaskRegistration $taskName $deadline }
                catch { throw "could not confirm removal of task ${taskName}: $($_.Exception.Message); inspect its scheduler state" }
        if (-not $task) { break }
        if ((Get-TaskOwnership $task) -ne $ownership) { throw "task $taskName registration changed; refusing to remove it" }
        if ((Get-Date) -ge $deadline) { throw "could not remove ${taskName}: $lastDeleteError" }
        if (-not $accepted) {
            try {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
                $accepted = $true
                $lastDeleteError = 'scheduler accepted deletion but registration is still present'
            } catch { $lastDeleteError = $_.Exception.Message }
        }
        # A rejected delete needs another attempt; an accepted delete only
        # needs its registration to disappear. Both share the same deadline.
        Start-Sleep -Milliseconds 200
    }
    if ($accepted) { $script:removedRegistration = $true }
    return $accepted
}
function Restart-UnchangedTask([string]$taskName, $ownership, [bool]$wasActive) {
    $current = Get-TaskRegistration $taskName
    if (-not $current) { return }
    if (-not $ownership -or (Get-TaskOwnership $current) -ne $ownership) {
        throw "task $taskName registration changed; inspect its action before retrying; refusing to restart it"
    }
    if (-not $wasActive) { return }
    Wait-TaskStopped $taskName $ownership
    $current = Get-TaskRegistration $taskName
    if (-not $current) { return }
    if ((Get-TaskOwnership $current) -ne $ownership) {
        throw "task $taskName registration changed; inspect its action before retrying; refusing to restart it"
    }
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
}
if ($c.Remove) {
    $script:removedRegistration = $false
    $names = @($name)
    if ($name -eq 'Pseudolife Claude Shim') { $names += 'Pseudolife Sonnet Shim' }
    # Read-only, before anything stops: an Administrators-owned task cannot
    # be removed from this shell, and stopping its shim first would leave
    # the task in place with nothing running.
    $present = @()
    foreach ($task in $names) {
        $registration = Get-TaskRegistration $task
        Assert-ShimRegistration $registration
        if ($registration) { $present += $task }
    }
    foreach ($task in $present) {
        $lock = Get-TaskLock $task
        if ($lock) { throw (Format-TaskLock $lock -Removal) }
    }
    foreach ($task in $names) {
        $null = Remove-OwnedTask $task
    }
    if ($script:removedRegistration) { Write-Host "Removed $name" }
    return
}
$registered = Get-TaskRegistration $name
$legacy = if ($name -eq 'Pseudolife Claude Shim') { Get-TaskRegistration 'Pseudolife Sonnet Shim' } else { $null }
Assert-ShimRegistration $registered
Assert-ShimRegistration $legacy
$siblings = @()
$presentSiblings = @()
if ($c.ReplaceSibling) {
    $siblings = if ($kind -eq 'claude') { @('Pseudolife Codex Shim') } else { @('Pseudolife Claude Shim', 'Pseudolife Sonnet Shim') }
    foreach ($sibling in $siblings) {
        if ($sibling -ne $name) {
            $registration = Get-TaskRegistration $sibling
            Assert-ShimRegistration $registration
            if ($registration) { $presentSiblings += $sibling }
        }
    }
}
# Read-only, before anything changes: a legacy or sibling task this shell
# cannot remove would keep starting a second shim at logon beside the new
# registration, so it is refused here with the one elevated command.
foreach ($other in @(@(if ($legacy) { $legacy.TaskName }) + $presentSiblings)) {
    $lock = Get-TaskLock $other
    if ($lock) { throw (Format-TaskLock $lock -Removal) }
}
$selectedLock = if ($registered) { Get-TaskLock $name } else { $null }
# Refuse ambiguous metadata-free runtimes before changing settings or stopping
# either original task; the settled snapshot repeats this check for late children.
$legacyProcesses = @(Get-OwnedTaskProcesses $legacy -RequireSettled)
$legacyWasActive = $legacy -and ($legacy.State -eq 'Running' -or $legacyProcesses.Count -gt 0)
# Only the selected registration can establish ownership. A matching basename
# and port from another checkout is a foreign listener, never a replacement.
$registeredProcesses = @(Get-OwnedTaskProcesses $registered -RequireSettled)
$stale = $registeredProcesses + $legacyProcesses
$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    if ($listener.OwningProcess -notin @($stale | ForEach-Object ProcessId)) {
        $conflict = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
        $image = if ($conflict.ExecutablePath) { $conflict.ExecutablePath } elseif ($conflict.Name) { $conflict.Name } else { '<unavailable>' }
        $command = if ($conflict.CommandLine) { Format-ForeignCommand $conflict.CommandLine } else { '<unavailable>' }
        throw "port $port belongs to another process (pid $($listener.OwningProcess), image: $image, command: $command); choose --port or stop its owner"
    }
}
function Quote-Argument([string]$value) {
    # Windows CommandLineToArgvW quoting, including a trailing backslash.
    '"' + [regex]::Replace([regex]::Replace($value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}
$argument = (@($c.ServeArgs | ForEach-Object { Quote-Argument ([string]$_) }) -join ' ')
# Keep the scheduler attached to the hidden child for its entire lifetime:
# console Rust executables otherwise open a visible window at interactive logon.
$inner = Get-NativeTaskCode $native $argument $ownerExe
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded"
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# This user, limited token, and a logon trigger for this user only: the
# shape an unelevated user may register (S4U itself still needs elevation).
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType ([string]$c.LogonType) -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden
function Test-HealthyListener {
    $candidate = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and (Test-SamePath $owner.ExecutablePath $ownerExe) -and (Test-NativeInvocation $owner.CommandLine $kind $c.EnvFile)) {
            try {
                $null = Invoke-WebRequest -UseBasicParsing -Uri "http://$($c.Host):$port/health" -TimeoutSec 2
                $verifyArgs = @('extractor-shim', 'verify-settings', [string]$candidate.OwningProcess) + @($c.ServeArgs | Select-Object -Skip 1)
                & $ownerExe @verifyArgs *> $null
                if ($LASTEXITCODE -ne 0) { return $false }
                $script:verifiedPid = $candidate.OwningProcess
                return $true
            } catch { }
        }
    }
    return $false
}
$configExisted = Test-Path -LiteralPath $c.EnvFile
$configBefore = [byte[]]@()
if ($configExisted) { $configBefore = [IO.File]::ReadAllBytes($c.EnvFile) }
$previousXml = if ($registered) { Export-ScheduledTask -TaskName $name } else { $null }
$wasActive = $registered -and ($registered.State -eq 'Running' -or $registeredProcesses.Count -gt 0)
$rollbackOwnership = if ($registered) { Get-TaskOwnership $registered } else { $null }
$originalOwnership = $rollbackOwnership
$replacementRegistered = $false
$mutated = $false
$unchanged = $false
$sameTask = $registered -and @($registered.Actions).Count -eq 1 -and
    $registered.Actions[0].Execute -eq $action.Execute -and $registered.Actions[0].Arguments -eq $action.Arguments -and
    [string]$registered.Principal.LogonType -eq [string]$c.LogonType
# A task this shell cannot replace is never changed: unless it already runs
# this action, and so reads the settings file at every start, it is refused
# here, before the settings file is written or anything is stopped.
if ($selectedLock -and -not ($sameTask -and $registered.Settings.Enabled)) { throw (Format-TaskLock $selectedLock) }
$followsFile = $false
try {
    $configArgs = @($c.ConfigArgs)
    & $native @configArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'could not write native shim settings' }
    $unchanged = $sameTask -and (Test-HealthyListener)
    # The task this shell cannot replace runs this action: it is left
    # registered and restarted below to pick up the settings just written.
    $followsFile = $selectedLock -and -not $unchanged
    if ($unchanged -and -not $registered.Settings.Enabled) {
        Enable-ScheduledTask -TaskName $name -ErrorAction Stop | Out-Null
    }
    if (-not $unchanged) {
        $mutated = $true
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($legacy) { Stop-ScheduledTask -TaskName $legacy.TaskName -ErrorAction SilentlyContinue }
        foreach ($old in $stale) { Stop-CapturedProcess $old }
        Wait-TaskStopped $name $rollbackOwnership
        if ($legacy) { Wait-TaskStopped $legacy.TaskName (Get-TaskOwnership $legacy) }
        foreach ($old in @((Get-OwnedTaskProcesses $registered $registeredProcesses -RequireSettled)) + @((Get-OwnedTaskProcesses $legacy $legacyProcesses -RequireSettled))) { Stop-CapturedProcess $old }
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "port $port did not become free; restoring the previous registration" }
        # Process and port settlement can outlive the earlier ownership check.
        # A new registration also requires the name to have remained absent.
        $beforeRegister = Get-TaskRegistration $name
        if (($beforeRegister -and (-not $rollbackOwnership -or (Get-TaskOwnership $beforeRegister) -ne $rollbackOwnership)) -or
            (-not $beforeRegister -and $rollbackOwnership)) {
            throw "task $name registration changed; inspect its action before retrying; refusing to overwrite it"
        }
        # A task this shell cannot replace already runs this action: it is
        # only restarted. An unelevated user may stop and start a task
        # Administrators own (Windows dev VM, 16 November 2026, filtered
        # token: Start-ScheduledTask, schtasks /Run and Stop-ScheduledTask
        # were accepted), so the new settings apply now.
        if (-not $followsFile) {
            $installed = Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description "Native $kind extractor CLI shim" -Force
            $replacementRegistered = $true
            $rollbackOwnership = Get-TaskOwnership $installed
            if (-not (Get-TaskRegistration $name)) { throw "task was not registered: $name" }
        }
        Start-ScheduledTask -TaskName $name
        $deadline = (Get-Date).AddSeconds([int]$c.Timeout)
        while (-not (Test-HealthyListener)) {
            if ((Get-Date) -ge $deadline) { throw "native $kind shim failed its health check on port $port; inspect its configured log" }
            Start-Sleep -Milliseconds 500
        }
    }
} catch {
    $failure = $_
    $rollbackErrors = @()
    $rollbackRemoved = $false
    if ($replacementRegistered) {
        try { $rollbackRemoved = Remove-OwnedTask $name $rollbackOwnership -RequireExpectedOwnership }
        catch { $rollbackErrors += "removing replacement task: $($_.Exception.Message)" }
    }
    try {
        if ($configExisted) { [IO.File]::WriteAllBytes($c.EnvFile, [byte[]]$configBefore) }
        elseif (Test-Path -LiteralPath $c.EnvFile) { Remove-Item -LiteralPath $c.EnvFile }
    } catch { $rollbackErrors += "restoring settings: $($_.Exception.Message)" }
    if ($replacementRegistered -and $previousXml -and $rollbackRemoved) {
        try {
            $remainingTask = Get-TaskRegistration $name
            if ($remainingTask -and (-not $rollbackOwnership -or (Get-TaskOwnership $remainingTask) -ne $rollbackOwnership)) {
                throw "task $name registration changed; inspect its action before retrying; refusing to overwrite it with the previous registration"
            }
            Register-ScheduledTask -TaskName $name -Xml $previousXml -Force | Out-Null
            if ($wasActive) { Start-ScheduledTask -TaskName $name }
        } catch { $rollbackErrors += "restoring previous task: $($_.Exception.Message)" }
    }
    if ($followsFile -and $mutated) {
        # The restart of a task this shell cannot replace started an instance
        # on the new settings: stop it before the original restarts on the
        # restored ones.
        try {
            $current = Get-TaskRegistration $name
            if ($current) {
                $started = @(Get-OwnedTaskProcesses $current -RequireSettled)
                Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                foreach ($process in @($started | Sort-Object CreationDate -Descending)) { Stop-CapturedProcess $process }
                Wait-TaskStopped $name $originalOwnership
                foreach ($process in @(Get-OwnedTaskProcesses $current $started -RequireSettled)) { Stop-CapturedProcess $process }
            }
        } catch { $rollbackErrors += "stopping the restarted task: $($_.Exception.Message)" }
    }
    if ($mutated -and -not $replacementRegistered) {
        # Stopping the original is not replacing its registration. Restart only
        # that unchanged task; a concurrent deletion must remain a deletion.
        try { Restart-UnchangedTask $name $originalOwnership $wasActive }
        catch { $rollbackErrors += "recovering task state: $($_.Exception.Message)" }
    }
    if ($mutated -and $legacyWasActive) {
        try { Restart-UnchangedTask $legacy.TaskName (Get-TaskOwnership $legacy) $true }
        catch { $rollbackErrors += "restarting legacy task: $($_.Exception.Message)" }
    }
    if ($rollbackErrors.Count) {
        $remaining = try {
            $task = Get-TaskRegistration $name
            if ($task) { "present (state $($task.State))" } else { 'absent' }
        } catch { "unreadable ($($_.Exception.Message))" }
        Write-Warning "Rollback incomplete: $($rollbackErrors -join '; '). Task '$name' is $remaining. Restore its previous registration manually if needed; password-logon tasks require credentials that task XML does not contain. Original failure: $($failure.Exception.Message)" -WarningAction Continue
    }
    throw $failure
}
if ($name -eq 'Pseudolife Claude Shim') {
    $null = Remove-OwnedTask 'Pseudolife Sonnet Shim'
}
if ($c.ReplaceSibling) {
    foreach ($sibling in $siblings) {
        if ($sibling -ne $name) {
            $null = Remove-OwnedTask $sibling
        }
    }
}
if (-not $unchanged -and -not $followsFile) {
    Write-Host "Registered + started native $kind shim (port $port, pid $script:verifiedPid)."
    Write-Host "To reload $($c.EnvFile): stop and start scheduled task $name."
} elseif ($followsFile) {
    Write-Host "Native $kind shim autostart '$name' keeps its registration and was restarted with the new settings in $($c.EnvFile) (port $port, pid $script:verifiedPid). Note: $(Format-TaskLock $selectedLock -Follows $c.EnvFile)"
} elseif ($selectedLock) {
    Write-Host "Native $kind shim autostart '$name' already runs these settings on port $port; nothing changed. Note: $(Format-TaskLock $selectedLock -Follows $c.EnvFile)"
} else {
    Write-Host "Native $kind shim autostart '$name' is already registered and serving these settings on port $port; nothing changed."
}
