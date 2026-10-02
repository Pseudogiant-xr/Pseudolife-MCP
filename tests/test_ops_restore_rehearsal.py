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
what is under test — no Postgres, no containers. The ten rehearsal scenarios
run in ONE pwsh (``tests/ops_harness.py``), each with its own row-count maps
and its own captured output.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

import pytest

from tests.ops_harness import PWSH, Scenario, run_ps1_batch, scenario_dir

REPO = Path(__file__).resolve().parents[1]
RESTORE_PS1 = REPO / "ops" / "restore.ps1"
RESTORE_SH = REPO / "ops" / "restore.sh"

TABLES = ["entries", "facts", "world_facts", "lessons",
          "entities", "edges", "episodes"]

# Per-test, NOT module-scoped: the static checks below cover restore.sh, and
# a Linux/macOS box without pwsh is exactly the platform that port exists for
# — a module-level skip would leave it with no coverage there at all.
requires_pwsh = pytest.mark.skipif(PWSH is None, reason="pwsh not available")


def _full(n: int) -> dict:
    return {t: n for t in TABLES}


def _lost(table: str) -> dict:
    restored = _full(100)
    restored[table] = 0
    return restored


def _unreadable(side: str) -> tuple[dict, dict]:
    live, restored = _full(100), _full(100)
    {"live": live, "restored": restored}[side]["lessons"] = -1
    return live, restored


def _whole() -> dict:
    restored = _full(100)
    restored["entries"] = 95
    return restored


def _partial() -> dict:
    restored = _full(100)
    restored["lessons"] = 3
    return restored


def _small_drift() -> tuple[dict, dict]:
    live = _full(100)
    live["episodes"] = 4
    restored = _full(100)
    restored["episodes"] = 1
    return live, restored


# One entry per execution test: (live counts, restored counts).
SCENARIOS: dict[str, tuple[dict, dict]] = {
    "whole_restore": (_full(100), _whole()),
    **{f"lost_{t}": (_full(100), _lost(t))
       for t in ("lessons", "episodes", "entities", "edges", "world_facts")},
    "partial_loss": (_full(100), _partial()),
    "unreadable_live": _unreadable("live"),
    "unreadable_restored": _unreadable("restored"),
    "small_table_drift": _small_drift(),
}


def _scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (live, restored) in SCENARIOS.items():
        sdir = scenario_dir(root, name)
        backup = sdir / "pseudolife_memory-20260825-000000.sql.gz"
        backup.write_bytes(gzip.compress(b"-- dump\n"))
        # Both maps are re-bound per scenario; a stale $global:live would
        # answer the next scenario's row-count queries from this fixture.
        setup = f"""
$global:live = ConvertFrom-Json '{json.dumps(live)}' -AsHashtable
$global:restored = ConvertFrom-Json '{json.dumps(restored)}' -AsHashtable
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
            $map = if ($db -eq "pseudolife_memory") {{ $global:live }} else {{ $global:restored }}
            return "$($map[$t])"
        }}
        return
    }}
    return
}}
"""
        # Invoked bare, not through the old driver's ``2>&1 | ForEach-Object``
        # stringifier: piping strands restore.ps1's Write-Host output on the
        # information stream, where the per-scenario ``6>`` capture (bound to
        # the last pipeline element) never sees it. Unpiped, each stream lands
        # in its own file and the harness rejoins them.
        invoke = f'& "{RESTORE_PS1}" -BackupFile "{backup.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    return scenarios


@pytest.fixture(scope="module")
def rehearsals(tmp_path_factory):
    """Every rehearsal scenario, run once. ``restore.ps1`` signals failure by
    ``throw``, so the default completion-based exit model is the right one."""
    if PWSH is None:
        pytest.skip("pwsh not available")
    root = tmp_path_factory.mktemp("restore_rehearsal")
    return run_ps1_batch(root, _scenarios(root))


def _out(res) -> str:
    return res.stdout + res.stderr


@requires_pwsh
def test_rehearsal_passes_when_the_restore_is_whole(rehearsals):
    """Control: restored trails live only by writes since the dump."""
    res = rehearsals["whole_restore"]
    assert res.returncode == 0, _out(res)
    assert "PASSED" in _out(res), _out(res)


@requires_pwsh
@pytest.mark.parametrize("lost", ["lessons", "episodes", "entities",
                                  "edges", "world_facts"])
def test_rehearsal_alarms_when_any_table_is_lost(rehearsals, lost):
    """The reported defect: a dump truncated after entries/facts rehearsed
    clean while every other table came back empty."""
    res = rehearsals[f"lost_{lost}"]
    out = _out(res)
    assert res.returncode != 0, (
        f"rehearsal PASSED with '{lost}' completely lost:\n{out}")
    # The count table prints every name, so the alarm has to name the table
    # WITH its counts for this to mean anything.
    assert re.search(rf"{lost} \(live=", out), (
        f"the alarm does not say which table was lost:\n{out}")


