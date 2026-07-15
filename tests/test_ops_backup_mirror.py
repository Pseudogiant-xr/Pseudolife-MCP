"""Count-based mirror retention in ``ops/backup.ps1|.sh``.

The off-disk mirror (``PSEUDOLIFE_BACKUP_MIRROR``) previously rotated by AGE
only (the primary's ``KeepDays`` window) — with one backup per deploy that
means 10+ files on the mirror and no way to say "keep exactly N". The
``-MirrorKeep`` / ``--mirror-keep`` / ``PSEUDOLIFE_BACKUP_MIRROR_KEEP`` knob
keeps the newest N mirror files by NAME (the stamp in the filename is
chronological; mtimes are untrustworthy on cloud-synced folders, which is the
whole point of the mirror). Unset/0 keeps the existing age-based behavior.

Same harness style as test_ops_prune_rollbacks.py: the real script runs with
``docker`` stubbed (PS function / export -f bash function), so no daemon or
Postgres is needed — ``docker cp`` just materializes a dummy artifact.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKUP_PS1 = REPO / "ops" / "backup.ps1"
BACKUP_SH = REPO / "ops" / "backup.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")


def _find_bash() -> str | None:
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).exists():
            return cand
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


BASH = _find_bash()

OLD_MIRROR_FILES = [
    "pseudolife_memory-20260701-000000.sql.gz",
    "pseudolife_memory-20260705-090000.sql.gz",
    "pseudolife_memory-20260712-225543.sql.gz",
]


def _run_ps1(tmp_path: Path, *args: str, env=None):
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f'''
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "exec" -and $a[2] -eq "sh") {{ return }}
    if ($a[0] -eq "cp") {{ Set-Content -Path $a[2] -Value "dummy-backup"; return }}
    if ($a[0] -eq "exec" -and $a[2] -eq "rm") {{ return }}
    throw "unexpected docker call: $($a -join ' ')"
}}
& "{BACKUP_PS1}" {" ".join(args)}
''',
        encoding="utf-8",
    )
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120, env=env,
    )


def _run_sh(tmp_path: Path, *args: str, env=None):
    driver = tmp_path / "driver.sh"
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
docker() {{
    if [ "$1" = "exec" ] && [ "$3" = "sh" ]; then return 0
    elif [ "$1" = "cp" ]; then echo dummy-backup > "$3"
    elif [ "$1" = "exec" ] && [ "$3" = "rm" ]; then return 0
    else echo "unexpected docker call: $*" >&2; return 1; fi
}}
export -f docker
bash "{BACKUP_SH.as_posix()}" "$@"
''',
        encoding="utf-8", newline="\n")
    return subprocess.run(
        [BASH, str(driver), *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


@pytest.fixture(params=["ps1", "sh"])
def backup(request, tmp_path):
    """Run the backup script variant with a stubbed docker into tmp dirs.
    Call as ``backup(mirror_keep=..., env_keep=...)``; returns
    (proc, out_dir, mirror_dir)."""
    out_dir = tmp_path / "out"
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    for name in OLD_MIRROR_FILES:
        (mirror / name).write_text("old", encoding="utf-8")

    if request.param == "ps1":
        if PWSH is None:
            pytest.skip("PowerShell not on PATH")

        def run(mirror_keep=None, env_keep=None):
            args = ["-OutDir", f'"{out_dir}"', "-MirrorDir", f'"{mirror}"']
            if mirror_keep is not None:
                args += ["-MirrorKeep", str(mirror_keep)]
            # Hermetic by default: the machine may legitimately set the knob
            # (it's a real user setting) — scrub it unless the test opts in.
            env = os.environ.copy()
            env.pop("PSEUDOLIFE_BACKUP_MIRROR_KEEP", None)
            if env_keep is not None:
                env["PSEUDOLIFE_BACKUP_MIRROR_KEEP"] = str(env_keep)
            return _run_ps1(tmp_path, *args, env=env), out_dir, mirror
    else:
        if BASH is None:
            pytest.skip("bash not available")

        def run(mirror_keep=None, env_keep=None):
            args = ["--out-dir", str(out_dir), "--mirror-dir", str(mirror)]
            if mirror_keep is not None:
                args += ["--mirror-keep", str(mirror_keep)]
            # Hermetic by default (see the ps1 twin above).
            env = os.environ.copy()
            env.pop("PSEUDOLIFE_BACKUP_MIRROR_KEEP", None)
            if env_keep is not None:
                env["PSEUDOLIFE_BACKUP_MIRROR_KEEP"] = str(env_keep)
            return _run_sh(tmp_path, *args, env=env), out_dir, mirror
    return run


def _mirror_names(mirror: Path) -> list[str]:
    return sorted(p.name for p in mirror.glob("pseudolife_memory-*.sql.gz"))


def test_mirror_keep_retains_newest_n_by_name(backup):
    proc, out_dir, mirror = backup(mirror_keep=2)
    assert proc.returncode == 0, proc.stderr
    names = _mirror_names(mirror)
    # The just-created backup (today's stamp) sorts newest; next is the
    # newest pre-seeded file. The two older pre-seeds are gone.
    assert len(names) == 2, names
    assert names[0] == "pseudolife_memory-20260712-225543.sql.gz"
    assert names[1].startswith("pseudolife_memory-2026")
    assert names[1] not in OLD_MIRROR_FILES


def test_mirror_keep_env_var_is_honored(backup):
    proc, out_dir, mirror = backup(env_keep=2)
    assert proc.returncode == 0, proc.stderr
    assert len(_mirror_names(mirror)) == 2


def test_default_stays_age_based(backup):
    # Without the knob, freshly-written pre-seeds are inside the KeepDays
    # window and must all survive (the pre-knob behavior, unchanged).
    proc, out_dir, mirror = backup()
    assert proc.returncode == 0, proc.stderr
    assert len(_mirror_names(mirror)) == len(OLD_MIRROR_FILES) + 1


def test_primary_backups_are_not_count_rotated(backup):
    # MirrorKeep governs the MIRROR only: the primary out-dir keeps its
    # age-based rotation regardless.
    proc, out_dir, mirror = backup(mirror_keep=1)
    assert proc.returncode == 0, proc.stderr
    primaries = list(out_dir.glob("pseudolife_memory-*.sql.gz"))
    assert len(primaries) == 1              # the new dump, untouched
    assert len(_mirror_names(mirror)) == 1  # mirror rotated to 1
