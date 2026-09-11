"""PostgreSQL persistence contracts for durable dream acknowledgement."""

from __future__ import annotations

import json
import math
import zipfile

import numpy as np
import psycopg
import pytest
import torch

from pseudolife_memory.storage.schema import BENCH_RESET_TABLES, ensure_schema
from tests.pg_fixtures import pg_conn, pg_service, pg_url  # noqa: F401


def _entry(text: str, *, ts: float, source: str = "eligible", **extra):
    row = {
        "band": "flat",
        "text": text,
        "embedding": np.zeros(1024, dtype=np.float32),
        "surprise": 0.5,
        "ts": ts,
        "access_count": 0,
        "source": source,
        "superseded_at": None,
        "superseded_by_text": None,
        "last_logical_turn": None,
        "episode_id": None,
        "episode_title": None,
        "tags": [],
        "slots": [],
    }
    row.update(extra)
    return row


@pytest.fixture()
def storage(pg_conn, pg_url):
    from pseudolife_memory.storage.postgres import PostgresStorage

    value = PostgresStorage(pg_url)
    yield value
    value.close()


def test_schema_upgrade_keeps_old_rows_null_but_defaults_new_rows_pending(pg_conn):
    vec = "[" + ",".join(["0"] * 1024) + "]"
    pg_conn.execute("ALTER TABLE entries DROP COLUMN IF EXISTS dream_state")
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts) VALUES "
        "('flat', 'old row', %s::vector, 1.0)",
        (vec,),
    )
    pg_conn.commit()

    ensure_schema(pg_conn)
    pg_conn.execute(
        "INSERT INTO entries (band, text, embedding, ts) VALUES "
        "('flat', 'new row', %s::vector, 2.0)",
        (vec,),
    )
    pg_conn.commit()

    states = dict(pg_conn.execute(
        "SELECT text, dream_state FROM entries ORDER BY id"
    ).fetchall())
    assert states == {"old row": None, "new row": "pending"}


def test_insert_default_and_explicit_migration_marker_are_distinct(storage):
    pending_id = storage.insert_entry(_entry("new", ts=1.0))
    old_id = storage.insert_entry(_entry("old", ts=2.0, dream_state=None))

    rows = {row[0]: row[1] for row in storage.conn.execute(
        "SELECT id, dream_state FROM entries ORDER BY id"
    ).fetchall()}
    assert rows[pending_id] == "pending"
    assert rows[old_id] is None


def test_initialize_classifies_only_null_rows_under_current_source_policy(storage):
    storage.meta_set("cortex_dream_cursor", 10.0)
    covered = storage.insert_entry(_entry("covered", ts=9.0, dream_state=None))
    newer = storage.insert_entry(_entry("newer", ts=11.0, dream_state=None))
    excluded = storage.insert_entry(_entry(
        "excluded", ts=8.0, source="excluded", dream_state=None))
    explicit = storage.insert_entry(_entry(
        "explicit pending", ts=5.0, dream_state="pending"))

    result = storage.initialize_dream_tracking(exclude_sources=["excluded"])

    assert len(result["secret"]) == 64
    assert bytes.fromhex(result["secret"])
    assert result["dream_cursor"] == 10.0
    assert result["updated_states"] == {
        covered: "legacy-covered",
        newer: "pending",
        excluded: "pending",
    }
    states = dict(storage.conn.execute(
        "SELECT id, dream_state FROM entries ORDER BY id"
    ).fetchall())
    assert states[explicit] == "pending"
    assert storage.initialize_dream_tracking()["secret"] == result["secret"]


