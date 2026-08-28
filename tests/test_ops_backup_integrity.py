"""``ops/backup.ps1|.sh`` must fail when the dump fails (issue #172).

Both scripts used to run, inside the container::

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

Harness style follows test_ops_backup_mirror.py: the real script runs with
``docker`` stubbed (PS function / exported bash function), so no Postgres is
needed — ``docker cp`` materializes a prepared artifact instead. The three
distinct dump states run as one batch per shell (``tests/ops_harness.py``),
each with its own artifact and its own output directory.
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
SCENARIOS: dict[str, tuple[str, bool]] = {
    "failing_dump": (COMPLETE_DUMP, True),
    "truncated": (TRUNCATED_DUMP, False),
    "complete": (COMPLETE_DUMP, False),
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
# Execution: a truncated dump must fail the backup
# ----------------------------------------------------------------------

def _stage(root: Path, name: str, sql: str) -> tuple[Path, Path]:
    sdir = scenario_dir(root, name)
    artifact = sdir / "artifact.sql.gz"
    artifact.write_bytes(gzip.compress(sql.encode("utf-8")))
    return artifact, sdir / "out"


def _ps1_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (sql, fail_dump) in SCENARIOS.items():
        artifact, out_dir = _stage(root, name, sql)
        # The dump stub fails the way a killed pg_dump does: non-zero status
        # from the `docker exec sh -c "pg_dump ..."` call itself.
        dump_stub = ('$global:LASTEXITCODE = 1; return' if fail_dump
                     else '$global:LASTEXITCODE = 0; return')
        setup = f'''
$global:Artifact = "{artifact.as_posix()}"
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "exec" -and (($a -join " ") -match "pg_dump")) {{ {dump_stub} }}
    if ($a[0] -eq "cp") {{
        Copy-Item -LiteralPath $global:Artifact -Destination $a[2] -Force
        return
    }}
    return
}}
'''
        invoke = f'& "{BACKUP_PS1}" -OutDir "{out_dir.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    return scenarios


def _sh_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (sql, fail_dump) in SCENARIOS.items():
        artifact, out_dir = _stage(root, name, sql)
        setup = f'''
export ART="{artifact.as_posix()}"
docker() {{
    case "$*" in *pg_dump*) return {1 if fail_dump else 0} ;; esac
    if [ "$1" = "cp" ]; then cp "$ART" "$3"; return 0; fi
    return 0
}}
export -f docker
'''
        invoke = f'bash "{BACKUP_SH.as_posix()}" --out-dir "{out_dir.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    return scenarios


@pytest.fixture(scope="module")
def ps1_batch(tmp_path_factory):
    if PWSH is None:
        pytest.skip("PowerShell not on PATH")
    root = tmp_path_factory.mktemp("backup_integrity_ps1")
    return run_ps1_batch(root, _ps1_scenarios(root), env=hermetic_env())


@pytest.fixture(scope="module")
def sh_batch(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("backup_integrity_sh")
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
