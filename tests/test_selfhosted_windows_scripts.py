"""The Windows parity runner's setup script and job-started hook
(ops/selfhosted/setup-windows-runner.ps1, windows-job-started.ps1)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parent.parent / "ops" / "selfhosted"
SETUP = OPS / "setup-windows-runner.ps1"
HOOK = OPS / "windows-job-started.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 (pwsh) is not installed")


def pwsh(command: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=120, **kwargs)


@needs_pwsh
@pytest.mark.parametrize("script", [SETUP, HOOK], ids=lambda path: path.name)
def test_the_script_parses(script):
    result = pwsh("$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                  f"'{script}', [ref]$null, [ref]$errors) | Out-Null; $errors | ForEach-Object {{ $_.ToString() }}")
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout + result.stderr


@needs_pwsh
def test_the_egress_rule_names_every_range():
    # In PowerShell ',' binds tighter than '+': unparenthesised, the split
    # private ranges joined into one string that the firewall refused.
    text = SETUP.read_text(encoding="utf-8")
    block = re.search(r"^\$blocked = @\(.*?\)\n", text, re.S | re.M).group(0)
    result = pwsh(block + "$blocked.Count; $blocked -join ' '")
    count, ranges = result.stdout.strip().splitlines()
    assert int(count) == 9, result.stdout
    # Split like the script's own list: the identifier guards flag private
    # addresses. All of IPv6, as on Linux (the firewall takes it as two
    # halves, not ::/0): a LAN's global prefix is no private range.
    assert ranges.split() == ["10.0" ".0.0/8", "172.16" ".0.0/12", "192.168" ".0.0/16", "169.254.0.0/16",
                              "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255", "::/1", "8000::/1"]


@pytest.mark.skipif(os.name != "nt" or PWSH is None, reason="the hook's egress check reads Windows routes")
def test_the_hook_refuses_a_job_for_an_account_the_firewall_does_not_bind(tmp_path):
    # This test's own account has no egress rule, so it can reach (or at
    # least is not refused by the firewall at) the LAN gateway: the hook
    # must fail the job before any cleanup runs.
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    env.pop("PSEUDOLIFE_PARITY_RUNNER_DIR", None)
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(HOOK)],
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "so its firewall rule does not bind; the job is refused" in result.stdout


@needs_pwsh
def test_the_runner_password_survives_cmd():
    # config.cmd is a batch file: cmd.exe strips ^ and expands % and !, so a
    # password holding one registers the service with a different password.
    text = SETUP.read_text(encoding="utf-8")
    function = re.search(r"^function New-RunnerPassword \{.*?^\}\n", text, re.S | re.M).group(0)
    result = pwsh(function + "1..300 | ForEach-Object { New-RunnerPassword }")
    passwords = result.stdout.split()
    assert len(passwords) == 300 and len(set(passwords)) == 300, result.stderr
    assert all(re.fullmatch(r"[A-Za-z0-9]{40}", password) for password in passwords)