@requires_pwsh
def test_rehearsal_alarms_on_material_partial_loss(rehearsals):
    """Truncation usually takes *most* of a table, not all of it."""
    res = rehearsals["partial_loss"]
    out = _out(res)
    assert res.returncode != 0, (
        "rehearsal PASSED with 97% of lessons missing:\n" + out)
    assert re.search(r"lessons \(live=", out), out


@requires_pwsh
@pytest.mark.parametrize("side", ["live", "restored"])
def test_rehearsal_alarms_when_a_row_count_could_not_be_read(rehearsals, side):
    """``Get-Counts`` returns -1 when the query fails. Passing on that would
    be the original defect wearing a different hat: a PASSED verdict for a
    comparison that never ran. It is asymmetric on purpose — -1 on the
    RESTORED side used to alarm via ``-le 0`` while -1 on the LIVE side
    silently disabled every check for that table."""
    res = rehearsals[f"unreadable_{side}"]
    out = _out(res)
    assert res.returncode != 0, (
        f"rehearsal PASSED with an unreadable {side} count:\n{out}")
    assert "row count failed" in out, out


@requires_pwsh
def test_rehearsal_does_not_cry_wolf_on_a_small_table(rehearsals):
    """A handful of rows added since the dump is normal drift, and a ratio
    over single-digit counts is meaningless — it must not fail there."""
    res = rehearsals["small_table_drift"]
    assert res.returncode == 0, (
        "a 4-row table drifting by 3 rows failed the rehearsal:\n" + _out(res))


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


# ----------------------------------------------------------------------
# Real restore, --no-start / -NoStart (``pseudolife-mcp move``)
# ----------------------------------------------------------------------
#
# ``move`` restores the source's final backup on the target and must check
# the restored row counts, write its move marker and carry the environment
# identities BEFORE the target daemon starts once: a restore that starts the
# daemon would let it serve (and write) a bank that has not been verified.
# The real script runs from a copy in a temp repo whose ops/backup.* (the
# safety dump the real restore takes first) is a stub, with docker and the
# health probe stubbed too; every docker call is logged.

from tests.ops_harness import BASH, run_sh_batch  # noqa: E402

requires_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

APPLY_SCENARIOS = {
    "apply_no_start": True,
    "apply_start": False,
}


def _stage_apply(root: Path, name: str, script: Path) -> tuple[Path, Path, Path]:
    import shutil

    sdir = scenario_dir(root, name)
    repo = sdir / "repo"
    (repo / "ops").mkdir(parents=True, exist_ok=True)
    copy = repo / "ops" / script.name
    shutil.copy(script, copy)
    if script.suffix == ".ps1":
        (repo / "ops" / "backup.ps1").write_text(
            'Add-Content -LiteralPath "$ScenarioDir/calls.log" -Value ("backup " + ($args -join " "))\n'
            'Write-Host "stub safety dump"\n', encoding="utf-8")
    else:
        (repo / "ops" / "backup.sh").write_text('#!/usr/bin/env bash\n'
                                                'echo "backup $*" >> "$SCENARIO_DIR/calls.log"\n'
                                                'echo "stub safety dump"\n',
                                                encoding="utf-8", newline="\n")
        # restore.sh runs it directly: on Linux and macOS it must be
        # executable (Git Bash on Windows runs it either way).
        (repo / "ops" / "backup.sh").chmod(0o755)
    backup = sdir / "pseudolife_memory-20261002-000000.sql.gz"
    backup.write_bytes(gzip.compress(b"-- dump\n"))
    state = sdir / "pseudolife_state-20261002-000000.tgz"
    state.write_bytes(gzip.compress(b"state"))
    return copy, backup, state


_APPLY_PS1_STUB = r"""
function global:docker {
    $a = @($args | ForEach-Object { "$_" })
    Add-Content -LiteralPath "$ScenarioDir\calls.log" -Value ("docker " + ($a -join " "))
    $global:LASTEXITCODE = 0
    if ($a[0] -eq "inspect") { return "pseudolife-daemon:test" }
    return
}
function global:Invoke-RestMethod {
    Add-Content -LiteralPath "$ScenarioDir\calls.log" -Value "health"
    return [pscustomobject]@{ status = "ok"; schema = 52; db = "ok" }
}
"""

_APPLY_SH_STUB = """
docker() {
    echo "docker $*" >> "$SCENARIO_DIR/calls.log"
    case "$1" in inspect) echo "pseudolife-daemon:test" ;; esac
    return 0
}
curl() {
    echo "health" >> "$SCENARIO_DIR/calls.log"
    echo '{"status": "ok"}'
}
export -f docker curl
export SCENARIO_DIR
"""


