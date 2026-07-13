# Overnight queue (2026-07-06): Gemma-4 QAT bake-off leg, then Ornith _s run.
#
#   A  candidate: gemma-4-26B QAT q4_0 : ladder rung + KU oracle extract
#   B  Qwen 27B                        : KU oracle answer + report
#   C  candidate: Ornith-9B            : KU s extract (the long leg)
#   D  Qwen 27B                        : KU s answer + report
#
# Watchdog pattern as before; bench invocations resume from their JSONL.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$ladder = Join-Path $repo "evals\ladder_sweep.py"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$qatGguf = Join-Path $repo "evals\models\gemma-4-26B_q4_0-it.gguf"
$ornithGguf = Join-Path $repo "evals\models\deepreinforce-ai_Ornith-1.0-9B-Q4_K_M.gguf"
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
    Log "starting Qwen 27B server"
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

function Stop-Candidate {
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    Start-Sleep -Seconds 3
}

# The current candidate model + extra server args, set per phase below.
$script:CandGguf = $qatGguf
$script:CandExtra = @()

function Start-Candidate {
    if (Wait-Endpoint "http://127.0.0.1:8081/health" 5) { return $true }
    Log "starting candidate container: $(Split-Path -Leaf $script:CandGguf) $($script:CandExtra -join ' ')"
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    docker run -d --name pseudolife-mcp-extractor-bench --gpus all `
        --no-healthcheck `
        -p 127.0.0.1:8081:8081 -v "$($script:CandGguf):/models/extractor.gguf:ro" `
        ghcr.io/ggml-org/llama.cpp:server-cuda `
        --model /models/extractor.gguf --host 0.0.0.0 --port 8081 `
        --ctx-size 8192 --jinja -ngl 999 @($script:CandExtra) | Out-Null
    return (Wait-Endpoint "http://127.0.0.1:8081/health" 300)
}

function Invoke-Step($label, $server, $stopper, $exe, $stepArgs) {
    Log "=== $label ==="
    for ($try = 1; $try -le $maxRetries; $try++) {
        $ok = & $server
        if (-not $ok) { Log "$label : server failed to start (try $try)"; & $stopper; continue }
        & $exe @stepArgs 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        & $stopper
        Start-Sleep -Seconds 10
    }
    Log "$label : GAVE UP after $maxRetries tries"
    return $false
}

try {
    # ── A: Gemma-4 QAT ladder + KU oracle extract ────────────────────────
    Stop-Qwen
    $script:CandGguf = $qatGguf
    $script:CandExtra = @()   # bare config; probe before launch decides flags
    Invoke-Step "A1 ladder gemma4-26b-qat" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($ladder, "--rung", "gemma4-26b-qat")
    Invoke-Step "A2 KU extract gemma4-26b-qat" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($bench, "--dataset", "oracle", "--extractor", "gemma4-26b-qat",
          "--phase", "extract")

    # ── B: judge the QAT run ─────────────────────────────────────────────
    Stop-Candidate
    Invoke-Step "B KU answer gemma4-26b-qat" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "oracle", "--extractor", "gemma4-26b-qat",
          "--phase", "answer")
    & $py $bench --dataset oracle --extractor gemma4-26b-qat --report 2>&1 |
        Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"

    # ── C: Ornith _s extract (long leg) ──────────────────────────────────
    Stop-Qwen
    $script:CandGguf = $ornithGguf
    # Ornith is Qwen3.5-based: without these it burns the budget on <think>
    $script:CandExtra = @("--reasoning-format", "deepseek", "--reasoning-budget", "0")
    Invoke-Step "C KU s extract ornith-9b" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($bench, "--dataset", "s", "--extractor", "ornith-9b",
          "--phase", "extract")

    # ── D: judge the Ornith _s run ───────────────────────────────────────
    Stop-Candidate
    Invoke-Step "D KU s answer ornith-9b" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "s", "--extractor", "ornith-9b",
          "--phase", "answer")
    & $py $bench --dataset s --extractor ornith-9b --report 2>&1 |
        Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
} finally {
    Stop-Candidate
    Stop-Qwen
    Log "overnight qat+ornith queue finished"
}
