"""Rollback-tag retention: ``ops/prune-rollbacks.ps1`` + its update.ps1 wiring.

Why this exists: update.ps1 tags a ``pre-*`` rollback image on every deploy and
never garbage-collected them — by 2026-07-14 that was ~60 stale tags inside a
177GB docker_data.vhdx, pruned by hand. The retention script keeps the newest N
rollback tags and removes the rest, never touching the deployed tag, any image
in use by a running container, or volumes.

The tests drive the REAL script under pwsh with a stubbed ``docker``
*function* (a PowerShell function shadows docker.exe on command lookup), so the
exact docker CLI contract is pinned without needing a Docker daemon.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "prune-rollbacks.ps1"
UPDATE_PS1 = REPO / "ops" / "update.ps1"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell not on PATH")

LIVE = "sha256:" + "a" * 16   # image the running daemon container uses
ID_B = "sha256:" + "b" * 16
ID_C = "sha256:" + "c" * 16
ID_D = "sha256:" + "d" * 16
ID_E = "sha256:" + "e" * 16

DAEMON = "pseudolife-daemon"


def _fixture():
    """A realistic mid-deploy state: rollback for the new build just tagged
    (shares the live image id), three older rollbacks, plus refs that must
    never be candidates (the deployed tag, a dangling <none>)."""
    return {
        "running_containers": ["cont-daemon", "cont-pg"],
        "container_images": {
            "cont-daemon": LIVE,
            "cont-pg": "sha256:" + "f" * 16,
        },
        "tags": {
            f"{DAEMON}:0.7.0":
                {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
            f"{DAEMON}:0.7.0-pre-update-20260714-120000":
                {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
            f"{DAEMON}:0.7.0-pre-update-20260712-225543":
                {"id": ID_B, "created": "2026-07-12T22:55:43Z"},
            f"{DAEMON}:0.7.0-pre-linux-parity":
                {"id": ID_C, "created": "2026-07-05T09:00:00Z"},
            f"{DAEMON}:0.7.0-pre-update-20260701-000000":
                {"id": ID_D, "created": "2026-07-01T00:00:00Z"},
            f"{DAEMON}:<none>":
                {"id": ID_E, "created": "2026-06-01T00:00:00Z"},
        },
    }


def _run(tmp_path: Path, fixture: dict, *args: str):
    """Run the retention script with `docker` stubbed from the fixture.
    Returns (completed_process, list of refs passed to `docker rmi`)."""
    fx_path = tmp_path / "fixture.json"
    fx_path.write_text(json.dumps(fixture), encoding="utf-8")
    rmi_log = tmp_path / "rmi.log"
    rmi_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f'''
$fx = Get-Content -Raw "{fx_path}" | ConvertFrom-Json
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "ps" -and $a[1] -eq "-q") {{ return @($fx.running_containers) }}
    if ($a[0] -eq "inspect" -and $a[1] -eq "--format" -and $a[2] -eq "{{{{.Image}}}}") {{
        return @($a[3..($a.Count - 1)] | ForEach-Object {{ $fx.container_images.$_ }})
    }}
    if ($a[0] -eq "image" -and $a[1] -eq "ls" -and $a[3] -eq "--format") {{
        if ($a[2] -ne "{DAEMON}") {{ return @() }}
        return @($fx.tags.PSObject.Properties.Name)
    }}
    if ($a[0] -eq "image" -and $a[1] -eq "inspect" -and $a[2] -eq "--format") {{
        $t = $fx.tags.($a[4])
        if (-not $t) {{ throw "image inspect on unknown ref: $($a[4])" }}
        return "$($t.id)|$($t.created)"
    }}
    if ($a[0] -eq "rmi") {{ Add-Content "{rmi_log}" $a[1]; return }}
    throw "unexpected docker call: $($a -join ' ')"
}}
& "{SCRIPT}" {" ".join(args)}
''',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120,
    )
    removed = [ln for ln in rmi_log.read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    return proc, removed


def test_default_keeps_newest_two_rollbacks(tmp_path):
    proc, removed = _run(tmp_path, _fixture())
    assert proc.returncode == 0, proc.stderr
    assert sorted(removed) == [
        f"{DAEMON}:0.7.0-pre-linux-parity",
        f"{DAEMON}:0.7.0-pre-update-20260701-000000",
    ]


def test_keep_parameter_overrides_retention_count(tmp_path):
    proc, removed = _run(tmp_path, _fixture(), "-Keep", "3")
    assert proc.returncode == 0, proc.stderr
    assert removed == [f"{DAEMON}:0.7.0-pre-update-20260701-000000"]


def test_deployed_tag_and_dangling_ref_are_never_candidates(tmp_path):
    # Even -Keep 0 must only ever remove pre-* rollback tags that no running
    # container uses: 0.7.0 and <none> stay, and the just-tagged rollback is
    # protected because the running daemon still uses its image.
    proc, removed = _run(tmp_path, _fixture(), "-Keep", "0")
    assert proc.returncode == 0, proc.stderr
    assert f"{DAEMON}:0.7.0" not in removed
    assert f"{DAEMON}:<none>" not in removed
    assert f"{DAEMON}:0.7.0-pre-update-20260714-120000" not in removed
    assert f"{DAEMON}:0.7.0-pre-update-20260712-225543" in removed


def test_image_in_use_by_running_container_is_kept(tmp_path):
    fx = _fixture()
    # A (stopped-deploy recovery, say) container still runs the oldest
    # rollback image: that tag must survive even though it is beyond N.
    fx["running_containers"].append("cont-old")
    fx["container_images"]["cont-old"] = ID_D
    proc, removed = _run(tmp_path, fx)
    assert proc.returncode == 0, proc.stderr
    assert removed == [f"{DAEMON}:0.7.0-pre-linux-parity"]


def test_nothing_to_prune_is_a_quiet_success(tmp_path):
    fx = _fixture()
    fx["tags"] = {
        f"{DAEMON}:0.7.0": {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
        f"{DAEMON}:0.7.0-pre-update-20260714-120000":
            {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
    }
    proc, removed = _run(tmp_path, fx)
    assert proc.returncode == 0, proc.stderr
    assert removed == []
    # The script must still have run and said so (otherwise this test would
    # pass vacuously against a missing script).
    assert "rollback" in proc.stdout.lower()


def test_update_ps1_wires_retention_in():
    """update.ps1 must expose -KeepRollbacks and call the retention script;
    retention failures must not abort a deploy (wrapped, not bare)."""
    text = UPDATE_PS1.read_text(encoding="utf-8")
    assert "KeepRollbacks" in text
    assert "prune-rollbacks.ps1" in text
    assert "$KeepRollbacks = 2" in text
