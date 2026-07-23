# Heartbeat check for the overnight pair — one screen showing, per
# workload, how much output exists and how stale it is. A workload whose
# "age" keeps growing past its expected cadence is stalled and needs
# investigating (see the runbook's expected-by table).
#
#   evals\overnight_status.ps1
param([string]$Tag = "slice2")

$repo = Split-Path -Parent $PSScriptRoot
$results = Join-Path $repo "evals\results"
$now = Get-Date

function Row($label, $count, $unit, $path) {
    if ($path -and (Test-Path $path)) {
        $age = [int]($now - (Get-Item $path).LastWriteTime).TotalMinutes
        Write-Host ("{0,-34} {1,5} {2,-10} last output {3,4} min ago" -f
            $label, $count, $unit, $age)
    } else {
        Write-Host ("{0,-34} {1,5} {2,-10} (no output yet)" -f $label, $count, $unit)
    }
}

# V2 slice — count = questions finished; the jsonl is the cursor.
$v2 = Join-Path $results "lme-v2-smoke-$Tag.jsonl"
$n = if (Test-Path $v2) { @(Get-Content $v2 | Where-Object { $_.Trim() }).Count } else { 0 }
Row "V2 $Tag (of 74 questions)" $n "rows" $v2

# Band replays — count = dumped questions (78 target each).
foreach ($pair in @(
        @("continuum replay", "s-qwen-27b-ablbands"),
        @("flat replay", "s-qwen-27b-ablbands-flat"))) {
    $d = Join-Path $results "banks\$($pair[1])"
    if (Test-Path $d) {
        $files = @(Get-ChildItem $d -Filter *.json.gz | Sort-Object LastWriteTime)
        $latest = if ($files) { $files[-1].FullName } else { $null }
        Row "$($pair[0]) (of 78 dumps)" $files.Count "dumps" $latest
    } else {
        Row "$($pair[0]) (of 78 dumps)" 0 "dumps" $null
    }
}

# Answer-phase artifacts, if that window has started.
$aggs = @(Get-ChildItem $results -Filter "longmemeval-ku-s-qwen-27b-*abl*.agg.json" -ErrorAction SilentlyContinue)
if ($aggs) { Write-Host ("{0,-34} {1,5} files" -f "ablation .agg.json", $aggs.Count) }

# Ledgers — the last line of each is the most recent phase event.
$ledgerDir = Join-Path $env:LOCALAPPDATA "pseudolife-overnight"
if (Test-Path $ledgerDir) {
    Write-Host ""
    foreach ($l in Get-ChildItem $ledgerDir -Filter *.ledger.log |
             Sort-Object LastWriteTime -Descending | Select-Object -First 4) {
        Write-Host ("{0}: {1}" -f $l.Name, (Get-Content $l.FullName -Tail 1))
    }
}
