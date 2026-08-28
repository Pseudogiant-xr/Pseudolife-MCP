"""``ops/backup.ps1|.sh``: dump integrity (issue #172) and mirror retention.

Both concerns drive the same two scripts through the same stubbed ``docker``,
so they share one batch per shell rather than two.

**Dump integrity.** Both scripts used to run, inside the container::

    sh -c "pg_dump ... | gzip -9 > /tmp/x.sql.gz"

The container's POSIX ``sh`` has no ``pipefail``, so the status ``docker
exec`` returns is *gzip's*, not pg_dump's. A pg_dump that dies partway
(OOM, a killed session, a read error) still leaves a well-formed, non-empty
gzip of a truncated SQL stream — which sails past the only other guard, a
zero-length check (gzip of empty input is ~20 bytes). ``update.ps1`` then
proceeds believing it has a backup, and age-based rotation eventually
deletes the last good one.

Two independent guards are pinned here:

1. the pipeline is gone — pg_dump's own exit status is what ``docker exec``
   returns (``pg_dump -Z9`` writes the gzip itself, no second process);
2. the artifact that lands on the HOST is checked for PostgreSQL's own
   end-of-dump marker, which proves the dump ran to completion *and* that
   the copy out of the container is intact.

**Mirror retention.** The off-disk mirror (``PSEUDOLIFE_BACKUP_MIRROR``)
previously rotated by AGE only (the primary's ``KeepDays`` window) — with one
backup per deploy that means 10+ files on the mirror and no way to say "keep
exactly N". The ``-MirrorKeep`` / ``--mirror-keep`` /
``PSEUDOLIFE_BACKUP_MIRROR_KEEP`` knob keeps the newest N mirror files by NAME
(the stamp in the filename is chronological; mtimes are untrustworthy on
cloud-synced folders, which is the whole point of the mirror). Unset/0 keeps
the existing age-based behavior.

Harness style follows test_ops_prune_rollbacks.py: the real script runs with
``docker`` stubbed (PS function / exported bash function), so no daemon or
Postgres is needed — ``docker cp`` materializes a prepared artifact instead.
Every scenario of a shell runs in ONE interpreter (``tests/ops_harness.py``),
each with its own artifact and its own out/mirror directories. The stub is
STRICT: an unexpected docker verb is an error, not a silent success, and the
scripts here only ever issue ``exec … sh``, ``cp`` and ``exec … rm``.

``PSEUDOLIFE_BACKUP_MIRROR_KEEP`` is the one knob here that lives in the
PROCESS environment, so a shared interpreter could leak it between
scenarios: every scenario therefore sets or clears it explicitly rather than
relying on the batch's starting environment (which ``hermetic_env`` scrubs —
the knob is a real user setting and a maintainer's machine must not change
what these tests exercise).
"""

from __future__ import annotations

import gzip
import re
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
# Carries the same cutover-dump pipeline shape; covered by the no-pipeline
# check only — it has no end-of-dump marker check (the migration's exact
# table-count verification is its completion guard).
MIGRATE_PG18_PS1 = REPO / "ops" / "migrate-pg18.ps1"

# The last line PostgreSQL writes into a plain-format dump.
MARKER = "PostgreSQL database dump complete"

COMPLETE_DUMP = (
    "--\n-- PostgreSQL database dump\n--\n"
    "CREATE TABLE entries (id bigint);\n"
    "COPY entries (id) FROM stdin;\n1\n2\n\\.\n"
    f"--\n-- {MARKER}\n--\n"
)
# What a pg_dump killed mid-COPY leaves behind: valid gzip, non-empty,
# plausible-looking SQL, no end-of-dump marker.
TRUNCATED_DUMP = (
    "--\n-- PostgreSQL database dump\n--\n"
    "CREATE TABLE entries (id bigint);\n"
    "COPY entries (id) FROM stdin;\n1\n"
)

# One entry per distinct dump state: (dump SQL, does the dump command fail).
# ``truncated`` backs two tests, which read the same run rather than
# repeating it.
INTEGRITY_SCENARIOS: dict[str, tuple[str, bool]] = {
    "failing_dump": (COMPLETE_DUMP, True),
    "truncated": (TRUNCATED_DUMP, False),
    "complete": (COMPLETE_DUMP, False),
}

OLD_MIRROR_FILES = [
    "pseudolife_memory-20260701-000000.sql.gz",
    "pseudolife_memory-20260705-090000.sql.gz",
    "pseudolife_memory-20260712-225543.sql.gz",
]

