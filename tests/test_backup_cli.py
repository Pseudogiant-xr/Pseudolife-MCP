"""``pseudolife-mcp backup`` — tier-agnostic bank backup (backup_cli.py).

The Docker tier keeps ops/backup.ps1; this command serves the pip tiers.
Contract under test:

* pg_dump | gzip to backups/pseudolife_memory-<ts>.sql.gz when a DSN is
  resolvable (explicit env, or a lite-tier embedded instance that already
  has a pgdata — backup must never CREATE a bank);
* the rest of the data dir is archived to
  backups/pseudolife_state-<ts>.tar.gz, excluding embedded_pg/ (the dump
  covers it) and backups/ itself;
* rotation deletes only files this tool created, only past --keep-days,
  and only after a successful fresh backup.
"""

from __future__ import annotations

import gzip
import os
import tarfile
import time
from pathlib import Path

import pytest

from pseudolife_memory import backup_cli
from pseudolife_memory.storage import embedded_pg as ep


def _age(path: Path, days: float) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


# ----------------------------------------------------------------------
# Rotation
# ----------------------------------------------------------------------

def test_rotation_only_touches_own_expired_files(tmp_path):
    bdir = tmp_path / "backups"
    bdir.mkdir()
    expired = bdir / "pseudolife_lite_memory-20260701-000000.sql.gz"
    expired.write_bytes(b"x")
    _age(expired, days=10)
    fresh = bdir / "pseudolife_lite_memory-20260814-000000.sql.gz"
    fresh.write_bytes(b"x")
    foreign_old = bdir / "my-manual-export.sql.gz"
    foreign_old.write_bytes(b"x")
    _age(foreign_old, days=30)
    # THE collision case (review 2026-08-14): ops/backup.* writes
    # `pseudolife_memory-<stamp>.sql.gz` into this same default dir. A
    # Docker-tier bank dump must survive lite rotation, always.
    docker_dump = bdir / "pseudolife_memory-20260601-000000.sql.gz"
    docker_dump.write_bytes(b"docker tier bank dump")
    _age(docker_dump, days=60)

    pruned = backup_cli._rotate(bdir, keep_days=7)

    assert expired in pruned and not expired.exists()
    assert fresh.exists()
    assert foreign_old.exists(), "rotation must never delete files it didn't create"
    assert docker_dump.exists(), (
        "lite rotation deleted a Docker-tier bank dump — namespace collision"
    )


def test_dumpless_run_never_rotates_dumps(tmp_path):
    """A run that produced no bank dump must not reduce dump copies."""
    bdir = tmp_path / "backups"
    bdir.mkdir()
    old_dump = bdir / "pseudolife_lite_memory-20260101-000000.sql.gz"
    old_dump.write_bytes(b"x")
    _age(old_dump, days=200)
    old_state = bdir / "pseudolife_lite_state-20260101-000000.tar.gz"
    old_state.write_bytes(b"x")
    _age(old_state, days=200)

    pruned = backup_cli._rotate(bdir, keep_days=7, include_dumps=False)

    assert old_dump.exists(), "dump rotated by a run that produced no dump"
    assert old_state in pruned and not old_state.exists()


# ----------------------------------------------------------------------
# State archive
# ----------------------------------------------------------------------

def test_state_archive_excludes_embedded_pg_and_backups(tmp_path):
    (tmp_path / "memory_state").mkdir()
    (tmp_path / "memory_state" / "weights.pt").write_bytes(b"w")
    (tmp_path / "chromadb").mkdir()
    (tmp_path / "chromadb" / "chroma.sqlite3").write_bytes(b"c")
    (tmp_path / "embedded_pg").mkdir()
    (tmp_path / "embedded_pg" / "PG_VERSION").write_text("18")
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "pseudolife_memory-20260101-000000.sql.gz").write_bytes(b"old")

    out = backup_cli._archive_state(tmp_path, bdir, "20260814-120000")

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert any("memory_state" in n for n in names)
    assert any("chromadb" in n for n in names)
    assert not any("embedded_pg" in n for n in names)
    assert not any(".sql.gz" in n for n in names)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def test_file_mode_backup_archives_state_only(tmp_path, monkeypatch):
    """No DSN, no embedded pgdata: still worth a state archive — but no
    dump, no bank creation as a side effect, and no dump rotation."""
    monkeypatch.setattr(ep, "available", lambda: True)  # installed, but
    (tmp_path / "memory_state").mkdir()                 # ...no pgdata
    bdir = tmp_path / "backups"
    bdir.mkdir()
    old_dump = bdir / "pseudolife_lite_memory-20260101-000000.sql.gz"
    old_dump.write_bytes(b"only remaining dump copy")
    _age(old_dump, days=200)

    result = backup_cli.perform_backup(tmp_path, environ={})

    assert result["dump"] is None
    assert result["state"].exists()
    assert not (tmp_path / "embedded_pg").exists(), (
        "backup must never initialize a bank"
    )
    assert old_dump.exists(), (
        "a dump-less run rotated out the only dump copy"
    )


