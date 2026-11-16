# Whether this PC may run a full suite, by its owner's word only.
#
# Dot-sourced by ops/remote-suite.ps1 and ops/wsl-suite.ps1. A host with a
# second machine configured (~/.pseudolife-mcp/locks/full-suite.remote, or
# PSEUDOLIFE_SUITE_REMOTE) runs full suites there, and on this PC only while
# its owner has said it is available: a full suite here overloads a desktop
# in use (maintainer ruling, 16 November 2026: "keyed to when I say I'm
# going to bed, or when I specifically say it's available"). That statement
# is the file ~/.pseudolife-mcp/locks/pc-available, written and removed by
# whoever acts on it; nothing here guesses from processes, the GPU, input or
# the hour. docs/dev/dev-machines.md ("Sharing") has the format and the
# commands that set and clear it:
#
#   (empty: no content but blank lines)  available until the file is removed
#   until=2026-11-17T07:00+11:00         available until then (an offset or Z
#                                        is required), after which it reads
#                                        as not available
#
# Anything else (a comment, a commented-out or second setting, a time without
# an offset, garbage, a file that cannot be read) reads as NOT available, and
# the reason says so: a statement the launcher cannot read is not a
# statement, and a commented-out expiry must not open this PC with no end
# (conductor ruling, 16 November 2026).

function Get-SuiteRemote([string]$home_) {
    # The second machine, as ops/remote-suite.ps1 reads it: ssh= in
    # full-suite.remote, overridden by PSEUDOLIFE_SUITE_REMOTE.
    $remote = $null
    $cfgFile = Join-Path $home_ '.pseudolife-mcp/locks/full-suite.remote'
    if (Test-Path -LiteralPath $cfgFile -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $cfgFile) {
            if ($line -match '^\s*ssh\s*=\s*(.*?)\s*$' -and $line -notmatch '^\s*#') { $remote = $Matches[1] }
        }
    }
    if ($env:PSEUDOLIFE_SUITE_REMOTE) { $remote = $env:PSEUDOLIFE_SUITE_REMOTE }
    if ($remote) { $remote } else { $null }
}

function Get-SuiteAvailability([string]$home_, [DateTimeOffset]$now = [DateTimeOffset]::Now) {
    # @{ Available = $true|$false; Why = '<one line>' }
    $file = Join-Path $home_ '.pseudolife-mcp/locks/pc-available'
    $shown = '~/.pseudolife-mcp/locks/pc-available'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        return @{ Available = $false; Why = "no ${shown}: the owner has not said this PC is available" }
    }
    try {
        $lines = @(Get-Content -LiteralPath $file -ErrorAction Stop)
    } catch {
        return @{ Available = $false; Why = "${shown} cannot be read ($($_.Exception.Message)), so it says nothing" }
    }
    $settings = @($lines | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($settings.Count -eq 0) {
        return @{ Available = $true; Why = "${shown} says so, until it is removed" }
    }
    $pattern = '^until=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2}))$'
    if ($settings.Count -gt 1 -or $settings[0] -notmatch $pattern) {
        return @{ Available = $false; Why = ("${shown} is not one ``until=<date>T<time><offset>`` line " +
            "(for example until=2026-11-17T07:00+11:00), so it says nothing") }
    }
    $until = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse($Matches[1], [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None, [ref]$until)) {
        return @{ Available = $false; Why = "${shown} names a time that does not exist ($($Matches[1])), so it says nothing" }
    }
    if ($now -ge $until) {
        return @{ Available = $false; Why = "${shown} expired at $($Matches[1])" }
    }
    @{ Available = $true; Why = "${shown} says so until $($Matches[1])" }
}
