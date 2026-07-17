# Regression gate: pinned oracle/e4b-ft "arm1" slice, replicated, vs the
# committed baseline (evals/results/regression_gate.baseline.json).
#
# SCOPE: retrieval knobs + fact-ranking + answer/judge path. Extraction and
# dream-path changes are NOT covered here — re-run the ladder for those
# (existing rule). Run this before committing eval- or retrieval-affecting
# changes (CLAUDE.md review discipline).
#
# Stages: 0 cleanup of the arm1-gate namespace (stale judged gate files
# would resume as no-ops and silently pass); 1 rebuild contexts from local
# bank dumps with CURRENT knobs (falls back to strip-copying pinned
# contexts if banks are absent — reduced scope, loud warning); 2 judge
# N replicates; 3 verdict vs baseline.
#
#   evals\regression_gate.ps1                # 3 replicates, gate verdict
#   evals\regression_gate.ps1 -Replicates 1  # quick mode
#   evals\regression_gate.ps1 -Establish     # (re)write the baseline
#
# Exit codes: 0 pass, 1 regression, 2 infrastructure (endpoint/rebuild).
param([int]$Replicates = 3, [switch]$Establish)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$replicatePy = Join-Path $repo "evals\replicate.py"
$rebuild = Join-Path $repo "evals\rebuild_contexts.py"
$results = Join-Path $repo "evals\results"
$banks = Join-Path $results "banks\oracle-e4b-ft-arm1"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$env:PYTHONPATH = $repo

function Log($msg) { Write-Host "$(Get-Date -Format 'HH:mm:ss') $msg" }

function Wait-Endpoint($url, $seconds) {
    for ($i = 0; $i -lt ($seconds / 5); $i++) {
        try { Invoke-RestMethod -Uri $url -TimeoutSec 3 | Out-Null; return $true }
        catch { Start-Sleep -Seconds 5 }
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
    Log "starting Qwen 27B server"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

# -- Stage 0: cleanup ------------------------------------------------------
Log "stage 0: clearing arm1-gate namespace"
Remove-Item (Join-Path $results "longmemeval-ku-oracle-e4b-ft-arm1-gate*") `
    -Force -ErrorAction SilentlyContinue

# -- Stage 1: contexts -----------------------------------------------------
if (Test-Path $banks) {
    Log "stage 1: rebuilding contexts from banks with current knobs"
    & $py $rebuild --dataset oracle --extractor e4b-ft `
        --src-tag arm1 --out-tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "rebuild failed"; exit 2 }
} else {
    Write-Warning ("banks missing at $banks — falling back to pinned " +
        "contexts; gate covers answer/judge drift only")
    & $py $replicatePy copy --extractor e4b-ft --tag arm1 --to-tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "copy failed"; exit 2 }
}

# -- Stage 2: judge replicates --------------------------------------------
try {
    if (-not (Start-Qwen)) { Log "no Qwen endpoint"; exit 2 }
    & $py $replicatePy run --extractor e4b-ft --tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "run (r1) failed"; exit 2 }
    if ($Replicates -gt 1) {
        & $py $replicatePy spawn --extractor e4b-ft --tag arm1-gate `
            -n ($Replicates - 1)
        if ($LASTEXITCODE -ne 0) { Log "spawn failed"; exit 2 }
        & $py $replicatePy run --extractor e4b-ft --tag arm1-gate
        if ($LASTEXITCODE -ne 0) { Log "run (rN) failed"; exit 2 }
    }
    & $py $replicatePy agg --extractor e4b-ft --tag arm1-gate
    if ($LASTEXITCODE -ne 0) { Log "agg failed"; exit 2 }

    # -- Stage 3: verdict --------------------------------------------------
    if ($Establish) {
        & $py $replicatePy baseline --extractor e4b-ft --tag arm1-gate
        exit $LASTEXITCODE
    }
    & $py $replicatePy gate-check --extractor e4b-ft --tag arm1-gate
    exit $LASTEXITCODE
} finally {
    Stop-Qwen
    Log "regression gate finished"
}
