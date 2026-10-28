# The Windows parity runner's job-started hook, installed by
# setup-windows-runner.ps1 (which fills in the Python path). It runs as the
# runner account before each job's first step.
#
# It first proves that the account cannot reach the LAN, and fails the job if
# it can. Then it removes the previous job's PostgreSQL data directories under the
# account's HOME. It stops only a postgres.exe owned by this account whose
# command line names that directory (pg0 starts it with -D <directory>), so a
# reused PID is left alone. Then it trims this runner's checkout and caches
# against their caps (runner_cleanup.py; it never fails the job).
$ErrorActionPreference = 'Continue'
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

# The account's LAN block must bind before any job code runs: a connection to
# the LAN gateway is refused by the firewall itself (WSAEACCES, 10013), and so
# is any IPv6 one when the machine has an IPv6 route (the rule blocks all of
# IPv6). Anything else fails the job, since the machine would then expose its
# LAN.
function Connect-Probe([string] $address, [int] $port) {
    $client = $null
    try {
        $client = [Net.Sockets.TcpClient]::new([Net.IPAddress]::Parse($address).AddressFamily)
        if ($client.ConnectAsync($address, $port).Wait(8000)) { 'connected' } else { 'timed out' }
    } catch {
        $inner = $_.Exception
        while ($inner.InnerException) { $inner = $inner.InnerException }
        if ($inner -is [Net.Sockets.SocketException]) { "socket error $($inner.ErrorCode)" } else { $inner.Message }
    } finally { if ($client) { $client.Dispose() } }
}
$probes = @()
$gateway = (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Sort-Object RouteMetric | Select-Object -First 1).NextHop
$probes += , @('the LAN gateway', $(if ($gateway) { Connect-Probe $gateway 53 } else { 'no default gateway' }))
if (Get-NetRoute -DestinationPrefix '::/0' -ErrorAction SilentlyContinue) {
    $probes += , @('IPv6', (Connect-Probe '2606:4700:4700::1111' 443))
}
foreach ($probe in $probes) {
    if ($probe[1] -ne 'socket error 10013') {
        Write-Output "The runner account reached $($probe[0]) ($($probe[1])), so its firewall rule does not bind; the job is refused. Rerun ops/selfhosted/setup-windows-runner.ps1 on this machine."
        exit 1
    }
}
Write-Output "egress: refused to this account by the firewall (10013): $(($probes | ForEach-Object { $_[0] }) -join ', ')"
foreach ($dir in Get-ChildItem -LiteralPath $HOME -Directory -Force -Filter 'pl-cli-lease-postgres-*' -ErrorAction SilentlyContinue) {
    if ($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
    foreach ($pidfile in Get-ChildItem -LiteralPath $dir.FullName -Recurse -Depth 3 -File -Filter postmaster.pid -ErrorAction SilentlyContinue) {
        $id = 0
        if (-not [int]::TryParse(((Get-Content -LiteralPath $pidfile.FullName -TotalCount 1) ?? ''), [ref] $id) -or $id -le 0) { continue }
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $id"
        if (-not $proc -or $proc.Name -ne 'postgres.exe' -or $proc.CommandLine -notlike "*$($dir.FullName)*") { continue }
        if ((Invoke-CimMethod -InputObject $proc -MethodName GetOwnerSid).Sid -ne $me) { continue }
        Stop-Process -Id $id -Force
        for ($i = 0; $i -lt 30 -and (Get-Process -Id $id -ErrorAction SilentlyContinue); $i++) { Start-Sleep 1 }
    }
    Remove-Item -LiteralPath $dir.FullName -Recurse -Force
}
if ($env:PSEUDOLIFE_PARITY_RUNNER_DIR) {
    & '@PYTHON@' -I (Join-Path $PSScriptRoot 'runner_cleanup.py') --runner $env:PSEUDOLIFE_PARITY_RUNNER_DIR `
        --home $HOME --log (Join-Path $HOME 'parity-cleanup.jsonl')
}
exit 0
