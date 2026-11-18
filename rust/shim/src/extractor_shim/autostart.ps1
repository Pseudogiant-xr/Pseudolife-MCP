param([string]$ConfigPath, [string]$ConfigJson)
$ErrorActionPreference = 'Stop'
$c = if ($PSBoundParameters.ContainsKey('ConfigJson')) { $ConfigJson | ConvertFrom-Json }
else { Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json }
$native = [string]$c.Native
$ownerExe = [string]$c.OwnerExe
$kind = [string]$c.Kind
$port = [int]$c.Port
$name = [string]$c.Name
function Stop-TaskLocked([string]$line) {
    # A refusal over a task this shell may not change, before any change.
    # The native client passes LockExitCode so its callers can tell it from
    # a failure and print only this line.
    if ($c.LockExitCode) { [Console]::Error.WriteLine($line); exit [int]$c.LockExitCode }
    throw $line
}
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
            try { $digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes((Get-CanonicalPath $repo))) | ForEach-Object { $_.ToString('x2') }) }
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
function Get-AccountSid([string]$Account) {
    # An account name or SID as a SID, or '' when it does not resolve.
    if (-not $Account) { return '' }
    if ($Account -match '^S-1-') { return $Account }
    try { (New-Object Security.Principal.NTAccount $Account).Translate([Security.Principal.SecurityIdentifier]).Value }
    catch { '' }
}
function Read-RootScheduledTask([string]$TaskName, [datetime]$Deadline = (Get-Date).AddSeconds(15)) {
    # The scheduler provider can report file-not-found for a task that still
    # exists. Retry only that provider error, within the caller's deadline.
    while ($true) {
        try { return Get-ScheduledTask -TaskPath '\' -TaskName $TaskName -ErrorAction Stop }
        catch {
            if ($_.FullyQualifiedErrorId -ne 'HRESULT 0x80070002,Get-ScheduledTask' -or (Get-Date) -ge $Deadline) { throw }
        }
        Start-Sleep -Milliseconds 200
    }
}
function Get-TaskPrincipalSid([string]$TaskName) {
    # The account the task runs as, as a SID, or '' when it names none
    # (a group principal) or the name does not resolve.
    Get-AccountSid (Get-TaskPrincipalName $TaskName)
}
function Get-TaskPrincipalName([string]$TaskName) {
    # The account name the task runs as, exactly as stored ('' for a group
    # principal). Read only when it did not resolve to a SID.
    "$((Read-RootScheduledTask $TaskName).Principal.UserId)"
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
        # A name that does not resolve may still be this user's (a domain
        # account offline, a renamed account): refused like another
        # account's, but never called another account's.
        $unresolved = if (-not $runsAs) { Get-TaskPrincipalName $TaskName } else { '' }
        if ($unresolved) {
            return [pscustomobject]@{ TaskName = $TaskName; Owner = (Get-TaskOwnerName $owner); Hidden = $false; OtherUser = $true; Unresolved = $unresolved }
        }
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
    $remove = "Stop-ScheduledTask -TaskPath '\' -TaskName '$quoted'; Unregister-ScheduledTask -TaskPath '\' -TaskName '$quoted' -Confirm:`$false"
    $elevated = ("run this once in Windows PowerShell opened with Run as administrator from the Start menu " +
                 "(not from a shell inside Claude Desktop: anthropics/claude-code#61635)")
    if ($lock.Unresolved) {
        return ("the scheduled task '$($lock.TaskName)' is owned by $($lock.Owner), and this shell could not resolve the account it runs as " +
                "('$($lock.Unresolved)'), so it cannot tell whether the task is yours; nothing was changed. " +
                "If it is yours, $elevated, then rerun this command in a normal window: $remove")
    }
    if ($lock.OtherUser) {
        return ("the scheduled task '$($lock.TaskName)' belongs to another account ($($lock.Owner)), " +
                "and this run never changes another account's task; nothing was changed. Ask that account or an administrator to remove it: $remove")
    }
    $why = if ($lock.Owner -eq 'Administrators') {
        "was registered from an elevated PowerShell, so Administrators own it"
    } else { "is owned by $($lock.Owner)" }
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
    if (-not $script:finalPathApi) {
        try {
            # Emit only the two interop declarations in memory: Run has no
            # assembly file to compile or load from the temporary directory.
            $assemblyName = [Reflection.AssemblyName]::new('PseudolifeFinalPath')
            $access = [Reflection.Emit.AssemblyBuilderAccess]::Run
            $assembly = if ($PSVersionTable.PSEdition -eq 'Desktop') {
                [AppDomain]::CurrentDomain.DefineDynamicAssembly($assemblyName, $access)
            } else { [Reflection.Emit.AssemblyBuilder]::DefineDynamicAssembly($assemblyName, $access) }
            $type = $assembly.DefineDynamicModule('Paths').DefineType('PseudolifeFinalPath', [Reflection.TypeAttributes]::Public)
            foreach ($signature in @(
                @{ Name='CreateFileW'; Return=[Microsoft.Win32.SafeHandles.SafeFileHandle]; Parameters=[type[]]@([string],[uint32],[uint32],[IntPtr],[uint32],[uint32],[IntPtr]) },
                @{ Name='GetFinalPathNameByHandleW'; Return=[uint32]; Parameters=[type[]]@([Microsoft.Win32.SafeHandles.SafeFileHandle],[Text.StringBuilder],[uint32],[uint32]) }
            )) {
                $method = $type.DefinePInvokeMethod($signature.Name, 'kernel32.dll',
                    [Reflection.MethodAttributes]'Public,Static,PinvokeImpl', [Reflection.CallingConventions]::Standard,
                    $signature.Return, $signature.Parameters, [Runtime.InteropServices.CallingConvention]::Winapi,
                    [Runtime.InteropServices.CharSet]::Unicode)
                $method.SetImplementationFlags([Reflection.MethodImplAttributes]::PreserveSig)
            }
            $script:finalPathApi = $type.CreateType()
        } catch { return $full }
    }
    $final = try {
        # No access rights, every share mode, OPEN_EXISTING, and
        # FILE_FLAG_BACKUP_SEMANTICS so a directory opens too.
        $handle = $script:finalPathApi::CreateFileW($full, 0, 7, [IntPtr]::Zero, 3, 0x02000000, [IntPtr]::Zero)
        try {
            if ($handle.IsInvalid) { return $full }
            $buffer = [Text.StringBuilder]::new(32768)
            $length = $script:finalPathApi::GetFinalPathNameByHandleW($handle, $buffer, $buffer.Capacity, 0)
            if ($length -eq 0 -or $length -ge $buffer.Capacity) { return $full }
            $buffer.ToString()
        } finally { $handle.Dispose() }
    } catch { $null }
    if (-not $final) { return $full }
    if ($final.StartsWith('\\?\UNC\')) { return '\\' + $final.Substring(8) }
    if ($final.StartsWith('\\?\')) { return $final.Substring(4) }
    $final
}
# <<< task lock <<<
function Get-TaskRegistration([string]$taskName, [datetime]$deadline = (Get-Date).AddSeconds(15)) {
    try { return Read-RootScheduledTask $taskName $deadline }
    catch {
        # With -TaskPath the named absence ID can omit its _TaskName suffix.
        if ($_.FullyQualifiedErrorId -in @('CmdletizationQuery_NotFound,Get-ScheduledTask', 'CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask')) { return }
        throw
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
    Stop-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction SilentlyContinue
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
                Unregister-ScheduledTask -TaskPath '\' -TaskName $taskName -Confirm:$false -ErrorAction Stop
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
    try { Start-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction Stop }
    catch { $script:restartNotStarted = $true; throw "Task Scheduler refused to start '$taskName' from this shell: $($_.Exception.Message)" }
}
if ($c.Remove) {
    $script:removedRegistration = $false
    $names = @($name)
    if ($name -eq 'Pseudolife Claude Shim') { $names += 'Pseudolife Sonnet Shim' }
    # Read-only, before anything stops: an Administrators-owned task cannot
    # be removed from this shell, and stopping its shim first would leave
    # the task in place with nothing running.
    foreach ($task in $names) { Assert-ShimRegistration (Get-TaskRegistration $task) }
    foreach ($task in $names) {
        $lock = Get-TaskLock $task
        if ($lock) { Stop-TaskLocked (Format-TaskLock $lock -Removal) }
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
if ($c.ReplaceSibling) {
    $siblings = if ($kind -eq 'claude') { @('Pseudolife Codex Shim') } else { @('Pseudolife Claude Shim', 'Pseudolife Sonnet Shim') }
    foreach ($sibling in $siblings) {
        if ($sibling -ne $name) { Assert-ShimRegistration (Get-TaskRegistration $sibling) }
    }
}
# Read-only, before anything changes: a legacy or sibling task this shell
# cannot remove would keep starting a second shim at logon beside the new
# registration, so it is refused here with the one elevated command. Every
# name's owner is read, listed or not: a task this account cannot read is
# not returned by Get-ScheduledTask.
$others = @(@(if ($name -eq 'Pseudolife Claude Shim') { 'Pseudolife Sonnet Shim' }) + @($siblings | Where-Object { $_ -ne $name }))
foreach ($other in $others) {
    $lock = Get-TaskLock $other
    if ($lock) { Stop-TaskLocked (Format-TaskLock $lock -Removal) }
}
$selectedLock = Get-TaskLock $name
if ($selectedLock -and ($selectedLock.Hidden -or $selectedLock.OtherUser -or -not $registered)) { Stop-TaskLocked (Format-TaskLock $selectedLock) }
# Refuse ambiguous metadata-free runtimes before changing settings or stopping
# either original task; the settled snapshot repeats this check for late children.
$legacyProcesses = @(Get-OwnedTaskProcesses $legacy -RequireSettled)
$legacyWasActive = $legacy -and ($legacy.State -eq 'Running' -or $legacyProcesses.Count -gt 0)
# Only the selected registration can establish ownership. A matching basename
# and port from another checkout is a foreign listener, never a replacement.
$registeredProcesses = @(Get-OwnedTaskProcesses $registered -RequireSettled)
$stale = $registeredProcesses + $legacyProcesses
$orphans = @()
function Get-OrphanedOwnShim($process) {
    # The process tree of a listener that is this install's own shim with no
    # registration left to name it, or nothing. Either this install's native
    # shim of any release (the launcher, the current runtime, or an older
    # numbered runtime beside it) run with this settings file; or the
    # Python-era shim of the --repo checkout that the runner's PID file
    # records (that PID, or its child the base interpreter, on this port,
    # started no later than the file was written).
    if (-not $process) { return }
    $image = [string]$process.ExecutablePath
    $runtimes = Split-Path -Parent (Split-Path -Parent $ownerExe)
    $olderRuntime = $image -and ([IO.Path]::GetFileName($image) -ieq [IO.Path]::GetFileName($ownerExe)) -and
        (Test-SamePath (Split-Path -Parent (Split-Path -Parent $image)) $runtimes)
    if (((Test-SamePath $image $ownerExe) -or (Test-SamePath $image $native) -or $olderRuntime) -and
        (Test-NativeInvocation $process.CommandLine $kind $c.EnvFile)) { return Add-ProcessDescendants @($process) }
    $serve = @($c.ServeArgs)
    $at = [array]::IndexOf($serve, '--repo')
    if ($at -lt 0 -or $at + 1 -ge $serve.Count) { return }
    $repo = Get-CanonicalPath ([string]$serve[$at + 1])
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($repo)) | ForEach-Object { $_.ToString('x2') }) }
    finally { $sha.Dispose() }
    $pidFile = Join-Path $env:USERPROFILE ('.pseudolife-mcp/' + $kind + '-shim-' + $digest.Substring(0, 8) + '.pid')
    $recorded = Get-Content -Raw -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if ($recorded -notmatch '^(\d+)\s+(\d+)\s*$' -or [int]$Matches[2] -ne $port) { return }
    $recordedPid = [int]$Matches[1]
    $written = (Get-Item -LiteralPath $pidFile).LastWriteTime
    $script = [regex]::Escape((Join-Path $repo "evals\${kind}_shim.py"))
    $root = if ($process.ProcessId -eq $recordedPid) { $process } else {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $recordedPid" -ErrorAction SilentlyContinue
        if ($parent -and $process.ParentProcessId -eq $recordedPid -and $process.CreationDate -ge $parent.CreationDate) { $parent }
    }
    if (-not $root -or $root.CreationDate -gt $written -or "$($root.CommandLine)" -notmatch ($script + '"?\s.*--port\s+"?' + $port + '"?(\s|$)')) { return }
    Add-ProcessDescendants @($root)
}
function Add-ProcessDescendants($roots) {
    # The roots and every descendant started after its parent: the base
    # interpreter, and a provider CLI a request in flight runs, which would
    # otherwise outlive the shim that supervises its timeout.
    $snapshot = try { @(Get-CimInstance Win32_Process -ErrorAction Stop) }
        catch { throw "could not inspect shim processes: $($_.Exception.Message); restore access to Get-CimInstance Win32_Process, then retry the autostart command" }
    $tree = @($roots)
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
$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    if ($listener.OwningProcess -notin @($stale | ForEach-Object ProcessId)) {
        $conflict = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
        # No registration names it, but it is this install's own shim that a
        # task removed from an elevated shell left running (Stop-ScheduledTask
        # does not end a detached or orphaned child). It is stopped like any
        # captured process, and the failure path says it was.
        if (-not $registered) {
            $own = @(Get-OrphanedOwnShim $conflict)
            if ($own.Count) {
                # Children first, so none is left without its parent.
                $stale += @($own | Sort-Object CreationDate -Descending)
                $orphans += $own
                continue
            }
        }
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
$powershell = if ($c.PowerShell) { [string]$c.PowerShell }
else { Join-Path ([Environment]::SystemDirectory) 'WindowsPowerShell\v1.0\powershell.exe' }
if ($powershell -notmatch '^[A-Za-z]:[\\/]') {
    throw 'Windows PowerShell path is not an absolute local path; rerun install-autostart'
}
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
$previousXml = if ($registered) { Export-ScheduledTask -TaskPath '\' -TaskName $name } else { $null }
$wasActive = $registered -and ($registered.State -eq 'Running' -or $registeredProcesses.Count -gt 0)
$rollbackOwnership = if ($registered) { Get-TaskOwnership $registered } else { $null }
$originalOwnership = $rollbackOwnership
$replacementRegistered = $false
$mutated = $false
$unchanged = $false
$reenabled = $false
$sameTask = $registered -and @($registered.Actions).Count -eq 1 -and
    $registered.Actions[0].Execute -eq $action.Execute -and $registered.Actions[0].Arguments -eq $action.Arguments -and
    [string]$registered.Principal.LogonType -eq [string]$c.LogonType
function Get-ComparableTaskCode([string]$arguments) {
    # The wrapper's code without its first line, which records the runtime
    # image of the release that registered the task: each client update
    # installs a numbered runtime, so that line differs after any update
    # while the launcher, its arguments and the settings file stay the same.
    if ($arguments -notmatch '-EncodedCommand ([A-Za-z0-9+/=]+)') { return $null }
    try { $code = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1])) } catch { return $null }
    ($code.Replace("`r`n", "`n") -replace '^\$runtimeExe = ''(?:[^'']|'''')*''\n', '').TrimEnd()
}
# The same launcher, arguments and settings file as this install would
# register, whatever release registered it.
$sameLauncher = $sameTask
if (-not $sameLauncher -and $registered -and @($registered.Actions).Count -eq 1 -and
    $registered.Actions[0].Execute -eq $action.Execute -and [string]$registered.Principal.LogonType -eq [string]$c.LogonType -and
    ($registered.Actions[0].Arguments -replace '-EncodedCommand \S+', '') -ceq ($action.Arguments -replace '-EncodedCommand \S+', '')) {
    $registeredCode = Get-ComparableTaskCode $registered.Actions[0].Arguments
    $sameLauncher = $registeredCode -and $registeredCode -ceq (Get-ComparableTaskCode $action.Arguments)
}
# A task this shell cannot replace is never changed: unless it already runs
# this launcher with this settings file, and so reads the file at every
# start, it is refused here, before the file is written or anything is
# stopped.
if ($selectedLock) {
    # The task must also start at this user's logon, or following it would
    # report an autostart that does not happen.
    $registeredExe = if ($registered) { @(Get-RegisteredInvocation $registered | Where-Object { $_.Exe } | ForEach-Object { $_.Exe }) | Select-Object -First 1 } else { $null }
    $olderRuntime = $registeredExe -and -not (Test-SamePath $registeredExe $native) -and
        (Test-SamePath (Split-Path -Parent (Split-Path -Parent $registeredExe)) (Split-Path -Parent (Split-Path -Parent $ownerExe)))
    $why = if (-not $sameLauncher -and $olderRuntime) { "runs an older numbered runtime ($registeredExe) instead of the launcher this install registers" }
           elseif (-not $sameLauncher) { 'runs another command than the one this install registers' }
           elseif (-not $registered.Settings.Enabled) { 'is disabled, so it never starts at logon' }
           else { Get-TaskLogonMismatch $registered }
    if ($why) { Stop-TaskLocked (Format-TaskLock $selectedLock -Reason $why) }
}
$followsFile = $false
try {
    $configArgs = @($c.ConfigArgs)
    & $native @configArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'could not write native shim settings' }
    $unchanged = $(if ($selectedLock) { $sameLauncher } else { $sameTask }) -and (Test-HealthyListener)
    # The task this shell cannot replace runs this action: it is left
    # registered and restarted below to pick up the settings just written.
    $followsFile = $selectedLock -and -not $unchanged
    if ($unchanged -and -not $registered.Settings.Enabled) {
        Enable-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction Stop | Out-Null
        $reenabled = $true
    }
    if (-not $unchanged) {
        $mutated = $true
        Stop-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
        if ($legacy) { Stop-ScheduledTask -TaskPath '\' -TaskName $legacy.TaskName -ErrorAction SilentlyContinue }
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
        # CIM cmdlet errors do not reliably terminate under
        # $ErrorActionPreference alone; a refused start must not become a
        # health-check timeout.
        try { Start-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction Stop }
        catch { throw "Task Scheduler refused to start '$name' from this shell: $($_.Exception.Message)" }
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
    $settingsRestored = $false
    try {
        if ($configExisted) { [IO.File]::WriteAllBytes($c.EnvFile, [byte[]]$configBefore) }
        elseif (Test-Path -LiteralPath $c.EnvFile) { Remove-Item -LiteralPath $c.EnvFile }
        $settingsRestored = $true
    } catch { $rollbackErrors += "restoring settings: $($_.Exception.Message)" }
    $script:restartNotStarted = $false
    if ($replacementRegistered -and $previousXml -and $rollbackRemoved) {
        try {
            $remainingTask = Get-TaskRegistration $name
            if ($remainingTask -and (-not $rollbackOwnership -or (Get-TaskOwnership $remainingTask) -ne $rollbackOwnership)) {
                throw "task $name registration changed; inspect its action before retrying; refusing to overwrite it with the previous registration"
            }
            Register-ScheduledTask -TaskName $name -Xml $previousXml -Force | Out-Null
            if ($wasActive) { Start-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction Stop }
        } catch { $rollbackErrors += "restoring previous task: $($_.Exception.Message)" }
    }
    if ($followsFile -and $mutated) {
        # The restart of a task this shell cannot replace started an instance
        # on the new settings: stop it before the original restarts on the
        # restored ones.
        try {
            $current = Get-TaskRegistration $name
            # Another installer may have replaced the task during the health
            # wait: only the registration this run restarted is stopped.
            if ($current -and (Get-TaskOwnership $current) -ne $originalOwnership) {
                throw "task $name registration changed; refusing to stop it"
            }
            if ($current) {
                $started = @(Get-OwnedTaskProcesses $current -RequireSettled)
                Stop-ScheduledTask -TaskPath '\' -TaskName $name -ErrorAction SilentlyContinue
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
    if ($orphans.Count -and $mutated) {
        # Nothing registered restarts a shim whose task was removed: say so.
        Write-Warning ("the shim the removed task had left running (pid " + (($orphans | ForEach-Object ProcessId) -join ', ') +
            ") was stopped for the new registration, which failed; nothing starts it again until this command succeeds. " +
            "Fix the failure below and rerun this command") -WarningAction Continue
    }
    if ($rollbackErrors.Count) {
        $remaining = try {
            $task = Get-TaskRegistration $name
            if ($task) { "present (state $($task.State))" } else { 'absent' }
        } catch { "unreadable ($($_.Exception.Message))" }
        $state = if ($followsFile) {
            $settingsLine = if ($settingsRestored) { "$($c.EnvFile) holds its previous settings again" } else { "$($c.EnvFile) could not be put back and holds the new settings" }
            # Only a start Task Scheduler refused leaves it down; that same
            # start would be refused again, so no command is offered.
            $shimLine = if ($script:restartNotStarted) { " The shim is not running: it starts at your next logon." } else { '' }
            "Its registration was not changed, and $settingsLine.$shimLine"
        } else { 'Restore its previous registration manually if needed; password-logon tasks require credentials that task XML does not contain.' }
        Write-Warning "Rollback incomplete: $($rollbackErrors -join '; '). Task '$name' is $remaining. Original failure: $($failure.Exception.Message). $state" -WarningAction Continue
    } elseif ($mutated -or $replacementRegistered) {
        # Every rollback step succeeded: say what was put back.
        $restored = @("$($c.EnvFile) holds its previous settings again")
        if ($replacementRegistered) { $restored += $(if ($previousXml) { 'the previous registration was restored' } else { 'the new registration was removed' }) }
        if ($wasActive) { $restored += 'the previous shim was started again' }
        # Information, not a warning: nothing was left half-done.
        Write-Host ("Rolled back: " + ($restored -join ', ') + ".")
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
} elseif ($reenabled) {
    Write-Host "Native $kind shim autostart '$name' was disabled and is enabled again; it already serves these settings on port $port."
} else {
    Write-Host "Native $kind shim autostart '$name' is already registered and serving these settings on port $port; nothing changed."
}
