param([string]$Source, [string]$Root, [ValidateSet('PasswordRollback','LegacyMigration','LegacyJuly','LegacyDetached','DelayedStop','DelayedRemoval','DeleteRetry','DeleteFailure','StopTimeout','RemovalTimeout','ProcessExitRace','ProcessStopFailure','ProcessIdentityChanged','QueryInitialRetry','QueryRollbackRetry','QueryRemovalRetry','QueryFailure','QueryAccessDenied','QueryNamedAbsent','DisabledReinstall','QueryStopFailure','QueryRemovalFailure','ForeignRemoval','ForeignLegacy','ForeignSibling','ForeignUninstallLegacy','ForeignNativeArguments','CustomLauncherRemoval','ForeignEncodedArguments','ForeignNativeBasename','DetachedClaude','DetachedCodex','DetachedCodexCli','LateLauncherChild','ProcessExitWait','ProcessExitTimeout','ProcessQueryFailure')][string]$Case)
$ErrorActionPreference = 'Stop'
# Every scheduler/process operation is mocked; these cases need no credentials,
# real registrations, listeners or process termination.
$directory = Join-Path $Root $Case
$null = New-Item -ItemType Directory -Path $directory -Force
$settings = Join-Path $directory 'settings.env'
$global:r6settings = $settings
[IO.File]::WriteAllText($settings, 'before')
$writer = Join-Path $directory 'writer.ps1'
[IO.File]::WriteAllText($writer, "[IO.File]::WriteAllText('$($settings.Replace("'", "''"))', 'after'); `$global:LASTEXITCODE=0")
$global:r6fixtureTask = $null
$global:r6legacyTask = $null
$global:r6siblingTask = $null
$global:r6listening = $false
$global:r6stoppedLegacy = $false
$global:r6restartedLegacy = $false
$global:r6warningText = ''
$global:r6clock = [datetime]'2026-01-01'
$global:r6stopPending = 0
$global:r6removePending = 0
$global:r6stopWaits = 0
$global:r6removeWaits = 0
$global:r6deleteAttempts = 0
$global:r6registered = $false
$global:r6stoppedPids = @()
$global:r6oldProcess = $null
$global:r6newProcess = $null
$global:r6queryFaults = 0
$global:r6startupFailed = $false
$global:r6enabled = $false
if ($Case -in @('ProcessExitWait','ProcessExitTimeout','ProcessQueryFailure')) {
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    $stopper = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-CapturedProcess' }, $true)
    Invoke-Expression $stopper.Extent.Text
    $old = [PSCustomObject]@{ProcessId=4242;CreationDate=[datetime]'2026-01-01';CommandLine='fixture-native --owned'}
    $global:r6exitWaited=$false
    function Get-CimInstance {
        [CmdletBinding()]
        param($ClassName,$Filter)
        if ($Case -eq 'ProcessQueryFailure') { Write-Error 'fixture process query failed'; return }
        $old
    }
    function Stop-Process {
        param($Id,[switch]$PassThru)
        if ($Id -ne 4242) { throw 'wrong process stopped' }
        $process=[PSCustomObject]@{}
        $process | Add-Member ScriptMethod WaitForExit {
            param($timeout)
            if ($timeout -ne 15000) { throw 'process exit wait changed its deadline' }
            $global:r6exitWaited=$true
            return $Case -ne 'ProcessExitTimeout'
        }
        if ($PassThru) { $process }
    }
    $caught=''
    try { Stop-CapturedProcess $old } catch { $caught=$_.Exception.Message }
    if ($Case -eq 'ProcessQueryFailure') {
        if ($caught -ne 'fixture process query failed' -or $global:r6exitWaited) { throw 'process query failure was silently treated as an exit' }
    } else {
        if (-not $global:r6exitWaited) { throw 'process termination returned before waiting for exit' }
        if ($Case -eq 'ProcessExitWait' -and $caught) { throw "process exit wait failed: $caught" }
        if ($Case -eq 'ProcessExitTimeout' -and $caught -ne 'process 4242 did not exit after termination') { throw "process exit timeout was lost: $caught" }
    }
    Write-Output "$Case passed without real scheduler operations"
    return
}
if ($Case -eq 'LateLauncherChild') {
    # The waiting launcher can create its runtime after the first process
    # snapshot, immediately before the scheduler stops its outer wrapper.
    $native = Join-Path $directory 'launcher.exe'
    $ownerExe = Join-Path $directory 'runtime.exe'
    $kind = 'codex'
    $arguments = '"extractor-shim" "codex" "--env-file" "' + $settings + '"'
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    foreach ($functionName in @('Test-SamePath','Test-NativeInvocation','Get-NativeTaskCode','Get-RegisteredInvocation','Assert-ShimRegistration','Get-OwnedTaskProcesses','Stop-CapturedProcess','Get-TaskRegistration','Wait-TaskStopped','Remove-OwnedTask')) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        Invoke-Expression $definition.Extent.Text
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes((Get-NativeTaskCode $native $arguments)))
    $global:r6fixtureTask = [PSCustomObject]@{TaskName='fixture-late';State='Running';Actions=@([PSCustomObject]@{Execute='powershell.exe';Arguments="-EncodedCommand $encoded"})}
    $global:r6lateLauncher = [PSCustomObject]@{ProcessId=4242;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$native;CommandLine=('"' + $native + '" ' + $arguments)}
    $global:r6lateRuntime = $null
    $global:r6foreignRuntime = [PSCustomObject]@{ProcessId=4444;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$ownerExe;CommandLine=('"' + $ownerExe + '" ' + $arguments.Replace($settings, $settings + '.other'))}
    $global:r6foreignImage = [PSCustomObject]@{ProcessId=4445;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=($ownerExe + '.other');CommandLine=('"' + $ownerExe + '.other" ' + $arguments)}
    function Get-CimInstance {
        param($ClassName,$Filter)
        $all=@($global:r6lateLauncher,$global:r6lateRuntime,$global:r6foreignRuntime,$global:r6foreignImage) | Where-Object { $_ }
        if ($Filter -match 'ProcessId = (\d+)') { $all | Where-Object ProcessId -eq ([int]$Matches[1]) }
        else { $all }
    }
    function Get-ScheduledTask { $global:r6fixtureTask }
    function Stop-ScheduledTask {
        $global:r6lateRuntime=[PSCustomObject]@{ProcessId=4343;ParentProcessId=4242;CreationDate=[datetime]'2026-01-01T00:00:01';ExecutablePath=$ownerExe;CommandLine=('"' + $ownerExe + '" ' + $arguments)}
        $global:r6fixtureTask.State='Ready'
    }
    function Stop-Process {
        param($Id)
        $global:r6stoppedPids += $Id
        if ($Id -eq 4242) { $global:r6lateLauncher=$null }
        elseif ($Id -eq 4343) { $global:r6lateRuntime=$null }
        else { throw 'foreign process was stopped' }
    }
    function Unregister-ScheduledTask { param($TaskName,[switch]$Confirm) $global:r6fixtureTask=$null }
    function Start-Sleep { }
    Remove-OwnedTask 'fixture-late'
    if ($global:r6lateRuntime -or $global:r6lateLauncher -or $global:r6fixtureTask) { throw 'late launcher child survived successful task removal' }
    if (($global:r6stoppedPids | Sort-Object) -join ',' -ne '4242,4343') { throw 'removal did not stop precisely the launcher and its late runtime' }
    Write-Output "$Case passed without real scheduler operations"
    return
}
if ($Case -in @('DetachedClaude','DetachedCodex','DetachedCodexCli')) {
    # Actual detached actions immediately before 5de8b597f introduced the
    # ops/.env runner. Keep both providers' flags and quoting from that source.
    $repo = Join-Path $directory 'old checkout'
    $PythonExe = Join-Path $repo '.venv\Scripts\python.exe'
    $Port = 12345
    $LogFile = Join-Path $directory 'shim.log'
    if ($Case -eq 'DetachedClaude') {
        $provider = 'claude'
        $Model = 'claude-opus-5-5'
        $promptPath = Join-Path $repo 'evals\prompts\sonnet_extractor_v5.md'
        $innerCmd = "`"$PythonExe`" `"$repo\evals\claude_shim.py`" --port $Port " +
                    "--model $Model --system-prompt-file `"$promptPath`""
        $historicalName = 'Pseudolife Claude Shim'
    } else {
        $provider = 'codex'
        $Model = 'gpt-5.6-terra'
        $HealthTtl = 1800
        $CodexCli = if ($Case -eq 'DetachedCodexCli') { Join-Path $directory 'provider stub\codex.exe' } else { '' }
        $innerCmd = "`"$PythonExe`" `"$repo\evals\codex_shim.py`" --port $Port " +
                    "--model $Model --health-ttl $HealthTtl"
        if ($CodexCli) { $innerCmd += " --cli `"$CodexCli`"" }
        $historicalName = 'Pseudolife Codex Shim'
    }
    $cmdArgs = "/c `"$innerCmd >> `"`"$LogFile`"`" 2>&1`""
    $inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = 'cmd.exe'
`$psi.Arguments = '$($cmdArgs -replace "'", "''")'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$psi.WorkingDirectory = '$repo'
[System.Diagnostics.Process]::Start(`$psi) | Out-Null
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
    $task = [PSCustomObject]@{TaskName=$historicalName;Actions=@([PSCustomObject]@{Execute='powershell.exe';Arguments="-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"})}
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    foreach ($functionName in @('Test-SamePath','Get-NativeTaskCode','Get-RegisteredInvocation','Get-OwnedTaskProcesses')) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        Invoke-Expression $definition.Extent.Text
    }
    $invocations = @(Get-RegisteredInvocation $task)
    $expectedScript = "$repo\evals\${provider}_shim.py"
    if ($invocations.Count -ne 1 -or $invocations[0].PythonScript -ne $expectedScript -or ($invocations[0].Ports -join ',') -ne "$Port") { throw "$Case did not identify the historical script and port exactly" }
    $global:r6historicalProcess = [PSCustomObject]@{ProcessId=4242;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$PythonExe;CommandLine=$innerCmd}
    $global:r6otherCheckout = $global:r6historicalProcess.PSObject.Copy()
    $global:r6otherCheckout.ProcessId = 4243
    $global:r6otherCheckout.CommandLine = $innerCmd.Replace($repo, "$repo-other")
    $global:r6otherPort = $global:r6historicalProcess.PSObject.Copy()
    $global:r6otherPort.ProcessId = 4244
    $global:r6otherPort.CommandLine = $innerCmd.Replace('--port 12345', '--port 12346')
    function Get-CimInstance { @($global:r6historicalProcess, $global:r6otherCheckout, $global:r6otherPort) }
    $owned = @(Get-OwnedTaskProcesses $task)
    if (($owned.ProcessId -join ',') -ne '4242') { throw "$Case did not isolate the historical listener from other checkouts and ports" }
    Write-Output "$Case passed without real scheduler operations"
    return
}
if ($Case -in @('ProcessExitRace','ProcessStopFailure','ProcessIdentityChanged')) {
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    $stopper = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-CapturedProcess' }, $true)
    Invoke-Expression $stopper.Extent.Text
    $old = [PSCustomObject]@{ProcessId=4242;CreationDate=[datetime]'2026-01-01';CommandLine='fixture-native --owned'}
    $global:r6currentProcess = $old.PSObject.Copy()
    function Get-CimInstance { $global:r6currentProcess }
    function Stop-Process {
        param($Id)
        $global:r6stoppedPids += $Id
        $global:r6currentProcess=$null
        if ($Case -eq 'ProcessExitRace') {
            throw [Management.Automation.ErrorRecord]::new([ArgumentException]::new('fixture process already exited'), 'NoProcessFoundForGivenId,Microsoft.PowerShell.Commands.StopProcessCommand', [Management.Automation.ErrorCategory]::ObjectNotFound, $Id)
        }
        throw [Management.Automation.ErrorRecord]::new([UnauthorizedAccessException]::new('fixture stop access denied'), 'CouldNotStopProcess,Microsoft.PowerShell.Commands.StopProcessCommand', [Management.Automation.ErrorCategory]::PermissionDenied, $Id)
    }
    if ($Case -eq 'ProcessIdentityChanged') {
        $global:r6currentProcess.CreationDate=$old.CreationDate.AddSeconds(1)
        Stop-CapturedProcess $old
        $global:r6currentProcess=$old.PSObject.Copy()
        $global:r6currentProcess.CommandLine='fixture-foreign --unrelated'
        Stop-CapturedProcess $old
        $global:r6currentProcess=$null
        Stop-CapturedProcess $old
        if ($global:r6stoppedPids.Count) { throw 'changed or absent process identity was stopped' }
    } else {
        $caught=''
        try { Stop-CapturedProcess $old } catch { $caught=$_.Exception.Message }
        if ($Case -eq 'ProcessExitRace' -and $caught) { throw "process exit race was not tolerated: $caught" }
        if ($Case -eq 'ProcessStopFailure' -and $caught -ne 'fixture stop access denied') { throw "process stop failure was lost: $caught" }
        if (($global:r6stoppedPids -join ',') -ne '4242') { throw 'stop did not target precisely the captured process' }
    }
    Write-Output "$Case passed without real process operations"
    return
}
$taskName = 'fixture-native'
if ($Case -in @('CustomLauncherRemoval','ForeignEncodedArguments')) {
    $custom = if ($Case -eq 'ForeignEncodedArguments') { $writer } else { Join-Path $directory 'custom-launcher.exe' }
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = '$($custom.Replace("'", "''"))'
`$psi.Arguments = '$($arguments.Replace("'", "''"))'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$process = [System.Diagnostics.Process]::Start(`$psi)
`$process.WaitForExit()
exit `$process.ExitCode
"@
    if ($Case -eq 'ForeignEncodedArguments') { $inner += "`nWrite-Output 'unrelated action'" }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
    $global:r6fixtureTask = [PSCustomObject]@{TaskName=$taskName;State='Ready';Actions=@([PSCustomObject]@{Execute='powershell.exe';Arguments="-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded"})}
    $global:r6oldProcess = [PSCustomObject]@{ProcessId=4242;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$custom;CommandLine=('"' + $custom + '" ' + $arguments)}
    $expectedFailure = 'not a recognized shim registration'
} elseif ($Case -eq 'ForeignUninstallLegacy') {
    $taskName = 'Pseudolife Claude Shim'
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Ready'; Actions=@([PSCustomObject]@{Execute=$writer;Arguments=('extractor-shim claude --env-file "' + $settings + '"')}) }
    $global:r6legacyTask = [PSCustomObject]@{ TaskName='Pseudolife Sonnet Shim'; State='Ready'; Actions=@([PSCustomObject]@{Execute='foreign.exe';Arguments='--unrelated'}) }
    $expectedFailure = 'not a recognized shim registration'
} elseif ($Case -in @('ForeignNativeArguments','ForeignNativeBasename')) {
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Ready'; Actions=@([PSCustomObject]@{Execute='foreign.exe';Arguments=('extractor-shim claude --env-file "' + $settings + '"')}) }
    $expectedFailure = 'not a recognized shim registration'
    if ($Case -eq 'ForeignNativeBasename') { $global:r6fixtureTask.Actions[0].Execute = Join-Path $directory 'foreign/pseudolife-mcp.exe' }
} elseif ($Case -eq 'ForeignRemoval') {
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Ready'; Actions=@([PSCustomObject]@{Execute='foreign.exe';Arguments='--unrelated'}) }
    $expectedFailure = 'not a recognized shim registration'
} elseif ($Case -eq 'ForeignSibling') {
    $global:r6siblingTask = [PSCustomObject]@{ TaskName='Pseudolife Codex Shim'; State='Ready'; Actions=@([PSCustomObject]@{Execute='foreign.exe';Arguments='--unrelated'}) }
    $expectedFailure = 'not a recognized shim registration'
} elseif ($Case -eq 'ForeignLegacy') {
    $taskName = 'Pseudolife Claude Shim'
    $global:r6legacyTask = [PSCustomObject]@{ TaskName='Pseudolife Sonnet Shim'; State='Ready'; Actions=@([PSCustomObject]@{Execute='foreign.exe';Arguments='--unrelated'}) }
    $expectedFailure = 'not a recognized shim registration'
} elseif ($Case -eq 'PasswordRollback') {
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Ready'; Principal=[PSCustomObject]@{LogonType='Password'}; Actions=@([PSCustomObject]@{Execute=$writer;Arguments=('extractor-shim claude --env-file "' + $settings + '"')}) }
    $expectedFailure = 'original fixture startup failure'
} elseif ($Case -in @('LegacyMigration','LegacyJuly','LegacyDetached')) {
    $taskName = 'Pseudolife Claude Shim'
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $global:r6legacyTask = [PSCustomObject]@{ TaskName='Pseudolife Sonnet Shim'; State='Running'; Actions=@([PSCustomObject]@{Execute=$writer;Arguments=$arguments}) }
    $global:r6legacyProcess = [PSCustomObject]@{ ProcessId=4242; ParentProcessId=1; CreationDate=[datetime]'2026-01-01'; ExecutablePath=$writer; CommandLine=('"' + $writer + '" ' + $arguments) }
    if ($Case -ne 'LegacyMigration') {
        $repo = Join-Path $directory 'old checkout'
        $PythonExe = Join-Path $repo '.venv\Scripts\python.exe'
        $promptPath = Join-Path $repo 'evals\prompts\sonnet_extractor_v1.md'
        $LogFile = Join-Path $directory 'sonnet-shim.log'
        $Port = 12345
        # Historical installer action templates: 1cb8b3697 (July) and
        # 2f0edc4ce^ (detached cmd.exe wrapper). No template is executed.
        if ($Case -eq 'LegacyJuly') {
            $inner = "& '$PythonExe' '$repo\evals\sonnet_shim.py' --port $Port " +
                     "--system-prompt-file '$promptPath' *>> '$LogFile'"
        } else {
            $innerCmd = "`"$PythonExe`" `"$repo\evals\sonnet_shim.py`" --port $Port " +
                        "--system-prompt-file `"$promptPath`""
            $cmdArgs = "/c `"$innerCmd >> `"`"$LogFile`"`" 2>&1`""
            $inner = @"
`$psi = New-Object System.Diagnostics.ProcessStartInfo
`$psi.FileName = 'cmd.exe'
`$psi.Arguments = '$($cmdArgs -replace "'", "''")'
`$psi.UseShellExecute = `$false
`$psi.CreateNoWindow = `$true
`$psi.WorkingDirectory = '$repo'
[System.Diagnostics.Process]::Start(`$psi) | Out-Null
"@
        }
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
        $global:r6legacyTask.Actions = @([PSCustomObject]@{Execute='powershell.exe';Arguments="-NoProfile -WindowStyle Hidden -EncodedCommand $encoded"})
        $global:r6legacyProcess.ExecutablePath = $PythonExe
        $global:r6legacyProcess.CommandLine = "`"$PythonExe`" `"$repo\evals\sonnet_shim.py`" --port $Port --system-prompt-file `"$promptPath`""
    }
    $global:r6listening = $true
    $expectedFailure = 'original fixture registration failure'
} elseif ($Case -in @('DelayedStop','StopTimeout','QueryInitialRetry','QueryFailure','QueryAccessDenied','DisabledReinstall','QueryStopFailure')) {
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Running'; Principal=[PSCustomObject]@{LogonType='S4U'}; Actions=@([PSCustomObject]@{Execute=$writer;Arguments=$arguments}) }
    $global:r6oldProcess = [PSCustomObject]@{ ProcessId=4242; ParentProcessId=1; CreationDate=[datetime]'2026-01-01'; ExecutablePath=$writer; CommandLine=($writer + ' ' + $arguments) }
    $global:r6listening = $true
    $expectedFailure = "task $taskName did not stop; inspect its scheduler state"
    if ($Case -eq 'DisabledReinstall') { $global:r6fixtureTask | Add-Member Settings ([PSCustomObject]@{Enabled=$false}) }
    if ($Case -eq 'QueryStopFailure') { $expectedFailure = 'could not confirm task fixture-native stopped: fixture task query failed; inspect its scheduler state' }
    if ($Case -in @('QueryFailure','QueryAccessDenied')) { $expectedFailure = 'fixture task query failed' }
} else {
    $expectedFailure = 'original fixture startup failure'
}
function Get-ScheduledTask {
    [CmdletBinding()]
    param($TaskName)
    $fault = ($Case -eq 'QueryStopFailure' -and $global:r6stopPending -gt 0) -or ($Case -eq 'QueryRemovalFailure' -and $global:r6deleteAttempts -gt 0) -or $Case -in @('QueryFailure','QueryAccessDenied','QueryNamedAbsent') -or (
        $global:r6queryFaults -lt 2 -and (
            $Case -eq 'QueryInitialRetry' -or
            ($Case -eq 'QueryRollbackRetry' -and $global:r6startupFailed) -or
            ($Case -eq 'QueryRemovalRetry' -and $global:r6deleteAttempts -gt 0)))
    if ($fault) {
        $global:r6queryFaults++
        $id = if ($Case -eq 'QueryNamedAbsent') { 'CmdletizationQuery_NotFound_TaskName' }
              elseif ($Case -eq 'QueryAccessDenied') { 'HRESULT 0x80070005' }
              else { 'HRESULT 0x80070002' }
        $record = [Management.Automation.ErrorRecord]::new([InvalidOperationException]::new('fixture task query failed'), $id, [Management.Automation.ErrorCategory]::NotSpecified, $TaskName)
        $PSCmdlet.WriteError($record)
        return
    }
    if ($TaskName -eq 'Pseudolife Codex Shim') { return $global:r6siblingTask }
    if ($TaskName -eq 'Pseudolife Sonnet Shim') { return $global:r6legacyTask }
    if ($global:r6stopPending -gt 0) {
        $global:r6stopWaits++
        if ($Case -ne 'StopTimeout') { $global:r6stopPending-- }
        if ($global:r6stopPending -eq 0) { $global:r6fixtureTask.State='Ready' }
    }
    if ($global:r6removePending -gt 0) {
        $global:r6removeWaits++
        if ($Case -ne 'RemovalTimeout') { $global:r6removePending-- }
        if ($global:r6removePending -eq 0) { $global:r6fixtureTask=$null }
    }
    return $global:r6fixtureTask
}
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Case -in @('LegacyMigration','LegacyJuly','LegacyDetached')) { return $global:r6legacyProcess }
    if ($Filter -and $Filter -match 'ProcessId = (\d+)') {
        if ($global:r6oldProcess -and [int]$Matches[1] -eq $global:r6oldProcess.ProcessId) { return $global:r6oldProcess }
        if ($global:r6newProcess -and [int]$Matches[1] -eq $global:r6newProcess.ProcessId) { return $global:r6newProcess }
        return
    }
    if ($global:r6oldProcess) { $global:r6oldProcess }
    if ($global:r6newProcess) { $global:r6newProcess }
}
function Get-NetTCPConnection {
    if ($global:r6listening) { [PSCustomObject]@{OwningProcess=$(if ($global:r6newProcess) { 4343 } else { 4242 })} }
}
function Stop-Process {
    param($Id)
    $global:r6stoppedPids += $Id
    $global:r6listening=$false
    if ($global:r6oldProcess -and $global:r6oldProcess.ProcessId -eq $Id) { $global:r6oldProcess=$null }
    if ($global:r6newProcess -and $global:r6newProcess.ProcessId -eq $Id) { $global:r6newProcess=$null }
    if ($global:r6legacyProcess -and $global:r6legacyProcess.ProcessId -eq $Id) { $global:r6legacyProcess=$null }
}
function Get-Date { $global:r6clock }
function Start-Sleep { param($Milliseconds) $global:r6clock=$global:r6clock.AddMilliseconds($Milliseconds) }
function New-ScheduledTaskAction {
    param($Execute,$Argument)
    $action = [PSCustomObject]@{Execute=$Execute;Arguments=$Argument}
    if ($Case -eq 'DisabledReinstall') { $global:r6fixtureTask.Actions=@($action) }
    $action
}
function Enable-ScheduledTask { param($TaskName) $global:r6enabled=$true; $global:r6fixtureTask.Settings.Enabled=$true }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType) [PSCustomObject]@{LogonType=$LogonType} }
function New-ScheduledTaskTrigger { }
function New-ScheduledTaskSettingsSet { }
function Export-ScheduledTask { '<Task><LogonType>Password</LogonType></Task>' }
function Stop-ScheduledTask {
    param($TaskName)
    if ($TaskName -eq 'Pseudolife Sonnet Shim') { $global:r6stoppedLegacy=$true; $global:r6listening=$false; $global:r6legacyTask.State='Ready'; return }
    if ($global:r6fixtureTask -and $global:r6fixtureTask.State -eq 'Running') { $global:r6stopPending=3 }
}
function Unregister-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [switch]$Confirm)
    $global:r6deleteAttempts++
    if ($Case -eq 'DeleteFailure' -or ($Case -eq 'DeleteRetry' -and $global:r6deleteAttempts -lt 3)) {
        Write-Error 'fixture delete rejected'
        return
    }
    if ($global:r6fixtureTask -and $global:r6fixtureTask.State -eq 'Running') { throw 'task unregistered before scheduler stopped' }
    if ($Case -in @('DelayedRemoval','RemovalTimeout','QueryRemovalRetry','QueryRemovalFailure')) { $global:r6removePending=3 }
    else { $global:r6fixtureTask=$null }
}
function Register-ScheduledTask {
    param($TaskName,$Action,$Principal,$Trigger,$Settings,$Description,$Xml,[switch]$Force)
    if ($Xml) { throw 'password credentials required' }
    if ($Case -in @('LegacyMigration','LegacyJuly','LegacyDetached')) { throw $expectedFailure }
    if ($global:r6fixtureTask -and $global:r6fixtureTask.State -eq 'Running') { throw 'replacement registered before scheduler stopped' }
    $global:r6registered=$true
    $global:r6fixtureTask = [PSCustomObject]@{TaskName=$TaskName;State='Ready';Actions=@($Action);Principal=$Principal}
}
function Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -eq 'Pseudolife Sonnet Shim') { $global:r6restartedLegacy=$true; return }
    if ($Case -in @('DelayedStop','QueryInitialRetry')) {
        $global:r6newProcess = [PSCustomObject]@{ ProcessId=4343; ParentProcessId=1; CreationDate=[datetime]'2026-01-02'; ExecutablePath=$writer; CommandLine=($writer + ' extractor-shim claude --env-file "' + $global:r6settings + '"') }
        $global:r6listening=$true
        return
    }
    if ($Case -in @('DelayedRemoval','RemovalTimeout')) { $global:r6fixtureTask.State='Running' }
    $global:r6startupFailed=$true
    throw $expectedFailure
}
function Invoke-WebRequest {
    if ($Case -notin @('DelayedStop','QueryInitialRetry','DisabledReinstall')) { throw 'unexpected HTTP call in scheduler failure fixture' }
}
function Write-Warning { param($Message) $global:r6warningText += $Message }
$config = Join-Path $directory 'registration.json'
@{ Native=$writer;OwnerExe=$writer;Kind='claude';Port=12345;Host='127.0.0.1';Name=$taskName;EnvFile=$settings;LogonType='S4U';Timeout=1;Remove=($Case -in @('QueryNamedAbsent','ForeignRemoval','ForeignUninstallLegacy','ForeignNativeArguments','CustomLauncherRemoval','ForeignEncodedArguments','ForeignNativeBasename'));ReplaceSibling=($Case -eq 'ForeignSibling');ConfigArgs=@();ServeArgs=@('extractor-shim','claude','--env-file',$settings) } |
    ConvertTo-Json | Set-Content -LiteralPath $config -Encoding UTF8
