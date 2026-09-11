"""A missing bank directory must fail before deleting results or launching models."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_missing_bank_directory_preserves_prior_results_and_does_not_launch(tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    root = Path(__file__).resolve().parents[1]
    evals = tmp_path / "evals"
    results = evals / "results"
    results.mkdir(parents=True)
    shutil.copyfile(root / "evals/regression_gate.ps1", evals / "regression_gate.ps1")
    # A fake helper makes an accidental launch observable without using a GPU.
    (evals / "qwen_server.ps1").write_text(
        'function Start-Qwen { Set-Content (Join-Path $PSScriptRoot "launched") "yes"; return $false }\n'
        'function Stop-Qwen {}\n', encoding="utf-8")
    prior = results / "longmemeval-ku-oracle-e4b-ft-arm1-gate-prior.json"
    prior.write_text('prior evidence', encoding="utf-8")
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(evals / "regression_gate.ps1")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert prior.exists(), "Missing inputs must not destroy the previous gate evidence"
    assert prior.read_text(encoding="utf-8") == 'prior evidence'
    assert not (evals / "launched").exists()
    assert "required bank dumps missing" in (result.stdout + result.stderr).lower()
