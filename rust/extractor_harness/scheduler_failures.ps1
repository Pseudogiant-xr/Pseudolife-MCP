param([string]$Source, [string]$Root, [ValidateSet('PasswordRollback','LegacyMigration','DelayedStop','DelayedRemoval','DeleteRetry','DeleteFailure','StopTimeout','RemovalTimeout','ProcessExitRace','ProcessStopFailure','ProcessIdentityChanged','QueryInitialRetry','QueryRollbackRetry','QueryRemovalRetry','QueryFailure','QueryAccessDenied','QueryNamedAbsent')][string]$Case)
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
if ($Case -eq 'PasswordRollback') {
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Ready'; Principal=[PSCustomObject]@{LogonType='Password'}; Actions=@([PSCustomObject]@{Execute='previous.exe';Arguments='--previous'}) }
    $expectedFailure = 'original fixture startup failure'
} elseif ($Case -eq 'LegacyMigration') {
    $taskName = 'Pseudolife Claude Shim'
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $global:r6legacyTask = [PSCustomObject]@{ TaskName='Pseudolife Sonnet Shim'; State='Running'; Actions=@([PSCustomObject]@{Execute='legacy.exe';Arguments=$arguments}) }
    $global:r6legacyProcess = [PSCustomObject]@{ ProcessId=4242; ParentProcessId=1; CreationDate=[datetime]'2026-01-01'; ExecutablePath='legacy.exe'; CommandLine=('legacy.exe ' + $arguments) }
    $global:r6listening = $true
    $expectedFailure = 'original fixture registration failure'
} elseif ($Case -in @('DelayedStop','StopTimeout','QueryInitialRetry','QueryFailure','QueryAccessDenied')) {
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Running'; Principal=[PSCustomObject]@{LogonType='S4U'}; Actions=@([PSCustomObject]@{Execute=$writer;Arguments=$arguments}) }
    $global:r6oldProcess = [PSCustomObject]@{ ProcessId=4242; ParentProcessId=1; CreationDate=[datetime]'2026-01-01'; ExecutablePath=$writer; CommandLine=($writer + ' ' + $arguments) }
    $global:r6listening = $true
    $expectedFailure = "task $taskName did not stop; inspect its scheduler state"
    if ($Case -in @('QueryFailure','QueryAccessDenied')) { $expectedFailure = 'fixture task query failed' }
} else {
    $expectedFailure = 'original fixture startup failure'
}
function Get-ScheduledTask {
    [CmdletBinding()]
    param($TaskName)
    $fault = $Case -in @('QueryFailure','QueryAccessDenied','QueryNamedAbsent') -or (
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
    if ($Case -eq 'LegacyMigration') { return $global:r6legacyProcess }
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
function Stop-Process { param($Id) $global:r6stoppedPids += $Id; $global:r6listening=$false }
function Get-Date { $global:r6clock }
function Start-Sleep { param($Milliseconds) $global:r6clock=$global:r6clock.AddMilliseconds($Milliseconds) }
function New-ScheduledTaskAction { param($Execute,$Argument) [PSCustomObject]@{Execute=$Execute;Arguments=$Argument} }
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
    if ($Case -in @('DelayedRemoval','RemovalTimeout','QueryRemovalRetry')) { $global:r6removePending=3 }
    else { $global:r6fixtureTask=$null }
}
function Register-ScheduledTask {
    param($TaskName,$Action,$Principal,$Trigger,$Settings,$Description,$Xml,[switch]$Force)
    if ($Xml) { throw 'password credentials required' }
    if ($Case -eq 'LegacyMigration') { throw $expectedFailure }
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
    if ($Case -notin @('DelayedStop','QueryInitialRetry')) { throw 'unexpected HTTP call in scheduler failure fixture' }
}
function Write-Warning { param($Message) $global:r6warningText += $Message }
$config = Join-Path $directory 'registration.json'
@{ Native=$writer;OwnerExe=$writer;Kind='claude';Port=12345;Host='127.0.0.1';Name=$taskName;EnvFile=$settings;LogonType='S4U';Timeout=1;Remove=($Case -eq 'QueryNamedAbsent');ConfigArgs=@();ServeArgs=@('extractor-shim','claude','--env-file',$settings) } |
    ConvertTo-Json | Set-Content -LiteralPath $config -Encoding UTF8
$caught = ''
try { & $Source -ConfigPath $config } catch { $caught = $_.Exception.Message }
if ($Case -in @('QueryFailure','QueryAccessDenied','QueryNamedAbsent')) {
    if ($Case -eq 'QueryNamedAbsent') {
        if ($caught) { throw "confirmed missing task was not a quiet no-op: $caught" }
    } elseif ($caught -ne $expectedFailure) { throw "task query failure was lost: $caught" }
    if ($global:r6registered -or $global:r6deleteAttempts -or $global:r6stoppedPids.Count -or [IO.File]::ReadAllText($settings) -ne 'before') { throw 'unconfirmed registration caused a mutation' }
    $elapsed=($global:r6clock - [datetime]'2026-01-01').TotalSeconds
    if ($Case -eq 'QueryFailure' -and ($elapsed -ne 15 -or $global:r6queryFaults -lt 2)) { throw 'persistent query failure was not bounded by its deadline' }
    if ($Case -ne 'QueryFailure' -and ($elapsed -ne 0 -or $global:r6queryFaults -ne 1)) { throw 'nonretryable query result was retried' }
    Write-Output "$Case passed without real scheduler operations"
    return
} elseif ($Case -in @('DelayedStop','QueryInitialRetry')) {
    if ($caught) { throw "replacement failed: $caught" }
    if (-not $global:r6registered -or -not $global:r6listening -or $global:r6stopWaits -lt 3) { throw 'replacement did not wait for the delayed scheduler stop' }
    if (($global:r6stoppedPids -join ',') -ne '4242') { throw 'replacement did not stop precisely its captured process' }
} else {
    if ($caught -ne $expectedFailure) { throw "original failure was lost: $caught" }
    if ([IO.File]::ReadAllText($settings) -ne 'before') { throw 'settings were not restored' }
    if ($Case -notin @('StopTimeout','RemovalTimeout','DeleteFailure') -and $global:r6fixtureTask) { throw 'replacement task survived failed installation' }
}
if ($Case -eq 'DeleteRetry') {
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
} elseif ($Case -eq 'LegacyMigration' -and (-not $global:r6stoppedLegacy -or -not $global:r6restartedLegacy)) {
    throw 'legacy registration was not recognized and restored'
}
if ($Case -in @('QueryInitialRetry','QueryRollbackRetry','QueryRemovalRetry') -and $global:r6queryFaults -ne 2) { throw 'transient query errors were not retried' }
if (($global:r6clock - [datetime]'2026-01-01').TotalSeconds -gt 31) { throw 'scheduler waits exceeded their bounded deadlines' }
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
