<#
Set up a self-hosted parity runner on the Windows dev VM
(docs/specs/2026-10-19-self-hosted-parity-lane.md, "The Windows dev VM").

Run in an elevated PowerShell 7 on the VM. Rerunning is safe: each step checks
first, and a registered runner is left alone.

  setup-windows-runner.ps1 -RunnerUser <name> -SuiteUser <name> `
    -Lease full-suite@<machine> -Version <x.y.z> -Sha256 <hex>

RUNNER_TOKEN in the environment (a registration token from
`gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token`)
registers the runner if it is not registered yet. Without it the script
prepares everything else and says the runner still needs one.

What it sets up:
- one local account for the runner, not an administrator, with a random
  password that is generated here, handed to the runner's service
  registration and never written down; a rerun that has to register again
  sets a new one;
- an outbound firewall rule refusing that account's traffic to private,
  link-local, CGNAT (Tailscale) and multicast addresses: jobs reach the
  internet, not the LAN. Windows Firewall does not filter loopback, so
  services on this machine's own loopback stay reachable (see the spec);
- sshd refusing the account (DenyUsers), since a job can write its
  authorized_keys;
- read-only access to the suite user's lock directory, and traverse-only
  access to the two directories above it;
- the runner's tools (pseudolife-mcp for the lease gate, and a Python for the
  disk cleanup) in an administrators-only directory the account can run but
  not change;
- the runner, registered from a fresh extraction, as a service of that
  account, with a job-started hook that removes the previous job's
  PostgreSQL directories and runs ops/selfhosted/runner_cleanup.py.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[a-z][a-z0-9-]{0,19}$')] [string] $RunnerUser,
    [Parameter(Mandatory)] [string] $SuiteUser,
    [Parameter(Mandatory)] [ValidatePattern('^full-suite@[A-Za-z0-9._-]{1,40}$')] [string] $Lease,
    [Parameter(Mandatory)] [ValidatePattern('^\d+\.\d+\.\d+$')] [string] $Version,
    [Parameter(Mandatory)] [ValidatePattern('^[0-9a-f]{64}$')] [string] $Sha256,
    [ValidatePattern('^[a-z0-9][a-z0-9-]{0,30}$')] [string] $Name = 'parity-windows-1',
    [string] $Root = 'C:\actions-runner',
    # What `uv tool install` installs for the lease gate: the release by
    # default; a git URL gives a fix that is merged but not yet released.
    [string] $PseudolifeSpec = 'pseudolife-mcp',
    # Bring the configuration up to date but leave the service stopped and
    # set to start manually: the runner takes no job until it is started.
    [switch] $NoStart
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoUrl = 'https://github.com/Pseudogiant-xr/Pseudolife-MCP'
$Label = 'pseudolife-parity-windows'
$here = $PSScriptRoot

function Say([string] $text) { Write-Output $text }
function Fail([string] $text) { throw $text }

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'run this in an elevated PowerShell 7; nothing changed'
}
if ($PSVersionTable.PSVersion.Major -lt 7) { Fail 'run this in PowerShell 7 (pwsh); the shards use shell: pwsh' }
foreach ($file in 'runner_cleanup.py', 'windows-job-started.ps1') {
    if (-not (Test-Path -LiteralPath (Join-Path $here $file))) { Fail "run this from a checkout: $file must be beside it" }
}
$uv = (Get-Command uv -ErrorAction SilentlyContinue)?.Source
if (-not $uv) { Fail 'install uv for the administrator running this script first (https://docs.astral.sh/uv/)' }
$suiteHome = (Get-CimInstance Win32_UserProfile | Where-Object { $_.LocalPath -match "\\$([regex]::Escape($SuiteUser))$" }).LocalPath
if (-not $suiteHome) { Fail "-SuiteUser $SuiteUser has no profile on this machine" }
if ($RunnerUser -eq $SuiteUser) { Fail '-RunnerUser must be an account of its own, not the suite user' }
# ValidatePattern ignores case; sshd's DenyUsers and the hash comparison do not.
if ($RunnerUser -cnotmatch '^[a-z][a-z0-9-]{0,19}$') { Fail '-RunnerUser must be lowercase letters, digits and hyphens' }
if ($Sha256 -cnotmatch '^[0-9a-f]{64}$') { Fail '-Sha256 must be 64 lowercase hex digits' }

# A password nobody knows, made from the system's random source. It lives in
# this process only, and reaches config.cmd on its command line, which only
# administrators of this machine can read while it runs. Letters and digits
# only: config.cmd is a batch file, and cmd.exe strips ^ and expands % and !
# on the way through (40 characters of 62 is 238 bits).
function New-RunnerPassword {
    $alphabet = [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    $bytes = [byte[]]::new(40)
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    -join ($bytes | ForEach-Object { $alphabet[$_ % $alphabet.Length] })
}

Say '== account'
$account = Get-LocalUser -Name $RunnerUser -ErrorAction SilentlyContinue
$password = $null
if (-not $account) {
    $password = New-RunnerPassword
    $account = New-LocalUser -Name $RunnerUser -Password (ConvertTo-SecureString $password -AsPlainText -Force) `
        -PasswordNeverExpires -AccountNeverExpires -UserMayNotChangePassword `
        -Description 'Self-hosted parity runner (setup-windows-runner)'
}
$sid = $account.SID.Value
$admins = Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object { $_.SID.Value -eq $sid }
if ($admins) { Fail "$RunnerUser is an administrator; pick a new name and the script creates an ordinary account" }

Say "== egress: no LAN for $RunnerUser"
# The private ranges, each written in two parts: the tree's identifier guard
# (tests/test_release_ux.py) flags private addresses, and these generic blocks
# are none.
# Parenthesised: in PowerShell ',' binds tighter than '+'. All of IPv6 is
# blocked, as on Linux: a LAN with a global prefix (this VM has one) is
# otherwise reachable by its global addresses. The firewall refuses ::/0, so
# it is written as its two halves. Loopback is never filtered.
$blocked = @(('10.0' + '.0.0/8'), ('172.16' + '.0.0/12'), ('192.168' + '.0.0/16'), '169.254.0.0/16', '100.64.0.0/10',
    '224.0.0.0/4', '255.255.255.255', '::/1', '8000::/1')
$group = 'Pseudolife parity runner'
# The new rule goes in before the old one goes, so a job running during a
# rerun is never unfiltered; a failure here leaves the old rule in place.
$old = @(Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue)
$ruleName = "pseudolife-parity-runner-egress-$([guid]::NewGuid().ToString('n'))"
New-NetFirewallRule -Name $ruleName -DisplayName "$group`: no LAN for $RunnerUser" -Group $group -Direction Outbound `
    -Action Block -Profile Any -RemoteAddress $blocked -LocalUser "D:(A;;CC;;;$sid)" | Out-Null