@pytest.mark.parametrize(
    "policy,expected",
    [
        ({"exclude_sources": ["excluded"]},
         {"eligible old": "legacy-covered",
          "eligible boundary": "legacy-covered",
          "eligible new": "pending",
          "excluded old": "pending",
          "excluded new": "pending"}),
        ({"eligible_sources": ["eligible"]},
         {"eligible old": "legacy-covered",
          "eligible boundary": "legacy-covered",
          "eligible new": "pending",
          "excluded old": "pending",
          "excluded new": "pending"}),
    ],
    ids=["exclude-list", "allow-list"],
)
def test_initialize_classification_equals_the_per_row_rule(
        storage, policy, expected):
    """Characterization pin for the set-based classification: over a mixed
    fixture the result must equal the per-row rule it replaced — covered
    only when the source is dream-eligible AND ``ts <= cursor`` (boundary
    inclusive), pending otherwise — and rows that already carry a state
    must not be touched under either source policy."""
    storage.meta_set("cortex_dream_cursor", 10.0)
    ids = {
        text: storage.insert_entry(
            _entry(text, ts=ts, source=source, dream_state=None))
        for text, ts, source in (
            ("eligible old", 9.0, "eligible"),
            ("eligible boundary", 10.0, "eligible"),
            ("eligible new", 11.0, "eligible"),
            ("excluded old", 1.0, "excluded"),
            ("excluded new", 12.0, "excluded"),
        )
    }
    settled = storage.insert_entry(
        _entry("already settled", ts=2.0, dream_state="acknowledged"))

    result = storage.initialize_dream_tracking(**policy)

    assert result["updated_states"] == {
        ids[text]: state for text, state in expected.items()}
    states = dict(storage.conn.execute(
        "SELECT id, dream_state FROM entries"
    ).fetchall())
    assert states[settled] == "acknowledged"
    assert {ids[text]: states[ids[text]] for text in expected} == {
        ids[text]: state for text, state in expected.items()}


def test_initialize_classifies_without_one_statement_per_row(
        storage, monkeypatch):
    """The classification runs under the service lock, so it must not cost
    one round trip per legacy row: two set-based UPDATEs implement the
    whole rule."""
    storage.meta_set("cortex_dream_cursor", 10.0)
    for index in range(12):
        storage.insert_entry(_entry(f"legacy {index}", ts=float(index),
                                    dream_state=None))
    conn = storage.conn
    real_cursor = conn.cursor
    statements: list[str] = []

    class _Counting:
        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def execute(self, sql, *args, **kwargs):
            statements.append(str(sql))
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(
        conn, "cursor", lambda *a, **k: _Counting(real_cursor(*a, **k)))
    storage.initialize_dream_tracking()

    updates = [s for s in statements if s.lstrip().startswith("UPDATE entries")]
    assert len(updates) == 2, (
        f"expected two set-based UPDATEs, got {len(updates)}")


@pytest.mark.parametrize("cursor", [math.nan, math.inf, -math.inf])
def test_initialize_rejects_nonfinite_legacy_cursor_atomically(storage, cursor):
    # JSONB itself rejects bare IEEE non-finite numbers, but old snapshots
    # could persist their string spellings and float() accepts those.
    storage.meta_set("cortex_dream_cursor", str(cursor))
    entry_id = storage.insert_entry(_entry("unclassified", ts=1.0,
                                           dream_state=None))

    with pytest.raises(ValueError, match=r"^invalid_legacy_dream_cursor:"):
        storage.initialize_dream_tracking()

    assert storage.conn.execute(
        "SELECT dream_state FROM entries WHERE id = %s", (entry_id,)
    ).fetchone()[0] is None
    assert storage.meta_get("dream_ack_secret_v1") is None


def test_acknowledge_exact_ids_is_atomic_and_idempotent(storage):
    first = storage.insert_entry(_entry("first", ts=5.0))
    second = storage.insert_entry(_entry("second", ts=7.0))
    storage.initialize_dream_tracking()

    result = storage.acknowledge_dream_entries([second, first], 7.0)
    assert result == {
        "acknowledged_ids": [second, first],
        "missing_ids": [],
        "newly_acknowledged": 2,
        "dream_cursor": 7.0,
    }
    retry = storage.acknowledge_dream_entries([second, first], 7.0)
    assert retry["acknowledged_ids"] == [second, first]
    assert retry["newly_acknowledged"] == 0
    assert retry["dream_cursor"] == 7.0


