"""The opt-in, backup-first retirement migration; no runtime retirement yet."""

import time

import pytest
import yaml

from pseudolife_memory import migrate_flat_cli as migration
from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401


@pytest.mark.parametrize("failure", ["lock", "permission", "connection"])
def test_finalization_lock_error_names_only_actual_contention(tmp_path, failure):
    from contextlib import nullcontext
    import psycopg
    errors = {"lock": psycopg.errors.LockNotAvailable,
              "permission": psycopg.errors.InsufficientPrivilege,
              "connection": psycopg.OperationalError}

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, sql):
            if sql.startswith("LOCK TABLE"):
                raise errors[failure]("fixture")

    with pytest.raises(migration.MigrationError) as caught:
        migration._finish(Connection(), tmp_path / "config.yaml", {})
    if failure == "lock":
        assert "entry lock unavailable; stop all bank writers" in str(caught.value)
    else:
        assert "entry table lock failed; check database connectivity and permissions" in str(caught.value)
        assert "stop all bank writers" not in str(caught.value)


def test_config_override_refuses_before_connect(tmp_path, monkeypatch, capsys):
    import psycopg
    (tmp_path / "config.yaml").write_text("memory: {miras: {preset: continuum}}\n")
    alternate = tmp_path / "alternate.yaml"
    alternate.write_text("memory: {miras: {preset: continuum}}\n")
    monkeypatch.setenv("PSEUDOLIFE_MCP_CONFIG", str(alternate))
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", "postgresql://unused.invalid/bank")
    calls = []
    def connect(*args, **kwargs):
        calls.append(True)
        raise AssertionError("must refuse before connecting")
    monkeypatch.setattr(psycopg, "connect", connect)
    assert migration.main(["--apply", "--external-recovery-verified", "--data-dir", str(tmp_path)]) == 1
    assert calls == []
    assert "PSEUDOLIFE_MCP_CONFIG does not resolve" in capsys.readouterr().err
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("config", [{"memory": None}, {"memory": {"miras": {"preset": "custom", "bands": [None]}}}])
def test_malformed_flat_layout_returns_false(config):
    assert migration._flat_layout(config, 5250) is False


def test_pending_flat_config_edits_finalize_without_restoring_entries(pg_conn, tmp_path):
    import json
    config = tmp_path / "config.yaml"
    config.write_text("unrelated: changed\nmemory: {miras: {preset: flat}}\n")
    marker = {"complete": False, "capacity": 5250, "entries": 0,
              "original_sha256": "original", "target_sha256": "target",
              "backup": {"dump": "bank.sql.gz", "state": "state.tgz"}}
    pg_conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s::jsonb)", (migration.MARKER, json.dumps(marker)))
    pg_conn.execute("INSERT INTO entries(band,text,embedding,ts) VALUES ('flat','new since migration',%s::vector,2)",
                    ("[" + ",".join(["0.1"] * 1024) + "]",))
    pg_conn.commit()
    before = config.read_bytes()
    result = migration.migrate(pg_conn, tmp_path, backup=lambda _: pytest.fail("backup repeated"))
    assert result["status"] == "resumed"
    assert pg_conn.execute("SELECT text FROM entries").fetchall() == [("new since migration",)]
    assert pg_conn.execute("SELECT value->>'complete' FROM meta WHERE key=%s", (migration.MARKER,)).fetchone()[0] == "true"
    assert config.read_bytes() == before


