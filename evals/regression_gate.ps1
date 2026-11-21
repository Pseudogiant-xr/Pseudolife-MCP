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
if (-not (Test-Path -LiteralPath $Fixture -PathType Leaf)) {
    Write-Host 'Local Rust gate fixture missing. Project the pinned dumps with evals/rebuild_contexts.py.'
    exit 2
}
if (-not $Establish -and -not (Test-Path -LiteralPath $Baseline -PathType Leaf)) {
    Write-Host 'Rust baseline missing. The judged gate cannot pass until a measured Rust baseline exists.'
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
        & $Python @arguments
        if ($LASTEXITCODE -in @(0, 1)) { exit $LASTEXITCODE }
        exit 2
    } finally {
        Stop-Qwen -Owned
    }
} catch {
    Write-Host "Rust regression gate incomplete ($($_.Exception.Message))"
    exit 2
}