def test_acknowledge_commits_survivors_and_reports_vanished_ids(storage):
    """A row deleted between the pull and the commit can never be pulled
    again, so it must not cost the rest of the batch its acknowledgement
    (which would re-extract the survivors on every sweep, forever)."""
    survivor = storage.insert_entry(_entry("survivor", ts=5.0))
    storage.initialize_dream_tracking()

    result = storage.acknowledge_dream_entries([survivor, 999_999_999], 5.0)

    assert result["acknowledged_ids"] == [survivor]
    assert result["missing_ids"] == [999_999_999]
    assert result["newly_acknowledged"] == 1
    assert result["dream_cursor"] == 5.0
    assert storage.conn.execute(
        "SELECT dream_state FROM entries WHERE id = %s", (survivor,)
    ).fetchone()[0] == "acknowledged"


def test_acknowledge_of_an_entirely_vanished_batch_still_advances_display(
        storage):
    storage.insert_entry(_entry("unrelated", ts=1.0))
    storage.initialize_dream_tracking()

    result = storage.acknowledge_dream_entries([999_999_998, 999_999_999], 5.0)

    assert result["acknowledged_ids"] == []
    assert result["missing_ids"] == [999_999_998, 999_999_999]
    assert result["newly_acknowledged"] == 0
    assert result["dream_cursor"] == 5.0


def test_acknowledge_rejects_unclassified_or_legacy_covered_batch(storage):
    good = storage.insert_entry(_entry("good", ts=20.0))
    old = storage.insert_entry(_entry("old", ts=2.0, dream_state=None))
    storage.meta_set("cortex_dream_cursor", 10.0)
    storage.initialize_dream_tracking()

    with pytest.raises(ValueError, match=r"^dream_ack_invalid_states:"):
        storage.acknowledge_dream_entries([good, old], 20.0)

    assert storage.conn.execute(
        "SELECT dream_state FROM entries WHERE id = %s", (good,)
    ).fetchone()[0] == "pending"
    assert storage.meta_get("cortex_dream_cursor") == 10.0


def test_acknowledgement_does_not_call_reconnecting_mutator_helpers(
        storage, monkeypatch):
    entry_id = storage.insert_entry(_entry("one connection", ts=3.0))
    storage.initialize_dream_tracking()
    monkeypatch.setattr(storage, "meta_set", lambda *_a, **_k: pytest.fail(
        "acknowledgement called meta_set inside its transaction"))
    monkeypatch.setattr(storage, "update_entry", lambda *_a, **_k: pytest.fail(
        "acknowledgement called update_entry inside its transaction"))

    assert storage.acknowledge_dream_entries([entry_id], 3.0)[
        "newly_acknowledged"] == 1


def test_initialize_and_acknowledge_each_resolve_connection_once(
        storage, monkeypatch):
    entry_id = storage.insert_entry(_entry("captured", ts=3.0,
                                           dream_state=None))
    cls = type(storage)
    original = cls.conn
    accesses = 0

    def _counted(instance):
        nonlocal accesses
        accesses += 1
        return original.__get__(instance, cls)

    monkeypatch.setattr(cls, "conn", property(_counted))
    storage.initialize_dream_tracking()
    assert accesses == 1
    storage.acknowledge_dream_entries([entry_id], 3.0)
    assert accesses == 2


