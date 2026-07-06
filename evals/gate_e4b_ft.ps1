# Gate run (2026-07-06): E4B QLoRA fine-tune vs base E4B.
#
#   A  candidate: e4b-ft Q4_K_M : ladder rung + KU oracle extract
#   B  Qwen 27B                 : KU oracle answer + report
#
# Gate: beat base E4B QAT (cortex 0.359 / hybrid 0.551) with stale_leak 0.0.
# -Tag suffixes the bench result files (re-runs after pipeline changes keep
# the baseline JSONLs intact); the ladder JSON has no tag — copy it aside
# before re-running if the previous rung result must be preserved.
param([string]$Tag = "")
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$ladder = Join-Path $repo "evals\ladder_sweep.py"
$qwenDir = "C:\Users\HAMO9\ClaudeCode\llama.ccp"
$ftGguf = Join-Path $repo "evals\models\e4b-extractor-Q4_K_M.gguf"
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

function Start-Candidate {
    if (Wait-Endpoint "http://127.0.0.1:8081/health" 5) { return $true }
    Log "starting candidate container: $(Split-Path -Leaf $ftGguf)"
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    docker run -d --name pseudolife-mcp-extractor-bench --gpus all `
        --no-healthcheck `
        -p 127.0.0.1:8081:8081 -v "${ftGguf}:/models/extractor.gguf:ro" `
        ghcr.io/ggml-org/llama.cpp:server-cuda `
        --model /models/extractor.gguf --host 0.0.0.0 --port 8081 `
        --ctx-size 8192 --jinja -ngl 999 --parallel 1 | Out-Null
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
    # ── A: fine-tune ladder + KU oracle extract ──────────────────────────
    Stop-Qwen
    Invoke-Step "A1 ladder e4b-ft" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($ladder, "--rung", "e4b-ft")
    # Array concatenation must happen in EXPRESSION context (an assignment):
    # written inline as `@(...) + $tagArgs` in argument position, the `+` is
    # parsed as a literal argument and the tag silently never reaches the
    # bench (the first -Tag run resumed the untagged baseline as a no-op).
    $tagArgs = @(); if ($Tag) { $tagArgs = @("--tag", $Tag) }
    $extractArgs = @($bench, "--dataset", "oracle", "--extractor", "e4b-ft",
                     "--phase", "extract") + $tagArgs
    Invoke-Step "A2 KU extract e4b-ft" ${function:Start-Candidate} ${function:Stop-Candidate} $py $extractArgs

    # ── B: judge ─────────────────────────────────────────────────────────
    Stop-Candidate
    $answerArgs = @($bench, "--dataset", "oracle", "--extractor", "e4b-ft",
                    "--phase", "answer") + $tagArgs
    Invoke-Step "B KU answer e4b-ft" ${function:Start-Qwen} ${function:Stop-Qwen} $py $answerArgs
    $reportArgs = @($bench, "--dataset", "oracle", "--extractor", "e4b-ft",
                    "--report") + $tagArgs
    & $py @reportArgs 2>&1 |
        Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
} finally {
    Stop-Candidate
    Stop-Qwen
    Log "e4b-ft gate run finished"
}
