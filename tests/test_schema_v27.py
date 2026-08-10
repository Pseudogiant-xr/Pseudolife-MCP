"""Schema v27 — dream-run audit + pre-image journal.

Adds ``dream_runs`` (one row per dream pass that pulled entries: cursor
movement, tallies, lifecycle status) and ``dream_run_slots`` (the per-claim
pre-image journal rollback replays, FK ON DELETE CASCADE so pruning a run
removes its journal). ``dream_run_slots.src_entry_id`` deliberately carries
NO foreign key — entries are evictable, and the ``memory_traces`` FK is the
origin of the reflush-stall class the dream self-heals around.

Skips without a PG server (mirrors test_schema_v26).
"""
from __future__ import annotations

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_meta_version_is_27():
    assert SCHEMA_META_VERSION >= 27  # dream-run journal landed at v27; persists into later schemas


def test_dream_runs_table_exists_with_lifecycle_columns(pg_conn):
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'dream_runs'").fetchall()}
    assert {"id", "started_at", "finished_at", "cursor_before",
            "cursor_after", "pulled", "claims", "tallies", "status",
            "extractor", "writer_id", "rolled_back_at"} <= cols


def test_dream_run_slots_table_exists_with_pre_image_columns(pg_conn):
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'dream_run_slots'").fetchall()}
    assert {"id", "run_id", "seq", "entity", "attribute", "entity_norm",
            "attribute_norm", "kind", "op", "prev_kind", "prev_value",
            "prev_status", "prev_confidence", "prev_support", "new_value",
            "action", "src_entry_id", "at"} <= cols


def test_src_entry_id_carries_no_foreign_key(pg_conn):
    """Regression lock on the no-FK decision (see module docstring)."""
    fks = pg_conn.execute(
        "SELECT conname FROM pg_constraint c "
        "JOIN pg_class t ON c.conrelid = t.oid "
        "WHERE t.relname = 'dream_run_slots' AND c.contype = 'f'"
    ).fetchall()
    assert len(fks) == 1, f"expected only the run_id FK, got {fks}"


def test_run_delete_cascades_to_journal(pg_conn):
    pg_conn.execute(
        "INSERT INTO dream_runs (started_at, cursor_before, pulled, status) "
        "VALUES (1.0, 0.0, 3, 'committed')")
    run_id = pg_conn.execute(
        "SELECT id FROM dream_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    pg_conn.execute(
        "INSERT INTO dream_run_slots (run_id, seq, entity, attribute, "
        "entity_norm, attribute_norm, kind, action, at) "
        "VALUES (%s, 0, 'proj', 'lang', 'proj', 'lang', 'scalar', "
        "'inserted', 1.0)", (run_id,))
    pg_conn.commit()
    pg_conn.execute("DELETE FROM dream_runs WHERE id = %s", (run_id,))
    pg_conn.commit()
    left = pg_conn.execute(
        "SELECT count(*) FROM dream_run_slots WHERE run_id = %s",
        (run_id,)).fetchone()[0]
    assert left == 0


def test_null_prev_status_and_jsonb_tallies_round_trip(pg_conn):
    pg_conn.execute(
        "INSERT INTO dream_runs (started_at, cursor_before, pulled, status, "
        "tallies) VALUES (1.0, 0.0, 2, 'committed', "
        "'{\"inserted\": 2, \"literal_dropped\": 1}'::jsonb)")
    run_id = pg_conn.execute(
        "SELECT id FROM dream_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    pg_conn.execute(
        "INSERT INTO dream_run_slots (run_id, seq, entity, attribute, "
        "entity_norm, attribute_norm, kind, prev_status, action, at) "
        "VALUES (%s, 0, 'p', 'a', 'p', 'a', 'scalar', NULL, 'inserted', "
        "1.0)", (run_id,))
    pg_conn.commit()
    tallies = pg_conn.execute(
        "SELECT tallies FROM dream_runs WHERE id = %s", (run_id,)).fetchone()[0]
    assert tallies == {"inserted": 2, "literal_dropped": 1}
    prev = pg_conn.execute(
        "SELECT prev_status FROM dream_run_slots WHERE run_id = %s",
        (run_id,)).fetchone()[0]
    assert prev is None


def test_ensure_schema_rerun_is_idempotent(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)
    ensure_schema(pg_conn)
    tables = {r[0] for r in pg_conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public'").fetchall()}
    assert {"dream_runs", "dream_run_slots"} <= tables
