"""``ops/backup.ps1|.sh``: dump integrity (issue #172), mirror retention and
the row-count gate.

All three concerns drive the same two scripts through the same stubbed
``docker``, so they share one batch per shell rather than one each.

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

**Row-count gate.** Rotation used to promote any dump that carried the
end-of-dump marker, however empty. A logical wipe (a TRUNCATE on the wrong
database, say) followed by ``MirrorKeep`` backups would therefore rotate
every good copy off the mirror — with ``PSEUDOLIFE_BACKUP_MIRROR_KEEP=2``,
two runs (2026-09-23 fresh-eyes review). Each dump now gets a manifest of
its per-table row counts, read from the dump itself. When ``entries``,
``facts`` or ``lessons`` fell by more than the threshold against the newest
manifest that was not itself held, the run keeps the new dump but holds
local rotation and mirror pruning: it deletes nothing. The hold is sticky
until a run passes ``-AcceptRowDrop``, because the next backup of a wiped
bank would otherwise compare against the wipe and resume rotating. The
manifest is also pushed into the daemon, where ``/health`` reports its age.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from dataclasses import dataclass
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

# Killed mid-COPY right after rows that QUOTE the marker — a bank that
# remembers its own backup work holds exactly such rows. The marker is in
# the last 4096 bytes, and the single-column row IS the marker line, but
# both are data, not the end of the dump.
QUOTED_MARKER_DUMP = (
    "--\n-- PostgreSQL database dump\n--\n"
    "CREATE TABLE entries (id bigint, text text);\n"
    "COPY entries (id, text) FROM stdin;\n"
    f"1\tbackup.ps1 checks for '-- {MARKER}'\n"
    "\\.\n\n"
    "CREATE TABLE notes (text text);\n"
    "COPY notes (text) FROM stdin;\n"
    f"-- {MARKER}\n"
)

# One entry per distinct dump state: (dump SQL, does the dump command fail).
# ``truncated`` backs two tests, which read the same run rather than
# repeating it.
INTEGRITY_SCENARIOS: dict[str, tuple[str, bool]] = {
    "failing_dump": (COMPLETE_DUMP, True),
    "truncated": (TRUNCATED_DUMP, False),
    "quoted_marker": (QUOTED_MARKER_DUMP, False),
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
# Row-count gate fixtures
# ----------------------------------------------------------------------

def _counts(entries: int = 100, facts: int = 100, lessons: int = 100,
            slots: int = 50) -> dict[str, int]:
    """Per-table row counts, keyed the way pg_dump names tables.
    ``dream_run_slots`` stands in for housekeeping tables that shrink
    legitimately between dumps (642 -> 526 across 2026-09-20..23)."""
    return {"public.entries": entries, "public.facts": facts,
            "public.lessons": lessons, "public.dream_run_slots": slots}


def _dump_with(tables: dict[str, int]) -> str:
    """A complete plain-format dump whose COPY blocks hold ``tables``' rows."""
    blocks = "".join(
        f"COPY {table} (id) FROM stdin;\n"
        + "".join(f"{i}\n" for i in range(rows)) + "\\.\n\n"
        for table, rows in tables.items())
    return ("--\n-- PostgreSQL database dump\n--\n\n" + blocks
            + f"--\n-- {MARKER}\n--\n\n")


def _manifest_json(stamp: str, rotation: str, tables: dict[str, int]) -> str:
    return json.dumps({
        "dump": f"pseudolife_memory-{stamp}.sql.gz",
        "created_at": "2026-01-01T00:00:00Z",
        "rotation": rotation,
        "baseline": None,
        "note": "",
        "tables": tables,
    }, indent=2)


def _manifest_name(stamp: str) -> str:
    return f"pseudolife_manifest-{stamp}.json"


# Every staged stamp sorts before any stamp a run can produce today.
AGED_STAMP = "20250101-000000"     # an old local dump + manifest, aged past KeepDays
BASELINE_STAMP = "20260101-000000"  # the last good backup before the scenario
LATER_STAMP = "20260102-000000"     # a later run: held, or accepted
STAGED_STAMPS = {AGED_STAMP, BASELINE_STAMP, LATER_STAMP}
AGED_DUMP = f"pseudolife_memory-{AGED_STAMP}.sql.gz"
BASE = _counts()
WIPED = _counts(entries=3)
NO_LESSONS = {k: v for k, v in BASE.items() if k != "public.lessons"}


