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
Postgres is needed — ``docker cp`` just materializes a dummy artifact. The
four scenarios run as one batch per shell (``tests/ops_harness.py``), each
with its own out/mirror directories.

``PSEUDOLIFE_BACKUP_MIRROR_KEEP`` is the one knob here that lives in the
PROCESS environment, so a shared interpreter could leak it between
scenarios: every scenario therefore sets or clears it explicitly rather than
relying on the batch's starting environment (which ``hermetic_env`` scrubs —
the knob is a real user setting and a maintainer's machine must not change
what these tests exercise).
"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from tests.ops_harness import (
    BASH,
    PWSH,
    Scenario,
    hermetic_env,
    run_ps1_batch,
    run_sh_batch,
    scenario_dir,
)

REPO = Path(__file__).resolve().parents[1]
BACKUP_PS1 = REPO / "ops" / "backup.ps1"
BACKUP_SH = REPO / "ops" / "backup.sh"

OLD_MIRROR_FILES = [
    "pseudolife_memory-20260701-000000.sql.gz",
    "pseudolife_memory-20260705-090000.sql.gz",
    "pseudolife_memory-20260712-225543.sql.gz",
]

# One entry per test: (-MirrorKeep value, PSEUDOLIFE_BACKUP_MIRROR_KEEP value).
SCENARIOS: dict[str, tuple[int | None, int | None]] = {
    "mirror_keep_2": (2, None),
    "env_keep_2": (None, 2),
    "default_age_based": (None, None),
    "mirror_keep_1": (1, None),
}


def _dump_fixture(sdir: Path) -> Path:
    """What ``docker cp`` materializes: a COMPLETE dump artifact.

    A plain "dummy-backup" text file used to do here, but the backup scripts
    now verify the artifact they copied out (issue #172) — gzip that carries
    PostgreSQL's end-of-dump marker. Retention is what these tests are about,
    so the artifact has to be one the scripts accept.
    """
    art = sdir / "artifact.sql.gz"
    art.write_bytes(gzip.compress(
        b"--\n-- PostgreSQL database dump\n--\n"
        b"CREATE TABLE entries (id bigint);\n"
        b"--\n-- PostgreSQL database dump complete\n--\n"))
    return art


def _stage(root: Path, name: str) -> tuple[Path, Path, Path]:
    sdir = scenario_dir(root, name)
    artifact = _dump_fixture(sdir)
    out_dir = sdir / "out"
    mirror = sdir / "mirror"
    mirror.mkdir(exist_ok=True)
    for filename in OLD_MIRROR_FILES:
        (mirror / filename).write_text("old", encoding="utf-8")
    return artifact, out_dir, mirror


def _ps1_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (mirror_keep, env_keep) in SCENARIOS.items():
        artifact, out_dir, mirror = _stage(root, name)
        env_line = (f"$env:PSEUDOLIFE_BACKUP_MIRROR_KEEP = '{env_keep}'"
                    if env_keep is not None else
                    "Remove-Item Env:\\PSEUDOLIFE_BACKUP_MIRROR_KEEP "
                    "-ErrorAction SilentlyContinue")
        setup = f'''
{env_line}
$global:Artifact = "{artifact.as_posix()}"
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "exec" -and $a[2] -eq "sh") {{ return }}
    if ($a[0] -eq "cp") {{
        Copy-Item -LiteralPath $global:Artifact -Destination $a[2] -Force
        return
    }}
    if ($a[0] -eq "exec" -and $a[2] -eq "rm") {{ return }}
    throw "unexpected docker call: $($a -join ' ')"
}}
'''
        args = f'-OutDir "{out_dir}" -MirrorDir "{mirror}"'
        if mirror_keep is not None:
            args += f" -MirrorKeep {mirror_keep}"
        scenarios.append(Scenario(name, setup, f'& "{BACKUP_PS1}" {args}'))
    return scenarios