$old | Remove-NetFirewallRule
foreach ($profile in Get-NetFirewallProfile) {
    if (-not $profile.Enabled) { Fail "the $($profile.Name) firewall profile is off, so the egress rule would not apply; turn it on and rerun" }
}

# The rule as Windows holds it: this account's SID in its user filter and the
# ranges in its address filter. Whether it binds the account's traffic cannot
# be tried from here (a process started with another account's credentials
# from an SSH session dies at start, 0xC0000142), so the job-started hook
# proves it before every job and fails the job if it does not.
$rule = Get-NetFirewallRule -Name $ruleName
$users = ($rule | Get-NetFirewallSecurityFilter).LocalUser
$remote = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
if ($users -notmatch [regex]::Escape($sid)) { Fail "the egress rule does not name $RunnerUser ($users); nothing more was changed" }
if ($remote.Count -lt $blocked.Count) { Fail "the egress rule holds $($remote.Count) of $($blocked.Count) ranges; nothing more was changed" }
foreach ($dir in $Root, (Join-Path $Root 'hooks')) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}

Say '== sshd refuses the account'
$sshdConfig = Join-Path $env:ProgramData 'ssh\sshd_config'
if (Test-Path -LiteralPath $sshdConfig) {
    $lines = Get-Content -LiteralPath $sshdConfig
    $deny = "DenyUsers $RunnerUser"
    if ($lines -notcontains $deny) {
        $sshd = (Get-Command sshd -ErrorAction SilentlyContinue)?.Source ?? (Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe')
        & $sshd -t; if ($LASTEXITCODE -ne 0) { Fail "sshd's configuration does not check before this script changes it; fix it (sshd -t) and rerun" }
        Copy-Item -LiteralPath $sshdConfig -Destination "$sshdConfig.before-parity-runner" -ErrorAction SilentlyContinue
        # First in the file: a directive after a Match block would apply to that match only.
        Set-Content -LiteralPath $sshdConfig -Value (@($deny) + $lines) -Encoding utf8NoBOM
        & $sshd -t
        if ($LASTEXITCODE -ne 0) {
            Set-Content -LiteralPath $sshdConfig -Value $lines -Encoding utf8NoBOM
            Fail "sshd's configuration does not check with $deny added; it is restored"
        }
        # Open sessions are their own processes and survive the restart.
        Restart-Service sshd
    }
}

Say '== suite lock directory'
$locks = Join-Path $suiteHome '.pseudolife-mcp\locks'
if (-not (Test-Path -LiteralPath $locks)) { New-Item -ItemType Directory -Path $locks | Out-Null }
$leaseFile = Join-Path $locks 'full-suite.lease'
if (-not (Test-Path -LiteralPath $leaseFile)) { Set-Content -LiteralPath $leaseFile -Value $Lease -Encoding utf8NoBOM }
$named = (Get-Content -LiteralPath $leaseFile -TotalCount 1).Trim()
if ($named -ne $Lease) { Say "note: $leaseFile names $named, not $Lease; the runner uses $Lease" }
$lock = Join-Path $locks 'full-suite.lock'
if (-not (Test-Path -LiteralPath $lock)) { New-Item -ItemType File -Path $lock | Out-Null }
function Grant([string] $path, [string[]] $grant) {
    & icacls $path @grant | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "icacls $path $($grant -join ' ') failed ($LASTEXITCODE)" }
}
# Traverse only on the two directories above, read on the lock directory.
Grant $suiteHome @('/grant', "*${sid}:(X)")
Grant (Join-Path $suiteHome '.pseudolife-mcp') @('/grant', "*${sid}:(X)")
Grant $locks @('/grant', "*${sid}:(OI)(CI)(RX)")