@dataclass(frozen=True)
class GateCase:
    dump: dict[str, int]
    # Manifests staged beside the aged dump: (stamp, rotation, tables).
    local: tuple[tuple[str, str, dict[str, int]], ...] = ()
    # None = no mirror; otherwise the manifests staged on the mirror, which
    # also gets OLD_MIRROR_FILES and is pruned to ``mirror_keep`` if allowed.
    mirror: tuple[tuple[str, str, dict[str, int]], ...] | None = None
    mirror_keep: int | None = None
    accept: bool = False
    max_drop: int | None = None
    daemon_down: bool = False


_GOOD = ((BASELINE_STAMP, "ok", BASE),)

GATE_SCENARIOS: dict[str, GateCase] = {
    "gate_first_run": GateCase(dump=BASE),
    "gate_small_drop": GateCase(dump=_counts(entries=90, slots=5), local=_GOOD),
    "gate_wipe_entries": GateCase(dump=WIPED, local=_GOOD, mirror=(),
                                  mirror_keep=1),
    "gate_wipe_facts": GateCase(dump=_counts(facts=0), local=_GOOD),
    "gate_lessons_table_gone": GateCase(dump=NO_LESSONS, local=_GOOD),
    "gate_sticky": GateCase(dump=WIPED,
                            local=_GOOD + ((LATER_STAMP, "held", WIPED),)),
    "gate_accept": GateCase(dump=WIPED, local=_GOOD, accept=True),
    "gate_after_accept": GateCase(
        dump=WIPED, local=_GOOD + ((LATER_STAMP, "accepted", WIPED),)),
    "gate_mirror_baseline": GateCase(dump=WIPED, mirror=_GOOD, mirror_keep=1),
    "gate_custom_threshold": GateCase(dump=_counts(entries=90), local=_GOOD,
                                      max_drop=5),
    "gate_daemon_down": GateCase(dump=BASE, daemon_down=True),
}
WIPE_SCENARIOS = ["gate_wipe_entries", "gate_wipe_facts",
                  "gate_lessons_table_gone"]


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