def test_postcommit_refusal_reports_committed_bank(pg_conn, tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("memory: {miras: {preset: continuum}}\n")
    def fail(*args):
        raise migration.MigrationError("fixture finalization failure; nothing changed")
    monkeypatch.setattr(migration, "_finish", fail)
    with pytest.raises(migration.MigrationError, match="bank migration committed") as caught:
        migration.migrate(pg_conn, tmp_path, backup=lambda _: {"dump": "bank.sql.gz", "state": "state.tgz"})
    assert "nothing changed" not in str(caught.value)


@pytest.mark.parametrize("complete", [False, True])
def test_migrated_bank_refuses_nonflat_rows_without_rewriting(pg_conn, tmp_path, complete):
    import json
    config = tmp_path / "config.yaml"
    config.write_text("memory: {miras: {preset: flat}}\n")
    marker = {"complete": complete, "capacity": 5250, "entries": 0,
              "original_sha256": "original", "target_sha256": migration._hash(config.read_bytes()),
              "backup": {"dump": "bank.sql.gz", "state": "state.tgz"}}
    pg_conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s::jsonb)", (migration.MARKER, json.dumps(marker)))
    pg_conn.execute("INSERT INTO entries(band,text,embedding,ts) VALUES ('working','late writer',%s::vector,2)",
                    ("[" + ",".join(["0.1"] * 1024) + "]",))
    pg_conn.commit()
    before = config.read_bytes()
    with pytest.raises(migration.MigrationError, match="entries outside the flat band"):
        migration.migrate(pg_conn, tmp_path, backup=lambda _: pytest.fail("backup repeated"))
    assert pg_conn.execute("SELECT value FROM meta WHERE key=%s", (migration.MARKER,)).fetchone()[0] == marker
    assert pg_conn.execute("SELECT band,text FROM entries").fetchall() == [("working", "late writer")]
    assert config.read_bytes() == before


@pytest.mark.parametrize("complete", [False, True])
def test_small_flat_config_names_required_layout(pg_conn, tmp_path, complete):
    import json
    config = tmp_path / "config.yaml"
    config.write_text("memory: {miras: {preset: flat}}\n")
    marker = {"complete": complete, "capacity": 6000, "entries": 0,
              "original_sha256": "original", "target_sha256": "target",
              "backup": {"dump": "bank.sql.gz", "state": "state.tgz"}}
    pg_conn.execute("INSERT INTO meta(key,value) VALUES (%s,%s::jsonb)", (migration.MARKER, json.dumps(marker)))
    pg_conn.commit()
    with pytest.raises(migration.MigrationError) as caught:
        migration.migrate(pg_conn, tmp_path, backup=lambda _: pytest.fail("backup repeated"))
    message = str(caught.value)
    assert "single balanced band named flat with max_entries >= 6000" in message
    if "restore" in message:
        assert message.index("max_entries >= 6000") < message.index("restore")
        assert "last resort" in message
    assert pg_conn.execute("SELECT value FROM meta WHERE key=%s", (migration.MARKER,)).fetchone()[0] == marker


def test_explicit_target_requires_recovery_acknowledgement_before_effects(tmp_path, monkeypatch, capsys):
    import psycopg
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("preflight must not access the bank or config")

    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", "postgresql://unused.invalid/bank")
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(migration, "_read_config", forbidden)
    assert migration.main(["--apply", "--data-dir", str(tmp_path)]) == 1
    assert calls == []
    assert list(tmp_path.iterdir()) == []
    output = capsys.readouterr()
    assert not output.out
    assert "a paired restore for an explicit DSN is not available yet" in output.err
    assert "--external-recovery-verified" in output.err


@pytest.mark.parametrize("preset", ["continuum", "titans", "moneta", "yaad", "memora"])
def test_retired_preset_preserves_unrelated_config(preset):
    original = {"embedding": {"device": "cpu"}, "memory": {
        "miras": {"preset": preset}, "surprise_threshold": 0.17}}
    updated, capacity = migration.flat_config(original, 6001)
    assert capacity == 6001
    assert updated["embedding"] == original["embedding"]
    assert updated["memory"]["surprise_threshold"] == 0.17
    assert updated["memory"]["miras"] == {
        "preset": "custom", "bands": [{"name": "flat", "max_entries": 6001,
        "retention_policy": "balanced", "update_interval": 1000000000,
        "promotion_access_count": 1000000000, "promotion_surprise": 1.1}]}
    assert original["memory"]["miras"]["preset"] == preset


def test_custom_capacity_sums_all_bands_without_clipping():
    config = {"memory": {"miras": {"preset": "custom", "bands": [
        {"name": "first", "max_entries": 7000, "retention_policy": "recency_heavy"},
        {"name": "second", "max_entries": 9000, "retention_policy": "surprise_heavy"}]}}}
    updated, capacity = migration.flat_config(config, 12)
    assert capacity == 16000
    assert updated["memory"]["miras"]["bands"][0]["max_entries"] == 16000


