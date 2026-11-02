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
function Get-RegisteredInvocation($task) {
    foreach ($item in $task.Actions) {
        $exe = $item.Execute
        $arguments = $item.Arguments
        if ($arguments -match '-EncodedCommand ([A-Za-z0-9+/=]+)') {
            try {
                $code = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
                if ($code -notmatch '(?m)^\$psi\.FileName = ''((?:[^'']|'''')*)''\r?$') { continue }
                $exe = $Matches[1].Replace("''", "'")
                if ($code -notmatch '(?m)^\$psi\.Arguments = ''((?:[^'']|'''')*)''\r?$') { continue }
                $arguments = $Matches[1].Replace("''", "'")
            } catch { continue }
        }
        if ($arguments -match '^"?extractor-shim"?\s+"?(claude|codex)"?\s+"?--env-file"?\s+"([^"]+)"') {
            [PSCustomObject]@{ Exe=$exe; Kind=$Matches[1]; EnvFile=$Matches[2] }
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
        } elseif ($arguments -match '^"?(.+?[\\/]evals[\\/](?:claude|codex)_shim\.py)"?\s+') {
            $script = $Matches[1]
            if ($arguments -match '--port\s+"?(\d+)"?(\s|$)') {
                [PSCustomObject]@{ PythonScript=$script; Ports=@([int]$Matches[1]) }
            }
        }
    }
}
function Stop-CapturedProcess($old) {
    $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($old.ProcessId)" -ErrorAction SilentlyContinue
    if ($current -and $current.CreationDate -eq $old.CreationDate -and $current.CommandLine -eq $old.CommandLine) {
        Stop-Process -Id $old.ProcessId -Force
    }
}
function Get-OwnedTaskProcesses($task) {
    if (-not $task) { return }
    $captured = @()
    $snapshot = @(Get-CimInstance Win32_Process)
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
            $owned = Test-SamePath $process.ExecutablePath $invocation.Exe
            if (-not $owned) {
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" -ErrorAction SilentlyContinue
                $owned = $parent -and (Test-SamePath $parent.ExecutablePath $invocation.Exe) -and
                    $parent.CreationDate -le $process.CreationDate -and (Test-NativeInvocation $parent.CommandLine $invocation.Kind $invocation.EnvFile)
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
function Remove-OwnedTask([string]$taskName) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    $script:removedRegistration = $true
    $captured = @(Get-OwnedTaskProcesses $task)
    # Capture children before stopping the wrapper: its exit can orphan them.
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    foreach ($process in @($captured | Sort-Object CreationDate -Descending)) { Stop-CapturedProcess $process }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw "could not remove $taskName" }
}
if ($c.Remove) {
    $script:removedRegistration = $false
    $names = @($name)
    if ($name -eq 'Pseudolife Claude Shim') { $names += 'Pseudolife Sonnet Shim' }
    foreach ($task in $names) {
        Remove-OwnedTask $task
    }
    if ($script:removedRegistration) { Write-Host "Removed $name" }
    return
}
$registered = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
$legacy = if ($name -eq 'Pseudolife Claude Shim') { Get-ScheduledTask -TaskName 'Pseudolife Sonnet Shim' -ErrorAction SilentlyContinue } else { $null }
$legacyProcesses = @(Get-OwnedTaskProcesses $legacy)
$legacyWasActive = $legacy -and ($legacy.State -eq 'Running' -or $legacyProcesses.Count -gt 0)
# Only the selected registration can establish ownership. A matching basename
# and port from another checkout is a foreign listener, never a replacement.
$registeredProcesses = @(Get-OwnedTaskProcesses $registered)
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
$inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = '$($native.Replace("'", "''"))'
`$psi.Arguments = '$($argument.Replace("'", "''"))'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$process = [System.Diagnostics.Process]::Start(`$psi)
`$process.WaitForExit()
exit `$process.ExitCode
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded"
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType ([string]$c.LogonType)
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
$mutated = $false
$unchanged = $false
try {
    $configArgs = @($c.ConfigArgs)
    & $native @configArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'could not write native shim settings' }
    $sameTask = $registered -and @($registered.Actions).Count -eq 1 -and
        $registered.Actions[0].Execute -eq $action.Execute -and $registered.Actions[0].Arguments -eq $action.Arguments -and
        [string]$registered.Principal.LogonType -eq [string]$c.LogonType
    $unchanged = $sameTask -and (Test-HealthyListener)
    if (-not $unchanged) {
        $mutated = $true
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($legacy) { Stop-ScheduledTask -TaskName $legacy.TaskName -ErrorAction SilentlyContinue }
        foreach ($old in $stale) { Stop-CapturedProcess $old }
        $deadline = (Get-Date).AddSeconds(15)
        while ((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
        if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "port $port did not become free; restoring the previous registration" }
        Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description "Native $kind extractor CLI shim" -Force | Out-Null
        if (-not (Get-ScheduledTask -TaskName $name)) { throw "task was not registered: $name" }
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
    if ($mutated) {
        try { Remove-OwnedTask $name }
        catch { $rollbackErrors += "removing replacement task: $($_.Exception.Message)" }
    }
    try {
        if ($configExisted) { [IO.File]::WriteAllBytes($c.EnvFile, [byte[]]$configBefore) }
        elseif (Test-Path -LiteralPath $c.EnvFile) { Remove-Item -LiteralPath $c.EnvFile }
    } catch { $rollbackErrors += "restoring settings: $($_.Exception.Message)" }
    if ($mutated -and $previousXml) {
        try {
            Register-ScheduledTask -TaskName $name -Xml $previousXml -Force | Out-Null
            if ($wasActive) { Start-ScheduledTask -TaskName $name }
        } catch { $rollbackErrors += "restoring previous task: $($_.Exception.Message)" }
    }
    if ($mutated -and $legacyWasActive) {
        try { Start-ScheduledTask -TaskName $legacy.TaskName }
        catch { $rollbackErrors += "restarting legacy task: $($_.Exception.Message)" }
    }
    if ($rollbackErrors.Count) {
        $remaining = try {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
            if ($task) { "present (state $($task.State))" } else { 'absent' }
        } catch { 'absent or unreadable' }
        Write-Warning "Rollback incomplete: $($rollbackErrors -join '; '). Task '$name' is $remaining. Restore its previous registration manually if needed; password-logon tasks require credentials that task XML does not contain. Original failure: $($failure.Exception.Message)" -WarningAction Continue
    }
    throw $failure
}
if ($name -eq 'Pseudolife Claude Shim') {
    Remove-OwnedTask 'Pseudolife Sonnet Shim'
}
if ($c.ReplaceSibling) {
    $siblings = if ($kind -eq 'claude') { @('Pseudolife Codex Shim') } else { @('Pseudolife Claude Shim', 'Pseudolife Sonnet Shim') }
    foreach ($sibling in $siblings) {
        if ($sibling -ne $name) {
            Remove-OwnedTask $sibling
        }
    }
}
if (-not $unchanged) {
    Write-Host "Registered + started native $kind shim (port $port, pid $script:verifiedPid)."
    Write-Host "To reload $($c.EnvFile): stop and start scheduled task $name."
}
