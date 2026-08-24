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
needed — ``docker cp`` materializes a prepared artifact instead.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BACKUP_PS1 = REPO / "ops" / "backup.ps1"
BACKUP_SH = REPO / "ops" / "backup.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

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


def _hermetic_env() -> dict[str, str]:
    """The mirror knobs are real user settings; scrub them so a maintainer's
    machine config cannot change what these tests exercise."""
    env = os.environ.copy()
    env.pop("PSEUDOLIFE_BACKUP_MIRROR", None)
    env.pop("PSEUDOLIFE_BACKUP_MIRROR_KEEP", None)
    return env


def _fixture(tmp_path: Path, sql: str) -> Path:
    art = tmp_path / "artifact.sql.gz"
    art.write_bytes(gzip.compress(sql.encode("utf-8")))
    return art


# ----------------------------------------------------------------------
# Static shape: the pipeline is gone, the marker check is there
# ----------------------------------------------------------------------

@pytest.mark.parametrize("script", [BACKUP_PS1, BACKUP_SH], ids=["ps1", "sh"])
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

def _run_ps1(tmp_path: Path, artifact: Path, out_dir: Path, fail_dump: bool):
    driver = tmp_path / "driver.ps1"
    # The dump stub fails the way a killed pg_dump does: non-zero status from
    # the `docker exec sh -c "pg_dump ..."` call itself.
    dump_stub = ('$global:LASTEXITCODE = 1; return' if fail_dump
                 else '$global:LASTEXITCODE = 0; return')
    driver.write_text(
        f'''
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "exec" -and (($a -join " ") -match "pg_dump")) {{ {dump_stub} }}
    if ($a[0] -eq "cp") {{
        Copy-Item -LiteralPath "{artifact.as_posix()}" -Destination $a[2] -Force
        return
    }}
    return
}}
& "{BACKUP_PS1}" -OutDir "{out_dir.as_posix()}"
''',
        encoding="utf-8",
    )
    return subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=120, env=_hermetic_env(),
    )


def _run_sh(tmp_path: Path, artifact: Path, out_dir: Path, fail_dump: bool):
    driver = tmp_path / "driver.sh"
    dump_rc = 1 if fail_dump else 0
    driver.write_text(
        f'''#!/usr/bin/env bash
set -u
ART="{artifact.as_posix()}"
docker() {{
    case "$*" in *pg_dump*) return {dump_rc} ;; esac
    if [ "$1" = "cp" ]; then cp "$ART" "$3"; return 0; fi
    return 0
}}
export -f docker
export ART
bash "{BACKUP_SH.as_posix()}" --out-dir "{out_dir.as_posix()}"
''',
        encoding="utf-8", newline="\n")
    return subprocess.run(
        [BASH, str(driver)],
        capture_output=True, text=True, timeout=120, env=_hermetic_env(),
    )


@pytest.fixture(params=["ps1", "sh"])
def run_backup(request, tmp_path):
    """Run a backup-script variant against a prepared artifact; returns
    (proc, out_dir)."""
    if request.param == "ps1" and PWSH is None:
        pytest.skip("PowerShell not on PATH")
    if request.param == "sh" and BASH is None:
        pytest.skip("bash not available")

    def run(sql: str, fail_dump: bool = False):
        out_dir = tmp_path / "out"
        artifact = _fixture(tmp_path, sql)
        runner = _run_ps1 if request.param == "ps1" else _run_sh
        return runner(tmp_path, artifact, out_dir, fail_dump), out_dir

    return run


def test_a_failing_dump_fails_the_backup(run_backup):
    """The other half of #172, and the half a text search cannot pin: when
    the dump command itself reports failure, the script must stop there —
    not copy out whatever the container happened to leave behind.

    Without this, deleting the ``$LASTEXITCODE`` / ``if !`` check would leave
    every other test in this file green, because they all pair a successful
    stub with a good artifact.
    """
    proc, out_dir = run_backup(COMPLETE_DUMP, fail_dump=True)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "a failed pg_dump was reported as a successful backup:\n" + out)
    assert "pg_dump failed" in out.lower(), out
    assert list(out_dir.glob("pseudolife_memory-*")) == [], (
        "a failed dump still produced an artifact")


def test_truncated_dump_fails_the_backup(run_backup):
    """The reported defect: valid gzip, non-empty, silently truncated."""
    proc, out_dir = run_backup(TRUNCATED_DUMP)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        "a truncated dump was accepted as a good backup:\n" + out)
    assert "truncated" in out.lower() or "incomplete" in out.lower(), out


def test_truncated_dump_does_not_masquerade_as_the_newest_backup(run_backup):
    """``restore.ps1`` and the rotation both glob ``*.sql.gz``: a rejected
    artifact must not sit there looking like the newest good backup."""
    proc, out_dir = run_backup(TRUNCATED_DUMP)
    assert proc.returncode != 0
    left = list(out_dir.glob("pseudolife_memory-*.sql.gz"))
    assert left == [], (
        f"a rejected backup was left where restore would pick it up: {left}")


def test_complete_dump_still_succeeds(run_backup):
    """The happy path must keep working."""
    proc, out_dir = run_backup(COMPLETE_DUMP)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert len(list(out_dir.glob("pseudolife_memory-*.sql.gz"))) == 1, out