@pytest.mark.parametrize("text,reason", [
    ("date: 2026-11-02\n", "timestamp or binary"),
    ("blob: !!binary YQ==\n", "timestamp or binary"),
    ("large: 9223372036854775808\n", "integer past 64 bits"),
    ("a: &x [1, 2]\nb: *x\n", "anchor and alias"),
])
def test_unwritable_yaml_refuses_before_rewrite(tmp_path, text, reason):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(migration.MigrationError, match=reason):
        migration._read_config(path)
    assert path.read_text() == text


@pytest.mark.parametrize("miras", [
    {"preset": "typo"}, {"preset": "custom", "bands": []},
    {"preset": "custom", "bands": [{"max_entries": -1}]},
    {"preset": "custom", "bands": [{"retention_policy": "typo"}]},
])
def test_malformed_config_refused_without_guessing(miras):
    with pytest.raises(migration.MigrationError):
        migration.flat_config({"memory": {"miras": miras}}, 0)


def test_migration_preserves_every_entry_column_and_episode(pg_conn, tmp_path):
    pg_conn.execute("INSERT INTO episodes (id,title,started_at) VALUES ('ep','session',42)")
    for index, band in enumerate(["working", "micro", "instant", "fast", "medium",
                                  "slow", "archival", "forever", "orphan"]):
        pg_conn.execute(
            "INSERT INTO entries (band,text,embedding,surprise,ts,episode_id,superseded_at) "
            "VALUES (%s,'same text',%s::vector,0.4,%s,'ep',%s)",
            (band, "[" + ",".join(["0.1"] * 1024) + "]", index, 20 if index == 0 else None))
    pg_conn.commit()
    before = pg_conn.execute("SELECT to_jsonb(e)-'band' FROM entries e ORDER BY id").fetchall()
    pg_conn.commit()
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  miras:\n    preset: continuum\n", encoding="utf-8")
    calls = []

    def backup(data_dir):
        calls.append(pg_conn.execute("SELECT DISTINCT band FROM entries").fetchall())
        return {"dump": str(tmp_path / "bank.sql.gz"), "state": str(tmp_path / "state.tgz")}

    result = migration.migrate(pg_conn, tmp_path, backup=backup)
    assert result["entries"] == 9
    assert len(calls) == 1 and ("working",) in calls[0]
    assert pg_conn.execute("SELECT DISTINCT band FROM entries").fetchall() == [("flat",)]
    assert pg_conn.execute("SELECT to_jsonb(e)-'band' FROM entries e ORDER BY id").fetchall() == before
    assert pg_conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1
    assert yaml.safe_load(config.read_text())["memory"]["miras"]["preset"] == "flat"
    pg_conn.commit()
    assert migration.migrate(pg_conn, tmp_path, backup=backup)["status"] == "unchanged"
    assert len(calls) == 1


def test_pending_migration_fences_only_mismatched_config(tmp_path):
    from types import SimpleNamespace
    config = tmp_path / "config.yaml"
    config.write_bytes(b"memory: {miras: {preset: continuum}}\n")
    marker = {"capacity": 5250, "complete": False, "target_sha256": migration._hash(b"memory: {miras: {preset: flat}}\n"),
              "backup": {"dump": "bank.sql.gz", "state": "state.tgz"}}
    storage = SimpleNamespace(get_meta=lambda _: marker)
    with pytest.raises(migration.MigrationError, match="migrate-flat --apply.*external recovery tooling"):
        migration.require_config_finalized(storage, config, [("flat", 5250, "balanced")])
    config.write_bytes(b"memory: {miras: {preset: flat}}\n")
    migration.require_config_finalized(storage, config, [("flat", 5250, "balanced")])
    marker["complete"] = True
    with pytest.raises(migration.MigrationError, match="loaded config"):
        migration.require_config_finalized(storage, config, [("working", 200, "balanced")])
    config.write_bytes(b"memory: {miras: {preset: continuum}}\n")
    migration.require_config_finalized(storage, config, [("flat", 5250, "balanced")])
    migration.require_config_finalized(SimpleNamespace(get_meta=lambda _: None), config, [])


