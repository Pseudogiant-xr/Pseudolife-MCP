<#
Probe what this Windows machine's current account can reach, for the parity
runner's LAN isolation (docs/specs/2026-10-19-self-hosted-parity-lane.md,
"The Windows dev VM's network filter").

  windows-egress-proof.ps1 -LanHost <LAN address> -LanHttp <LAN host:port> `
    -LanIPv6 <IPv6 address%interface> -LanIPv6Port <port> [-TailnetHost <tailnet address>] [-Out <file>]

The decisive evidence is a listener on the LAN host that logs every
connection's source address (the spec gives one): a probe "reached" the LAN
exactly when the listener saw this machine, whatever the protocol answered.

Each probe prints one line: what was tried, through which path, and the
outcome. The paths that matter are the ones Windows takes for the account
through system services (the SMB client in the kernel, BITS, WebDAV), which a
per-user firewall rule cannot match:

- smb: `net use \\<LanHost>\IPC$` with a wrong password. "reached" is an
  authentication refusal (the server answered); "blocked" is system error 53
  or 64 (the network path was not found).
- bits: a BITS download from http://<LanHttp>/ (BITS needs a batch or
  interactive logon, so run this from a scheduled task, not over SSH).
- webdav: \\<host>@<port>\DavWWWRoot through the WebClient service.
- ipv6-lan: a TCP connection to <LanIPv6>:<LanIPv6Port>.
- internet: DNS for github.com and an HTTPS request to it, which must work.
Run it as the runner account and as an ordinary account, before and after a
filter change, and compare.
#>
param(
    [Parameter(Mandatory)] [string] $LanHost,
    [Parameter(Mandatory)] [string] $LanHttp,
    [Parameter(Mandatory)] [string] $LanIPv6,
    [Parameter(Mandatory)] [int] $LanIPv6Port,
    [string] $TailnetHost,
    [string] $Out
)
$ErrorActionPreference = 'Continue'
$lines = [Collections.Generic.List[string]]::new()
function Note([string] $name, [string] $path, [string] $outcome) {
    $lines.Add(('{0,-10} {1,-34} {2}' -f $name, $path, $outcome))
}
function Within([int] $seconds, [scriptblock] $work, [object[]] $arguments) {
    $job = Start-ThreadJob -ScriptBlock $work -ArgumentList $arguments
    if (Wait-Job $job -Timeout $seconds) { $result = Receive-Job $job 2>&1 | Out-String } else { $result = "timed out after $seconds s" }
    Remove-Job $job -Force
    $result.Trim()
}

$lines.Add("account: $([Security.Principal.WindowsIdentity]::GetCurrent().Name)")

function Test-Smb([string] $target) {
    $text = (& net.exe use "\\$target\IPC$" /user:pl-egress-probe 'pl-egress-probe-wrong' 2>&1 | Out-String).Trim()
    & net.exe use "\\$target\IPC$" /delete /y 2>&1 | Out-Null
    if ($text -match 'System error (53|64|67|1231)\b') { "blocked ($($Matches[0]))" }
    elseif ($text -match 'System error (\d+)') { "reached ($($Matches[0]): the server answered)" }
    elseif ($text -match 'successfully') { 'reached (connected)' }
    else { "unclear: $text" }
}
Note 'smb' "\\$LanHost\IPC`$" (Test-Smb $LanHost)
if ($TailnetHost) { Note 'smb' "\\$TailnetHost\IPC`$ (tailnet)" (Test-Smb $TailnetHost) }

$bits = Within 90 {
    param($url)
    $file = Join-Path $env:TEMP "pl-egress-bits-$([guid]::NewGuid().ToString('n'))"
    try { Start-BitsTransfer -Source $url -Destination $file -ErrorAction Stop; 'reached (downloaded)' }
    catch { $_.Exception.Message } finally { Remove-Item -LiteralPath $file -ErrorAction SilentlyContinue }
} @("http://$LanHttp/")
$bitsOutcome = if ($bits -match 'reached|certificate|status code|HTTP|0x80190|0x80072F8F|security') { "reached ($bits)" }
               elseif ($bits -match 'timed out|0x80072EE2|0x80072EFD|0x80072EFE|0x800704C9|could not be|cannot connect|refused|unreachable|0x80070005') { "blocked ($bits)" }
               else { "unclear: $bits" }
Note 'bits' "http://$LanHttp/" $bitsOutcome

$host_, $port = $LanHttp -split ':', 2
$dav = Within 90 { param($unc) try { Get-ChildItem -LiteralPath $unc -ErrorAction Stop | Out-Null; 'reached (listed)' } catch { $_.Exception.Message } } @("\\$host_@$port\DavWWWRoot")
$davOutcome = if ($dav -match 'reached|Access is denied|access denied|denied') { "reached ($dav)" }
              elseif ($dav -match 'network path was not found|network name cannot be found|could not be found|not found|timed out') { "blocked ($dav)" }
              else { "unclear: $dav" }
Note 'webdav' "\\$host_@$port\DavWWWRoot" $davOutcome

$v6 = @($LanIPv6, $LanIPv6Port, 'LAN host')
if ($v6) {
    $result = Within 20 {
        param($address, $port)
        $client = [Net.Sockets.TcpClient]::new([Net.Sockets.AddressFamily]::InterNetworkV6)
        try { if ($client.ConnectAsync([Net.IPAddress]::Parse($address), $port).Wait(10000)) { 'connected' } else { 'timed out' } }
        catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }; "socket: $($e.Message)" }
        finally { $client.Dispose() }
    } @($v6[0], $v6[1])
    $outcome = if ($result -match 'connected|actively refused') { "reached ($result)" } else { "blocked ($result)" }
    Note 'ipv6-lan' "[$($v6[0])]:$($v6[1]) ($($v6[2]))" $outcome
}

$dns = try { (Resolve-DnsName github.com -Type A -ErrorAction Stop | Where-Object IPAddress | Select-Object -First 1).IPAddress } catch { "failed: $($_.Exception.Message)" }
Note 'dns' 'github.com A' $dns
$web = Within 30 { try { (Invoke-WebRequest https://github.com -Method Head -TimeoutSec 20 -ErrorAction Stop).StatusCode } catch { "failed: $($_.Exception.Message)" } } @()
Note 'internet' 'https://github.com' $web

$text = $lines -join "`n"
if ($Out) { Set-Content -LiteralPath $Out -Value $text -Encoding utf8NoBOM }
$text
