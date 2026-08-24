"""``ops/restore.ps1|.sh`` rehearsal must alarm on every table (issue #182).

The rehearsal restores a backup into a scratch db and prints a live-vs-
restored row-count table for seven tables — but it only ever set its failure
flag from ``entries`` and ``facts``. A dump truncated after those two
sections (they are the first and largest) therefore rehearsed "PASSED" while
losing every lesson, episode, entity, edge and world fact in the bank.

Two things are pinned:

* the whole-table alarm (live > 0, restored == 0) covers ALL counted tables,
  not just entries/facts;
* a *materially* smaller restored count alarms too — partial truncation is
  the likelier outcome than a section vanishing outright.

Same file, the real-restore path: ``DROP DATABASE`` / ``CREATE DATABASE``
ran with their exit status ignored, so a drop blocked by one leftover
session turned into a confusing failure several steps later.

Harness style follows test_ops_update_rollback.py: the real script runs with
``docker`` stubbed as a PowerShell function, so the script's own branching is
what is under test — no Postgres, no containers.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RESTORE_PS1 = REPO / "ops" / "restore.ps1"
RESTORE_SH = REPO / "ops" / "restore.sh"
PWSH = shutil.which("pwsh") or shutil.which("powershell")

TABLES = ["entries", "facts", "world_facts", "lessons",
          "entities", "edges", "episodes"]

# Per-test, NOT module-scoped: the static checks below cover restore.sh, and
# a Linux/macOS box without pwsh is exactly the platform that port exists for
# — a module-level skip would leave it with no coverage there at all.
requires_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh not available")


def _run_rehearsal(tmp_path: Path, live: dict, restored: dict):
    """Drive restore.ps1's rehearsal with stubbed row counts.

    ``docker exec <c> psql ... -d <db> -c "SELECT count(*) FROM <t>"`` is
    answered from the two maps; every other docker call succeeds quietly.
    """
    backup = tmp_path / "pseudolife_memory-20260825-000000.sql.gz"
    backup.write_bytes(gzip.compress(b"-- dump\n"))
    driver = tmp_path / "driver.ps1"
    driver.write_text(
        f"""
$live = ConvertFrom-Json '{json.dumps(live)}' -AsHashtable
$restored = ConvertFrom-Json '{json.dumps(restored)}' -AsHashtable
function global:docker {{
    $a = @($args | ForEach-Object {{ "$_" }})
    $global:LASTEXITCODE = 0
    $di = [array]::IndexOf($a, "-d")
    $ci = [array]::IndexOf($a, "-c")
    if ($a[0] -eq "exec" -and $a[2] -eq "psql" -and $di -ge 0 -and $ci -ge 0) {{
        $db = $a[$di + 1]
        $sql = $a[$ci + 1]
        if ($sql -match 'FROM (\\w+)$') {{
            $t = $Matches[1]
            $map = if ($db -eq "pseudolife_memory") {{ $live }} else {{ $restored }}
            return "$($map[$t])"
        }}
        return
    }}
    return
}}
& "{RESTORE_PS1}" -BackupFile "{backup.as_posix()}" 2>&1 | ForEach-Object {{ "$_" }}
exit $LASTEXITCODE
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", str(driver)],
        capture_output=True, text=True, timeout=180,
    )
    return proc, proc.stdout + proc.stderr


def _full(n: int) -> dict:
    return {t: n for t in TABLES}


@requires_pwsh
def test_rehearsal_passes_when_the_restore_is_whole(tmp_path):
    """Control: restored trails live only by writes since the dump."""
    restored = _full(100)
    restored["entries"] = 95
    proc, out = _run_rehearsal(tmp_path, _full(100), restored)
    assert proc.returncode == 0, out
    assert "PASSED" in out, out


@requires_pwsh
@pytest.mark.parametrize("lost", ["lessons", "episodes", "entities",
                                  "edges", "world_facts"])
def test_rehearsal_alarms_when_any_table_is_lost(tmp_path, lost):
    """The reported defect: a dump truncated after entries/facts rehearsed
    clean while every other table came back empty."""
    restored = _full(100)
    restored[lost] = 0
    proc, out = _run_rehearsal(tmp_path, _full(100), restored)
    assert proc.returncode != 0, (
        f"rehearsal PASSED with '{lost}' completely lost:\n{out}")
    # The count table prints every name, so the alarm has to name the table
    # WITH its counts for this to mean anything.
    assert re.search(rf"{lost} \(live=", out), (
        f"the alarm does not say which table was lost:\n{out}")