def test_backup_failure_never_changes_config_or_rows(pg_conn, tmp_path):
    config = tmp_path / "config.yaml"
    original = b"memory:\n  miras:\n    preset: continuum\n"
    config.write_bytes(original)

    def failed_backup(_):
        raise migration.MigrationError("backup failed")

    with pytest.raises(migration.MigrationError, match="backup failed"):
        migration.migrate(pg_conn, tmp_path, backup=failed_backup)
    assert config.read_bytes() == original
    assert pg_conn.execute("SELECT value FROM meta WHERE key=%s", (migration.MARKER,)).fetchone() is None


def test_backup_flush_failure_rolls_back_before_band_update(pg_conn, tmp_path, monkeypatch):
    import gzip
    import tarfile
    from pseudolife_memory import backup_cli
    config = tmp_path / "config.yaml"
    original = b"memory: {miras: {preset: continuum}}\n"
    config.write_bytes(original)
    pg_conn.execute("INSERT INTO entries(band,text,embedding,ts) VALUES ('forever','kept',%s::vector,1)",
                    ("[" + ",".join(["0.1"] * 1024) + "]",))
    pg_conn.commit()

    def backup(data_dir, out):
        out.mkdir(parents=True)
        dump, state = out / "bank.sql.gz", out / "state.tar.gz"
        with gzip.open(dump, "wb") as stream:
            stream.write(b"-- PostgreSQL database dump complete\n")
        with tarfile.open(state, "w:gz") as archive:
            archive.add(config, arcname="config.yaml")
        return {"dump": dump, "state": state}

    def failed_flush(_):
        raise OSError("fixture flush failure")

    monkeypatch.setattr(backup_cli, "perform_backup", backup)
    monkeypatch.setattr(migration.os, "fsync", failed_flush)
    with pytest.raises(migration.MigrationError, match="could not be flushed to disk"):
        migration.migrate(pg_conn, tmp_path)
    assert config.read_bytes() == original
    assert pg_conn.execute("SELECT band FROM entries").fetchall() == [("forever",)]
    assert pg_conn.execute("SELECT key FROM meta WHERE key IN (%s,'writer_lease_epoch')", (migration.MARKER,)).fetchall() == []


def test_committed_migration_resumes_config_replace(pg_conn, tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("memory:\n  miras:\n    preset: continuum\n", encoding="utf-8")
    backup = lambda _: {"dump": "bank.sql.gz", "state": "state.tgz"}
    replace = migration.os.replace

    def fail_replace(*_):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(migration.os, "replace", fail_replace)
    with pytest.raises(migration.MigrationError, match="migrate-flat --apply"):
        migration.migrate(pg_conn, tmp_path, backup=backup)
    marker = pg_conn.execute("SELECT value FROM meta WHERE key=%s", (migration.MARKER,)).fetchone()[0]
    assert marker["capacity"] == 5250
    assert yaml.safe_load(config.read_text())["memory"]["miras"]["preset"] == "continuum"
    pg_conn.commit()
    monkeypatch.setattr(migration.os, "replace", replace)
    assert migration.migrate(pg_conn, tmp_path, backup=lambda _: pytest.fail("backup repeated"))["status"] == "resumed"
    assert yaml.safe_load(config.read_text())["memory"]["miras"]["preset"] == "flat"


def test_running_writer_refuses_before_backup(pg_conn, pg_url, tmp_path):
    import psycopg
    config = tmp_path / "config.yaml"
    config.write_text("memory: {miras: {preset: continuum}}\n", encoding="utf-8")
    with psycopg.connect(pg_url, autocommit=True) as writer:
        writer.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (migration.WRITER_LOCK,))
        with pytest.raises(migration.MigrationError, match="bank writer is running"):
            migration.migrate(pg_conn, tmp_path, backup=lambda _: pytest.fail("backup before admission"))


