#Requires -Version 7
<#
Shared Qwen-27B bench server control. Dot-source it:

    . (Join-Path $PSScriptRoot "qwen_server.ps1")
    if (-not (Start-Qwen))      { ... }   # reproducible (default)
    if (-not (Start-Qwen -Fast)) { ... }  # turboq, throughput only

TWO CONFIGS, AND THE CHOICE IS NOT A PREFERENCE
-----------------------------------------------
Reproducible (default): the stock llama-server with q8_0 KV. Byte-identical
outputs for byte-identical inputs — verified 2026-07-27 over 12 repeats,
including a variant that interleaved decoy requests to vary KV-slot history
and with the prompt cache off (evals/results/judge-determinism-check.json).

Fast (-Fast): the TurboQuant+MTP fork (run-server-turboq.bat). Its fused
TBQ4_0 flash-attention KV is NOT bit-reproducible: re-running identical
inputs flips ~7% of judged verdicts and swings arm accuracy up to +/-0.05.
Measured both with and without MTP (6.8% / 7.7%) and with the prompt cache
both on and off, so neither speculative decoding nor caching is the cause —
it is the quantized fused-attention KV itself.

RULE: anything whose output is JUDGED — an answerer or judge call, i.e.
longmemeval_bench.py --phase answer/full, replicate.py, lme_v2_smoke.py —
uses the reproducible config. -Fast is for throughput work whose result is
a bank or a raw generation, never a graded number. When in doubt, omit
-Fast: correctness costs less than re-running a bench you cannot trust.

Both paths pin the eval env protocol (cache-ram 0, ctx-checkpoints 0). That
protocol used to be copy-pasted into nine harnesses; it lives here now, so a
harness cannot forget it. Note run-server.bat passes --cache-ram/-ctx-
checkpoints explicitly on its command line, where they would OVERRIDE the
env vars and reintroduce the ~350-request 0xC0000409 crash, so the
reproducible path invokes llama-server.exe directly with the verified flags
rather than going through that .bat.
#>

$script:QwenDir = "$env:USERPROFILE\ClaudeCode\llama.ccp"
$script:QwenUrl = "http://127.0.0.1:1234/v1/models"

function Get-QwenModelPath {
    param([switch]$Fast)
    # The MTP-enabled GGUF lives under models\mtp\; the stock one beside it.
    if ($Fast) { return "$script:QwenDir\models\mtp\Qwen3.6-27B-UD-Q4_K_XL.gguf" }
    return "$script:QwenDir\models\Qwen3.6-27B-UD-Q4_K_XL.gguf"
}

function Wait-QwenEndpoint {
    param([int]$Seconds = 300)
    for ($i = 0; $i -lt ($Seconds / 5); $i++) {
        try { Invoke-RestMethod -Uri $script:QwenUrl -TimeoutSec 3 | Out-Null; return $true }
        catch { Start-Sleep -Seconds 5 }
    }
    return $false
}

function Get-RunningQwenConfig {
    <# Which config is actually serving :1234 — 'fast', 'reproducible', or
       $null if nothing is up. Both configs bind the same port, so "something
       answered the probe" is NOT enough: a harness that reused a leftover
       fast server for a judged phase would silently produce ungated noise.
       The MTP flag in the command line is the discriminator. #>
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" `
        -ErrorAction SilentlyContinue
    if (-not $procs) { return $null }
    foreach ($p in $procs) {
        if ($p.CommandLine -match '--spec-type\s+mtp') { return 'fast' }
    }
    return 'reproducible'
}

function Stop-Qwen {
    Get-Process llama-server -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 5
}

function Test-GpuBusy {
    <# Maintainer rule (2026-08-13): VRAM above 5 GB in use = the GPU
       belongs to someone else — hold GPU work and tell the maintainer,
       never launch onto it and never displace what is running. Set after
       an unguarded Start-Qwen piled a 27B load onto a busy GPU.

       Returns the used-MiB reading when busy, $null when free. Fails
       OPEN when nvidia-smi is absent or unreadable — a CPU-only
       environment must not be blocked by a GPU probe. Multi-GPU: the
       busiest device decides (the bench pins everything to one card, so
       any card above the bar means contention somewhere). #>
    try {
        $readings = (nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits) |
            ForEach-Object { [int]$_.Trim() }
        $used = ($readings | Measure-Object -Maximum).Maximum
    } catch { return $null }
    if ($used -gt 5000) { return $used }
    return $null
}