def _stage_gate(root: Path, name: str, case: GateCase) -> tuple[Path, Path, Path | None]:
    """Stage a gate scenario on top of ``_stage``: an aged local dump (so a
    rotation that runs is observable), the scenario's manifests, and the
    mirror's manifests beside its OLD_MIRROR_FILES."""
    artifact, out_dir, mirror = _stage(root, name, _dump_with(case.dump),
                                       mirror=case.mirror is not None)
    out_dir.mkdir(exist_ok=True)
    aged = out_dir / AGED_DUMP
    aged.write_text("old", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(aged, (old, old))
    for stamp, rotation, tables in case.local:
        (out_dir / _manifest_name(stamp)).write_text(
            _manifest_json(stamp, rotation, tables), encoding="utf-8")
    for stamp, rotation, tables in case.mirror or ():
        (mirror / _manifest_name(stamp)).write_text(
            _manifest_json(stamp, rotation, tables), encoding="utf-8")
    return artifact, out_dir, mirror


def _ps1_setup(artifact: Path, *, env_keep: int | None, fail_dump: bool,
               daemon_data: Path, daemon_down: bool = False) -> str:
    env_line = (f"$env:PSEUDOLIFE_BACKUP_MIRROR_KEEP = '{env_keep}'"
                if env_keep is not None else
                "Remove-Item Env:\\PSEUDOLIFE_BACKUP_MIRROR_KEEP "
                "-ErrorAction SilentlyContinue")
    # The dump stub fails the way a killed pg_dump does: non-zero status from
    # the `docker exec sh -c "pg_dump ..."` call itself. The state-volume tar
    # goes through the same `exec ... sh` verb and must stay successful.
    dump_rc = 1 if fail_dump else 0
    # `docker cp` runs both ways: the dump and state tar come OUT of a
    # container (materialized from the prepared artifact), while the backup
    # record goes INTO the daemon (landed in daemon_data for inspection).
    daemon_rc = 1 if daemon_down else 0
    return f'''
{env_line}
$global:Artifact = "{artifact.as_posix()}"
$global:DaemonData = "{daemon_data.as_posix()}"
function global:docker {{
    $global:LASTEXITCODE = 0
    $a = @($args | ForEach-Object {{ "$_" }})
    if ($a[0] -eq "exec" -and $a[2] -eq "sh") {{
        if (($a -join " ") -match "pg_dump") {{ $global:LASTEXITCODE = {dump_rc} }}
        return
    }}
    if ($a[0] -eq "cp" -and $a[2] -like "pseudolife-mcp-daemon:*") {{
        if ({daemon_rc}) {{ $global:LASTEXITCODE = {daemon_rc}; return }}
        New-Item -ItemType Directory -Force -Path $global:DaemonData | Out-Null
        Copy-Item -LiteralPath $a[1] -Force `
            -Destination (Join-Path $global:DaemonData ($a[2] -replace '^.*/', ''))
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


def _sh_setup(artifact: Path, *, env_keep: int | None, fail_dump: bool,
              daemon_data: Path, daemon_down: bool = False) -> str:
    env_line = (f"export PSEUDOLIFE_BACKUP_MIRROR_KEEP={env_keep}"
                if env_keep is not None
                else "unset PSEUDOLIFE_BACKUP_MIRROR_KEEP || true")
    return f'''
{env_line}
export ART="{artifact.as_posix()}"
export DAEMON_DATA="{daemon_data.as_posix()}"
docker() {{
    if [ "$1" = "exec" ] && [ "$3" = "sh" ]; then
        case "$*" in *pg_dump*) return {1 if fail_dump else 0} ;; esac
        return 0
    elif [ "$1" = "cp" ]; then
        case "$3" in
            pseudolife-mcp-daemon:*)
                [ {1 if daemon_down else 0} -eq 0 ] || return 1
                mkdir -p "$DAEMON_DATA" && cp "$2" "$DAEMON_DATA/${{3##*/}}" ;;
            *) cp "$ART" "$3" ;;
        esac
    elif [ "$1" = "exec" ] && [ "$3" = "rm" ]; then return 0
    else echo "unexpected docker call: $*" >&2; return 1; fi
}}
export -f docker
'''


def _ps1_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (sql, fail_dump) in INTEGRITY_SCENARIOS.items():
        artifact, out_dir, _ = _stage(root, name, sql)
        setup = _ps1_setup(artifact, env_keep=None, fail_dump=fail_dump,
                           daemon_data=out_dir.parent / "daemon_data")
        invoke = f'& "{BACKUP_PS1}" -OutDir "{out_dir.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    for name, (mirror_keep, env_keep) in MIRROR_SCENARIOS.items():
        artifact, out_dir, mirror = _stage(root, name, COMPLETE_DUMP,
                                           mirror=True)
        setup = _ps1_setup(artifact, env_keep=env_keep, fail_dump=False,
                           daemon_data=out_dir.parent / "daemon_data")
        args = f'-OutDir "{out_dir}" -MirrorDir "{mirror}"'
        if mirror_keep is not None:
            args += f" -MirrorKeep {mirror_keep}"
        scenarios.append(Scenario(name, setup, f'& "{BACKUP_PS1}" {args}'))
    for name, case in GATE_SCENARIOS.items():
        artifact, out_dir, mirror = _stage_gate(root, name, case)
        setup = _ps1_setup(artifact, env_keep=None, fail_dump=False,
                           daemon_data=out_dir.parent / "daemon_data",
                           daemon_down=case.daemon_down)
        args = f'-OutDir "{out_dir}"'
        if mirror is not None:
            args += f' -MirrorDir "{mirror}"'
        if case.mirror_keep is not None:
            args += f" -MirrorKeep {case.mirror_keep}"
        if case.accept:
            args += " -AcceptRowDrop"
        if case.max_drop is not None:
            args += f" -MaxRowDropPercent {case.max_drop}"
        scenarios.append(Scenario(name, setup, f'& "{BACKUP_PS1}" {args}'))
    return scenarios


def _sh_scenarios(root: Path) -> list[Scenario]:
    scenarios = []
    for name, (sql, fail_dump) in INTEGRITY_SCENARIOS.items():
        artifact, out_dir, _ = _stage(root, name, sql)
        setup = _sh_setup(artifact, env_keep=None, fail_dump=fail_dump,
                          daemon_data=out_dir.parent / "daemon_data")
        invoke = f'bash "{BACKUP_SH.as_posix()}" --out-dir "{out_dir.as_posix()}"'
        scenarios.append(Scenario(name, setup, invoke))
    for name, (mirror_keep, env_keep) in MIRROR_SCENARIOS.items():
        artifact, out_dir, mirror = _stage(root, name, COMPLETE_DUMP,
                                           mirror=True)
        setup = _sh_setup(artifact, env_keep=env_keep, fail_dump=False,
                          daemon_data=out_dir.parent / "daemon_data")
        args = (f'--out-dir "{out_dir.as_posix()}" '
                f'--mirror-dir "{mirror.as_posix()}"')
        if mirror_keep is not None:
            args += f" --mirror-keep {mirror_keep}"
        invoke = f'bash "{BACKUP_SH.as_posix()}" {args}'
        scenarios.append(Scenario(name, setup, invoke))
    for name, case in GATE_SCENARIOS.items():
        artifact, out_dir, mirror = _stage_gate(root, name, case)
        setup = _sh_setup(artifact, env_keep=None, fail_dump=False,
                          daemon_data=out_dir.parent / "daemon_data",
                          daemon_down=case.daemon_down)
        args = f'--out-dir "{out_dir.as_posix()}"'
        if mirror is not None:
            args += f' --mirror-dir "{mirror.as_posix()}"'
        if case.mirror_keep is not None:
            args += f" --mirror-keep {case.mirror_keep}"
        if case.accept:
            args += " --accept-row-drop"
        if case.max_drop is not None:
            args += f" --max-row-drop-percent {case.max_drop}"
        scenarios.append(Scenario(
            name, setup, f'bash "{BACKUP_SH.as_posix()}" {args}'))
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


def test_a_quoted_marker_inside_copy_data_is_not_the_end(run_backup):
    """The marker only counts outside COPY data. A dump killed right after
    rows that quote it — even a single-column row that is exactly the marker
    line — is still truncated. (The pre-gate tail check accepted this.)"""
    res, out_dir = run_backup("quoted_marker")
    assert res.returncode != 0, (
        "a truncated dump passed because a data row quoted the marker:\n"
        + res.stdout + res.stderr)
    assert list(out_dir.glob("pseudolife_memory-*.sql.gz")) == [], res.detail()


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


# ----------------------------------------------------------------------
# Execution: the row-count gate
# ----------------------------------------------------------------------

@pytest.fixture(params=["ps1", "sh"])
def gate(request):
    """Look up one gate scenario's run for the script variant under test.
    Call as ``gate("gate_wipe_entries")``; returns
    (result, out_dir, mirror_dir, new_manifest) where ``new_manifest`` is
    the parsed manifest this run wrote (None when it wrote none)."""
    batch = request.getfixturevalue(f"{request.param}_batch")

    def get(name: str):
        res = batch[name]
        out_dir = res.dir / "out"
        return res, out_dir, res.dir / "mirror", _new_manifest(out_dir)

    return get


def _new_manifest(out_dir: Path) -> dict | None:
    """The manifest written by THIS run — every staged one carries a stamp
    from STAGED_STAMPS."""
    fresh = [p for p in out_dir.glob("pseudolife_manifest-*.json")
             if p.name[len("pseudolife_manifest-"):-len(".json")]
             not in STAGED_STAMPS]
    assert len(fresh) <= 1, fresh
    if not fresh:
        return None
    return json.loads(fresh[0].read_text(encoding="utf-8-sig"))


def _dumps(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.glob("pseudolife_memory-*.sql.gz"))


def test_a_manifest_records_the_dump_row_counts(gate):
    """Every promoted dump gets a manifest of its per-table row counts, read
    from the dump's own COPY blocks: what a restore would really yield."""
    res, out_dir, _, manifest = gate("gate_first_run")
    assert res.returncode == 0, res.detail()
    assert manifest is not None, "no manifest beside the new dump" + res.detail()
    new_dumps = [n for n in _dumps(out_dir) if n != AGED_DUMP]
    assert manifest["dump"] == new_dumps[0], manifest
    assert manifest["tables"] == BASE, manifest
    # UTC, second precision, 'Z' suffix: the daemon parses this for /health.
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ",
                        manifest["created_at"]), manifest
    assert manifest["rotation"] == "ok", manifest
    assert manifest["baseline"] is None, manifest   # nothing to compare yet
    assert AGED_DUMP not in _dumps(out_dir), (
        "a first run has no baseline and must rotate as before" + res.detail())


def test_small_drops_and_housekeeping_shrinkage_pass(gate):
    """A drop under the threshold, and ANY drop in an ungated table
    (dream_run_slots fell 50 -> 5 here), must not hold rotation."""
    res, out_dir, _, manifest = gate("gate_small_drop")
    assert res.returncode == 0, res.detail()
    assert manifest["rotation"] == "ok", manifest
    assert manifest["baseline"] == _manifest_name(BASELINE_STAMP), manifest
    assert AGED_DUMP not in _dumps(out_dir), res.detail()


@pytest.mark.parametrize("name", WIPE_SCENARIOS)
def test_a_wipe_holds_rotation_and_deletes_nothing(gate, name):
    """THE guard. A dump whose entries, facts or lessons collapsed (or whose
    table is gone) is kept, but nothing older is deleted, the run still
    exits 0 (restore.ps1 takes its safety backup through this script), and
    the hold is said out loud."""
    res, out_dir, _, manifest = gate(name)
    out = res.stdout + res.stderr
    assert res.returncode == 0, res.detail()
    assert AGED_DUMP in _dumps(out_dir), (
        "rotation deleted an older backup after the row counts collapsed"
        + res.detail())
    assert len(_dumps(out_dir)) == 2, res.detail()   # aged + the kept new dump
    assert manifest is not None and manifest["rotation"] == "held", manifest
    assert manifest["baseline"] == _manifest_name(BASELINE_STAMP), manifest
    assert "HELD" in out, out
    assert "-AcceptRowDrop" in out or "--accept-row-drop" in out, out
    dropped = {"gate_wipe_entries": "public.entries",
               "gate_wipe_facts": "public.facts",
               "gate_lessons_table_gone": "public.lessons"}[name]
    assert dropped in out and dropped in manifest["note"], (out, manifest)


def test_a_wipe_does_not_prune_the_mirror(gate):
    """MirrorKeep=1 would leave only the wiped dump on the mirror. Held, the
    mirror keeps every older copy — and still receives the new dump."""
    res, _, mirror, manifest = gate("gate_wipe_entries")
    assert res.returncode == 0, res.detail()
    names = _mirror_names(mirror)
    assert set(OLD_MIRROR_FILES) <= set(names), names
    assert len(names) == len(OLD_MIRROR_FILES) + 1, names
    stamp = manifest["dump"].removeprefix("pseudolife_memory-").removesuffix(".sql.gz")
    assert (mirror / _manifest_name(stamp)).exists(), (
        "the manifest must travel with its dump" + res.detail())


def test_a_held_run_is_never_the_baseline(gate):
    """Sticky: the second backup of a wiped bank must compare against the
    last good manifest, not against the wipe — otherwise it passes and
    rotation resumes one run later."""
    res, out_dir, _, manifest = gate("gate_sticky")
    assert res.returncode == 0, res.detail()
    assert manifest["rotation"] == "held", manifest
    assert manifest["baseline"] == _manifest_name(BASELINE_STAMP), manifest
    assert AGED_DUMP in _dumps(out_dir), res.detail()


def test_accept_row_drop_releases_the_hold(gate):
    res, out_dir, _, manifest = gate("gate_accept")
    assert res.returncode == 0, res.detail()
    assert manifest["rotation"] == "accepted", manifest
    assert AGED_DUMP not in _dumps(out_dir), res.detail()


def test_an_accepted_drop_becomes_the_new_baseline(gate):
    res, out_dir, _, manifest = gate("gate_after_accept")
    assert res.returncode == 0, res.detail()
    assert manifest["rotation"] == "ok", manifest
    assert manifest["baseline"] == _manifest_name(LATER_STAMP), manifest
    assert AGED_DUMP not in _dumps(out_dir), res.detail()


def test_the_mirror_manifest_is_a_baseline(gate):
    """A deploy from a fresh worktree has an empty out-dir, but shares the
    mirror: the mirror's manifests must still gate its pruning."""
    res, _, mirror, manifest = gate("gate_mirror_baseline")
    assert res.returncode == 0, res.detail()
    assert manifest["rotation"] == "held", manifest
    assert manifest["baseline"] == _manifest_name(BASELINE_STAMP), manifest
    assert set(OLD_MIRROR_FILES) <= set(_mirror_names(mirror)), res.detail()


def test_the_threshold_is_configurable(gate):
    res, _, _, manifest = gate("gate_custom_threshold")
    assert res.returncode == 0, res.detail()
    assert manifest["rotation"] == "held", manifest


@pytest.mark.parametrize("name", ["gate_first_run", "gate_wipe_entries"])
def test_the_backup_record_reaches_the_daemon(gate, name):
    """/health reads its last_backup from this copy inside the daemon."""
    res, _, _, manifest = gate(name)
    pushed = res.dir / "daemon_data" / "last-backup.json"
    assert pushed.exists(), "no backup record pushed to the daemon" + res.detail()
    assert json.loads(pushed.read_text(encoding="utf-8-sig")) == manifest


def test_an_unreachable_daemon_does_not_fail_the_backup(gate):
    res, out_dir, _, manifest = gate("gate_daemon_down")
    assert res.returncode == 0, res.detail()
    assert manifest is not None, res.detail()
    assert "last-backup" in (res.stdout + res.stderr), res.detail()


def test_a_rejected_dump_writes_no_manifest_and_no_record(run_backup):
    """A manifest is a baseline candidate and the pushed record feeds
    /health: neither may exist for a dump that was never promoted."""
    for name in ("failing_dump", "truncated", "quoted_marker"):
        res, out_dir = run_backup(name)
        assert res.returncode != 0, res.detail()
        assert list(out_dir.glob("pseudolife_manifest-*")) == [], name
        assert not (res.dir / "daemon_data").exists(), name
