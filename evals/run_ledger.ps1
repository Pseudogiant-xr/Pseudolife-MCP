# Heartbeat ledger for an unattended eval run.
#
# One line per beat, so a whole overnight window can be audited at a glance the
# next morning. Two separate VRAM columns, because they catch different
# failures and one number cannot do both jobs (2026-09-02):
#
#   llama_mb  - VRAM held by the serving process itself. Drift here is the run.
#               A drop to 0 while the bench is still alive means the server died
#               under it and every subsequent row is garbage.
#   other_mb  - everything else on the device (desktop, browser, another job).
#               This is what kills a run by starving it, and it is invisible if
#               you only log the device total.
#
# Per-process VRAM comes from the Windows "GPU Process Memory" counter, NOT from
# nvidia-smi: under the WDDM driver model `--query-compute-apps=used_memory`
# returns [N/A] for every process, which is why the first version of this ledger
# logged only the device total and misread a Chrome window as run drift.
#
# Sentinels: a VRAM column reads -1 when it could not be measured (nvidia-smi
# failed -> note NO-SMI; or no counter instance for the server pid), which is
# deliberately distinct from 0 (the serving process is gone -> SERVER-GONE).
#
# Usage:
#   pwsh -NoProfile -File evals/run_ledger.ps1 `
#       -RunPid 40748 `
#       -RowsFile  <path to the run's .jsonl> `
#       -LogFile   <path to the run's stdout log> `
#       -LedgerFile <path to write> `
#       [-ProgressPattern 'ingested|cognified|\(reused\)']

param(
    [Parameter(Mandatory = $true)][int]$RunPid,
    [Parameter(Mandatory = $true)][string]$RowsFile,
    [Parameter(Mandatory = $true)][string]$LogFile,
    [Parameter(Mandatory = $true)][string]$LedgerFile,
    [string]$ServerProcessName = 'llama-server',
    [int]$IntervalSeconds = 900,
    [int]$LowHeadroomMb = 1000,
    # What a "chat done" line looks like in the run's stdout log, per harness:
    # beam_adapter prints "chat N: ingested ...", cognee_adapter prints
    # "chat N: ... cognified" / "(reused)". A pattern that matches neither
    # leaves the chats column at 0 all night while looking like a real count.
    [string]$ProgressPattern = 'ingested|cognified|\(reused\)'
)

function Get-ServerVramMb {
    # Re-resolved every beat rather than cached: a server that dies and is
    # restarted mid-run must show up as a new pid, not as a stale zero.
    # Returns 0 only when the process is genuinely gone; -1 means "could not
    # measure" (counter unavailable, or no counter instance for that pid),
    # which must never be read as SERVER-GONE.
    param([string]$Name)
    $proc = Get-Process $Name -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $proc) { return 0 }
    try {
        $samples = (Get-Counter "\GPU Process Memory(*)\Local Usage" -ErrorAction Stop).CounterSamples
        $mine = @($samples | Where-Object { $_.InstanceName -like "pid_$($proc.Id)_*" })
        if ($mine.Count -eq 0) { return -1 }
        return [math]::Round((($mine | Measure-Object -Property CookedValue -Sum).Sum) / 1MB, 0)
    } catch {
        return -1
    }
}

if (-not (Test-Path $LedgerFile)) {
    "timestamp`trows`tdelta`tchats`tlog_kb`talive`tllama_mb`tother_mb`ttotal_mb`tfree_mb`tnote" |
        Out-File $LedgerFile -Encoding ascii
}

$prev = 0
$prevLogKb = -1
$firstBeat = $true
$quietBeats = 0
while ($true) {
    $rows = 0
    if (Test-Path $RowsFile) {
        $rows = (Get-Content $RowsFile -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
    }
    $chats = 0; $logKb = 0
    if (Test-Path $LogFile) {
        $raw = Get-Content $LogFile -Raw -ErrorAction SilentlyContinue
        if ($raw) { $chats = ([regex]::Matches($raw, $ProgressPattern)).Count }
        $logKb = [math]::Round((Get-Item $LogFile).Length / 1KB, 1)
    }
    $alive = [bool](Get-Process -Id $RunPid -ErrorAction SilentlyContinue)

    # The GPU query is guarded: a wedged driver (the documented WSL failure
    # mode) makes nvidia-smi error or return nothing, and an unguarded
    # [int]'' here would end this loop — leaving the ledger silent, which is
    # indistinguishable from "all fine" and is the failure the ledger exists
    # to prevent. Unmeasurable reads -1 and the beat says NO-SMI.
    $smiOk = $true
    try {
        $smi = @(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>$null)
        if (-not $smi -or -not $smi[0]) { throw 'nvidia-smi returned nothing' }
        $gpu = $smi[0] -split ','          # first device; the bench box has one
        $totalUsed = [int](($gpu[0] -replace '[^0-9]', ''))
        $capacity  = [int](($gpu[1] -replace '[^0-9]', ''))
    } catch {
        $smiOk = $false; $totalUsed = -1; $capacity = -1
    }
    $llama = if ($smiOk) { Get-ServerVramMb -Name $ServerProcessName } else { -1 }
    $other = if ($smiOk -and $llama -ge 0) { $totalUsed - $llama } else { -1 }
    $free  = if ($smiOk) { $capacity - $totalUsed } else { -1 }

    # Notes are the point of the ledger: a human scanning it at 7am should see
    # the word, not have to diff two columns to find the moment it went wrong.
    $notes = @()
    $delta = $rows - $prev
    # One quiet beat is not a stall — a beat can legitimately land mid-unit
    # when the interval is close to the per-unit cost. Two consecutive quiet
    # beats is, and still catches a real stall inside two intervals. "Quiet"
    # means NO rows AND NO log growth: a harness whose unit of work is a whole
    # chat (cognee_adapter writes its first row only after the chat's entire
    # cognify) can legitimately go 30+ min without a row while its log grows.
    $logGrew = ($prevLogKb -ge 0) -and ($logKb -gt $prevLogKb)
    if ($alive -and -not $firstBeat -and $delta -le 0 -and -not $logGrew) { $quietBeats++ } else { $quietBeats = 0 }
    if ($quietBeats -eq 1)                             { $notes += 'quiet' }
    if ($quietBeats -ge 2)                             { $notes += 'STALLED' }
    if (-not $smiOk)                                   { $notes += 'NO-SMI' }
    if ($alive -and $llama -eq 0)                      { $notes += 'SERVER-GONE' }
    if ($smiOk -and $free -lt $LowHeadroomMb)          { $notes += 'LOW-HEADROOM' }
    if (-not $alive)                                   { $notes += 'RUN-EXITED' }
    $note = if ($notes.Count) { $notes -join ',' } else { 'ok' }

    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts`t$rows`t$delta`t$chats`t$logKb`t$alive`t$llama`t$other`t$totalUsed`t$free`t$note" |
        Out-File $LedgerFile -Append -Encoding ascii

    $prev = $rows
    $prevLogKb = $logKb
    $firstBeat = $false
    if (-not $alive) { break }   # final beat is written before stopping
    Start-Sleep -Seconds $IntervalSeconds
}