@requires_pwsh
def test_rehearsal_alarms_on_material_partial_loss(tmp_path):
    """Truncation usually takes *most* of a table, not all of it."""
    restored = _full(100)
    restored["lessons"] = 3
    proc, out = _run_rehearsal(tmp_path, _full(100), restored)
    assert proc.returncode != 0, (
        "rehearsal PASSED with 97% of lessons missing:\n" + out)
    assert re.search(r"lessons \(live=", out), out


@requires_pwsh
@pytest.mark.parametrize("side", ["live", "restored"])
def test_rehearsal_alarms_when_a_row_count_could_not_be_read(tmp_path, side):
    """``Get-Counts`` returns -1 when the query fails. Passing on that would
    be the original defect wearing a different hat: a PASSED verdict for a
    comparison that never ran. It is asymmetric on purpose — -1 on the
    RESTORED side used to alarm via ``-le 0`` while -1 on the LIVE side
    silently disabled every check for that table."""
    live, restored = _full(100), _full(100)
    {"live": live, "restored": restored}[side]["lessons"] = -1
    proc, out = _run_rehearsal(tmp_path, live, restored)
    assert proc.returncode != 0, (
        f"rehearsal PASSED with an unreadable {side} count:\n{out}")
    assert "row count failed" in out, out


@requires_pwsh
def test_rehearsal_does_not_cry_wolf_on_a_small_table(tmp_path):
    """A handful of rows added since the dump is normal drift, and a ratio
    over single-digit counts is meaningless — it must not fail there."""
    live = _full(100)
    live["episodes"] = 4
    restored = _full(100)
    restored["episodes"] = 1
    proc, out = _run_rehearsal(tmp_path, live, restored)
    assert proc.returncode == 0, (
        "a 4-row table drifting by 3 rows failed the rehearsal:\n" + out)


# ----------------------------------------------------------------------
# Static shape — the .sh port and the real-restore drop/create
# ----------------------------------------------------------------------

def test_restore_sh_alarm_is_not_limited_to_entries_and_facts():
    """The Linux/macOS port carries the identical bug and must carry the
    identical guard."""
    text = RESTORE_SH.read_text(encoding="utf-8")
    assert not re.search(r"case \"\$t\" in entries\|facts\)", text), (
        "restore.sh still restricts the rehearsal alarm to entries|facts")
    assert "row count failed" in text, (
        "restore.sh does not alarm when a row count could not be read")
    assert "more than half the rows are missing" in text, (
        "restore.sh has no partial-loss check")


# The status checks below are asserted STRUCTURALLY — the statement, then a
# check of its status, then the message — because a bare `"DROP DATABASE
# .*failed"` search is satisfied by a comment, or by a throw stranded inside
# an `if ($false)`. The pattern has to break if the check stops guarding the
# statement.

def test_real_restore_ps1_checks_drop_and_create_status():
    text = RESTORE_PS1.read_text(encoding="utf-8")
    assert "pg_terminate_backend" in text, (
        "restore.ps1 does not terminate straggler sessions before DROP DATABASE")
    for stmt in ("DROP DATABASE IF EXISTS", "CREATE DATABASE"):
        verb = stmt.split()[0]
        assert re.search(
            rf'-c "{stmt} \$Db"\s*\n\s*if \(\$LASTEXITCODE -ne 0\) \{{'
            rf'[\s\S]{{0,200}}{verb} DATABASE \$Db failed', text), (
            f"restore.ps1 does not check the exit status of {stmt} "
            f"immediately after issuing it")


def test_real_restore_sh_checks_drop_and_create_status():
    text = RESTORE_SH.read_text(encoding="utf-8")
    assert "pg_terminate_backend" in text, (
        "restore.sh does not terminate straggler sessions before DROP DATABASE")
    for stmt in ("DROP DATABASE IF EXISTS", "CREATE DATABASE"):
        verb = stmt.split()[0]
        assert re.search(
            rf'if ! docker exec[^\n]*-c "{stmt} \$DB"; then'
            rf'[\s\S]{{0,200}}{verb} DATABASE \$DB failed', text), (
            f"restore.sh does not check the exit status of {stmt} "
            f"immediately after issuing it")
