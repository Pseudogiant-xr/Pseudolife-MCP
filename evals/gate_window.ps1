# Gate run (2026-07-11): known-facts window 2x2 (spec 2026-07-10).
#
#   A  candidate (e4b-ft :8081): echo check -> ladder --window 20
#                                -> KU oracle extract w0 + w20
#   B  Qwen 27B (:1234)        : qwen-27b full w0 + w20 (extractor = answerer)
#                                -> e4b-ft answer w0 + w20 + reports
#
# Gate (docs/specs/2026-07-10-known-facts-window-design.md): e4b-ft w20
# cortex >= w0 cortex + 0.05, hybrid no regression, ladder stale_leak 0.0,
# echo check PASS. The echo check aborts the whole run on FAIL — a leaky
# prompt must not burn bench GPU-hours.
#
# Modeled on gate_e4b_ft.ps1 (2026-07-06) — same server management. The
# ladder JSON (results\e4b-ft.json) is copied aside to e4b-ft-w20-ladder.json
# after A1 and the original removed, preserving the working tree's
# pre-existing deletion of that path.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$ladder = Join-Path $repo "evals\ladder_sweep.py"
$echo = Join-Path $repo "evals\window_echo_check.py"
$qwenDir = "C:\Users\HAMO9\ClaudeCode\llama.ccp"
$ftGguf = Join-Path $repo "evals\models\e4b-extractor-Q4_K_M.gguf"
$ladderJson = Join-Path $repo "evals\results\e4b-ft.json"
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
    # ── A0: echo check — hard abort on FAIL (exit 1 = echo detected) ─────
    Stop-Qwen
    Log "=== A0 window echo check ==="
    if (-not (Start-Candidate)) { throw "A0: candidate server failed to start" }
    & $py $echo --extractor e4b-ft 2>&1 |
        Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
    if ($LASTEXITCODE -ne 0) {
        throw "A0: ECHO CHECK FAILED (exit $LASTEXITCODE) — window leaks into claims; aborting before bench runs"
    }
    Log "A0 : echo check PASS"

    # ── A1: ladder with window on (stale_leak must hold 0.0) ─────────────
    Invoke-Step "A1 ladder e4b-ft --window 20" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($ladder, "--rung", "e4b-ft", "--window", "20")
    if (Test-Path $ladderJson) {
        Copy-Item $ladderJson (Join-Path $repo "evals\results\e4b-ft-w20-ladder.json") -Force
        Remove-Item $ladderJson -Force   # keep the tree's pre-existing deletion intact
        Log "A1 : ladder JSON copied to e4b-ft-w20-ladder.json"
    }

    # ── A2/A3: e4b-ft KU oracle extract, window off / on ─────────────────
    Invoke-Step "A2 KU extract e4b-ft w0" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($bench, "--dataset", "oracle", "--extractor", "e4b-ft", "--phase", "extract", "--tag", "w0")
    Invoke-Step "A3 KU extract e4b-ft w20" ${function:Start-Candidate} ${function:Stop-Candidate} $py `
        @($bench, "--dataset", "oracle", "--extractor", "e4b-ft", "--phase", "extract", "--tag", "w20", "--window", "20")

    # ── B: Qwen tenancy — 27B control pair (full) + e4b-ft answers ───────
    Stop-Candidate
    Invoke-Step "B1 KU full qwen-27b w0" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "oracle", "--extractor", "qwen-27b", "--phase", "full", "--tag", "w0")
    Invoke-Step "B2 KU full qwen-27b w20" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "oracle", "--extractor", "qwen-27b", "--phase", "full", "--tag", "w20", "--window", "20")
    Invoke-Step "B3 KU answer e4b-ft w0" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "oracle", "--extractor", "e4b-ft", "--phase", "answer", "--tag", "w0")
    Invoke-Step "B4 KU answer e4b-ft w20" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "oracle", "--extractor", "e4b-ft", "--phase", "answer", "--tag", "w20")
} finally {
    Stop-Candidate
    Stop-Qwen
    Log "known-facts-window gate run finished (servers stopped — box left as found)"
}
