# Overnight LongMemEval-V2 run — expand the procedure slice past the pilot.
#
#   evals\overnight_lme_v2.ps1                    # full 74-question slice
#   evals\overnight_lme_v2.ps1 -Limit 40          # bounded night
#
# One GPU phase (qwen-27b serves extractor + answerer + judge at :1234):
#   1  full ingest+dream+answer per question, --out-tag $Tag   (~10.6 min/q)
#   2  compose-prompt reanswer of the same rows (cheap, no re-ingest)
#   3  reports for both tags
#
# The harness appends each finished question to its JSONL and skips done
# ones on rerun, so this script's retry loop makes a 2am llama-server
# crash cost one question, not the night (the pilot saw a crash every
# ~60-90 min of sustained ingest). Windows sleep is disabled and restored.
# A ledger line per phase/retry lands in $env:LOCALAPPDATA\pseudolife-overnight.
param([int]$Limit = 74, [string]$Tag = "slice2")

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$smoke = Join-Path $repo "evals\lme_v2_smoke.py"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$env:PYTHONPATH = $repo
$env:TORCHDYNAMO_DISABLE = "1"
$maxRetries = 20   # full-slice ingest is hours; the crash cadence is ~hourly

$ledgerDir = Join-Path $env:LOCALAPPDATA "pseudolife-overnight"
New-Item -ItemType Directory -Force $ledgerDir | Out-Null
$ledger = Join-Path $ledgerDir "lme-v2-$Tag-$(Get-Date -Format 'yyyyMMdd').ledger.log"

# Write-Host, not Write-Output: Log runs inside functions whose return value
# the watchdog checks — Write-Output would pollute it.
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
    # archive the previous server log before the '>' redirect below truncates it
    # (crash evidence: the GGML abort message lives in the tail of the old log)
    $qlog = Join-Path $qwenDir 'qwen-server.log'
    if (Test-Path $qlog) {
        $qarch = Join-Path $qwenDir ('crash-logs\qwen-server-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-prelaunch.log')
        New-Item -ItemType Directory -Force (Split-Path $qarch) | Out-Null
        try { Move-Item $qlog $qarch -Force -ErrorAction Stop } catch { try { Copy-Item $qlog $qarch -Force } catch {} }
    }
    # Round 1 (2026-07-24), SUPERSEDED as a diagnosis: the prompt cache was
    # blamed for the ~hourly crashes and disabled here. It was not the
    # cause (see round 2 below) — the flag stays only because the cache is
    # pure overhead for eval prompts this hybrid model always
    # full-reprocesses. Env var, not a bat edit: interactive use elsewhere
    # depends on warm cache hits; the bat passes no competing CLI flag.
    $env:LLAMA_ARG_CACHE_RAM = "0"
    # Round 2 (2026-07-25): cache-ram=0 did NOT stop the crashes — six more
    # 0xC0000409 aborts at a hard ~350-requests-per-launch budget with the
    # cache verifiably off. The crash logs show 149.6 MiB recurrent-state
    # checkpoints still churning (create/restore/erase) on every task; the
    # leak tracks that per-request churn, not cached prompts. Checkpoints
    # only help partial prefix reuse, which this hybrid model can't do for
    # eval workloads anyway ("forcing full prompt re-processing"), so
    # disabling them costs eval runs nothing (server-context.cpp:2741 gates
    # creation on n_ctx_checkpoints > 0).
    $env:LLAMA_ARG_CTX_CHECKPOINTS = "0"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

function Invoke-Phase($label, $phaseArgs) {
    Log "=== $label ==="
    for ($try = 1; $try -le $maxRetries; $try++) {
        if (-not (Start-Qwen)) { Log "$label : server failed to start (try $try)"; continue }
        & $py $smoke @phaseArgs 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        Stop-Qwen
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP after $maxRetries tries"
    return $false
}

# ── sleep guard ──────────────────────────────────────────────────────────
$oldStandby = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE |
    Select-String "Current AC Power Setting Index: (0x[0-9a-f]+)").Matches.Groups[1].Value
Log "disabling system sleep (was $oldStandby)"
powercfg /change standby-timeout-ac 0

try {
    Log "V2 overnight: --limit $Limit --out-tag $Tag (ledger: $ledger)"
    $ok = Invoke-Phase "1 ingest+answer ($Tag)" @(
        "--limit", "$Limit", "--max-trajectories", "100",
        "--bm25", "--rerank", "--lexical-cortex", "--out-tag", $Tag)
    if ($ok) {
        Invoke-Phase "2 compose reanswer" @(
            "--reanswer-from", $Tag, "--answer-prompt", "compose",
            "--out-tag", "$Tag-compose") | Out-Null
        Log "=== 3 reports ==="
        & $py $smoke --report --out-tag $Tag 2>&1
        & $py $smoke --report --out-tag "$Tag-compose" 2>&1
    } else {
        Log "INCOMPLETE RUN — rerun this script; it resumes from the JSONL cursor"
    }
} finally {
    Stop-Qwen
    if ($oldStandby) {
        $mins = [int]("$oldStandby" -replace "0x", "0x") / 60
        Log "restoring sleep timeout ($mins min)"
        powercfg /change standby-timeout-ac ([int]$mins)
    }
    Log "overnight V2 script finished"
}