def test_acknowledgement_rolls_back_rows_when_cursor_write_fails(storage):
    entry_id = storage.insert_entry(_entry("rollback", ts=3.0))
    storage.initialize_dream_tracking()
    conn = storage.conn
    conn.execute(
        "CREATE FUNCTION fail_dream_cursor_write() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW.key = 'cortex_dream_cursor' THEN "
        "RAISE EXCEPTION 'injected cursor failure'; END IF; "
        "RETURN NEW; END $$"
    )
    conn.execute(
        "CREATE TRIGGER fail_dream_cursor_write "
        "BEFORE INSERT OR UPDATE ON meta FOR EACH ROW "
        "EXECUTE FUNCTION fail_dream_cursor_write()"
    )
    try:
        with pytest.raises(Exception, match="injected cursor failure"):
            storage.acknowledge_dream_entries([entry_id], 3.0)
        assert conn.execute(
            "SELECT dream_state FROM entries WHERE id = %s", (entry_id,)
        ).fetchone()[0] == "pending"
    finally:
        conn.execute("DROP TRIGGER fail_dream_cursor_write ON meta")
        conn.execute("DROP FUNCTION fail_dream_cursor_write()")


def test_retry_after_lost_success_response_observes_committed_state(storage):
    entry_id = storage.insert_entry(_entry("lost response", ts=3.0))
    storage.initialize_dream_tracking()

    storage.acknowledge_dream_entries([entry_id], 3.0)
    retry = storage.acknowledge_dream_entries([entry_id], 3.0)

    assert retry["newly_acknowledged"] == 0
    assert retry["acknowledged_ids"] == [entry_id]
    assert retry["dream_cursor"] == 3.0


def test_postgres_service_restart_keeps_exact_ack_and_admits_backdated_write(
        pg_service, pg_url):
    from pseudolife_memory.service import MemoryService

    service = pg_service
    for text in ("tied alpha", "tied beta", "tied gamma"):
        service.store(text, source="notes")
    entries = [entry for band in service._cms.bands for entry in band.entries]
    for entry in entries:
        entry.timestamp = 100.0
        service._storage.conn.execute(
            "UPDATE entries SET ts = 100.0 WHERE id = %s", (entry.db_id,))

    first = service.dream_pull(limit=1)
    first_id = first["entries"][0]["db_id"]
    assert service.dream_commit(first["commit_token"])["acknowledged"] == 1
    assert service._storage.conn.execute(
        "SELECT dream_state FROM entries WHERE id = %s", (first_id,)
    ).fetchone()[0] == "acknowledged"

    service._storage.close()
    restarted = MemoryService(data_dir=service.data_dir, database_url=pg_url)
    restarted._ensure_init()
    remaining = restarted.dream_pull(limit=10)
    assert remaining["count"] == 2
    assert first_id not in {row["db_id"] for row in remaining["entries"]}

    restarted.store("later ingestion with an older clock", source="notes")
    newest = max(
        (entry for band in restarted._cms.bands for entry in band.entries),
        key=lambda entry: entry.db_id or 0,
    )
    newest.timestamp = 1.0
    restarted._storage.conn.execute(
        "UPDATE entries SET ts = 1.0 WHERE id = %s", (newest.db_id,))
    pulled = restarted.dream_pull(limit=10)
    assert newest.db_id in {row["db_id"] for row in pulled["entries"]}
    restarted._storage.close()


def test_ordinary_entry_update_cannot_regress_acknowledgement(storage):
    entry_id = storage.insert_entry(_entry("durable", ts=3.0))
    storage.initialize_dream_tracking()
    storage.acknowledge_dream_entries([entry_id], 3.0)

    with pytest.raises(ValueError, match="non-updatable fields"):
        storage.update_entry(entry_id, dream_state="pending")
    storage.update_entry(entry_id, band="working", access_count=4)
    row = storage.conn.execute(
        "SELECT dream_state, band, access_count FROM entries WHERE id = %s",
        (entry_id,),
    ).fetchone()
    assert row == ("acknowledged", "working", 4)


def test_stale_cortex_snapshot_cannot_regress_ack_display_metadata(storage):
    from pseudolife_memory.memory.cortex import CortexStore
    from pseudolife_memory.storage.sync import snapshot_cortex

    entry_id = storage.insert_entry(_entry("snapshot", ts=10.0))
    storage.initialize_dream_tracking()
    storage.acknowledge_dream_entries([entry_id], 10.0)
    stale = CortexStore()
    stale.dream_cursor = 2.0

    snapshot_cortex(stale, storage)

    assert storage.meta_get("cortex_dream_cursor") == 10.0
    assert storage.conn.execute(
        "SELECT dream_state FROM entries WHERE id = %s", (entry_id,)
    ).fetchone()[0] == "acknowledged"


