"""Schema v25 -- vector(1024), Qwen3-Embedding-0.6B default.

Four embedding columns (entries/facts/world_facts/lessons) move from
vector(384) to vector(1024) for the measured-best backbone (PR #59
artifacts: R@10 0.809 vs bge-base 0.742, +81/-6 vs shipped MiniLM). See
docs/superpowers/specs/2026-07-28-embedding-backbone-v25-design.md.

``ensure_schema`` stays additive-only: it never alters an existing
column's dimension, and instead REFUSES to start (before any DDL runs)
when the live bank's ``entries.embedding`` is dimensioned differently
than this build expects -- see
:func:`pseudolife_memory.storage.schema._refuse_on_embedding_dim_mismatch`.
The real migration is the human-gated ``ops/migrate_embeddings.py``.

Skips cleanly without a PG server (mirrors test_pg_storage.py /
test_schema_v24.py).
"""
from __future__ import annotations

import tempfile

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage import schema

_EMBEDDING_TABLES = ("entries", "facts", "world_facts", "lessons")


def test_schema_version_is_25():
    assert schema.SCHEMA_META_VERSION >= 25  # 1024-dim embeddings landed at v25; persist into later schemas


def test_all_four_embedding_columns_report_dim_1024(pg_conn):
    for table in _EMBEDDING_TABLES:
        row = pg_conn.execute(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = to_regclass(%s) AND attname = 'embedding' "
            "AND attnum > 0 AND NOT attisdropped",
            (f"public.{table}",),
        ).fetchone()
        assert row is not None, f"{table}.embedding column not found"
        assert row[0] == 1024, f"{table}.embedding is vector({row[0]}), expected vector(1024)"


# ---------------------------------------------------------------------------
# Missing-table / fresh-install case: the guard must never fire when there is
# nothing to refuse. Exercised as a direct unit test against the private
# helper with a stub cursor -- no PG server needed for this one, and it pins
# the "atttypmod absent" branch explicitly rather than relying on it being
# incidentally true every time the rest of the suite provisions a fresh bank.
# ---------------------------------------------------------------------------


class _FakeCursorNoRow:
    """Simulates ``to_regclass('public.entries')`` finding nothing: a fresh
    install where the ``entries`` table doesn't exist yet."""

    def execute(self, *_args, **_kwargs) -> None:
        pass

    def fetchone(self):
        return None


def test_fresh_install_with_no_entries_table_does_not_refuse():
    from pseudolife_memory.storage.schema import _refuse_on_embedding_dim_mismatch

    _refuse_on_embedding_dim_mismatch(_FakeCursorNoRow())  # must not raise


# ---------------------------------------------------------------------------
# The refusal itself: an existing bank whose entries.embedding was somehow
# left at the old dimension must abort ensure_schema loudly, before any DDL,
# naming the migration script.
# ---------------------------------------------------------------------------


def test_refusal_fires_on_dim_mismatch_and_names_the_migration_script(pg_conn):
    from pseudolife_memory.storage.schema import ensure_schema

    try:
        # Build the pre-v25 state: entries.embedding back at vector(384).
        # The table is freshly truncated (0 rows) by the pg_conn fixture, so
        # `USING NULL` needs nothing from existing data -- it's the safe
        # cast regardless of row count, matching how a real narrowing ALTER
        # would have to behave against a populated bank (a straight cast
        # from vector(1024) to vector(384) is not defined). Both ALTERs run
        # inside this try so the `finally` below still restores the
        # vector(1024) NOT NULL shape even if setup fails partway (e.g. the
        # DROP NOT NULL succeeds but the TYPE change doesn't) -- a mid-setup
        # failure must never leak a nullable entries.embedding into the
        # rest of the run.
        pg_conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
        pg_conn.execute(
            "ALTER TABLE entries ALTER COLUMN embedding TYPE vector(384) USING NULL"
        )
        pg_conn.commit()

        with pytest.raises(RuntimeError) as exc_info:
            ensure_schema(pg_conn)
        msg = str(exc_info.value)
        assert "ops/migrate_embeddings.py" in msg
        assert "384" in msg and "1024" in msg

        # The refusal must abort BEFORE any DDL runs (not merely before the
        # CREATE TABLE calls) -- entries.embedding must still be vector(384)
        # after the aborted call, proving ensure_schema touched nothing.
        pg_conn.rollback()  # return to a clean transaction state after the
        # raise before issuing a fresh query on the same connection.
        row = pg_conn.execute(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = to_regclass('public.entries') "
            "AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped"
        ).fetchone()
        assert row[0] == 384, (
            "ensure_schema must not have altered entries.embedding while refusing to run")
    finally:
        # Restore the vector(1024) NOT NULL shape the rest of the suite
        # (and the next test on this same per-run database) expects --
        # the per-test truncate does NOT restore DDL.
        pg_conn.execute(
            "ALTER TABLE entries ALTER COLUMN embedding TYPE vector(1024) USING NULL"
        )
        pg_conn.execute(
            "ALTER TABLE entries ALTER COLUMN embedding SET NOT NULL"
        )
        pg_conn.commit()


def test_refusal_does_not_fire_when_dims_already_match(pg_conn):
    """Sanity companion to the refusal test: ensure_schema on an untouched
    (already vector(1024)) bank must not raise -- the guard is dimension-
    specific, not a blanket refusal on every call."""
    from pseudolife_memory.storage.schema import ensure_schema

    ensure_schema(pg_conn)  # must not raise


# ---------------------------------------------------------------------------
# End-to-end: a fresh MemoryService round-trip at dim 1024 proves the real
# Qwen3-Embedding-0.6B model loads offline from the HF cache and its vectors
# fit the vector(1024) columns without a dimension error.
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc(pg_conn, pg_url):  # noqa: F811
    from pseudolife_memory.service import MemoryService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        s = MemoryService(data_dir=d, database_url=pg_url)
        try:
            yield s
        finally:
            if s._storage is not None:
                s._storage.close()


def test_service_round_trip_at_dim_1024(svc):
    stored = svc.store(
        "the bench postgres for schema v25 runs on port 5433", source="test",
    )
    assert stored["stored"], f"store was rejected: {stored.get('reason')}"

    result = svc.search("what port does the bench postgres use", top_k=3)
    assert result["entries"], "search returned no results for a just-stored memory"

    svc.cortex_write("schema-v25-probe", "status", "verified", support="user")
    facts = svc._storage.load_facts()
    row = next(f for f in facts if f["entity"] == "schema-v25-probe")
    assert row["attribute"] == "status" and row["value"] == "verified"

    # The embedder that just did all of the above is really Qwen3 at 1024,
    # not some stub -- pin it so this test would fail loudly if a future
    # change silently reverted the default.
    assert svc._embedder.embedding_dim == 1024
    assert svc.config.embedding.model_name == "Qwen/Qwen3-Embedding-0.6B"
