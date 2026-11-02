param([string]$Source, [string]$Root, [ValidateSet('PasswordRollback','LegacyMigration')][string]$Case)
$ErrorActionPreference = 'Stop'
# Every scheduler/process operation is mocked; these cases need no credentials,
# real registrations, listeners or process termination.
$directory = Join-Path $Root $Case
$null = New-Item -ItemType Directory -Path $directory -Force
$settings = Join-Path $directory 'settings.env'
[IO.File]::WriteAllText($settings, 'before')
$writer = Join-Path $directory 'writer.ps1'
[IO.File]::WriteAllText($writer, "[IO.File]::WriteAllText('$($settings.Replace("'", "''"))', 'after'); `$global:LASTEXITCODE=0")
$global:r6fixtureTask = $null
$global:r6legacyTask = $null
$global:r6listening = $false
$global:r6stoppedLegacy = $false
$global:r6restartedLegacy = $false
$global:r6warningText = ''
$taskName = 'fixture-native'
if ($Case -eq 'PasswordRollback') {
    $global:r6fixtureTask = [PSCustomObject]@{ TaskName=$taskName; State='Ready'; Principal=[PSCustomObject]@{LogonType='Password'}; Actions=@([PSCustomObject]@{Execute='previous.exe';Arguments='--previous'}) }
    $expectedFailure = 'original fixture startup failure'
} else {
    $taskName = 'Pseudolife Claude Shim'
    $arguments = 'extractor-shim claude --env-file "' + $settings + '"'
    $global:r6legacyTask = [PSCustomObject]@{ TaskName='Pseudolife Sonnet Shim'; State='Running'; Actions=@([PSCustomObject]@{Execute='legacy.exe';Arguments=$arguments}) }
    $global:r6legacyProcess = [PSCustomObject]@{ ProcessId=4242; ParentProcessId=1; CreationDate=[datetime]'2026-01-01'; ExecutablePath='legacy.exe'; CommandLine=('legacy.exe ' + $arguments) }
    $global:r6listening = $true
    $expectedFailure = 'original fixture registration failure'
}
function Get-ScheduledTask {
    param($TaskName)
    if ($TaskName -eq 'Pseudolife Sonnet Shim') { return $global:r6legacyTask }
    return $global:r6fixtureTask
}
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Case -eq 'LegacyMigration') { return $global:r6legacyProcess }
}
function Get-NetTCPConnection {
    if ($global:r6listening) { [PSCustomObject]@{OwningProcess=4242} }
}
function Stop-Process { }
function New-ScheduledTaskAction { param($Execute,$Argument) [PSCustomObject]@{Execute=$Execute;Arguments=$Argument} }
function New-ScheduledTaskPrincipal { param($UserId,$LogonType) [PSCustomObject]@{LogonType=$LogonType} }
function New-ScheduledTaskTrigger { }
function New-ScheduledTaskSettingsSet { }
function Export-ScheduledTask { '<Task><LogonType>Password</LogonType></Task>' }
function Stop-ScheduledTask {
    param($TaskName)
    if ($TaskName -eq 'Pseudolife Sonnet Shim') { $global:r6stoppedLegacy=$true; $global:r6listening=$false }
}
function Unregister-ScheduledTask { $global:r6fixtureTask=$null }
function Register-ScheduledTask {
    param($TaskName,$Action,$Principal,$Trigger,$Settings,$Description,$Xml,[switch]$Force)
    if ($Xml) { throw 'password credentials required' }
    if ($Case -eq 'LegacyMigration') { throw $expectedFailure }
    $global:r6fixtureTask = [PSCustomObject]@{TaskName=$TaskName;State='Ready';Actions=@($Action);Principal=$Principal}
}
function Start-ScheduledTask {
    param($TaskName)
    if ($TaskName -eq 'Pseudolife Sonnet Shim') { $global:r6restartedLegacy=$true; return }
    throw $expectedFailure
}
function Invoke-WebRequest { throw 'unexpected HTTP call in scheduler failure fixture' }
function Write-Warning { param($Message) $global:r6warningText += $Message }
$config = Join-Path $directory 'registration.json'
@{ Native=$writer;OwnerExe=$writer;Kind='claude';Port=12345;Host='127.0.0.1';Name=$taskName;EnvFile=$settings;LogonType='S4U';Timeout=1;ConfigArgs=@();ServeArgs=@('extractor-shim','claude','--env-file',$settings) } |
    ConvertTo-Json | Set-Content -LiteralPath $config -Encoding UTF8
$caught = ''
try { & $Source -ConfigPath $config } catch { $caught = $_.Exception.Message }
if ($caught -ne $expectedFailure) { throw "original failure was lost: $caught" }
if ([IO.File]::ReadAllText($settings) -ne 'before') { throw 'settings were not restored' }
if ($global:r6fixtureTask) { throw 'replacement task survived failed installation' }
if ($Case -eq 'PasswordRollback') {
    if ($global:r6warningText -notlike '*password credentials required*' -or $global:r6warningText -notlike '*absent*') { throw "missing remaining-state diagnostic: $global:r6warningText" }
} elseif (-not $global:r6stoppedLegacy -or -not $global:r6restartedLegacy) {
    throw 'legacy registration was not recognized and restored'
}
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