def _truncate(conn):
    conn.execute(
        "TRUNCATE " + ", ".join(BENCH_RESET_TABLES)
        + " RESTART IDENTITY CASCADE"
    )
    conn.commit()


def _rewrite_archive(source, target, *, schema_version, strip_dream_state):
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(target, "w") as new:
        for name in old.namelist():
            payload = old.read(name)
            if name == "manifest.json":
                manifest = json.loads(payload)
                manifest["schema_version"] = schema_version
                payload = json.dumps(manifest).encode()
            elif name == "entries.jsonl" and strip_dream_state:
                records = []
                for line in payload.decode().splitlines():
                    rec = json.loads(line)
                    rec.pop("dream_state", None)
                    records.append(json.dumps(rec))
                payload = ("\n".join(records) + "\n").encode()
            new.writestr(name, payload)


def test_logical_transfer_preserves_states_but_rotates_secret(pg_url, tmp_path):
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.transfer_cli import perform_export, perform_import

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        ensure_schema(conn)
        _truncate(conn)
    storage = PostgresStorage(pg_url)
    try:
        storage.insert_entry(_entry("done", ts=1.0,
                                    dream_state="acknowledged"))
        storage.insert_entry(_entry("todo", ts=2.0, dream_state="pending"))
        storage.meta_set("dream_ack_secret_v1", "a" * 64)
    finally:
        storage.close()

    archive = tmp_path / "new.zip"
    perform_export(pg_url, archive)
    with zipfile.ZipFile(archive) as exported:
        meta_text = exported.read("meta.jsonl").decode()
        assert "dream_ack_secret_v1" not in meta_text

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        _truncate(conn)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES "
            "('dream_ack_secret_v1', %s::jsonb)",
            (json.dumps("b" * 64),),
        )
        conn.commit()
    perform_import(pg_url, archive, force=True)

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        assert conn.execute(
            "SELECT dream_state FROM entries ORDER BY id"
        ).fetchall() == [("acknowledged",), ("pending",)]
        assert conn.execute(
            "SELECT value FROM meta WHERE key = 'dream_ack_secret_v1'"
        ).fetchone()[0] == "b" * 64


def test_old_logical_export_imports_entries_with_null_marker(pg_url, tmp_path):
    from pseudolife_memory.storage.postgres import PostgresStorage
    from pseudolife_memory.transfer_cli import perform_export, perform_import

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        ensure_schema(conn)
        _truncate(conn)
    storage = PostgresStorage(pg_url)
    try:
        storage.insert_entry(_entry("old logical", ts=4.0))
        storage.meta_set("cortex_dream_cursor", 5.0)
    finally:
        storage.close()
    current = tmp_path / "current.zip"
    legacy = tmp_path / "legacy.zip"
    perform_export(pg_url, current)
    _rewrite_archive(current, legacy, schema_version=37,
                     strip_dream_state=True)

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        _truncate(conn)
    perform_import(pg_url, legacy, force=True)

    with psycopg.connect(pg_url) as conn:
        conn.execute("SET search_path TO public")
        assert conn.execute(
            "SELECT dream_state FROM entries"
        ).fetchone()[0] is None
        assert float(conn.execute(
            "SELECT value FROM meta WHERE key = 'cortex_dream_cursor'"
        ).fetchone()[0]) == 5.0


