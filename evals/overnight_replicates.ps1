#Requires -Version 7
# Arm-1 re-verification overnight run (spec 2026-07-18).
#
# For each config below: spawn N stripped replicates of the existing judged
# JSONL, answer-phase them against the local Qwen 27B judge, aggregate to
# mean +/- std. Then paired-permutation compare arm1 vs arm1-baseline.
#
# PRE-REGISTERED RULE: paired p < 0.05 on the cortex arm confirms the Arm-1
# gain (shipped default stands, docs get mean+/-std); otherwise the
# extractor-default decision is flagged for revisit.
#
# Resumable: kill and re-run continues per row (bench semantics).
param([int]$N = 4)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$replicatePy = Join-Path $repo "evals\replicate.py"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$env:PYTHONPATH = $repo
$maxRetries = 8

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

function Invoke-WithRetry($label, $stepArgs) {
    for ($try = 1; $try -le $maxRetries; $try++) {
        if (-not (Start-Qwen)) { Log "$label : no endpoint (try $try)"; Stop-Qwen; continue }
        # Out-Host: keep native output visible without contaminating this
        # function's OUTPUT stream — the caller consumes it as a boolean, and
        # @(lines..., $false) coerces truthy, silently breaking failure checks.
        & $py @stepArgs 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        Stop-Qwen
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP after $maxRetries tries"
    return $false
}

$configs = @(
    @{ Extractor = "e4b-ft";   Tag = "arm1" },
    @{ Extractor = "e4b-ft";   Tag = "arm1-baseline" },
    # NOTE: the untagged qwen-27b run (README 0.705) predates context
    # persistence and cannot be replicated; w0 is the same knob config
    # (window=0 control) with persisted contexts — a qwen-27b-class
    # variance estimate, NOT a replication of the published number.
    @{ Extractor = "qwen-27b"; Tag = "w0" }
)

try {
    $failed = @()
    foreach ($cfg in $configs) {
        $label = "$($cfg.Extractor)/$(if ($cfg.Tag) { $cfg.Tag } else { '(untagged)' })"
        Log "=== $label : spawn $N replicates ==="
        & $py $replicatePy spawn --extractor $cfg.Extractor --tag $cfg.Tag -n $N
        if ($LASTEXITCODE -ne 0) { Log "$label : spawn failed"; $failed += $label; continue }
        if (-not (Invoke-WithRetry "$label run" @(
                $replicatePy, "run", "--extractor", $cfg.Extractor,
                "--tag", $cfg.Tag))) {
            $failed += $label
            continue
        }
        & $py $replicatePy agg --extractor $cfg.Extractor --tag $cfg.Tag
    }

    if ($failed.Count -gt 0) {
        Log "INCOMPLETE RUN — configs that never finished judging: $($failed -join ', ')"
        Log "compare skipped; re-run this script to resume (per-row resumable)."
        exit 1
    }

    Log "=== compare: arm1 vs arm1-baseline ==="
    foreach ($arm in @("cortex", "hybrid")) {
        & $py $replicatePy compare --extractor e4b-ft --tag arm1 `
            --b-tag arm1-baseline --arm $arm
        if ($LASTEXITCODE -ne 0) { Log "compare ($arm) failed"; exit 1 }
    }
    Log ("PRE-REGISTERED RULE: cortex p < 0.05 confirms the Arm-1 gain; " +
         "otherwise flag the extractor default for revisit.")
    exit 0
} finally {
    Stop-Qwen
    Log "overnight replicates finished"
}