def test_pg_dump_discovery_prefers_pg0_bundle(tmp_path):
    bin_dir = tmp_path / ".pg0" / "installation" / "18.1.0" / "bin"
    bin_dir.mkdir(parents=True)
    exe = "pg_dump.exe" if os.name == "nt" else "pg_dump"
    (bin_dir / exe).write_bytes(b"")
    found = backup_cli._find_pg_dump(home=tmp_path)
    assert found == bin_dir / exe


def test_pg_dump_discovery_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        backup_cli.shutil, "which", lambda name: r"C:\somewhere\pg_dump.exe",
    )
    found = backup_cli._find_pg_dump(home=tmp_path)  # no ~/.pg0 tree here
    assert str(found) == r"C:\somewhere\pg_dump.exe"


# ----------------------------------------------------------------------
# Integration — real embedded instance, real pg_dump
# ----------------------------------------------------------------------

@pytest.mark.skipif(not ep.available(), reason="pg0-embedded not installed")
def test_backup_roundtrip_embedded(tmp_path):
    """Dump + state archive against a live embedded instance, and the
    three review-pinned properties: the backup ATTACHES without stopping
    the running instance; the dump carries no owner/ACL statements; and
    it restores under a role-named-`pseudolife` Postgres (the Docker
    tier's shape — rehearsed against the bench PG when it is up)."""
    import pg0 as _pg0

    data_dir = tmp_path / "lite bank"
    dsn = ep.start_embedded(data_dir)
    name = ep._instance_name(data_dir / "embedded_pg")
    try:
        from pseudolife_memory.storage.postgres import PostgresStorage

        storage = PostgresStorage(dsn)  # provisions the real schema
        storage.close()

        result = backup_cli.perform_backup(data_dir, environ={})

        assert _pg0.info(name).running, (
            "backup stopped an instance it merely attached to"
        )
        dump = result["dump"]
        assert dump is not None and dump.exists()
        with gzip.open(dump, "rt", encoding="utf-8", errors="replace") as fh:
            sql = fh.read()
        assert "PostgreSQL database dump" in sql
        assert "OWNER TO" not in sql, (
            "owner-qualified dump breaks the lite->Docker restore path"
        )
        assert "GRANT " not in sql
        assert result["state"].exists()
        _rehearse_restore_under_role_named_pg(sql, dsn, tmp_path)
    finally:
        ep.stop_embedded()
        _pg0.drop(name)


def _rehearse_restore_under_role_named_pg(sql: str, dsn: str, tmp_path) -> None:
    """Replay the lite dump under a role named `pseudolife` (the Docker
    tier's role shape) the way ops/restore.* does: psql with
    ON_ERROR_STOP=1 — the only executor that handles a plain-format
    dump's inline COPY data.

    The rehearsal target is a scratch DB inside the embedded PG 18
    instance itself, with a freshly created `pseudolife` role: that
    proves the dump is owner/ACL-free (the review-found breaker) at the
    dump's own major, and stays valid regardless of what major the
    bench happens to run. (Historical note: before the Docker tier's
    16→18 bump this deliberately avoided the then-PG 16 bench, whose
    server rejected the PG 17+ SET parameters in PG 18 dumps.)"""
    import subprocess

    import psycopg

    pg_dump = backup_cli._find_pg_dump()
    exe = "psql.exe" if os.name == "nt" else "psql"
    psql = pg_dump.parent / exe if pg_dump else None
    if psql is None or not psql.exists():
        pytest.skip("no psql available for the restore rehearsal")

    scratch = f"lite_restore_rehearsal_{os.getpid()}"
    sql_file = tmp_path / "lite_dump_rehearsal.sql"
    sql_file.write_text(sql, encoding="utf-8")
    admin = psycopg.connect(dsn, connect_timeout=5, autocommit=True)
    try:
        # SUPERUSER matches the Docker tier's shape: its `pseudolife` is
        # the initdb --username superuser, which is what lets the dump's
        # CREATE EXTENSION vector run on restore.
        admin.execute(
            "DO $$ BEGIN "
            "CREATE ROLE pseudolife LOGIN SUPERUSER PASSWORD 'rehearsal'; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{scratch}" OWNER pseudolife')
        base = dsn.rsplit("@", 1)[-1].rsplit("/", 1)[0]
        scratch_dsn = f"postgresql://pseudolife:rehearsal@{base}/{scratch}"
        proc = subprocess.run(
            [str(psql), "-v", "ON_ERROR_STOP=1", "--dbname", scratch_dsn,
             "-q", "-f", str(sql_file)],
            capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode == 0, (
            f"lite dump failed to restore under a role-named Postgres:\n"
            f"{proc.stderr[-2000:]}"
        )
        with psycopg.connect(scratch_dsn, autocommit=True) as sconn:
            row = sconn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'entries'",
            ).fetchone()
            assert row is not None and row[0] == 1, (
                "restored dump is missing the entries table"
            )
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        admin.close()