def _legacy_bank_with_cursor(tmp_path, cursor: float):
    from tests.test_migration import _build_legacy_bank

    _build_legacy_bank(tmp_path)
    cms_path = tmp_path / "memory_state" / "cms_state.pt"
    state = torch.load(cms_path, map_location="cpu", weights_only=True)
    entries = [
        entry
        for band in state["bands"].values()
        for entry in band["entries"]
    ]
    entries.sort(key=lambda entry: entry["text"])
    entries[0]["timestamp"] = 5.0
    entries[0]["source"] = "eligible"
    entries[0].pop("dream_state", None)
    entries[1]["timestamp"] = 6.0
    entries[1]["source"] = "excluded"
    entries[1].pop("dream_state", None)
    torch.save(state, cms_path)

    cortex_path = tmp_path / "cortex_state.pt"
    cortex_state = torch.load(
        cortex_path, map_location="cpu", weights_only=True)
    cortex_state["dream_cursor"] = cursor
    torch.save(cortex_state, cortex_path)


def test_file_to_postgres_classifies_old_rows_from_source_cursor_and_policy(
        pg_conn, pg_url, tmp_path):
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, 10.0)
    storage = PostgresStorage(pg_url)
    try:
        migrate_legacy(
            tmp_path, storage, _FakeEmbedder(),
            exclude_sources=["excluded"],
        )
        states = {row["source"]: row["dream_state"]
                  for row in storage.load_entries()}
        assert states == {
            "eligible": "legacy-covered",
            "excluded": "pending",
        }
    finally:
        storage.close()


@pytest.mark.parametrize("keep_cortex", [True, False])
def test_file_import_uses_v7_checkpoint_display_cursor(pg_conn, pg_url, tmp_path, keep_cortex):
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, 2.0)
    cms_path = tmp_path / "memory_state" / "cms_state.pt"
    state = torch.load(cms_path, map_location="cpu", weights_only=True)
    state.update(schema_version=7, dream_ack_secret="a" * 64, dream_display_cursor=10.0)
    for band in state["bands"].values():
        for entry in band["entries"]:
            entry["dream_state"] = "acknowledged"
    torch.save(state, cms_path)
    if not keep_cortex:
        (tmp_path / "cortex_state.pt").unlink()

    storage = PostgresStorage(pg_url)
    try:
        migrate_legacy(tmp_path, storage, _FakeEmbedder())
        assert storage.meta_get("cortex_dream_cursor") == 10.0
        assert all(row["dream_state"] == "acknowledged" for row in storage.load_entries())
    finally:
        storage.close()


def test_resumed_file_import_never_reclassifies_interleaved_daemon_write(
        pg_conn, pg_url, tmp_path):
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, 10.0)
    storage = PostgresStorage(pg_url)
    real_insert = storage.insert_entry
    calls = 0

    def _die_on_second(row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted")
        return real_insert(row)

    try:
        storage.insert_entry = _die_on_second
        with pytest.raises(RuntimeError, match="interrupted"):
            migrate_legacy(tmp_path, storage, _FakeEmbedder())

        storage.insert_entry = real_insert
        daemon_id = storage.insert_entry(_entry(
            "daemon during interruption", ts=1.0))
        migrate_legacy(tmp_path, storage, _FakeEmbedder())

        states = {row["id"]: row["dream_state"]
                  for row in storage.load_entries()}
        assert states[daemon_id] == "pending"
        imported = [row for row in storage.load_entries()
                    if row["id"] != daemon_id]
        assert len(imported) == 2
        assert {row["dream_state"] for row in imported} == {"legacy-covered"}
    finally:
        storage.close()


def test_repaired_preflight_resumes_after_daemon_write(pg_conn, pg_url, tmp_path):
    from pseudolife_memory.storage.migrate import MIGRATION_META_KEY, migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, 2.0)
    cms_path = tmp_path / "memory_state" / "cms_state.pt"
    state = torch.load(cms_path, map_location="cpu", weights_only=True)
    state.update(schema_version=7, dream_ack_secret="a" * 64,
                 dream_display_cursor="invalid")
    torch.save(state, cms_path)
    storage = PostgresStorage(pg_url)
    try:
        with pytest.raises(ValueError, match="invalid_legacy_dream_cursor:"):
            migrate_legacy(tmp_path, storage, _FakeEmbedder())
        record = storage.meta_get(MIGRATION_META_KEY)
        assert record is not None
        assert record["status"] == "in_progress"
        assert record["stage"] == "preflight"
        daemon_id = storage.insert_entry(_entry("new daemon row", ts=1.0))
        state["dream_display_cursor"] = 10.0
        torch.save(state, cms_path)
        result = migrate_legacy(tmp_path, storage, _FakeEmbedder())
        assert result["migrated"] is True
        rows = storage.load_entries()
        assert len(rows) == 3
        assert next(row for row in rows if row["id"] == daemon_id)["dream_state"] == "pending"
        assert {row["dream_state"] for row in rows if row["id"] != daemon_id} == {"legacy-covered"}
        assert storage.meta_get("cortex_dream_cursor") == 10.0
    finally:
        storage.close()