function Start-Qwen {
    <# Ensure the requested config is serving :1234. Returns $true on success.
       If the OTHER config is already running it is stopped and replaced —
       silently reusing the wrong server is the failure this guards against.

       GPU-busy guard (2026-08-13): when anything is holding > 5 GB VRAM
       and the wanted server is not already the thing serving, this
       REFUSES (returns $false) instead of launching or displacing —
       Get-RunningQwenConfig's discriminator is MTP-only, so a foreign
       llama-server (e.g. the daily driver) is indistinguishable from a
       leftover bench server, and the safe default is to touch nothing.
       That deliberately includes this helper's own leftover other-config
       server: replacing it now takes an operator decision — run
       Stop-Qwen first, or pass -Force. #>
    param([switch]$Fast, [switch]$Force)
    $want = if ($Fast) { 'fast' } else { 'reproducible' }

    $running = Get-RunningQwenConfig
    if ($running -eq $want -and (Wait-QwenEndpoint -Seconds 5)) {
        Write-Host "$(Get-Date -Format 'HH:mm:ss') qwen server already up ($want)"
        return $true
    }
    if (-not $Force) {
        $busy = Test-GpuBusy
        if ($null -ne $busy) {
            Write-Host ("$(Get-Date -Format 'HH:mm:ss') GPU BUSY — " +
                        "${busy} MiB VRAM in use (> 5000): holding, not " +
                        "launching or displacing. Stop the workload (or " +
                        "Stop-Qwen for a leftover bench server), or pass " +
                        "-Force to override deliberately.")
            # Pace before returning: the busy refusal fails in one
            # nvidia-smi call where a launch failure took ~300s, and the
            # overnight scripts' retry loops (budgeted for an ~hourly
            # crash cadence, e.g. overnight_lme_v2's maxRetries=20) would
            # otherwise burn their whole budget in seconds and silently
            # kill the batch — the exact 2026-07-19 failure class. 30s
            # here buys those loops ~10 minutes of patience at one probe
            # per retry; a true wait-until-free redesign of the callers
            # is follow-up work, noted in the v10 PR.
            Start-Sleep -Seconds 30
            return $false
        }
    }
    if ($running -and $running -ne $want) {
        Write-Host ("$(Get-Date -Format 'HH:mm:ss') qwen server running as " +
                    "'$running' but '$want' requested — restarting")
        Stop-Qwen
    }

    # Archive the previous log before any '>' redirect truncates it: a GGML
    # abort message only ever appears in the tail of the log it died writing.
    $qlog = Join-Path $script:QwenDir 'qwen-server.log'
    if (Test-Path $qlog) {
        $qarch = Join-Path $script:QwenDir ('crash-logs\qwen-server-' +
            (Get-Date -Format 'yyyyMMdd-HHmmss') + '-prelaunch.log')
        New-Item -ItemType Directory -Force (Split-Path $qarch) | Out-Null
        try { Move-Item $qlog $qarch -Force -ErrorAction Stop }
        catch { try { Copy-Item $qlog $qarch -Force } catch {} }
    }

    # --- Eval-run server env: the canonical record of why these two are 0.
    # (This history used to live in overnight_lme_v2.ps1, which the other
    # harnesses cross-referenced; it moved here when the copies were folded
    # into this helper.)
    #
    # Round 1 (2026-07-24), SUPERSEDED AS A DIAGNOSIS: the prompt cache was
    # blamed for ~hourly crashes and disabled. It was NOT the cause. The flag
    # stays only because the cache is pure overhead for eval prompts, which
    # this hybrid model full-reprocesses regardless — it pins ~0.5 GiB of KV
    # cells per cached prompt for zero hits.
    $env:LLAMA_ARG_CACHE_RAM = "0"
    # Round 2 (2026-07-25), THE ACTUAL CAUSE: cache-ram=0 did not stop the
    # crashes — six more 0xC0000409 aborts at a hard ~350-requests-per-launch
    # budget with the cache verifiably off. Crash logs showed 149.6 MiB
    # recurrent-state checkpoints churning (create/restore/erase) every task;
    # the KV-cell leak (~285/request) tracks that churn, not cached prompts.
    # Checkpoints only enable partial prefix reuse, which this hybrid model
    # cannot do for eval workloads anyway, so disabling them costs nothing
    # (server-context.cpp:2741 gates creation on n_ctx_checkpoints > 0).
    # Verified by a 600-request soak.
    $env:LLAMA_ARG_CTX_CHECKPOINTS = "0"

    if ($Fast) {
        Write-Host "$(Get-Date -Format 'HH:mm:ss') starting Qwen 27B (FAST/turboq — not reproducible)"
        Start-Process -FilePath cmd.exe -WorkingDirectory $script:QwenDir `
            -WindowStyle Minimized `
            -ArgumentList '/c', "`"$script:QwenDir\run-server-turboq.bat`" > qwen-server.log 2>&1"
        return (Wait-QwenEndpoint -Seconds 300)
    }

    Write-Host "$(Get-Date -Format 'HH:mm:ss') starting Qwen 27B (reproducible q8_0)"
    # From-source CUDA runtime DLLs must resolve for the stock exe too.
    $cuda = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.1"
    if ($env:PATH -notlike "*$cuda\bin\x64*") {
        $env:PATH = "$cuda\bin\x64;$cuda\bin;$env:PATH"
    }
    # Flags mirror run-server.bat (the proven rollback) EXCEPT --cache-ram and
    # --ctx-checkpoints, which that .bat hardcodes to values the eval protocol
    # forbids. This exact set is the one measured bit-reproducible.
    $qwenArgs = @(
        "-m", (Get-QwenModelPath),
        "--host", "127.0.0.1", "--port", "1234",
        "-ngl", "999",
        "-c", "100000",
        "-fa", "on",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--jinja",
        "--chat-template-file", "$script:QwenDir\qwen3-template-froggeric.jinja",
        "--reasoning-format", "deepseek",
        "--reasoning-budget", "4096",
        "--parallel", "1",
        "--metrics",
        "--temp", "0.6", "--top-k", "20", "--top-p", "0.95", "--min-p", "0.0",
        "--presence-penalty", "0.0",
        "-b", "512", "-ub", "256",
        "-t", "8", "-tb", "8",
        "--no-context-shift",
        "--cache-ram", "0",
        "--ctx-checkpoints", "0"
    )
    Start-Process -FilePath "$script:QwenDir\llama-server.exe" `
        -WorkingDirectory $script:QwenDir -WindowStyle Minimized `
        -ArgumentList $qwenArgs `
        -RedirectStandardOutput (Join-Path $script:QwenDir 'qwen-server.log') `
        -RedirectStandardError  (Join-Path $script:QwenDir 'qwen-server.err')
    return (Wait-QwenEndpoint -Seconds 300)
}