def test_over_capacity_bank_keeps_superseded_rows(pg_conn, pg_url, tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("memory: {miras: {preset: continuum}}\n", encoding="utf-8")
    pg_conn.execute("INSERT INTO entries (band,text,embedding,ts,superseded_at) "
                    "SELECT CASE WHEN n%%2=0 THEN 'working' ELSE 'forever' END, "
                    "'entry '||n, %s::vector, n, CASE WHEN n%%3=0 THEN n+1 END "
                    "FROM generate_series(1,5251) n", ("[" + ",".join(["0.1"] * 1024) + "]",))
    pg_conn.commit()
    before = pg_conn.execute(migration._FINGERPRINT).fetchall()
    pg_conn.commit()
    result = migration.migrate(pg_conn, tmp_path, backup=lambda _: {"dump": "bank.sql.gz", "state": "state.tgz"})
    assert result["entries"] == result["capacity"] == 5251
    assert pg_conn.execute(migration._FINGERPRINT).fetchall() == before
    miras = yaml.safe_load(config.read_bytes())["memory"]["miras"]
    assert miras["bands"][0]["max_entries"] == 5251
    pg_conn.commit()
    assert migration.migrate(pg_conn, tmp_path, backup=lambda _: pytest.fail("repeat backup"))["status"] == "unchanged"
    from pseudolife_memory.service import MemoryService
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    service = MemoryService(data_dir=tmp_path)
    try:
        service._ensure_init()
        assert len(service._cms.bands) == 1
        assert len(service._cms.bands[0].entries) == 5251
        assert pg_conn.execute(migration._FINGERPRINT).fetchall() == before
    finally:
        if service._storage is not None:
            service._storage.close()


def test_service_loaded_before_migration_cannot_hydrate_stale_bands(pg_conn, pg_url, tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService
    config = tmp_path / "config.yaml"
    config.write_text("memory: {miras: {preset: continuum}}\n", encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    service = MemoryService(data_dir=tmp_path)
    migration.migrate(pg_conn, tmp_path, backup=lambda _: {"dump": "bank.sql.gz", "state": "state.tgz"})
    try:
        with pytest.raises(RuntimeError, match="loaded config"):
            service._ensure_init()
    finally:
        if service._storage is not None:
            service._storage.close()


def _wait_for_session_locks_released(conn, backend_pid, timeout=10.0):
    """Wait until the server has ended a closed session and dropped its locks.

    ``close()`` returns once the client has sent its goodbye; the server
    releases the session's advisory locks a moment later. Measured on the
    bench PostgreSQL on 2026-11-12: a try-lock sent straight after the
    holder's ``close()`` lost 799 of 2000 times, and the lock was free within
    18 ms every time. ``pg_locks`` reads the live lock table on every query.
    """
    deadline = time.monotonic() + timeout
    while True:
        held = conn.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND pid = %s", (backend_pid,)
        ).fetchone()
        conn.commit()
        if not held:
            return
        assert time.monotonic() < deadline, f"backend {backend_pid} still holds an advisory lock after {timeout}s"
        time.sleep(0.01)


def test_reconnecting_resident_writer_observes_migration(pg_conn, pg_url, tmp_path, monkeypatch):
    from pseudolife_memory.service import MemoryService
    config = tmp_path / "config.yaml"
    config.write_text("memory: {miras: {preset: continuum}}\n", encoding="utf-8")
    monkeypatch.setenv("PSEUDOLIFE_MCP_DATABASE_URL", pg_url)
    service = MemoryService(data_dir=tmp_path)
    service._ensure_init()
    epoch = service._storage._lease_epoch
    writer_pid = service._storage._conn.info.backend_pid
    service._storage._conn.close()
    _wait_for_session_locks_released(pg_conn, writer_pid)
    try:
        migration.migrate(pg_conn, tmp_path, backup=lambda _: {"dump": "bank.sql.gz", "state": "state.tgz"})
        assert pg_conn.execute("SELECT value FROM meta WHERE key='writer_lease_epoch'").fetchone()[0] == epoch + 1
        pg_conn.commit()
        with pytest.raises(RuntimeError, match="loaded config"):
            service._ensure_init()
    finally:
        service._storage.close()