@pytest.mark.parametrize("schema,secret", [(7, "invalid"), (7, None), (6, "a" * 64)])
def test_file_import_rejects_invalid_checkpoint_authority(
        pg_conn, pg_url, tmp_path, schema, secret):
    from pseudolife_memory.storage.migrate import migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, 2.0)
    cms_path = tmp_path / "memory_state" / "cms_state.pt"
    state = torch.load(cms_path, map_location="cpu", weights_only=True)
    state.update(schema_version=schema, dream_ack_secret=secret,
                 dream_display_cursor=10.0)
    torch.save(state, cms_path)
    storage = PostgresStorage(pg_url)
    try:
        with pytest.raises(ValueError, match="invalid_dream_ack_state:"):
            migrate_legacy(tmp_path, storage, _FakeEmbedder())
        assert storage.load_entries() == []
    finally:
        storage.close()


@pytest.mark.parametrize("remove_both", [True, False])
def test_preflight_missing_sources_never_reconciles_old_backups(
        pg_conn, pg_url, tmp_path, remove_both):
    from pseudolife_memory.storage.migrate import MIGRATION_META_KEY, migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, float("nan"))
    storage = PostgresStorage(pg_url)
    try:
        with pytest.raises(ValueError, match="invalid_legacy_dream_cursor:"):
            migrate_legacy(tmp_path, storage, _FakeEmbedder())
        paths = [tmp_path / "cortex_state.pt"]
        if remove_both:
            paths.append(tmp_path / "memory_state" / "cms_state.pt")
        for path in paths:
            path.rename(path.with_name(path.name + ".pre-v8.bak"))
        result = migrate_legacy(tmp_path, storage, _FakeEmbedder())
        expected = "legacy_source_missing" if remove_both else "partial_migration_source_mismatch"
        assert result.get("reason") == expected
        assert storage.meta_get(MIGRATION_META_KEY)["status"] == "in_progress"
        assert storage.load_entries() == []
    finally:
        storage.close()


def test_importing_marker_precedes_even_the_first_episode(
        pg_conn, pg_url, tmp_path, monkeypatch):
    from pseudolife_memory.storage.migrate import MIGRATION_META_KEY, migrate_legacy
    from pseudolife_memory.storage.postgres import PostgresStorage
    from tests.test_migration import _FakeEmbedder

    _legacy_bank_with_cursor(tmp_path, 2.0)
    storage = PostgresStorage(pg_url)
    try:
        def fail_episode(row):
            assert storage.meta_get(MIGRATION_META_KEY)["stage"] == "importing"
            raise RuntimeError("episode interrupted")
        monkeypatch.setattr(storage, "upsert_episode", fail_episode)
        with pytest.raises(RuntimeError, match="episode interrupted"):
            migrate_legacy(tmp_path, storage, _FakeEmbedder())
        assert storage.meta_get(MIGRATION_META_KEY)["entries_done"] == 0
        _legacy_bank_with_cursor(tmp_path, 3.0)
        result = migrate_legacy(tmp_path, storage, _FakeEmbedder())
        assert result["reason"] == "partial_migration_source_mismatch"
    finally:
        storage.close()
