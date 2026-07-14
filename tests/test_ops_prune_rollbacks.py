"""Rollback-tag retention: ``ops/prune-rollbacks.ps1|.sh`` + update.ps1|.sh wiring.

Why this exists: update.ps1 tags a ``pre-*`` rollback image on every deploy and
never garbage-collected them — by 2026-07-14 that was ~60 stale tags inside a
177GB docker_data.vhdx, pruned by hand. The retention scripts keep the newest N
rollback tags and remove the rest, never touching the deployed tag, any image
in use by a running container, or volumes.

The tests drive the REAL scripts with a stubbed ``docker``: for PowerShell a
function (functions shadow docker.exe on command lookup), for bash an
``export -f``-ed function (inherited by the child bash running the script), so
each script's exact docker CLI contract is pinned without a Docker daemon.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PS1_SCRIPT = REPO / "ops" / "prune-rollbacks.ps1"
SH_SCRIPT = REPO / "ops" / "prune-rollbacks.sh"
UPDATE_PS1 = REPO / "ops" / "update.ps1"
UPDATE_SH = REPO / "ops" / "update.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _find_bash() -> str | None:
    # Prefer Git Bash on Windows — System32 bash.exe launches WSL, where the
    # C:-style script paths don't resolve.
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = _find_bash()

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


def _run_ps1(tmp_path: Path, fixture: dict, *args: str):
    """Run the .ps1 with `docker` stubbed as a PowerShell function."""
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
& "{PS1_SCRIPT}" {" ".join(args)}
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


def _run_sh(tmp_path: Path, fixture: dict, *args: str):
    """Run the .sh with `docker` stubbed as an exported bash function
    (inherited by the child bash that runs the script)."""
    fx_dir = tmp_path / "fx"
    fx_dir.mkdir(exist_ok=True)
    (fx_dir / "containers.txt").write_text(
        "".join(c + "\n" for c in fixture["running_containers"]),
        encoding="utf-8", newline="\n")
    (fx_dir / "container_images.txt").write_text(
        "".join(f"{c} {i}\n" for c, i in fixture["container_images"].items()),
        encoding="utf-8", newline="\n")
    (fx_dir / "tags.tsv").write_text(
        "".join(f"{ref}\t{t['id']}\t{t['created']}\n"
                for ref, t in fixture["tags"].items()),
        encoding="utf-8", newline="\n")
    rmi_log = tmp_path / "rmi.log"
    rmi_log.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
export FX="{fx_dir.as_posix()}"
export RMI_LOG="{rmi_log.as_posix()}"
docker() {{
    if [ "$1" = "ps" ] && [ "$2" = "-q" ]; then
        cat "$FX/containers.txt"
    elif [ "$1" = "inspect" ] && [ "$2" = "--format" ] && [ "$3" = "{{{{.Image}}}}" ]; then
        shift 3
        for c in "$@"; do awk -v c="$c" '$1==c{{print $2}}' "$FX/container_images.txt"; done
    elif [ "$1" = "image" ] && [ "$2" = "ls" ] && [ "$4" = "--format" ]; then
        if [ "$3" = "{DAEMON}" ]; then cut -f1 "$FX/tags.tsv"; fi
    elif [ "$1" = "image" ] && [ "$2" = "inspect" ] && [ "$3" = "--format" ]; then
        line="$(awk -F'\\t' -v r="$5" '$1==r{{print $2 "|" $3}}' "$FX/tags.tsv")"
        if [ -z "$line" ]; then echo "image inspect on unknown ref: $5" >&2; return 1; fi
        id="${{line%%|*}}"; created="${{line##*|}}"
        case "$4" in
            "{{{{.Id}}}}|{{{{.Created}}}}") echo "$id|$created" ;;
            "{{{{.Created}}}}|{{{{.Id}}}}") echo "$created|$id" ;;
            *) echo "unexpected inspect format: $4" >&2; return 1 ;;
        esac
    elif [ "$1" = "rmi" ]; then
        echo "$2" >> "$RMI_LOG"
    else
        echo "unexpected docker call: $*" >&2
        return 1
    fi
}}
export -f docker
bash "{SH_SCRIPT.as_posix()}" "$@"
''',
        encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [BASH, str(driver), *args],
        capture_output=True, text=True, timeout=120,
    )
    removed = [ln for ln in rmi_log.read_text(encoding="utf-8").splitlines()
               if ln.strip()]
    return proc, removed


@pytest.fixture(params=["ps1", "sh"])
def prune(request, tmp_path):
    """Run the retention script variant under test. Call as
    ``prune(fixture, keep=N)``; returns (proc, removed_refs)."""
    if request.param == "ps1":
        if PWSH is None:
            pytest.skip("PowerShell not on PATH")

        def run(fixture, keep=None):
            args = ("-Keep", str(keep)) if keep is not None else ()
            return _run_ps1(tmp_path, fixture, *args)
    else:
        if BASH is None:
            pytest.skip("bash not available")

        def run(fixture, keep=None):
            args = ("--keep", str(keep)) if keep is not None else ()
            return _run_sh(tmp_path, fixture, *args)
    return run


def test_default_keeps_newest_two_rollbacks(prune):
    proc, removed = prune(_fixture())
    assert proc.returncode == 0, proc.stderr
    assert sorted(removed) == [
        f"{DAEMON}:0.7.0-pre-linux-parity",
        f"{DAEMON}:0.7.0-pre-update-20260701-000000",
    ]


def test_keep_parameter_overrides_retention_count(prune):
    proc, removed = prune(_fixture(), keep=3)
    assert proc.returncode == 0, proc.stderr
    assert removed == [f"{DAEMON}:0.7.0-pre-update-20260701-000000"]


def test_deployed_tag_and_dangling_ref_are_never_candidates(prune):
    # Even keep=0 must only ever remove pre-* rollback tags that no running
    # container uses: 0.7.0 and <none> stay, and the just-tagged rollback is
    # protected because the running daemon still uses its image.
    proc, removed = prune(_fixture(), keep=0)
    assert proc.returncode == 0, proc.stderr
    assert f"{DAEMON}:0.7.0" not in removed
    assert f"{DAEMON}:<none>" not in removed
    assert f"{DAEMON}:0.7.0-pre-update-20260714-120000" not in removed
    assert f"{DAEMON}:0.7.0-pre-update-20260712-225543" in removed


def test_image_in_use_by_running_container_is_kept(prune):
    fx = _fixture()
    # A (stopped-deploy recovery, say) container still runs the oldest
    # rollback image: that tag must survive even though it is beyond N.
    fx["running_containers"].append("cont-old")
    fx["container_images"]["cont-old"] = ID_D
    proc, removed = prune(fx)
    assert proc.returncode == 0, proc.stderr
    assert removed == [f"{DAEMON}:0.7.0-pre-linux-parity"]


def test_nothing_to_prune_is_a_quiet_success(prune):
    fx = _fixture()
    fx["tags"] = {
        f"{DAEMON}:0.7.0": {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
        f"{DAEMON}:0.7.0-pre-update-20260714-120000":
            {"id": LIVE, "created": "2026-07-14T12:00:00.5Z"},
    }
    proc, removed = prune(fx)
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


def test_update_sh_wires_retention_in():
    """update.sh (the Linux/macOS port) must mirror the wiring: a
    --keep-rollbacks flag defaulting to 2 and a non-fatal retention call."""
    text = UPDATE_SH.read_text(encoding="utf-8")
    assert "--keep-rollbacks" in text
    assert "prune-rollbacks.sh" in text
    assert "KEEP_ROLLBACKS=2" in text
