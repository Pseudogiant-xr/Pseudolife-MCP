#Requires -Version 7
<#
Shared Qwen-27B bench server control. Dot-source it:

    . (Join-Path $PSScriptRoot "qwen_server.ps1")
    if (-not (Start-Qwen))      { ... }   # reproducible (default)
    if (-not (Start-Qwen -Fast)) { ... }  # MTP, throughput only

MODEL: Qwen3.8-27B-UD-Q4_K_XL (migrated 2026-08-17; previously Qwen3.6-27B —
rollback copy of this file: qwen_server.ps1.bak-qwen36-20260817, and the 3.6
model/engine remain on disk). Qwen3.8 is a hybrid DeltaNet architecture the
old root llama-server.exe (b9371) cannot load; the engine build lives in
$script:QwenEngine below — that variable, not this prose, is authoritative.

TWO CONFIGS, AND THE CHOICE IS NOT A PREFERENCE
-----------------------------------------------
Reproducible (default): stock flags with q8_0 KV, MTP OFF. The 3.6-era
byte-determinism result (12 repeats, decoy interleave, cache off —
evals/results/judge-determinism-check.json) does NOT automatically carry to
a new architecture and engine: 3.8 determinism is re-verified by
evals/results/judge-determinism-check-qwen38.json. Do not trust judged
numbers unless that artifact exists and passes for the running config.

Fast (-Fast): run-server-qwen38.bat = same GGUF, embedded-MTP speculative
decoding ON (--spec-type draft-mtp). Measured 2026-08-19
(evals/results/judge-determinism-check-qwen38-mtp.json): byte-deterministic
run-to-run (0 flips, 0 response diffs) and verdict-lossless vs stock (0
flips, swing 0.0) — but 3/234 responses differ textually, so it is a
DISTINCT config for baseline purposes: judged baselines stay on the
reproducible config, MTP is for throughput work.

RULE (unchanged): anything whose output is JUDGED — an answerer or judge
call, i.e. longmemeval_bench.py --phase answer/full, replicate.py,
lme_v2_smoke.py — uses the reproducible config. -Fast is for throughput
work whose result is a bank or a raw generation, never a graded number.
When in doubt, omit -Fast.

Both paths pin the eval env protocol (cache-ram 0, ctx-checkpoints 0). The
reproducible path invokes llama-server.exe directly with explicit flags;
the -Fast path goes through run-server-qwen38.bat, whose knobs are batch
variables (CACHE_RAM / CTX_CHECKPOINTS / MTP) that this helper sets in the
environment — the LLAMA_ARG_* env vars alone are NOT enough for the .bat
path because its explicit command-line flags would override them.
#>

# Directory renamed llama.ccp -> llama.cpp on 2026-08-21 (typo fix).
$script:QwenDir    = "$env:USERPROFILE\ClaudeCode\llama.cpp"
# b10488 (2026-08-19): byte-identical output to b10453 on the fixed probe
# (hash 6444B058CAAC both) and ~4% faster decode; gate canary re-run on bump.
$script:QwenEngine = "$script:QwenDir\engine-b10488"
$script:QwenUrl    = "http://127.0.0.1:1234/v1/models"

