#Requires -Version 7
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
#   evals\regression_gate.ps1                # 2 replicates, gate verdict
#   evals\regression_gate.ps1 -Replicates 1  # single pass, no drift canary
#   evals\regression_gate.ps1 -Establish     # (re)write the baseline
#
# Replicates: 2, and NOT because the judge is noisy — because it must not be.
# Replicates re-judge byte-identical persisted contexts, so on the
# reproducible server (qwen_server.ps1 default: stock llama-server + q8_0 KV)
# every replicate scores exactly the same. Measured 2026-07-27 at n=4:
# std 0.0000 on all three arms. The second replicate is therefore a CANARY,
# not an estimator — if the two ever disagree, replicate.py prints a
# nondeterminism WARNING and the run was served by the TurboQuant fork,
# whose TBQ4_0 KV flips ~7% of verdicts on identical input.
#
# History: the default was 10 (~32 min) from 2026-07-26, sized to average
# away a cortex std of ~0.033. That spread was the server, not the judge;
# with it gone the gate costs ~8 min AND is more sensitive, because the
# baseline margin (max(0.03, 2*std)) collapses to the 0.03 floor instead of
# the 0.0637 the noise used to buy. Establish the baseline the same way you
# check it — the comparison is only like-for-like if both sides are.
#
# Exit codes: 0 pass, 1 regression, 2 infrastructure (endpoint/rebuild).
param([int]$Replicates = 2, [switch]$Establish)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$replicatePy = Join-Path $repo "evals\replicate.py"
$rebuild = Join-Path $repo "evals\rebuild_contexts.py"
$results = Join-Path $repo "evals\results"
$banks = Join-Path $results "banks\oracle-e4b-ft-arm1"
$env:PYTHONPATH = $repo

function Log($msg) { Write-Host "$(Get-Date -Format 'HH:mm:ss') $msg" }

# Start-Qwen / Stop-Qwen, including the eval env protocol. Default (no -Fast)
# is the reproducible q8_0 config, which is mandatory here: this gate judges.
. (Join-Path $PSScriptRoot "qwen_server.ps1")

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
