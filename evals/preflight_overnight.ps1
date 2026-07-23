# Preflight for the overnight pair (band write-side ablation + LME-V2
# slice2). Read-only: checks every dependency both scripts will hit and
# prints PASS/FAIL per line — run it before launching, fix every FAIL.
#
#   evals\preflight_overnight.ps1
param([string]$Tag = "slice2")

$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo ".venv\Scripts\python.exe"
$results = Join-Path $repo "evals\results"
$fail = 0

function Check($label, [bool]$ok, $detail = "") {
    $mark = if ($ok) { "PASS" } else { $script:fail++; "FAIL" }
    Write-Host ("[{0}] {1}{2}" -f $mark, $label,
        ($(if ($detail) { " — $detail" } else { "" })))
}

# 1  venv + harnesses compile
Check "venv python" (Test-Path $py) $py
if (Test-Path $py) {
    & $py -m py_compile (Join-Path $repo "evals\band_ablation.py") `
        (Join-Path $repo "evals\lme_v2_smoke.py") `
        (Join-Path $repo "evals\replicate.py") 2>&1 | Out-Null
    Check "harnesses compile" ($LASTEXITCODE -eq 0)
}

# 2  bench Postgres
$pgOk = $false
try {
    $pgOk = (Test-NetConnection 127.0.0.1 -Port 5433 -WarningAction SilentlyContinue -InformationLevel Quiet)
} catch {}
Check "bench Postgres 127.0.0.1:5433" $pgOk

# 3  datasets
$sData = Join-Path $repo "evals\data\longmemeval_s_cleaned.json"
Check "s dataset" (Test-Path $sData)
foreach ($f in @("questions.jsonl", "haystacks\lme_v2_small.json",
                 "trajectories_small.jsonl")) {
    $p = Join-Path $repo "evals\data\lme_v2\$f"
    Check "lme_v2 $f" (Test-Path $p)
}

# 4  source run for the write-side replay (untagged s/qwen-27b, needs contexts)
$src = Join-Path $results "longmemeval-ku-s-qwen-27b.jsonl"
if (Test-Path $src) {
    $rows = @(Get-Content $src | Where-Object { $_.Trim() })
    $hasCtx = $rows.Count -gt 0 -and ($rows[0] | ConvertFrom-Json).contexts
    Check "wabl source run (78 rows + contexts)" `
        ($rows.Count -eq 78 -and [bool]$hasCtx) "$($rows.Count) rows"
} else {
    Check "wabl source run" $false "missing $src"
}

# 5  GPU + model server launcher
$bat = "$env:USERPROFILE\ClaudeCode\llama.ccp\run-server-turboq.bat"
Check "qwen server launcher" (Test-Path $bat) $bat
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
Check "nvidia-smi present" ([bool]$smi)

# 6  disk headroom (band dumps for s are ~hundreds of MB; models/logs more)
$free = (Get-PSDrive -Name (Split-Path -Qualifier $repo).TrimEnd(':')).Free / 1GB
Check "disk free >= 20 GB" ($free -ge 20) ("{0:N0} GB free" -f $free)

# 7  output-collision awareness (informative — resume is safe, but a
#    complete prior run under the same tag means this night is a no-op)
$v2Out = Join-Path $results "lme-v2-smoke-$Tag.jsonl"
if (Test-Path $v2Out) {
    $n = @(Get-Content $v2Out | Where-Object { $_.Trim() }).Count
    Write-Host "[note] $Tag already has $n rows — the run resumes after them"
}
foreach ($d in @("s-qwen-27b-ablbands", "s-qwen-27b-ablbands-flat")) {
    $p = Join-Path $results "banks\$d"
    if (Test-Path $p) {
        $n = @(Get-ChildItem $p -Filter *.json.gz).Count
        Write-Host "[note] $d already has $n dumps — replay skips them"
    }
}

Write-Host ""
if ($fail) { Write-Host "$fail FAILURE(S) — do not launch"; exit 1 }
Write-Host "all checks passed"
exit 0
