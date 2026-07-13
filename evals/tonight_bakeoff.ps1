# One-shot overnight queue (2026-07-04): finish the sidecar bake-off, then
# the s-dataset diagnostic run, then teacher-labeling datagen.
#
#   A  candidate GPU container : oracle/qwen3.5-4b --phase extract
#   B  Qwen 27B up             : --phase answer for all four candidates + reports
#   C  Qwen 27B up             : resume s/qwen-27b --tag diag (full)
#   D  Qwen 27B up             : distill_datagen.py (resumable; runs to limit)
#
# Same watchdog pattern as overnight_longmemeval.ps1 — every bench invocation
# resumes from its JSONL, retry loops restart the server that died.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$datagen = Join-Path $repo "evals\distill_datagen.py"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$candidateGguf = Join-Path $repo "evals\models\Qwen3.5-4B-UD-Q4_K_XL.gguf"
$env:PYTHONPATH = $repo
$env:TORCHDYNAMO_DISABLE = "1"
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
    Log "starting Qwen 27B server (log: $qwenDir\qwen-server.log)"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

function Stop-Candidate {
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    Start-Sleep -Seconds 3
}

function Start-Candidate {
    if (Wait-Endpoint "http://127.0.0.1:8081/health" 5) { return $true }
    Log "starting Qwen3.5-4B (GPU) container"
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    docker run -d --name pseudolife-mcp-extractor-bench --gpus all `
        --no-healthcheck `
        -p 127.0.0.1:8081:8081 -v "${candidateGguf}:/models/extractor.gguf:ro" `
        ghcr.io/ggml-org/llama.cpp:server-cuda `
        --model /models/extractor.gguf --host 0.0.0.0 --port 8081 `
        --ctx-size 8192 --jinja -ngl 999 | Out-Null
    return (Wait-Endpoint "http://127.0.0.1:8081/health" 300)
}

function Invoke-Step($label, $server, $exe, $stepArgs) {
    Log "=== $label ==="
    for ($try = 1; $try -le $maxRetries; $try++) {
        $ok = & $server
        if (-not $ok) { Log "$label : server failed to start (try $try)"; continue }
        & $exe @stepArgs 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        if ($server -eq ${function:Start-Qwen}) { Stop-Qwen } else { Stop-Candidate }
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP after $maxRetries tries"
    return $false
}

$oldStandby = (powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE |
    Select-String "Current AC Power Setting Index: (0x[0-9a-f]+)").Matches.Groups[1].Value
Log "disabling system sleep (was $oldStandby)"
powercfg /change standby-timeout-ac 0

try {
    # ── A: last bake-off extract ─────────────────────────────────────────
    Stop-Qwen
    Invoke-Step "A qwen3.5-4b extract" ${function:Start-Candidate} $py `
        @($bench, "--dataset", "oracle", "--extractor", "qwen3.5-4b",
          "--phase", "extract")

    # ── B: judge all four candidates + reports ───────────────────────────
    Stop-Candidate
    foreach ($cand in @("qwen3.5-4b", "ornith-9b", "lfm2-8b-a1b", "granite-h-tiny")) {
        Invoke-Step "B answer $cand" ${function:Start-Qwen} $py `
            @($bench, "--dataset", "oracle", "--extractor", $cand,
              "--phase", "answer")
    }
    Log "=== bake-off reports ==="
    foreach ($cand in @("qwen3.5-4b", "ornith-9b", "lfm2-8b-a1b", "granite-h-tiny")) {
        & $py $bench --dataset oracle --extractor $cand --report 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
    }

    # ── C: resume the s-dataset diagnostic run ───────────────────────────
    Invoke-Step "C s/qwen-27b diag" ${function:Start-Qwen} $py `
        @($bench, "--dataset", "s", "--extractor", "qwen-27b", "--tag", "diag")

    # ── D: teacher-labeling datagen (resumable) ──────────────────────────
    Invoke-Step "D distill datagen" ${function:Start-Qwen} $py `
        @($datagen, "--limit-rows", "2000")
} finally {
    if ($oldStandby) {
        $mins = [int]("$oldStandby" -replace "0x", "0x") / 60
        Log "restoring sleep timeout ($mins min)"
        powercfg /change standby-timeout-ac ([int]$mins)
    }
    Log "tonight queue finished"
}