# One entry per mirror test: (-MirrorKeep value, PSEUDOLIFE_BACKUP_MIRROR_KEEP
# value). These scenarios always dump successfully — retention is what they
# are about.
MIRROR_SCENARIOS: dict[str, tuple[int | None, int | None]] = {
    "mirror_keep_2": (2, None),
    "env_keep_2": (None, 2),
    "default_age_based": (None, None),
    "mirror_keep_1": (1, None),
}


# ----------------------------------------------------------------------
# Static shape: the pipeline is gone, the marker check is there
# ----------------------------------------------------------------------

@pytest.mark.parametrize("script", [BACKUP_PS1, BACKUP_SH, MIGRATE_PG18_PS1],
                         ids=["ps1", "sh", "migrate-pg18"])
def test_dump_is_not_piped_into_gzip(script):
    """A ``pg_dump ... | gzip`` pipeline inside ``sh -c`` reports gzip's exit
    status, so pg_dump's failure is invisible to the caller."""
    text = script.read_text(encoding="utf-8")
    pipeline = re.search(r"pg_dump[^\n\"']*\|\s*gzip", text)
    assert pipeline is None, (
        f"{script.name} still pipes pg_dump into gzip inside the container "
        f"(sh has no pipefail, so the reported status is gzip's): "
        f"{pipeline.group(0) if pipeline else ''}")


@pytest.mark.parametrize("script", [BACKUP_PS1, BACKUP_SH], ids=["ps1", "sh"])
def test_artifact_is_checked_for_the_end_of_dump_marker(script):
    text = script.read_text(encoding="utf-8")
    assert MARKER in text, (
        f"{script.name} never verifies the dump reached PostgreSQL's "
        f"end-of-dump marker — a truncated dump passes the size check")


# ----------------------------------------------------------------------
# Scenario staging, shared by both concerns
# ----------------------------------------------------------------------

def _stage(root: Path, name: str, sql: str,
           *, mirror: bool = False) -> tuple[Path, Path, Path | None]:
    """Prepare one scenario's sandbox: the artifact ``docker cp`` will
    materialize, the out dir, and (for mirror scenarios) a mirror dir
    pre-seeded with older backups."""
    sdir = scenario_dir(root, name)
    artifact = sdir / "artifact.sql.gz"
    artifact.write_bytes(gzip.compress(sql.encode("utf-8")))
    mirror_dir: Path | None = None
    if mirror:
        mirror_dir = sdir / "mirror"
        mirror_dir.mkdir(exist_ok=True)
        for filename in OLD_MIRROR_FILES:
            (mirror_dir / filename).write_text("old", encoding="utf-8")
    return artifact, sdir / "out", mirror_dir


def _ps1_setup(artifact: Path, *, env_keep: int | None, fail_dump: bool) -> str:
    env_line = (f"$env:PSEUDOLIFE_BACKUP_MIRROR_KEEP = '{env_keep}'"
                if env_keep is not None else
                "Remove-Item Env:\\PSEUDOLIFE_BACKUP_MIRROR_KEEP "
                "-ErrorAction SilentlyContinue")
    # The dump stub fails the way a killed pg_dump does: non-zero status from
    # the `docker exec sh -c "pg_dump ..."` call itself. The state-volume tar
    # goes through the same `exec ... sh` verb and must stay successful.
    dump_rc = 1 if fail_dump else 0
    return f'''
{env_line}
$global:Artifact = "{artifact.as_posix()}"
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "exec" -and $a[2] -eq "sh") {{
        if (($a -join " ") -match "pg_dump") {{ $global:LASTEXITCODE = {dump_rc} }}
        return
    }}
    if ($a[0] -eq "cp") {{
        Copy-Item -LiteralPath $global:Artifact -Destination $a[2] -Force
        return
    }}
    if ($a[0] -eq "exec" -and $a[2] -eq "rm") {{ return }}
    throw "unexpected docker call: $($a -join ' ')"
}}
'''


def _sh_setup(artifact: Path, *, env_keep: int | None, fail_dump: bool) -> str:
    env_line = (f"export PSEUDOLIFE_BACKUP_MIRROR_KEEP={env_keep}"
                if env_keep is not None
                else "unset PSEUDOLIFE_BACKUP_MIRROR_KEEP || true")
    return f'''
{env_line}
export ART="{artifact.as_posix()}"
docker() {{
    if [ "$1" = "exec" ] && [ "$3" = "sh" ]; then
        case "$*" in *pg_dump*) return {1 if fail_dump else 0} ;; esac
        return 0
    elif [ "$1" = "cp" ]; then cp "$ART" "$3"
    elif [ "$1" = "exec" ] && [ "$3" = "rm" ]; then return 0
    else echo "unexpected docker call: $*" >&2; return 1; fi
}}
export -f docker
'''


