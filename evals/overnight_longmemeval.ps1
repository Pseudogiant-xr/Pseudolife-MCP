# Overnight LongMemEval knowledge-update run — floor + ceiling extractors.
#
#   evals\overnight_longmemeval.ps1              # the full night
#   evals\overnight_longmemeval.ps1 -SkipOracle  # s-dataset phases only
#
# Phases (server tenancy is exclusive — the 4090 fits one model at a time):
#   A  Qwen up   : finish oracle/qwen-27b, then s/qwen-27b (full pass)
#   B  E2B up    : oracle/gemma-e2b + s/gemma-e2b (--phase extract)
#   C  Qwen up   : answer+judge the gemma-e2b rows (--phase answer)
#
# Every bench invocation resumes from its JSONL, and each phase wraps in a
# retry loop that restarts the model server on failure — a 2am crash costs
# minutes, not the night. Server output goes to qwen-server.log /
# `docker logs` so a death is diagnosable. Windows sleep is disabled for the
# duration and restored after.
param([switch]$SkipOracle)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$bench = Join-Path $repo "evals\longmemeval_bench.py"
$qwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$e2bGguf = Join-Path $repo "evals\models\gemma-4-E2B-it-qat-UD-Q4_K_XL.gguf"
$env:PYTHONPATH = $repo
$env:TORCHDYNAMO_DISABLE = "1"
$maxRetries = 12

# Write-Host, not Write-Output: Log runs inside functions whose return value
# the watchdog checks — Write-Output would pollute it (a [log, $false] array
# is truthy, silently swallowing server-start failures).
function Log($msg) { Write-Host "$(Get-Date -Format 'HH:mm:ss') $msg" }

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
    # Full path: NoDefaultCurrentDirectoryInExePath is set on this box, so cmd
    # will not resolve a bare .bat name from the working directory.
    Start-Process -FilePath cmd.exe -WorkingDirectory $qwenDir -WindowStyle Minimized `
        -ArgumentList '/c', "`"$qwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
    return (Wait-Endpoint "http://127.0.0.1:1234/v1/models" 300)
}

function Stop-E2B {
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    Start-Sleep -Seconds 3
}

function Start-E2B {
    if (Wait-Endpoint "http://127.0.0.1:8081/health" 5) { return $true }
    Log "starting Gemma E2B (GPU) container"
    docker rm -f pseudolife-mcp-extractor-bench 2>$null | Out-Null
    # --no-healthcheck: the base image bakes a healthcheck against :8080 (we
    # serve :8081), which otherwise flags the container unhealthy forever.
    docker run -d --name pseudolife-mcp-extractor-bench --gpus all `
        --no-healthcheck `
        -p 127.0.0.1:8081:8081 -v "${e2bGguf}:/models/extractor.gguf:ro" `
        ghcr.io/ggml-org/llama.cpp:server-cuda `
        --model /models/extractor.gguf --host 0.0.0.0 --port 8081 `
        --ctx-size 8192 --jinja -ngl 999 | Out-Null
    return (Wait-Endpoint "http://127.0.0.1:8081/health" 300)
}

# Run one bench invocation under a retry loop, restarting $server on failure.
function Invoke-Phase($label, $server, $benchArgs) {
    Log "=== $label ==="
    for ($try = 1; $try -le $maxRetries; $try++) {
        $ok = & $server
        if (-not $ok) { Log "$label : server failed to start (try $try)"; continue }
        & $py $bench @benchArgs 2>&1 |
            Select-String -NotMatch "Loading weights|FutureWarning|get_sentence"
        if ($LASTEXITCODE -eq 0) { Log "$label : done"; return $true }
        Log "$label : bench exited $LASTEXITCODE (try $try/$maxRetries) — restarting server"
        if ($server -eq ${function:Start-Qwen}) { Stop-Qwen } else { Stop-E2B }
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
    # ── Phase A — Qwen extractor (ceiling) ──────────────────────────────
    Stop-E2B                                   # Qwen needs the whole GPU
    if (-not $SkipOracle) {
        Invoke-Phase "A0 oracle/qwen-27b" ${function:Start-Qwen} `
            @("--dataset", "oracle", "--extractor", "qwen-27b")
    }
    Invoke-Phase "A  s/qwen-27b" ${function:Start-Qwen} `
        @("--dataset", "s", "--extractor", "qwen-27b")

    # ── Phase B — Gemma E2B extractor (floor), extract-only ─────────────
    Stop-Qwen                                  # E2B gets the GPU to itself
    if (-not $SkipOracle) {
        Invoke-Phase "B0 oracle/gemma-e2b extract" ${function:Start-E2B} `
            @("--dataset", "oracle", "--extractor", "gemma-e2b", "--phase", "extract")
    }
    Invoke-Phase "B  s/gemma-e2b extract" ${function:Start-E2B} `
        @("--dataset", "s", "--extractor", "gemma-e2b", "--phase", "extract")

    # ── Phase C — answer+judge the gemma rows with Qwen ─────────────────
    Stop-E2B
    if (-not $SkipOracle) {
        Invoke-Phase "C0 oracle/gemma-e2b answer" ${function:Start-Qwen} `
            @("--dataset", "oracle", "--extractor", "gemma-e2b", "--phase", "answer")
    }
    Invoke-Phase "C  s/gemma-e2b answer" ${function:Start-Qwen} `
        @("--dataset", "s", "--extractor", "gemma-e2b", "--phase", "answer")

    # ── final reports ────────────────────────────────────────────────────
    Log "=== reports ==="
    foreach ($ds in @("oracle", "s")) {
        foreach ($ex in @("qwen-27b", "gemma-e2b")) {
            & $py $bench --dataset $ds --extractor $ex --report 2>&1
        }
    }
} finally {
    if ($oldStandby) {
        $mins = [int]("$oldStandby" -replace "0x", "0x") / 60
        Log "restoring sleep timeout ($mins min)"
        powercfg /change standby-timeout-ac ([int]$mins)
    }
    Log "overnight run finished"
}
