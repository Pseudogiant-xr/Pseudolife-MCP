# DiffusionGemma bigger-extractor bench (2026-07-05), task #10.
#
#   1  dg_shim (GPU)   : ladder --rung diffusiongemma, then KU oracle --phase extract
#   2  Qwen 27B (GPU)  : KU oracle --phase answer + --report
#
# Same watchdog pattern as tonight_bakeoff.ps1 — bench invocations resume from
# their JSONL, retry loops restart whichever server died.
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$ladder = Join-Path $repo "evals\ladder_sweep.py"
$shim = Join-Path $repo "evals\dg_shim.py"
$dgGguf = Join-Path $repo "evals\models\diffusiongemma-26B-A4B-it-Q4_K_M.gguf"
$qwenDir = "C:\Users\HAMO9\ClaudeCode\llama.ccp"
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

function Stop-Shim {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'dg_shim' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-Process llama-diffusion-cli -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Start-Shim {
    if (Wait-Endpoint "http://127.0.0.1:8082/health" 5) { return $true }
    Log "starting dg_shim (GPU, log: evals\results\dgbench-shim.log)"
    Start-Process -FilePath $py -WindowStyle Minimized `
        -RedirectStandardOutput (Join-Path $repo "evals\results\dgbench-shim.log") `
        -RedirectStandardError (Join-Path $repo "evals\results\dgbench-shim.err.log") `
        -ArgumentList $shim, "--model", $dgGguf, "--ngl", "99", "--n-predict", "1024", "--port", "8082",
            "--n-cpu-moe", "12"  # keep 12 MoE layers' experts in RAM: headroom so long prompts don't spill
    return (Wait-Endpoint "http://127.0.0.1:8082/health" 300)
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
    # ── 1: ladder + KU extract on the shim ───────────────────────────────
    Stop-Qwen
    Invoke-Step "1a ladder diffusiongemma" ${function:Start-Shim} ${function:Stop-Shim} $py `
        @($ladder, "--rung", "diffusiongemma")
    Invoke-Step "1b KU extract diffusiongemma" ${function:Start-Shim} ${function:Stop-Shim} $py `
        @($bench, "--dataset", "oracle", "--extractor", "diffusiongemma",
          "--phase", "extract")

    # ── 2: judge with Qwen 27B ───────────────────────────────────────────
    Stop-Shim
    Invoke-Step "2 KU answer diffusiongemma" ${function:Start-Qwen} ${function:Stop-Qwen} $py `
        @($bench, "--dataset", "oracle", "--extractor", "diffusiongemma",
          "--phase", "answer")
    Log "=== reports ==="
    & $py $ladder --report 2>&1 |
        Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
    & $py $bench --dataset oracle --extractor diffusiongemma --report 2>&1 |
        Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
} finally {
    Stop-Shim
    Stop-Qwen
    Log "diffusiongemma bench finished"
}