def _ps1_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (sql, fail_dump) in INTEGRITY_SCENARIOS.items():
        artifact, out_dir, _ = _stage(root, name, sql)
        setup = _ps1_setup(artifact, env_keep=None, fail_dump=fail_dump)
        invoke = f'& "{BACKUP_PS1}" -OutDir "{out_dir.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    for name, (mirror_keep, env_keep) in MIRROR_SCENARIOS.items():
        artifact, out_dir, mirror = _stage(root, name, COMPLETE_DUMP,
                                           mirror=True)
        setup = _ps1_setup(artifact, env_keep=env_keep, fail_dump=False)
        args = f'-OutDir "{out_dir}" -MirrorDir "{mirror}"'
        if mirror_keep is not None:
            args += f" -MirrorKeep {mirror_keep}"
        scenarios.append(Scenario(name, setup, f'& "{BACKUP_PS1}" {args}'))
    return scenarios


def _sh_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (sql, fail_dump) in INTEGRITY_SCENARIOS.items():
        artifact, out_dir, _ = _stage(root, name, sql)
        setup = _sh_setup(artifact, env_keep=None, fail_dump=fail_dump)
        invoke = f'bash "{BACKUP_SH.as_posix()}" --out-dir "{out_dir.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    for name, (mirror_keep, env_keep) in MIRROR_SCENARIOS.items():
        artifact, out_dir, mirror = _stage(root, name, COMPLETE_DUMP,
                                           mirror=True)
        setup = _sh_setup(artifact, env_keep=env_keep, fail_dump=False)
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
    root = tmp_path_factory.mktemp("backup_ps1")
    return run_ps1_batch(root, _ps1_scenarios(root), env=hermetic_env())


@pytest.fixture(scope="module")
def sh_batch(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("backup_sh")
    return run_sh_batch(root, _sh_scenarios(root), env=hermetic_env())


@pytest.fixture(params=["ps1", "sh"])
def run_backup(request):
    """Look up one dump state's run for the script variant under test.
    Call as ``run_backup("truncated")``; returns (result, out_dir)."""
    batch = request.getfixturevalue(f"{request.param}_batch")

    def get(name: str):
        res = batch[name]
        return res, res.dir / "out"

    return get


@pytest.fixture(params=["ps1", "sh"])
def backup(request):
    """Look up one mirror scenario's run for the script variant under test.
    Call as ``backup("mirror_keep_2")``; returns (result, out_dir, mirror)."""
    batch = request.getfixturevalue(f"{request.param}_batch")

    def get(name: str):
        res = batch[name]
        return res, res.dir / "out", res.dir / "mirror"

    return get


# ----------------------------------------------------------------------
# Execution: a truncated dump must fail the backup
# ----------------------------------------------------------------------

def test_a_failing_dump_fails_the_backup(run_backup):
    """The other half of #172, and the half a text search cannot pin: when
    the dump command itself reports failure, the script must stop there —
    not copy out whatever the container happened to leave behind.

    Without this, deleting the ``$LASTEXITCODE`` / ``if !`` check would leave
    every other test in this file green, because they all pair a successful
    stub with a good artifact.
    """
    res, out_dir = run_backup("failing_dump")
    out = res.stdout + res.stderr
    assert res.returncode != 0, (
        "a failed pg_dump was reported as a successful backup:\n" + out)
    assert "pg_dump failed" in out.lower(), out
    assert list(out_dir.glob("pseudolife_memory-*")) == [], (
        "a failed dump still produced an artifact:\n" + out)


def test_truncated_dump_fails_the_backup(run_backup):
    """The reported defect: valid gzip, non-empty, silently truncated."""
    res, out_dir = run_backup("truncated")
    out = res.stdout + res.stderr
    assert res.returncode != 0, (
        "a truncated dump was accepted as a good backup:\n" + out)
    assert "truncated" in out.lower() or "incomplete" in out.lower(), out


def test_truncated_dump_does_not_masquerade_as_the_newest_backup(run_backup):
    """``restore.ps1`` and the rotation both glob ``*.sql.gz``: a rejected
    artifact must not sit there looking like the newest good backup."""
    res, out_dir = run_backup("truncated")
    assert res.returncode != 0
    left = list(out_dir.glob("pseudolife_memory-*.sql.gz"))
    assert left == [], (
        f"a rejected backup was left where restore would pick it up: {left}")


def test_complete_dump_still_succeeds(run_backup):
    """The happy path must keep working."""
    res, out_dir = run_backup("complete")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert len(list(out_dir.glob("pseudolife_memory-*.sql.gz"))) == 1, out


# ----------------------------------------------------------------------
# Execution: count-based mirror retention
# ----------------------------------------------------------------------

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
