param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$c = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
$native = [string]$c.Native
$ownerExe = [string]$c.OwnerExe
$kind = [string]$c.Kind
$port = [int]$c.Port
$name = [string]$c.Name
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
function Remove-OwnedTask([string]$taskName) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    $captured = @()
    $snapshot = @(Get-CimInstance Win32_Process)
    foreach ($invocation in @(Get-RegisteredInvocation $task)) {
        if ($invocation.PythonScript) {
            $script = [regex]::Escape($invocation.PythonScript).Replace('\\', '[\\/]')
            $pattern = '^(?:"[^"]+"|\S+)\s+"?' + $script + '"?(\s|$)'
            $ports = '--port\s+"?(' + ($invocation.Ports -join '|') + ')"?(\s|$)'
            $captured += @($snapshot | Where-Object { $_.CommandLine -match $pattern -and $_.CommandLine -match $ports })
            continue
        }
        $pattern = '"?extractor-shim"?\s+"?' + [regex]::Escape($invocation.Kind) + '"?\s+"?--env-file"?\s+"?' + [regex]::Escape($invocation.EnvFile) + '"?(\s|$)'
        foreach ($process in @($snapshot | Where-Object { $_.CommandLine -match $pattern })) {
            $owned = $process.ExecutablePath -eq $invocation.Exe
            if (-not $owned) {
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" -ErrorAction SilentlyContinue
                $owned = $parent -and $parent.ExecutablePath -eq $invocation.Exe -and
                    $parent.CreationDate -le $process.CreationDate -and $parent.CommandLine -match $pattern
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
    # Capture children before stopping the wrapper: its exit can orphan them.
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    foreach ($process in @($captured | Sort-Object CreationDate -Descending)) { Stop-CapturedProcess $process }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw "could not remove $taskName" }
}
if ($c.Remove) {
    $names = @($name)
    if ($name -eq 'Pseudolife Claude Shim') { $names += 'Pseudolife Sonnet Shim' }
    foreach ($task in $names) {
        Remove-OwnedTask $task
    }
    Write-Host "Removed $name"
    return
}
$marker = [regex]::Escape([string]$c.EnvFile)
$nativePattern = "`"?extractor-shim`"?\s+`"?$kind`"?\s+`"?--env-file`"?\s+`"?$marker`"?(\s|$)"
$pythonPattern = "${kind}_shim\.py.*--port\s+$port(\s|$)"
$registered = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
$executables = @($native, $ownerExe) + @($registered.Actions | ForEach-Object Execute)
foreach ($oldAction in $registered.Actions) {
    # Read a previous registration's literal target without executing its code.
    if ($oldAction.Arguments -match '-EncodedCommand ([A-Za-z0-9+/=]+)') {
        try {
            $previous = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1]))
            if ($previous -match '(?m)^\$psi\.FileName = ''((?:[^'']|'''')*)''\r?$') {
                $executables += $Matches[1].Replace("''", "'")
            }
        } catch { }
    }
}
function Test-OwnedProcess($process) {
    if ($process.ExecutablePath -in $executables) { return $true }
    # A stable launcher may wait for a versioned runtime child. Follow only
    # that child's recorded parent, not another process with the same name.
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ParentProcessId)" -ErrorAction SilentlyContinue
    return ($parent -and $parent.ExecutablePath -in $executables -and
        $parent.CreationDate -le $process.CreationDate -and $parent.CommandLine -match $nativePattern)
}
$stale = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and (($_.CommandLine -match $nativePattern -and (Test-OwnedProcess $_)) -or
        $_.CommandLine -match $pythonPattern)
})
$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    if ($listener.OwningProcess -notin @($stale | ForEach-Object ProcessId)) {
        throw "port $port belongs to another process (pid $($listener.OwningProcess)); choose --port or stop its owner"
    }
}
$configArgs = @($c.ConfigArgs)
& $native @configArgs
if ($LASTEXITCODE -ne 0) { throw 'could not write native shim settings' }
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
Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description "Native $kind extractor CLI shim" -Force | Out-Null
if (-not (Get-ScheduledTask -TaskName $name)) { throw "task was not registered: $name" }
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
foreach ($old in $stale) {
    Stop-CapturedProcess $old
}
$deadline = (Get-Date).AddSeconds(15)
while ((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "port $port did not become free; task registered but not started" }
Start-ScheduledTask -TaskName $name
$deadline = (Get-Date).AddSeconds([int]$c.Timeout)
$verified = $false
while ((Get-Date) -lt $deadline) {
    $candidate = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($candidate.OwningProcess)" -ErrorAction SilentlyContinue
        if ($owner -and $owner.ExecutablePath -eq $ownerExe -and $owner.CommandLine -match $nativePattern) {
            try {
                $null = Invoke-WebRequest -UseBasicParsing -Uri "http://$($c.Host):$port/models" -TimeoutSec 2
                $verified = $true; break
            } catch { }
        }
    }
    Start-Sleep -Milliseconds 500
}
if (-not $verified) {
    $info = Get-ScheduledTaskInfo -TaskName $name
    throw "native $kind shim did not bind port $port (task result $($info.LastTaskResult), last run $($info.LastRunTime)); inspect its configured log"
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
Write-Host "Registered + started native $kind shim (port $port, pid $($candidate.OwningProcess))."
Write-Host "To reload $($c.EnvFile): stop and start scheduled task $name."