$caught = ''
try { & $Source -ConfigPath $config } catch { $caught = $_.Exception.Message }
if ($Case -eq 'CustomLauncherRemoval') {
    if ($caught -or $global:r6fixtureTask -or $global:r6deleteAttempts -ne 1 -or ($global:r6stoppedPids -join ',') -ne '4242') { throw "custom native launcher was not removed: $caught" }
    Write-Output "$Case passed without real scheduler operations"
    return
} elseif ($Case -in @('ForeignRemoval','ForeignLegacy','ForeignSibling','ForeignUninstallLegacy','ForeignNativeArguments','ForeignEncodedArguments','ForeignNativeBasename')) {
    if ($caught -notlike '*not a recognized shim registration*' -or $global:r6registered -or $global:r6deleteAttempts -or $global:r6stoppedLegacy -or $global:r6stoppedPids.Count -or [IO.File]::ReadAllText($settings) -ne 'before') { throw "foreign registration was changed or not refused: $caught" }
    Write-Output "$Case passed without real scheduler operations"
    return
} elseif ($Case -eq 'DisabledReinstall') {
    if ($caught -or -not $global:r6enabled -or -not $global:r6fixtureTask.Settings.Enabled -or $global:r6registered -or $global:r6deleteAttempts -or $global:r6stoppedPids.Count) { throw "healthy disabled task was not enabled without restart: $caught" }
    Write-Output "$Case passed without real scheduler operations"
    return
} elseif ($Case -in @('QueryFailure','QueryAccessDenied','QueryNamedAbsent')) {
    if ($Case -eq 'QueryNamedAbsent') {
        if ($caught) { throw "confirmed missing task was not a quiet no-op: $caught" }
    } elseif ($caught -ne $expectedFailure) { throw "task query failure was lost: $caught" }
    if ($global:r6registered -or $global:r6deleteAttempts -or $global:r6stoppedPids.Count -or [IO.File]::ReadAllText($settings) -ne 'before') { throw 'unconfirmed registration caused a mutation' }
    $elapsed=($global:r6clock - [datetime]'2026-01-01').TotalSeconds
    if ($Case -eq 'QueryFailure' -and ($elapsed -ne 15 -or $global:r6queryFaults -lt 2)) { throw 'persistent query failure was not bounded by its deadline' }
    $expectedQueries = if ($Case -eq 'QueryNamedAbsent') { 2 } else { 1 }
    if ($Case -ne 'QueryFailure' -and ($elapsed -ne 0 -or $global:r6queryFaults -ne $expectedQueries)) { throw 'nonretryable query result was retried' }
    Write-Output "$Case passed without real scheduler operations"
    return
} elseif ($Case -in @('DelayedStop','QueryInitialRetry')) {
    if ($caught) { throw "replacement failed: $caught" }
    if (-not $global:r6registered -or -not $global:r6listening -or $global:r6stopWaits -lt 3) { throw 'replacement did not wait for the delayed scheduler stop' }
    if (($global:r6stoppedPids -join ',') -ne '4242') { throw 'replacement did not stop precisely its captured process' }
} else {
    if ($caught -ne $expectedFailure) { throw "original failure was lost: $caught" }
    if ([IO.File]::ReadAllText($settings) -ne 'before') { throw 'settings were not restored' }
    if ($Case -notin @('StopTimeout','RemovalTimeout','DeleteFailure','QueryStopFailure','QueryRemovalFailure') -and $global:r6fixtureTask) { throw 'replacement task survived failed installation' }
}
if ($Case -eq 'QueryStopFailure') {
    if ($global:r6registered -or $global:r6deleteAttempts -or -not $global:r6fixtureTask) { throw 'unreadable stop state caused registration mutation' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -ne 45) { throw 'stop, rollback and diagnostic queries exceeded their three deadlines' }
} elseif ($Case -eq 'QueryRemovalFailure') {
    if ($global:r6deleteAttempts -ne 1 -or -not $global:r6fixtureTask -or $global:r6warningText -notlike '*could not confirm removal of task fixture-native*') { throw 'unreadable deletion state lost operation context or retried deletion' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -ne 30) { throw 'deletion query and diagnostic query exceeded their two deadlines' }
} elseif ($Case -eq 'DeleteRetry') {
    if ($global:r6deleteAttempts -ne 3 -or $global:r6warningText) { throw 'rejected deletion was not retried until accepted' }
} elseif ($Case -eq 'DeleteFailure') {
    if (-not $global:r6fixtureTask -or $global:r6warningText -notlike '*fixture delete rejected*' -or $global:r6warningText -notlike '*present*') { throw 'delete failure diagnostic or remaining state was lost' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -lt 15) { throw 'rejected deletion did not await its deadline' }
} elseif ($Case -eq 'DelayedRemoval') {
    if ($global:r6stopWaits -lt 3 -or $global:r6removeWaits -lt 3 -or $global:r6warningText) { throw 'rollback did not await delayed scheduler stop and removal' }
} elseif ($Case -eq 'StopTimeout') {
    if ($global:r6registered -or -not $global:r6fixtureTask -or $global:r6warningText -notlike '*did not stop*') { throw 'stop timeout did not preserve the failure and remaining task' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -lt 15) { throw 'stop timeout did not await its deadline' }
} elseif ($Case -eq 'RemovalTimeout') {
    if (-not $global:r6fixtureTask -or $global:r6warningText -notlike '*could not remove*' -or $global:r6warningText -notlike '*present*') { throw 'removal timeout did not report the surviving task' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -lt 15) { throw 'removal timeout did not await its deadline' }
} elseif ($Case -eq 'PasswordRollback') {
    if ($global:r6warningText -notlike '*password credentials required*' -or $global:r6warningText -notlike '*absent*') { throw "missing remaining-state diagnostic: $global:r6warningText" }
} elseif ($Case -in @('LegacyMigration','LegacyJuly','LegacyDetached') -and (-not $global:r6stoppedLegacy -or -not $global:r6restartedLegacy -or ($global:r6stoppedPids -join ',') -ne '4242')) {
    throw 'legacy registration was not recognized and restored'
}
if ($Case -in @('QueryInitialRetry','QueryRollbackRetry','QueryRemovalRetry') -and $global:r6queryFaults -ne 2) { throw 'transient query errors were not retried' }
if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -gt $(if ($Case -eq 'QueryStopFailure') { 45 } else { 31 })) { throw 'scheduler waits exceeded their bounded deadlines' }
Write-Output "$Case passed without real scheduler operations"
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
$formatter = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Format-ForeignCommand' }, $true)
Invoke-Expression $formatter.Extent.Text
foreach ($arguments in @('--token secret-value', '--api-key="secret-value"', '"--password" "secret-value with spaces"', '/credential:secret-value', '--header "Authorization: Bearer secret-value"', '-EncodedCommand secret-value')) {
    $command = 'python.exe "fixture/claude_shim.py" --port 12345 ' + $arguments
    $redacted = Format-ForeignCommand $command
    if ($redacted -like '*secret-value*' -or $redacted -notlike '*claude_shim.py*' -or $redacted -notlike '*redacted*') { throw 'foreign credential argument was not safely redacted' }
}
if ((Format-ForeignCommand 'python.exe fixture/claude_shim.py --port 12345') -ne 'python.exe fixture/claude_shim.py --port 12345') { throw 'ordinary command detail was lost' }
Write-Output 'Foreign command redaction passed'