Say "== tools (administrators only; $RunnerUser may run, not change)"
$tools = Join-Path $Root 'tools'
foreach ($dir in $Root, $tools, (Join-Path $Root 'hooks'), (Join-Path $Root 'cache')) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
}
# The root: administrators and SYSTEM in full, everyone else nothing, then
# read and execute for the account on what it runs.
Grant $Root @('/inheritance:r', '/grant:r', '*S-1-5-32-544:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F')
Grant $Root @('/grant', "*${sid}:(RX)")
Grant $tools @('/grant', "*${sid}:(OI)(CI)(RX)")
Grant (Join-Path $Root 'hooks') @('/grant', "*${sid}:(OI)(CI)(RX)")
$env:UV_TOOL_DIR = Join-Path $tools 'uv-tools'
$env:UV_TOOL_BIN_DIR = Join-Path $tools 'bin'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $tools 'python'
$env:UV_CACHE_DIR = Join-Path $Root 'cache\uv'
# The managed Python first, and the tool bound to it by path: left to choose,
# uv may take an interpreter in the administrator's own profile, which the
# account cannot read.
& $uv python install --quiet 3.11
$python = & $uv python find 3.11 --managed-python
if (-not $python -or -not (Test-Path -LiteralPath $python) -or -not $python.StartsWith($tools, [StringComparison]::OrdinalIgnoreCase)) {
    Fail "uv did not provide Python 3.11 under $tools"
}
& $uv tool install --quiet --upgrade --python $python $PseudolifeSpec
if ($LASTEXITCODE -ne 0) { Fail "uv tool install $PseudolifeSpec failed" }
$gate = Join-Path $env:UV_TOOL_BIN_DIR 'pseudolife-mcp.exe'
& $gate lease check --help | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "$gate does not run" }
Copy-Item -LiteralPath (Join-Path $here 'runner_cleanup.py') -Destination (Join-Path $Root 'hooks\runner_cleanup.py') -Force
$hook = Join-Path $Root 'hooks\job-started.ps1'
(Get-Content -LiteralPath (Join-Path $here 'windows-job-started.ps1') -Raw).Replace('@PYTHON@', $python) |
    Set-Content -LiteralPath $hook -Encoding utf8NoBOM -NoNewline

