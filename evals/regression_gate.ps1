#Requires -Version 7
# The Rust public-search gate. Missing inputs or an invalid baseline are exit 2.
param(
    [int]$Replicates = 2,
    [switch]$Establish,
    [string]$Fixture = (Join-Path $PSScriptRoot 'results/banks/rust-oracle-ku78.json'),
    [string]$Data = (Join-Path $PSScriptRoot 'data/longmemeval_oracle.json'),
    [string]$Baseline = (Join-Path $PSScriptRoot 'results/rust-quality/regression_gate.baseline.json'),
    [string]$Out,
    [string]$Daemon,
    [string]$DaemonRevision,
    [string]$OnnxDir,
    [string]$OrtLibrary,
    [string]$Scratch,
    [string]$Model = 'bench',
    [string]$ServedModel = 'Qwen3.8-27B-UD-Q4_K_XL.gguf',
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    Write-Host 'Local Rust gate fixture missing. From the checkout run:'
    Write-Host "  python evals/rebuild_contexts.py --dump-dir <local-bank-dumps> --out `"$Fixture`""
    Write-Host 'The default fixture path is git-ignored by design.'
    exit 2
}
if (-not $Establish -and -not (Test-Path -LiteralPath $Baseline -PathType Leaf)) {
    Write-Host 'Rust baseline missing. Once authorised by the maintainer, run evals/regression_gate.ps1 -Establish -Out <dir> with the same instrument inputs.'
    Write-Host 'The maintainer reviews <dir>/baseline-candidate.json and commits it as evals/results/rust-quality/regression_gate.baseline.json.'
    exit 2
}
if (-not ($Out -and $Daemon -and $DaemonRevision -and $OnnxDir -and $OrtLibrary -and $Scratch)) {
    Write-Host 'Provide -Out, -Daemon, -DaemonRevision, -OnnxDir, -OrtLibrary and -Scratch.'
    exit 2
}

try {
    if (-not $Python) {
        $repo = Split-Path -Parent $PSScriptRoot
        $Python = Join-Path $repo '.venv/Scripts/python.exe'
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            $Python = (Get-Command python -ErrorAction Stop).Source
        }
    }
    $driver = Join-Path $PSScriptRoot 'regression_gate.py'
    $endpoint = 'http://127.0.0.1:1234/v1'
    $profile = 'qwen3.8-27b-ud-q4-k-xl-stock-q8_0'
    $arguments = @(
        $driver, '--fixture', $Fixture, '--data', $Data, '--baseline', $Baseline,
        '--out', $Out, '--daemon', $Daemon, '--daemon-revision', $DaemonRevision,
        '--onnx-dir', $OnnxDir, '--ort-library', $OrtLibrary, '--scratch', $Scratch,
        '--replicates', $Replicates, '--answerer-url', $endpoint, '--answerer-model', $Model,
        '--answerer-profile', $profile, '--answerer-served-model', $ServedModel,
        '--judge-url', $endpoint, '--judge-model', $Model, '--judge-profile', $profile,
        '--judge-served-model', $ServedModel
    )
    if ($Establish) { $arguments += '--establish' }
    & $Python @arguments --preflight
    if ($LASTEXITCODE -ne 0) { exit 2 }

    . (Join-Path $PSScriptRoot 'qwen_server.ps1')
    try {
        if (-not (Start-Qwen -Owned)) { exit 2 }
        if (-not (Test-QwenReproducibleListener)) {
            Write-Host 'Port 1234 is serving a different Qwen launch profile. Free the port deliberately and retry with the stock profile.'
            exit 2
        }
        $measuredRegression = $false
        & $Python @arguments | ForEach-Object {
            if ($_ -ceq 'Rust retrieval gate regressed') { $measuredRegression = $true }
            Write-Host $_
        }
        $driverExit = $LASTEXITCODE
        if ($driverExit -eq 0) { exit 0 }
        if ($driverExit -eq 1 -and $measuredRegression) {
            $verdictPath = Join-Path $Out 'verdict.json'
            if (Test-Path -LiteralPath $verdictPath -PathType Leaf) {
                $verdict = Get-Content -Raw -LiteralPath $verdictPath | ConvertFrom-Json -AsHashtable
                $regressions = $verdict['regressions']
                if ($regressions -is [array] -and $regressions.Count -gt 0 -and
                    @($regressions | Where-Object { $_ -isnot [string] -or -not $_.Trim() }).Count -eq 0) {
                    exit 1
                }
            }
            Write-Host 'Driver reported a regression without a valid verdict.json. Inspect the run output and retry.'
        } elseif ($driverExit -eq 1) {
            Write-Host 'Driver exited without confirming a measured regression. Inspect the run output and retry.'
        }
        exit 2
    } finally {
        Stop-Qwen -Owned
    }
} catch {
    Write-Host "Rust regression gate incomplete ($($_.Exception.Message))"
    exit 2
}