function Get-QwenModelPath {
    # Qwen3.8 GGUFs embed the MTP head in the main file — one model for both
    # configs (the 3.6 split between models\ and models\mtp\ is retired,
    # and with it this function's old -Fast switch).
    return "$script:QwenDir\models\Qwen3.8-27B-UD-Q4_K_XL.gguf"
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
    <# Which config is actually serving :1234 — 'fast', 'reproducible',
       'foreign' (a llama-server NOT serving the expected GGUF — e.g. a
       leftover 3.6 rollback server, which post-migration would otherwise
       masquerade as reproducible and mislabel an entire judged campaign),
       or $null if nothing is up. Both configs bind the same port, so
       "something answered the probe" is NOT enough. Discriminators: the
       model path in the command line first, then the MTP flag. #>
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" `
        -ErrorAction SilentlyContinue
    if (-not $procs) { return $null }
    $model = Split-Path (Get-QwenModelPath) -Leaf
    $sawExpected = $false
    foreach ($p in $procs) {
        if ($p.CommandLine -notlike "*$model*") { continue }
        $sawExpected = $true
        # Matches both the retired turboq fork's '--spec-type mtp' and the
        # mainline '--spec-type draft-mtp' (run-server-qwen38.bat).
        if ($p.CommandLine -match '--spec-type\s+(draft-)?mtp') { return 'fast' }
    }
    if ($sawExpected) { return 'reproducible' }
    return 'foreign'
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
       REFUSES (returns $false) instead of launching or displacing.
       A 'foreign' llama-server (wrong GGUF in its command line — e.g. a
       leftover 3.6 rollback server or the daily driver) is refused
       unconditionally, whatever its VRAM: reusing it mislabels a whole
       campaign, displacing it breaks the never-displace rule. Clearing
       one takes an operator decision — run Stop-Qwen deliberately. #>
    param([switch]$Fast, [switch]$Force, [int]$Ctx = 100000)
    $want = if ($Fast) { 'fast' } else { 'reproducible' }

    $running = Get-RunningQwenConfig
    if ($running -eq 'foreign') {
        Write-Host ("$(Get-Date -Format 'HH:mm:ss') FOREIGN llama-server on " +
                    ":1234 — it is not serving $(Split-Path (Get-QwenModelPath) -Leaf); " +
                    "refusing to reuse or displace it. Run Stop-Qwen to clear " +
                    "it deliberately.")
        Start-Sleep -Seconds 30   # same retry-loop pacing as the busy refusal
        return $false
    }
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
        Write-Host "$(Get-Date -Format 'HH:mm:ss') starting Qwen3.8 27B (FAST/MTP — treat as not reproducible)"
        # The .bat's knobs are batch variables with 'if not defined' defaults,
        # so setting them in the environment here pins the eval protocol; the
        # LLAMA_ARG_* vars above would lose to the .bat's explicit flags.
        $env:CACHE_RAM = "0"
        $env:CTX_CHECKPOINTS = "0"
        $env:CTX = "$Ctx"
        # Pin MTP explicitly too: the .bat's manual-use instructions suggest
        # `set MTP=0` for stock mode, and a leftover MTP=0 in this session
        # would otherwise launch a stock server under the -Fast label.
        $env:MTP = "1"
        Start-Process -FilePath cmd.exe -WorkingDirectory $script:QwenDir `
            -WindowStyle Minimized `
            -ArgumentList '/c', "`"$script:QwenDir\run-server-qwen38.bat`" > qwen-server.log 2>&1"
        return (Wait-QwenEndpoint -Seconds 300)
    }

    Write-Host "$(Get-Date -Format 'HH:mm:ss') starting Qwen3.8 27B (reproducible q8_0, MTP off)"
    # No CUDA-toolkit PATH surgery: the llama.cpp release engines bundle their
    # own cudart/cublas 12.4 DLLs beside the exe, which Windows resolves first
    # (verified present in the $script:QwenEngine folder on each bump).
    # Flags mirror run-server-qwen38.bat EXCEPT the eval-protocol trio that
    # bat defaults differently: --cache-ram 0, --ctx-checkpoints 0, and no
    # --spec-type (MTP measured deterministic AND verdict-lossless, but with
    # 3/234 textual response diffs vs stock it is a distinct config — judged
    # baselines stay here). Sampler is the official Qwen3.8 thinking set
    # (temp 1.0, pp 0.0); the 3.6-era froggeric template and
    # --reasoning-budget are retired — 3.8 uses its embedded template with
    # reasoning_effort (xhigh default | medium | low; only 'none' is
    # rejected by the template) and enable_thinking:false still works
    # per-request (verified 2026-08-17).
    # -Ctx: KV-cache CAPACITY only, as before.
    $qwenArgs = @(
        "-m", (Get-QwenModelPath),
        "--host", "127.0.0.1", "--port", "1234",
        "-ngl", "999",
        "-c", "$Ctx",
        "-fa", "on",
        "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
        "--jinja",
        # Backslash-escaped quotes survive Start-Process's naive space-join:
        # the child's CommandLineToArgvW turns \" back into literal quotes,
        # so llama-server receives valid JSON. Raw quotes here get stripped.
        "--chat-template-kwargs", '{\"reasoning_effort\":\"medium\"}',
        "--reasoning-preserve",
        "--parallel", "1",
        "--metrics",
        "--temp", "1.0", "--top-k", "20", "--top-p", "0.95", "--min-p", "0.0",
        "--presence-penalty", "0.0",
        "-b", "512", "-ub", "256",
        "-t", "8", "-tb", "8",
        "--no-context-shift",
        "--cache-ram", "0",
        "--ctx-checkpoints", "0"
    )
    Start-Process -FilePath "$script:QwenEngine\llama-server.exe" `
        -WorkingDirectory $script:QwenEngine -WindowStyle Minimized `
        -ArgumentList $qwenArgs `
        -RedirectStandardOutput (Join-Path $script:QwenDir 'qwen-server.log') `
        -RedirectStandardError  (Join-Path $script:QwenDir 'qwen-server.err')
    return (Wait-QwenEndpoint -Seconds 300)
}