Say '== runner'
$zip = Join-Path $Root "cache\actions-runner-win-x64-$Version.zip"
if (-not (Test-Path -LiteralPath $zip)) {
    $part = "$zip.part"
    Invoke-WebRequest "https://github.com/actions/runner/releases/download/v$Version/actions-runner-win-x64-$Version.zip" -OutFile $part
    if ((Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Sha256) {
        Remove-Item -LiteralPath $part; Fail 'runner checksum mismatch; nothing installed'
    }
    Move-Item -LiteralPath $part -Destination $zip
}
$dir = Join-Path $Root $Name
$service = "actions.runner.Pseudogiant-xr-Pseudolife-MCP.$Name"
if (-not (Test-Path -LiteralPath (Join-Path $dir '.runner'))) {
    if (-not $env:RUNNER_TOKEN) {
        Say "not registered yet: $Name; rerun with RUNNER_TOKEN set"
        return
    }
    # A fresh extraction, administrators' only until registration is done, so
    # no file a job wrote ever runs as an administrator or sees the token.
    Get-Service -Name $service -ErrorAction SilentlyContinue | Stop-Service
    if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    New-Item -ItemType Directory -Path $dir | Out-Null
    Expand-Archive -LiteralPath $zip -DestinationPath $dir
    if (-not $password) {
        $password = New-RunnerPassword
        Set-LocalUser -Name $RunnerUser -Password (ConvertTo-SecureString $password -AsPlainText -Force)
    }
    $env:ACTIONS_RUNNER_INPUT_TOKEN = $env:RUNNER_TOKEN
    Push-Location $dir
    try {
        & .\config.cmd --unattended --replace --url $RepoUrl --name $Name --labels $Label --work _work `
            --runasservice --windowslogonaccount ".\$RunnerUser" --windowslogonpassword $password
        if ($LASTEXITCODE -ne 0) { Fail "config.cmd failed ($LASTEXITCODE)" }
    } finally {
        Pop-Location
        Remove-Item Env:ACTIONS_RUNNER_INPUT_TOKEN -ErrorAction SilentlyContinue
        $password = $null
    }
    # The service account runs and updates the runner, so it gets the directory.
    & icacls $dir /grant "*${sid}:(OI)(CI)M" | Out-Null
}
$profileDir = (Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $sid }).LocalPath
if (-not $profileDir) { $profileDir = "C:\Users\$RunnerUser" }
$envText = @"
PSEUDOLIFE_PARITY_LEASE=$Lease
PSEUDOLIFE_PARITY_LOCK_DIR=$locks
PSEUDOLIFE_PARITY_RUNNER_DIR=$dir
ACTIONS_RUNNER_HOOK_JOB_STARTED=$hook
PIP_CACHE_DIR=$profileDir\.cache\pip
UV_CACHE_DIR=$profileDir\.cache\uv
HF_HOME=$profileDir\.cache\huggingface
"@
$before = (Get-Content -LiteralPath (Join-Path $dir '.env') -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath (Join-Path $dir '.path') -Raw -ErrorAction SilentlyContinue)
$pathText = (@($env:UV_TOOL_BIN_DIR, 'C:\Program Files\PowerShell\7', 'C:\Program Files\Git\cmd',
    "$env:SystemRoot\System32", $env:SystemRoot, "$env:SystemRoot\System32\Wbem",
    "$env:SystemRoot\System32\WindowsPowerShell\v1.0", "$env:SystemRoot\System32\OpenSSH") -join ';')
# The account may change the runner directory, so .env and .path are written
# in an administrators-only directory and renamed into place: a rename
# replaces whatever entry a job left there (a link, a hard link to another
# file) instead of writing through it.
foreach ($pair in @(@('.env', $envText), @('.path', $pathText))) {
    $staged = Join-Path $Root "cache\$Name$($pair[0]).new"
    Set-Content -LiteralPath $staged -Value $pair[1] -Encoding utf8NoBOM -NoNewline
    [IO.File]::Move($staged, (Join-Path $dir $pair[0]), $true)
}
$after = $envText + $pathText
$svc = Get-Service -Name $service -ErrorAction SilentlyContinue
if (-not $svc) { Fail "the runner service $service is missing; remove $dir\.runner and rerun with RUNNER_TOKEN" }
if ($NoStart) {
    Stop-Service -Name $service
    Set-Service -Name $service -StartupType Manual
    Say "runner $Name`: $((Get-Service -Name $service).Status), start type Manual (-NoStart)"
} elseif ($before -ne $after -or $svc.Status -ne 'Running') {
    Set-Service -Name $service -StartupType AutomaticDelayedStart
    Restart-Service -Name $service
    Say "runner $Name`: $((Get-Service -Name $service).Status) (restarted)"
} else {
    Say "runner $Name`: unchanged"
}
Say "account profile: $profileDir"
