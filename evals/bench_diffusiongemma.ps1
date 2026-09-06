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

# dg_shim's port. 8082 is ALSO the production Claude shim's port
# (ops/install-shim-autostart.ps1), and claude_shim.py serves /health too, so
# "something answered on 8082" is not proof dg_shim is up. Both bench
# harnesses read this var, so one setting redirects the whole run.
$dgPort = 8082
$env:PSEUDOLIFE_BENCH_DG_URL = "http://127.0.0.1:$dgPort/v1"

# True only if the server on $dgPort is dg_shim: its /v1/models lists exactly
# one id, "diffusiongemma"; claude_shim's list never contains it. Identity
# only — dg_shim serves /v1/models before the model has loaded.
function Test-DgShim {
    try {
        $m = Invoke-RestMethod -Uri "http://127.0.0.1:$dgPort/v1/models" -TimeoutSec 3
        return [bool]($m.data | Where-Object { $_.id -eq "diffusiongemma" })
    } catch { return $false }
}

# Identity AND readiness: dg_shim's /health is 503 until the GGUF is loaded,
# so a bench that starts on identity alone races the load.
function Test-DgShimReady {
    if (-not (Test-DgShim)) { return $false }
    try { Invoke-RestMethod -Uri "http://127.0.0.1:$dgPort/health" -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}

function Wait-DgShim($seconds) {
    for ($i = 0; $i -lt ($seconds / 5); $i++) {
        if (Test-DgShimReady) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Start-Shim {
    # dg_shim already owns the port (possibly still loading): wait for it.
    if (Test-DgShim) { return (Wait-DgShim 300) }
    if (Wait-Endpoint "http://127.0.0.1:$dgPort/health" 1) {
        Log "REFUSING: something that is not dg_shim already answers on :$dgPort (the production Claude shim?). Stop it, or set `$dgPort to a free port."
        return $false
    }
    Log "starting dg_shim (GPU, log: evals\results\dgbench-shim.log)"
    Start-Process -FilePath $py -WindowStyle Minimized `
        -RedirectStandardOutput (Join-Path $repo "evals\results\dgbench-shim.log") `
        -RedirectStandardError (Join-Path $repo "evals\results\dgbench-shim.err.log") `
        -ArgumentList $shim, "--model", $dgGguf, "--ngl", "99", "--n-predict", "1024", "--port", "$dgPort",
            "--n-cpu-moe", "12"  # keep 12 MoE layers' experts in RAM: headroom so long prompts don't spill
    return (Wait-DgShim 300)
}

# Start-Qwen / Stop-Qwen + the eval env protocol (cache-ram,
# ctx-checkpoints) live in one place. Default is the REPRODUCIBLE q8_0
# config; pass -Fast for throughput work whose output is never judged.
. (Join-Path $PSScriptRoot "qwen_server.ps1")

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
