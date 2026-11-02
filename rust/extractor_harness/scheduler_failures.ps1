param([string]$Source, [string]$Root, [ValidateSet('PasswordRollback','LegacyMigration','LegacyJuly','LegacyDetached','DelayedStop','DelayedRemoval','DeleteRetry','DeleteFailure','StopTimeout','RemovalTimeout','ProcessExitRace','ProcessStopFailure','ProcessIdentityChanged','QueryInitialRetry','QueryRollbackRetry','QueryRemovalRetry','QueryFailure','QueryAccessDenied','QueryNamedAbsent','DisabledReinstall','QueryStopFailure','QueryRemovalFailure','ForeignRemoval','ForeignLegacy','ForeignSibling','ForeignUninstallLegacy','ForeignNativeArguments','CustomLauncherRemoval','ForeignEncodedArguments','ForeignNativeBasename','DetachedClaude','DetachedCodex','DetachedCodexCli','LateLauncherChild','ProcessExitWait','ProcessExitTimeout','ProcessQueryFailure','ProcessPidReused','ProcessPinnedExitRace','ProcessHandleIdentityChanged','ProcessHandleFailure','ProcessStartTimeFailure','InitialForeignTask','DeleteReplacedTask','TaskQueryFailure','StopQueryFailure','RemovalQueryFailure','RollbackQueryFailure','TaskAbsent','HistoricalTailBoundaries','LateOldRuntimeRecorded','LateLegacyUnseen','LateLegacyKnown','LateLegacyUnseenNoLauncher','CimSnapshotFailure','CimParentFailure','ReplacementDuringStop','LateCurrentRuntimeRecorded')][string]$Case)
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
$global:r6rollbackStarted = $false
$global:r6stoppedPids = @()
$global:r6oldProcess = $null
$global:r6newProcess = $null
$global:r6queryFaults = 0
$global:r6startupFailed = $false
$global:r6enabled = $false
$global:r6disposed = 0
$global:r6handlePinned = $false
function New-FixtureProcess($identity) {
    $process = [PSCustomObject]@{ StartTime=$identity.CreationDate; HasExited=$false; FixtureId=$identity.ProcessId }
    $process | Add-Member ScriptMethod get_Handle {
        if ($Case -eq 'ProcessHandleFailure') { throw [UnauthorizedAccessException]::new('fixture handle access denied') }
        $global:r6handlePinned=$true
        return [IntPtr]123
    }
    $process | Add-Member ScriptMethod get_StartTime {
        if (-not $global:r6handlePinned) { throw 'identity read before pinning the process handle' }
        if ($Case -eq 'ProcessStartTimeFailure') { throw [UnauthorizedAccessException]::new('fixture start time access denied') }
        if ($Case -eq 'ProcessPinnedExitRace') {
            $global:r6currentProcess=$global:r6currentProcess.PSObject.Copy()
            $global:r6currentProcess.CreationDate=$global:r6currentProcess.CreationDate.AddSeconds(1)
        }
        return $this.StartTime
    }
    $process | Add-Member ScriptMethod get_HasExited { return $this.HasExited }
    $process | Add-Member ScriptMethod Kill {
        if (-not $global:r6handlePinned) { throw 'process handle was not pinned before termination' }
        $global:r6stoppedPids += $this.FixtureId
        if ($Case -eq 'ProcessStopFailure') { throw [UnauthorizedAccessException]::new('fixture stop access denied') }
        $global:r6listening=$false
        foreach ($variable in @('r6oldProcess','r6newProcess','r6legacyProcess','r6lateLauncher','r6lateRuntime')) {
            $identity = Get-Variable -Name $variable -Scope Global -ValueOnly -ErrorAction SilentlyContinue
            if ($identity -and $identity.ProcessId -eq $this.FixtureId) { Set-Variable -Name $variable -Scope Global -Value $null }
        }
        if ($Case -eq 'ProcessExitRace') { $this.HasExited=$true; throw [InvalidOperationException]::new('fixture process already exited') }
    }
    $process | Add-Member ScriptMethod WaitForExit {
        param($timeout)
        if ($timeout -ne 15000) { throw 'process exit wait changed its deadline' }
        $global:r6exitWaited=$true
        return $Case -ne 'ProcessExitTimeout'
    }
    $process | Add-Member ScriptMethod Dispose { $global:r6disposed++ }
    return $process
}
# Any unexpected old implementation call is also mocked, including PID-based
# termination after the fixture has replaced the captured process.
function Stop-Process { param($Id,[switch]$PassThru) $global:r6stoppedPids += $Id; $global:r6listening=$false; if ($PassThru) { New-FixtureProcess ([PSCustomObject]@{ProcessId=$Id;CreationDate=[datetime]'2026-01-01'}) } }
if ($Case -in @('LateLauncherChild','LateOldRuntimeRecorded','LateLegacyUnseen','LateLegacyKnown','LateLegacyUnseenNoLauncher','LateCurrentRuntimeRecorded')) {
    # The waiting launcher can create its runtime after the first process
    # snapshot, immediately before the scheduler stops its outer wrapper.
    $native = Join-Path $directory 'launcher.exe'
    $ownerExe = Join-Path $directory 'runtime.exe'
    $oldRuntime = if ($Case -in @('LateLauncherChild','LateLegacyKnown')) { $ownerExe } else { Join-Path $directory 'old-version-runtime.exe' }
    $kind = 'codex'
    $arguments = '"extractor-shim" "codex" "--env-file" "' + $settings + '"'
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    foreach ($functionName in @('Test-SamePath','Test-NativeInvocation','Get-NativeTaskCode','Get-RegisteredInvocation','Assert-ShimRegistration','Get-OwnedTaskProcesses','Stop-CapturedProcess','Get-TaskRegistration','Get-TaskOwnership','Wait-TaskStopped','Remove-OwnedTask')) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        if ($definition) { Invoke-Expression $definition.Extent.Text }
    }
    $code = Get-NativeTaskCode $native $arguments
    if ($Case -in @('LateOldRuntimeRecorded','LateLauncherChild','LateCurrentRuntimeRecorded')) { $code = "`$runtimeExe = '$($oldRuntime.Replace("'", "''"))'`n" + $code }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($code))
    if ($Case -eq 'LateOldRuntimeRecorded') {
        $foreignCode = $code + "`nWrite-Output 'unrelated action'"
        $foreignEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($foreignCode))
        $foreignTask = [PSCustomObject]@{Actions=@([PSCustomObject]@{Execute='powershell.exe';Arguments="-EncodedCommand $foreignEncoded"})}
        if (@(Get-RegisteredInvocation $foreignTask).Count) { throw 'runtime metadata allowed unrelated encoded code to grant ownership' }
    }
    $global:r6fixtureTask = [PSCustomObject]@{TaskName='fixture-late';State='Running';Actions=@([PSCustomObject]@{Execute='powershell.exe';Arguments="-EncodedCommand $encoded"})}
    $global:r6lateLauncher = [PSCustomObject]@{ProcessId=4242;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$native;CommandLine=('"' + $native + '" ' + $arguments)}
    if ($Case -eq 'LateLegacyUnseenNoLauncher') { $global:r6lateLauncher=$null }
    $global:r6lateRuntime = $null
    $global:r6foreignRuntime = [PSCustomObject]@{ProcessId=4444;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$oldRuntime;CommandLine=('"' + $oldRuntime + '" ' + $arguments.Replace($settings, $settings + '.other'))}
    $global:r6foreignProvider = [PSCustomObject]@{ProcessId=4446;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=$oldRuntime;CommandLine=('"' + $oldRuntime + '" ' + $arguments.Replace('"codex"', '"claude"'))}
    $global:r6foreignImage = [PSCustomObject]@{ProcessId=4445;ParentProcessId=1;CreationDate=[datetime]'2026-01-01';ExecutablePath=($ownerExe + '.other');CommandLine=('"' + $ownerExe + '.other" ' + $arguments)}
    if ($Case -eq 'LateLegacyKnown') { $global:r6foreignImage=$null }
    function Get-CimInstance {
        param($ClassName,$Filter)
        $all=@($global:r6lateLauncher,$global:r6lateRuntime,$global:r6foreignRuntime,$global:r6foreignImage,$global:r6foreignProvider) | Where-Object { $_ }
        if ($Filter -match 'ProcessId = (\d+)') { $all | Where-Object ProcessId -eq ([int]$Matches[1]) }
        else { $all }
    }
    function Get-ScheduledTask { $global:r6fixtureTask }
    function Stop-ScheduledTask {
        $global:r6lateRuntime=[PSCustomObject]@{ProcessId=4343;ParentProcessId=4242;CreationDate=[datetime]'2026-01-01T00:00:01';ExecutablePath=$(if ($Case -eq 'LateCurrentRuntimeRecorded') { $ownerExe } else { $oldRuntime });CommandLine=('"' + $(if ($Case -eq 'LateCurrentRuntimeRecorded') { $ownerExe } else { $oldRuntime }) + '" ' + $arguments)}
        $global:r6fixtureTask.State='Ready'
    }
    function Get-Process {
        [CmdletBinding()] param($Id)
        $identity = @($global:r6lateLauncher,$global:r6lateRuntime) | Where-Object { $_ -and $_.ProcessId -eq $Id }
        if (-not $identity) { throw 'foreign process handle requested' }
        New-FixtureProcess $identity
    }
    function Unregister-ScheduledTask { param($TaskName,[switch]$Confirm) $global:r6fixtureTask=$null }
    function Start-Sleep { }
    $caught=''
    try { Remove-OwnedTask 'fixture-late' } catch { $caught=$_.Exception.Message }
    if ($Case -in @('LateLegacyUnseen','LateLegacyUnseenNoLauncher')) {
        $expectedStopped = if ($Case -eq 'LateLegacyUnseen') { '4242' } else { '' }
        if (-not $global:r6lateRuntime -or ($global:r6stoppedPids -join ',') -ne $expectedStopped) { throw 'unseen metadata-free runtime was guessed or killed' }
        if (-not $global:r6fixtureTask -or $caught -notlike '*unverified runtime*' -or $caught -notlike '*Get-CimInstance*' -or $caught -notlike '*retry*') { throw "unseen metadata-free runtime lost its registration or actionable refusal: $caught" }
        Write-Output "$Case passed without real scheduler operations"
        return
    }
    if ($caught) { throw $caught }
    if ($global:r6lateRuntime -or $global:r6lateLauncher -or $global:r6fixtureTask) { throw 'late launcher child survived successful task removal' }
    if (($global:r6stoppedPids | Sort-Object) -join ',' -ne '4242,4343') { throw 'removal did not stop precisely the launcher and its late runtime' }
    Write-Output "$Case passed without real scheduler operations"
    return
}
if ($Case -eq 'HistoricalTailBoundaries') {
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    foreach ($functionName in @('Test-SamePath','Get-NativeTaskCode','Get-RegisteredInvocation')) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        if ($definition) { Invoke-Expression $definition.Extent.Text }
    }
    # sonnet: 2f0edc4ce^; claude: 265527e79/568ca7ca3/d94b3768d/5de8b597f^;
    # codex: c74485593. Preserve each installer's actual cmd.exe tail.
    $tails = @{
        sonnet='--system-prompt-file "fixture prompt.md"'
        claude='--model claude-opus-5 --system-prompt-file "fixture prompt.md"'
        codex='--model gpt-5.6-terra --health-ttl 1800'
    }
    foreach ($provider in @('sonnet','claude','codex')) {
        foreach ($tailProvider in @('sonnet','claude','codex')) {
            $arguments='/c ""C:\fixture\python.exe" "C:\fixture\evals\' + $provider + '_shim.py" --port 12345 ' + $tails[$tailProvider] + ' >> ""fixture.log"" 2>&1"'
            $task=[PSCustomObject]@{Actions=@([PSCustomObject]@{Execute='cmd.exe';Arguments=$arguments})}
            $count=@(Get-RegisteredInvocation $task).Count
            if ($count -ne [int]($provider -eq $tailProvider)) { throw "$provider accepted the $tailProvider tail or refused its own installer tail" }
        }
        $arguments='/c ""C:\fixture\python.exe" "C:\fixture\evals\' + $provider + '_shim.py" --port 12345 ' + ($tails[$provider] -replace '--model [^ ]+ ', '') + ' >> ""fixture.log"" 2>&1"'
        if ($provider -ne 'sonnet' -and @(Get-RegisteredInvocation ([PSCustomObject]@{Actions=@([PSCustomObject]@{Execute='cmd.exe';Arguments=$arguments})})).Count) { throw "$provider accepted a missing model" }
    }
    # 2f0edc4ce emitted a BEL-corrupted anthropic path; that command could
    # never start the shim and ccb6f9a07 fixed it, so it grants no ownership.
    $arguments='/c ""C:\fixture\python.exe" "C:\fixture\evals\' + [char]7 + 'nthropic_shim.py" --port 12345 ' + $tails.claude + ' >> ""fixture.log"" 2>&1"'
    if (@(Get-RegisteredInvocation ([PSCustomObject]@{Actions=@([PSCustomObject]@{Execute='cmd.exe';Arguments=$arguments})})).Count) { throw 'corrupted historical path granted ownership' }
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
        if ($definition) { Invoke-Expression $definition.Extent.Text }
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
if ($Case -in @('CimSnapshotFailure','CimParentFailure')) {
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    foreach ($functionName in @('Test-SamePath','Test-NativeInvocation','Get-NativeTaskCode','Get-RegisteredInvocation','Get-OwnedTaskProcesses')) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        if ($definition) { Invoke-Expression $definition.Extent.Text }
    }
    $native='fixture-native.exe'
    $ownerExe=$native
    $arguments='extractor-shim claude --env-file "' + $settings + '"'
    $task=[PSCustomObject]@{Actions=@([PSCustomObject]@{Execute=$native;Arguments=$arguments})}
    function Get-CimInstance {
        [CmdletBinding()] param($ClassName,$Filter)
        if ($Case -eq 'CimSnapshotFailure' -or $Filter) { Write-Error 'fixture CIM query denied'; return }
        [PSCustomObject]@{ProcessId=4242;ParentProcessId=4241;CreationDate=[datetime]'2026-01-01';ExecutablePath='unknown-runtime.exe';CommandLine=('unknown-runtime.exe ' + $arguments)}
    }
    $caught=''
    try { $null=@(Get-OwnedTaskProcesses $task) } catch { $caught=$_.Exception.Message }
    if ($caught -notlike '*fixture CIM query denied*' -or $caught -notlike '*Get-CimInstance Win32_Process*' -or $caught -notlike '*retry the autostart command*') { throw "CIM query failure lost its actionable fix: $caught" }
    Write-Output "$Case passed without real process operations"
    return
}
if ($Case -like 'Process*') {
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    $stopper = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-CapturedProcess' }, $true)
    Invoke-Expression $stopper.Extent.Text
    $old = [PSCustomObject]@{ProcessId=4242;CreationDate=[datetime]'2026-01-01';CommandLine='fixture-native --owned'}
    $global:r6currentProcess = $old.PSObject.Copy()
    function Get-CimInstance {
        [CmdletBinding()] param($ClassName, $Filter)
        if ($Case -eq 'ProcessQueryFailure') { Write-Error 'fixture process query denied'; return }
        $current = $global:r6currentProcess
        if ($Case -eq 'ProcessPidReused') { $global:r6currentProcess=$old.PSObject.Copy(); $global:r6currentProcess.CreationDate=$old.CreationDate.AddSeconds(1) }
        return $current
    }
    function Get-Process {
        [CmdletBinding()] param($Id)
        $identity = $global:r6currentProcess
        if (-not $identity) { throw 'unexpected process handle lookup for absent process' }
        $process = New-FixtureProcess $identity
        if ($Case -eq 'ProcessHandleIdentityChanged') { $process.StartTime=$old.CreationDate.AddSeconds(1) }
        return $process
    }
    function Stop-Process {
        param($Id)
        $global:r6stoppedPids += $Id
        $global:r6currentProcess=$null
        if ($Case -eq 'ProcessExitRace') {
            throw [Management.Automation.ErrorRecord]::new([ArgumentException]::new('fixture process already exited'), 'NoProcessFoundForGivenId,Microsoft.PowerShell.Commands.StopProcessCommand', [Management.Automation.ErrorCategory]::ObjectNotFound, $Id)
        }
        if ($Case -eq 'ProcessStopFailure') { throw [Management.Automation.ErrorRecord]::new([UnauthorizedAccessException]::new('fixture stop access denied'), 'CouldNotStopProcess,Microsoft.PowerShell.Commands.StopProcessCommand', [Management.Automation.ErrorCategory]::PermissionDenied, $Id) }
        New-FixtureProcess $old
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
    } elseif ($Case -in @('ProcessPidReused','ProcessHandleIdentityChanged','ProcessQueryFailure','ProcessHandleFailure','ProcessStartTimeFailure')) {
        $caught=''
        try { Stop-CapturedProcess $old } catch { $caught=$_.Exception.Message }
        if ($global:r6stoppedPids.Count) { throw 'changed or unreadable process was stopped by reused PID' }
        if ($Case -eq 'ProcessQueryFailure' -and ($caught -notlike '*fixture process query denied*' -or $caught -notlike '*Get-CimInstance*' -or $caught -notlike '*retry*')) { throw 'process query error was lost' }
        if ($Case -eq 'ProcessHandleFailure' -and $caught -notlike '*fixture handle access denied*') { throw 'process handle error was lost' }
        if ($Case -eq 'ProcessStartTimeFailure' -and $caught -notlike '*fixture start time access denied*') { throw 'process start time error was lost' }
        if ($Case -notin @('ProcessQueryFailure','ProcessHandleFailure','ProcessStartTimeFailure') -and $caught) { throw $caught }
        if ($Case -ne 'ProcessQueryFailure' -and $global:r6disposed -ne 1) { throw 'process handle was not disposed after refusal' }
    } else {
        $caught=''
        try { Stop-CapturedProcess $old } catch { $caught=$_.Exception.Message }
        if ($Case -in @('ProcessExitWait','ProcessExitTimeout')) {
            if (-not $global:r6exitWaited) { throw 'process termination returned before waiting for exit' }
            if ($Case -eq 'ProcessExitWait' -and $caught) { throw "process exit wait failed: $caught" }
            if ($Case -eq 'ProcessExitTimeout' -and ($caught -notlike '*did not exit after termination*' -or $caught -notlike '*Stop-Process -Id 4242*' -or $caught -notlike '*retry*')) { throw "process exit timeout lost its fixing action: $caught" }
        }
        if ($Case -eq 'ProcessExitRace' -and $caught) { throw "process exit race was not tolerated: $caught" }
        if ($Case -eq 'ProcessPinnedExitRace' -and ($caught -or $global:r6currentProcess.CreationDate -eq $old.CreationDate)) { throw 'termination did not retain the validated process handle across PID reuse' }
        if ($Case -eq 'ProcessStopFailure' -and $caught -notlike '*fixture stop access denied*') { throw "process stop failure was lost: $caught" }
        if (($global:r6stoppedPids -join ',') -ne '4242') { throw 'stop did not target precisely the captured process' }
        if ($global:r6disposed -ne 1) { throw 'captured process handle was not disposed' }
    }
    Write-Output "$Case passed without real process operations"
    return
}
if ($Case -in @('InitialForeignTask','DeleteReplacedTask','TaskQueryFailure','StopQueryFailure','RemovalQueryFailure','TaskAbsent')) {
    $ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
    foreach ($functionName in @('Get-RegisteredInvocation','Get-TaskRegistration','Get-TaskOwnership','Wait-TaskStopped','Remove-OwnedTask')) {
        $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName }, $true)
        if ($definition) { Invoke-Expression $definition.Extent.Text }
    }
    $global:r6fixtureTask=[PSCustomObject]@{TaskName='fixture-native';State='Ready';Actions=@([PSCustomObject]@{Execute='fixture-native.exe';Arguments='extractor-shim claude --env-file "fixture.env"'});Principal=[PSCustomObject]@{UserId='fixture-user';LogonType='S4U'}}
    if ($Case -eq 'InitialForeignTask') { $global:r6fixtureTask.Actions[0].Arguments='--foreign' }
    $global:r6queries=0
    $global:r6taskStops=0
    function Get-ScheduledTask {
        [CmdletBinding()] param($TaskName)
        $global:r6queries++
        if ($Case -eq 'TaskAbsent') {
            Write-Error -Message 'fixture task does not exist' -ErrorId 'CmdletizationQuery_NotFound_TaskName' -Category ObjectNotFound
            return
        }
        if ($Case -eq 'TaskQueryFailure' -or ($Case -eq 'StopQueryFailure' -and $global:r6queries -ge 2) -or ($Case -eq 'RemovalQueryFailure' -and $global:r6deleteAttempts)) {
            Write-Error -Message 'fixture scheduler query denied' -ErrorId 'FixtureQueryDenied' -Category PermissionDenied
            return
        }
        return $global:r6fixtureTask
    }
    function Get-OwnedTaskProcesses { }
    function Stop-ScheduledTask { $global:r6taskStops++ }
    function Unregister-ScheduledTask {
        [CmdletBinding()] param($TaskName, [switch]$Confirm)
        $global:r6deleteAttempts++
        if ($Case -eq 'DeleteReplacedTask' -and $global:r6deleteAttempts -eq 1) {
            $global:r6fixtureTask.Actions[0].Arguments='--foreign'
            Write-Error 'fixture delete rejected'
            return
        }
        $global:r6fixtureTask=$null
    }
    function Get-Date { $global:r6clock }
    function Start-Sleep { param($Milliseconds) $global:r6clock=$global:r6clock.AddMilliseconds($Milliseconds) }
    function Assert-ShimRegistration($task) { if ($task -and -not @(Get-RegisteredInvocation $task).Count) { throw 'not a recognized shim registration' } }
    function Test-SamePath($left,$right) { $left -eq $right }
    $native='fixture-native.exe'
    $caught=''
    try { Remove-OwnedTask 'fixture-native' } catch { $caught=$_.Exception.Message }
    if ($Case -eq 'InitialForeignTask') {
        if (-not $global:r6fixtureTask -or $global:r6deleteAttempts -or $global:r6taskStops -or $caught -notlike '*not a recognized shim registration*') { throw 'initial foreign registration was stopped or deleted' }
    } elseif ($Case -eq 'DeleteReplacedTask') {
        if (-not $global:r6fixtureTask -or $global:r6deleteAttempts -ne 1 -or $caught -notlike '*registration changed*') { throw 'foreign replacement registration was deleted during retry' }
    } elseif ($Case -eq 'TaskAbsent') {
        if ($caught -or $global:r6deleteAttempts) { throw 'confirmed absence did not remain a no-op' }
    } else {
        if ($caught -notlike '*fixture scheduler query denied*') { throw 'scheduler query failure was treated as task absence' }
        if ($Case -ne 'RemovalQueryFailure' -and $global:r6deleteAttempts) { throw 'task was deleted after an unreadable scheduler query' }
    }
    Write-Output "$Case passed without real scheduler operations"
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
} elseif ($Case -in @('DelayedStop','StopTimeout','QueryInitialRetry','QueryFailure','QueryAccessDenied','DisabledReinstall','QueryStopFailure','ReplacementDuringStop')) {
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Running'; Principal=[PSCustomObject]@{LogonType='S4U'}; Actions=@([PSCustomObject]@{Execute=$writer;Arguments=$arguments}) }
    $global:r6oldProcess = [PSCustomObject]@{ ProcessId=4242; ParentProcessId=1; CreationDate=[datetime]'2026-01-01'; ExecutablePath=$writer; CommandLine=($writer + ' ' + $arguments) }
    $global:r6listening = $true
    $expectedFailure = "task $taskName did not stop; inspect its scheduler state"
    if ($Case -eq 'ReplacementDuringStop') { $expectedFailure='task fixture-native registration changed; refusing to remove it' }
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
    if ($Case -eq 'RollbackQueryFailure' -and $global:r6rollbackStarted) { Write-Error 'fixture scheduler query denied'; return }
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
function Get-Process {
    [CmdletBinding()] param($Id)
    $identity = if ($Case -in @('LegacyMigration','LegacyJuly','LegacyDetached')) { $global:r6legacyProcess } else { $global:r6oldProcess }
    if (-not $identity -or $Id -ne $identity.ProcessId) { throw 'unexpected process handle lookup' }
    New-FixtureProcess $identity
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
$global:r6xmlRestored=$false
function Stop-ScheduledTask {
    param($TaskName)
    if ($Case -eq 'ReplacementDuringStop') {
        $global:r6fixtureTask=[PSCustomObject]@{TaskName=$TaskName;State='Ready';Actions=@([PSCustomObject]@{Execute='foreign.exe';Arguments='--unrelated'});Principal=[PSCustomObject]@{LogonType='S4U'}}
        return
    }
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
    if ($Xml) {
        if ($Case -eq 'ReplacementDuringStop') { $global:r6xmlRestored=$true; $global:r6fixtureTask=$null; return }
        throw 'password credentials required'
    }
    if ($Case -in @('LegacyMigration','LegacyJuly','LegacyDetached')) { throw $expectedFailure }
    if ($global:r6fixtureTask -and $global:r6fixtureTask.State -eq 'Running') { throw 'replacement registered before scheduler stopped' }
    $global:r6registered=$true
    $global:r6fixtureTask = [PSCustomObject]@{TaskName=$TaskName;State='Ready';Actions=@($Action);Principal=$Principal}
    $global:r6fixtureTask
}
function Start-ScheduledTask {
    param($TaskName)
    if ($Case -eq 'RollbackQueryFailure') { $global:r6rollbackStarted=$true }
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
} elseif ($Case -eq 'ReplacementDuringStop') {
    if ($caught -ne $expectedFailure -or -not $global:r6fixtureTask -or $global:r6fixtureTask.Actions[0].Execute -ne 'foreign.exe' -or $global:r6xmlRestored -or $global:r6deleteAttempts -or $global:r6registered -or [IO.File]::ReadAllText($settings) -ne 'before') { throw "concurrent foreign replacement was overwritten or original failure was lost: $caught" }
    if ($global:r6warningText -notlike '*registration changed*' -or $global:r6warningText -notlike '*present*' -or $global:r6warningText -notlike '*inspect*') { throw "foreign rollback refusal lost its actionable state: $global:r6warningText" }
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
    $encoded = [regex]::Match($global:r6fixtureTask.Actions[0].Arguments, '-EncodedCommand ([A-Za-z0-9+/=]+)').Groups[1].Value
    $code = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
    if (-not $code.StartsWith("`$runtimeExe = '$($writer.Replace("'", "''"))'`n")) { throw 'new registration did not persist its exact runtime image' }
} else {
    if ($caught -ne $expectedFailure) { throw "original failure was lost: $caught" }
    if ([IO.File]::ReadAllText($settings) -ne 'before') { throw 'settings were not restored' }
    if ($Case -notin @('StopTimeout','RemovalTimeout','DeleteFailure','QueryStopFailure','QueryRemovalFailure','RollbackQueryFailure') -and $global:r6fixtureTask) { throw 'replacement task survived failed installation' }
}
if ($Case -eq 'QueryStopFailure') {
    if ($global:r6registered -or $global:r6deleteAttempts -or -not $global:r6fixtureTask) { throw 'unreadable stop state caused registration mutation' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -ne 45) { throw 'stop, rollback and diagnostic queries exceeded their three deadlines' }
} elseif ($Case -eq 'QueryRemovalFailure') {
    if ($global:r6deleteAttempts -ne 1 -or -not $global:r6fixtureTask -or $global:r6warningText -notlike '*could not confirm removal of task fixture-native*') { throw 'unreadable deletion state lost operation context or retried deletion' }
    if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -ne 30) { throw 'deletion query and diagnostic query exceeded their two deadlines' }
} elseif ($Case -eq 'RollbackQueryFailure') {
    if (-not $global:r6fixtureTask -or $global:r6deleteAttempts -or $global:r6warningText -notlike '*unreadable (fixture scheduler query denied)*' -or $global:r6warningText -like '*absent*') { throw 'rollback query failure was not reported as unreadable' }
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