def _sh_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (mirror_keep, env_keep) in SCENARIOS.items():
        artifact, out_dir, mirror = _stage(root, name)
        env_line = (f"export PSEUDOLIFE_BACKUP_MIRROR_KEEP={env_keep}"
                    if env_keep is not None
                    else "unset PSEUDOLIFE_BACKUP_MIRROR_KEEP || true")
        setup = f'''
{env_line}
ART="{artifact.as_posix()}"
docker() {{
    if [ "$1" = "exec" ] && [ "$3" = "sh" ]; then return 0
    elif [ "$1" = "cp" ]; then cp "$ART" "$3"
    elif [ "$1" = "exec" ] && [ "$3" = "rm" ]; then return 0
    else echo "unexpected docker call: $*" >&2; return 1; fi
}}
export -f docker
export ART
'''
        args = (f'--out-dir "{out_dir.as_posix()}" '
                f'--mirror-dir "{mirror.as_posix()}"')
        if mirror_keep is not None:
            args += f" --mirror-keep {mirror_keep}"
        invoke = f'bash "{BACKUP_SH.as_posix()}" {args}'
        scenarios.append(Scenario(name, setup, invoke))
    return scenarios


@pytest.fixture(scope="module")
def ps1_batch(tmp_path_factory):
    if PWSH is None:
        pytest.skip("PowerShell not on PATH")
    root = tmp_path_factory.mktemp("backup_mirror_ps1")
    return run_ps1_batch(root, _ps1_scenarios(root), env=hermetic_env())


@pytest.fixture(scope="module")
def sh_batch(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("backup_mirror_sh")
    return run_sh_batch(root, _sh_scenarios(root), env=hermetic_env())


@pytest.fixture(params=["ps1", "sh"])
def backup(request):
    """Look up one scenario's run for the script variant under test.
    Call as ``backup("mirror_keep_2")``; returns (result, out_dir, mirror)."""
    batch = request.getfixturevalue(f"{request.param}_batch")

    def get(name: str):
        res = batch[name]
        return res, res.dir / "out", res.dir / "mirror"

    return get


def _mirror_names(mirror: Path) -> list[str]:
    return sorted(p.name for p in mirror.glob("pseudolife_memory-*.sql.gz"))


def test_mirror_keep_retains_newest_n_by_name(backup):
    res, out_dir, mirror = backup("mirror_keep_2")
    assert res.returncode == 0, res.detail()
    names = _mirror_names(mirror)
    # The just-created backup (today's stamp) sorts newest; next is the
    # newest pre-seeded file. The two older pre-seeds are gone.
    assert len(names) == 2, str(names) + res.detail()
    assert names[0] == "pseudolife_memory-20260712-225543.sql.gz", names
    assert names[1].startswith("pseudolife_memory-2026"), names
    assert names[1] not in OLD_MIRROR_FILES, names


def test_mirror_keep_env_var_is_honored(backup):
    res, out_dir, mirror = backup("env_keep_2")
    assert res.returncode == 0, res.detail()
    assert len(_mirror_names(mirror)) == 2, res.detail()


def test_default_stays_age_based(backup):
    # Without the knob, freshly-written pre-seeds are inside the KeepDays
    # window and must all survive (the pre-knob behavior, unchanged).
    res, out_dir, mirror = backup("default_age_based")
    assert res.returncode == 0, res.detail()
    assert len(_mirror_names(mirror)) == len(OLD_MIRROR_FILES) + 1, res.detail()


def test_primary_backups_are_not_count_rotated(backup):
    # MirrorKeep governs the MIRROR only: the primary out-dir keeps its
    # age-based rotation regardless.
    res, out_dir, mirror = backup("mirror_keep_1")
    assert res.returncode == 0, res.detail()
    primaries = list(out_dir.glob("pseudolife_memory-*.sql.gz"))
    assert len(primaries) == 1, res.detail()   # the new dump, untouched
    assert len(_mirror_names(mirror)) == 1, res.detail()  # mirror rotated to 1
