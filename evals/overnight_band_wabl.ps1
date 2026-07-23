# Band write-side ablation — flat-INGEST vs the 8-band continuum on the
# full-haystack ``s`` dataset (the only corpus where eviction actually
# fires: ~493 turns/question vs the 200-cap working band).
#
#   evals\overnight_band_wabl.ps1 -Phase cpu      # replays + rebuilds (no GPU)
#   evals\overnight_band_wabl.ps1 -Phase answer   # GPU window: answer + stats
#
# cpu phase (runs alongside a GPU night — needs only the bench Postgres):
#   1  replay continuum ingest -> band dumps        (private bench DB)
#   2  replay flat ingest      -> flat band dumps   (private bench DB)
#   3  rebuild both -> abl-* (4) + wabl-flat-* (2) tags + survival JSON
# answer phase (next GPU window; qwen-27b at :1234):
#   4  answer+judge all 6 tags, 4 spawned replicates each, agg
#   5  paired compares with --out artifacts (write-side + whole-system)
#
# Replays skip questions whose dump exists, so both phases are resumable.
param([ValidateSet("cpu", "answer")][string]$Phase = "cpu")

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$abl = Join-Path $repo "evals\band_ablation.py"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$replicate = Join-Path $repo "evals\replicate.py"
$results = Join-Path $repo "evals\results"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$env:PYTHONPATH = $repo
$env:TORCHDYNAMO_DISABLE = "1"
$env:HF_HUB_OFFLINE = "1"
$maxRetries = 12

$ds = "s"; $ex = "qwen-27b"   # src run: longmemeval-ku-s-qwen-27b.jsonl (untagged)
$ablTags = @("abl-continuum-wall", "abl-continuum-hist",
             "abl-flat-wall", "abl-flat-hist")
$wablTags = @("wabl-flat-wall", "wabl-flat-hist")

$ledgerDir = Join-Path $env:LOCALAPPDATA "pseudolife-overnight"
New-Item -ItemType Directory -Force $ledgerDir | Out-Null
$ledger = Join-Path $ledgerDir "band-wabl-$(Get-Date -Format 'yyyyMMdd').ledger.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss') $msg"
    Write-Host $line
    Add-Content -Path $ledger -Value $line
}

function Wait-Endpoint($url, $seconds) {
    for ($i = 0; $i -lt ($seconds / 5); $i++) {
        try {
            Invoke-RestMethod -Uri $url -TimeoutSec 3 | Out-Null
            return $true
        } catch { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Stop-Qwen {
    Get-Process llama-server -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Start-Qwen {
    if (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 5) { return $true }
    Log "starting Qwen 27B server (log: $qwenDir\qwen-server.log)"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

# CPU steps have no server to babysit — plain retry on nonzero exit.
function Invoke-Cpu($label, $stepArgs) {
    Log "=== $label ==="
    for ($try = 1; $try -le 3; $try++) {
        & $py @stepArgs 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/3)"
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP"
    return $false
}

function Invoke-Gpu($label, $stepArgs) {
    Log "=== $label ==="
    for ($try = 1; $try -le $maxRetries; $try++) {
        if (-not (Start-Qwen)) { Log "$label : server failed to start (try $try)"; continue }
        & $py @stepArgs 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        Stop-Qwen
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP after $maxRetries tries"
    return $false
}

if ($Phase -eq "cpu") {
    Log "band-wabl CPU phase (ledger: $ledger)"
    # Private bench DBs: never collides with a concurrently running suite
    # or another eval's fixed-name bench DB.
    $env:PSEUDOLIFE_BENCH_DB = "pl_bench_wabl_cont"
    $ok1 = Invoke-Cpu "1 replay continuum-s" @(
        $abl, "replay", "--dataset", $ds, "--extractor", $ex, "--src-tag", "")
    $env:PSEUDOLIFE_BENCH_DB = "pl_bench_wabl_flat"
    $ok2 = Invoke-Cpu "2 replay flat-s" @(
        $abl, "replay", "--dataset", $ds, "--extractor", $ex, "--src-tag", "",
        "--band-preset", "flat")
    Remove-Item Env:PSEUDOLIFE_BENCH_DB -ErrorAction SilentlyContinue
    if ($ok1 -and $ok2) {
        Invoke-Cpu "3a rebuild continuum" @(
            $abl, "rebuild", "--dataset", $ds, "--extractor", $ex,
            "--src-tag", "") | Out-Null
        Invoke-Cpu "3b rebuild flat (+survival stats)" @(
            $abl, "rebuild", "--dataset", $ds, "--extractor", $ex,
            "--src-tag", "", "--band-preset", "flat") | Out-Null
        Log "CPU phase complete — next: -Phase answer in a GPU window"
    } else {
        Log "INCOMPLETE — rerun; replays resume (dumps skip-if-exists)"
    }
    exit 0
}

# ── answer phase (GPU) ───────────────────────────────────────────────────
$oldStandby = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE |
    Select-String "Current AC Power Setting Index: (0x[0-9a-f]+)").Matches.Groups[1].Value
Log "disabling system sleep (was $oldStandby)"
powercfg /change standby-timeout-ac 0

try {
    $failed = @()
    foreach ($tag in ($ablTags + $wablTags)) {
        $ok = Invoke-Gpu "answer $tag" @(
            $bench, "--dataset", $ds, "--extractor", $ex,
            "--tag", $tag, "--phase", "answer")
        if ($ok) {
            Invoke-Cpu "spawn $tag" @(
                $replicate, "spawn", "--dataset", $ds, "--extractor", $ex,
                "--tag", $tag, "-n", "4") | Out-Null
            $ok = Invoke-Gpu "replicates $tag" @(
                $replicate, "run", "--dataset", $ds, "--extractor", $ex,
                "--tag", $tag)
        }
        if ($ok) {
            Invoke-Cpu "agg $tag" @(
                $replicate, "agg", "--dataset", $ds, "--extractor", $ex,
                "--tag", $tag) | Out-Null
        } else { $failed += $tag }
    }
    if ($failed.Count) {
        Log "INCOMPLETE: $($failed -join ', ') — rerun -Phase answer (resumes)"
    } else {
        Log "=== compares (paired permutation; --out per house rule) ==="
        foreach ($m in @("wall", "hist")) {
            foreach ($arm in @("rag", "hybrid")) {
                # write-side isolation: flat ranking, different survivors
                & $py $replicate compare --dataset $ds --extractor $ex `
                    --tag "abl-flat-$m" --b-tag "wabl-flat-$m" --arm $arm `
                    --out (Join-Path $results "longmemeval-ku-$ds-$ex-wabl-iso-$m-$arm.compare.json") 2>&1
                # whole-system: continuum as designed vs flat everything
                & $py $replicate compare --dataset $ds --extractor $ex `
                    --tag "abl-continuum-$m" --b-tag "wabl-flat-$m" --arm $arm `
                    --out (Join-Path $results "longmemeval-ku-$ds-$ex-wabl-sys-$m-$arm.compare.json") 2>&1
            }
        }
        Log "answer phase complete"
    }
} finally {
    Stop-Qwen
    if ($oldStandby) {
        $mins = [int]("$oldStandby" -replace "0x", "0x") / 60
        Log "restoring sleep timeout ($mins min)"
        powercfg /change standby-timeout-ac ([int]$mins)
    }
    Log "band-wabl script finished"
}
