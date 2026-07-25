"""``ops/update.ps1|.sh`` must not promise a rollback it did not create.

Both scripts compute ``$rollback`` unconditionally but only ``docker tag``
it when a current image exists. When it doesn't — a first build, or a
version bump whose image was never built — the tag is skipped with a
warning, and then BOTH exit paths print
``docker tag <rollback> <image_tag>`` anyway.

Seen live on 2026-07-26: the deploy warned "No current
pseudolife-daemon:0.10.0 image to tag" and then printed a rollback
command referencing a tag that does not exist. The dangerous path is the
unhealthy one — the operator is handed a command that fails at exactly
the moment the deploy just broke.

Drives the REAL script with ``docker`` and ``Invoke-RestMethod`` stubbed
as PowerShell functions (functions shadow both an exe and a cmdlet on
command lookup), so the script's own branching is what's under test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh not available")


def _run_update(tmp_path: Path, *, image_exists: bool, healthy: bool = True):
    """Run update.ps1 with docker + the health probe stubbed."""
    driver = tmp_path / "driver.ps1"
    inspect_rc = "0" if image_exists else "1"
    health = ("@{ status = 'ok'; schema = 22; persist_errors = 0 }"
              if healthy else "throw 'connection refused'")
    driver.write_text(
        f"""
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "image" -and $a[1] -eq "inspect") {{
        $global:LASTEXITCODE = {inspect_rc}
        if ({inspect_rc} -ne 0) {{ return }}
        return "[]"
    }}
    # tag / compose / anything else: succeed quietly
    $global:LASTEXITCODE = 0
    return
}}
function global:Invoke-RestMethod {{ {health} }}
& "{UPDATE_PS1}" -NoBackup -Tag unittest 2>&1 | ForEach-Object {{ "$_" }}
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=180,
    )
    return proc.stdout + proc.stderr


def test_no_rollback_image_means_no_rollback_promise(tmp_path):
    """The reported defect: a tag command for a tag that was never made."""
    out = _run_update(tmp_path, image_exists=False)

    assert "docker tag pseudolife-daemon" not in out, (
        "promised a rollback tag that was never created:\n" + out)


def test_no_rollback_image_says_so_and_offers_a_real_path(tmp_path):
    """Silence would be worse than the wrong command — the operator still
    needs to know how to get back."""
    out = _run_update(tmp_path, image_exists=False)

    assert "no rollback image" in out.lower(), out
    assert "ops" in out and "update" in out, (
        "no rebuild-from-git fallback offered:\n" + out)


def test_rollback_image_present_still_prints_the_tag_command(tmp_path):
    """The working case must keep working."""
    out = _run_update(tmp_path, image_exists=True)

    assert "docker tag pseudolife-daemon:0.10.0-unittest" in out, out


def test_unhealthy_deploy_without_a_rollback_image_is_honest(tmp_path):
    """The path that matters most: the deploy just failed, so a rollback
    instruction that cannot work is actively harmful."""
    out = _run_update(tmp_path, image_exists=False, healthy=False)

    assert "docker tag pseudolife-daemon" not in out, (
        "handed the operator a failing rollback mid-incident:\n" + out)
    assert "no rollback image" in out.lower(), out


def test_update_sh_guards_the_same_way():
    """The Linux/macOS port carries the identical bug and must carry the
    identical guard."""
    text = UPDATE_SH.read_text(encoding="utf-8")

    assert "rollback_ready" in text, (
        "update.sh does not track whether the rollback tag was created")
    assert "no rollback image" in text.lower(), (
        "update.sh has no honest no-rollback message")
