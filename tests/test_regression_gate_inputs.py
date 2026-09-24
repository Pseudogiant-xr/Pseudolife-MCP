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


@pytest.mark.parametrize(
    ("running", "initial_listener", "ready", "final_owner", "launch_fails", "expected"),
    [
        ("reproducible", False, True, 4242, False, (False, 0, 0)),
        ("", True, True, 4242, False, (False, 0, 0)),
        ("", False, True, 4242, False, (True, 1, 1)),
        ("", False, True, 9999, False, (False, 1, 1)),
        ("", False, False, 4242, False, (False, 1, 1)),
        ("", False, True, 4242, True, (False, 1, 0)),
    ],
)
def test_owned_qwen_launch_and_cleanup_never_stop_foreign_processes(
    running, initial_listener, ready, final_owner, launch_fails, expected
):
    """Exercise the helper with process and port mocks; never launch a model."""
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    helper = Path(__file__).resolve().parents[1] / "evals/qwen_server.ps1"
    script = f"""
. '{helper.as_posix()}'
$script:mockRunning = '{running}'
$script:mockInitialListener = ${str(initial_listener).lower()}
$script:mockReady = ${str(ready).lower()}
$script:mockFinalOwner = {final_owner}
$script:mockLaunchFails = ${str(launch_fails).lower()}
$script:listenerCalls = 0
$script:launches = 0
$script:fake = [pscustomobject]@{{Id=4242; HasExited=$false; Kills=0}}
$script:fake | Add-Member ScriptMethod Kill {{ $this.Kills++; $this.HasExited=$true }}
$script:fake | Add-Member ScriptMethod WaitForExit {{ param($ms) return $true }}
function Get-RunningQwenConfig {{ return $script:mockRunning }}
function Get-NetTCPConnection {{
    $script:listenerCalls++
    if ($script:listenerCalls -eq 1 -and $script:mockInitialListener) {{
        return [pscustomobject]@{{LocalPort=1234; OwningProcess=9999}}
    }}
    if ($script:listenerCalls -gt 1) {{
        return [pscustomobject]@{{LocalPort=1234; OwningProcess=$script:mockFinalOwner}}
    }}
}}
function Test-GpuBusy {{ return $null }}
function Test-Path {{ return $false }}
function Wait-QwenEndpoint {{ return $script:mockReady }}
function Start-Process {{
    $script:launches++
    if ($script:mockLaunchFails) {{ throw 'mock launch failure' }}
    return $script:fake
}}
function Get-Process {{ throw 'foreign process query during owned cleanup' }}
$result = Start-Qwen -Owned
Stop-Qwen -Owned
Write-Output "OWNED_RESULT=$result,$script:launches,$($script:fake.Kills)"
"""
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual = next(
        line.removeprefix("OWNED_RESULT=")
        for line in result.stdout.splitlines() if line.startswith("OWNED_RESULT=")
    )
    assert actual == f"{str(expected[0])},{expected[1]},{expected[2]}", (
        result.stdout + result.stderr
    )


def test_regression_gate_uses_owned_qwen_lifecycle():
    gate = Path(__file__).resolve().parents[1] / "evals/regression_gate.ps1"
    source = gate.read_text(encoding="utf-8")
    assert "Start-Qwen -Owned" in source
    assert "Stop-Qwen -Owned" in source


@pytest.mark.parametrize("extra_arg", ["-Fast", "-Force"])
def test_owned_qwen_rejects_incompatible_launch_modes(extra_arg):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell 7 is not installed")
    helper = Path(__file__).resolve().parents[1] / "evals/qwen_server.ps1"
    script = f". '{helper.as_posix()}'; Start-Qwen -Owned {extra_arg}"
    result = subprocess.run(
        [pwsh, "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.rstrip().endswith("False")