@pytest.fixture(scope="module")
def apply_ps1_runs(tmp_path_factory):
    if PWSH is None:
        pytest.skip("pwsh not available")
    root = tmp_path_factory.mktemp("restore_apply_ps1")
    scenarios = []
    for name, no_start in APPLY_SCENARIOS.items():
        copy, backup, state = _stage_apply(root, name, RESTORE_PS1)
        flag = " -NoStart -Container pgc -Db mydb -User myuser -DaemonContainer dmn" if no_start else ""
        scenarios.append(Scenario(name, _APPLY_PS1_STUB,
                                  f'& "{copy}" -Apply{flag} -BackupFile "{backup.as_posix()}" '
                                  f'-StateArchive "{state.as_posix()}"'))
    scenarios.append(Scenario("no_start_without_apply", _APPLY_PS1_STUB,
                              f'& "{_stage_apply(root, "no_start_without_apply", RESTORE_PS1)[0]}" '
                              f'-NoStart -BackupFile "{(root / "no_start_without_apply" / "pseudolife_memory-20261002-000000.sql.gz").as_posix()}"'))
    return run_ps1_batch(root, scenarios)


@pytest.fixture(scope="module")
def apply_sh_runs(tmp_path_factory):
    if BASH is None:
        pytest.skip("bash not available")
    root = tmp_path_factory.mktemp("restore_apply_sh")
    scenarios = []
    for name, no_start in APPLY_SCENARIOS.items():
        copy, backup, state = _stage_apply(root, name, RESTORE_SH)
        flag = " --no-start --container pgc --db mydb --user myuser --daemon-container dmn" if no_start else ""
        scenarios.append(Scenario(name, _APPLY_SH_STUB,
                                  f'bash "{copy.as_posix()}" --apply{flag} --backup-file "{backup.as_posix()}" '
                                  f'--state-archive "{state.as_posix()}"'))
    copy, backup, _state = _stage_apply(root, "no_start_without_apply", RESTORE_SH)
    scenarios.append(Scenario("no_start_without_apply", _APPLY_SH_STUB,
                              f'bash "{copy.as_posix()}" --no-start --backup-file "{backup.as_posix()}"'))
    return run_sh_batch(root, scenarios)


@pytest.fixture(params=["ps1", "sh"])
def applied(request):
    batch = request.getfixturevalue(f"apply_{request.param}_runs")
    return lambda name: batch[name]


def _docker_calls(res) -> list[str]:
    return [line for line in res.lines("calls.log") if line.startswith("docker ")]


def test_apply_no_start_restores_and_leaves_the_daemon_stopped(applied):
    res = applied("apply_no_start")
    calls = _docker_calls(res)
    assert res.returncode == 0, res.detail()
    # It restored: the dump went into the database and the state archive
    # into /data, with the daemon stopped first.
    assert any(c.startswith("docker stop ") for c in calls), res.detail()
    assert any("ON_ERROR_STOP=1" in c for c in calls), res.detail()
    assert any(c.startswith("docker run ") and "tar xzf" in c for c in calls), res.detail()
    # ...and never started the daemon or probed its health.
    assert not any(c.startswith("docker start ") for c in calls), res.detail()
    assert "health" not in res.lines("calls.log"), res.detail()
    assert "left stopped" in res.stdout + res.stderr, res.detail()


def test_apply_without_no_start_still_starts_the_daemon(applied):
    """Control: the default real restore is unchanged."""
    res = applied("apply_start")
    calls = _docker_calls(res)
    assert res.returncode == 0, res.detail()
    assert any(c.startswith("docker start ") for c in calls), res.detail()
    assert "health" in res.lines("calls.log"), res.detail()


def test_no_start_without_apply_is_refused(applied):
    """A rehearsal never starts anything, so --no-start there is a mistake
    (the operator meant a real restore): refused before any docker call."""
    res = applied("no_start_without_apply")
    assert res.returncode != 0, res.detail()
    assert "--no-start" in res.stdout + res.stderr or "-NoStart" in res.stdout + res.stderr, res.detail()
    assert _docker_calls(res) == [], res.detail()


def test_the_safety_dump_backs_up_the_database_being_replaced(applied):
    """A real restore into a non-default container, database or user must
    take its safety dump of THAT database, not of the defaults."""
    res = applied("apply_no_start")
    backup = [line for line in res.lines("calls.log") if line.startswith("backup")]
    assert len(backup) == 1, res.detail()
    words = backup[0].split()
    pairs = {words[i].lstrip("-").replace("-", "").lower(): words[i + 1] for i in range(1, len(words) - 1, 2)}
    assert pairs == {"container": "pgc", "db": "mydb", "user": "myuser", "daemoncontainer": "dmn"}, res.detail()
