"""Schema v26 -- set-valued slots groundwork.

Adds ``facts.kind`` (``scalar`` | ``member``, default ``scalar``) and
``facts.value_norm`` (member identity; NULL on scalar rows), and splits the
single per-slot current-uniqueness index by kind: scalar facts stay unique
per (entity_norm, attribute_norm) as before, member facts are unique per
(entity_norm, attribute_norm, value_norm) so a set-valued slot can hold
multiple concurrently-current members. The old ``facts_slot_current_uq`` is
dropped -- idempotent re-runs of ``ensure_schema`` must not recreate it.

Skips without a PG server (mirrors test_pg_storage / test_schema_v16).
"""
from __future__ import annotations

import psycopg

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)

from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def _insert_fact(conn, *, entity, attribute, value, kind="scalar",
                  value_norm=None, status="current"):
    conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, confidence, status, asserted_at, last_confirmed, kind, "
        "value_norm) VALUES (%s, %s, %s, %s, %s, 0.9, %s, "
        "extract(epoch from now()), extract(epoch from now()), %s, %s)",
        (entity, attribute, entity, attribute, value, status, kind, value_norm),
    )


def test_meta_version_is_26():
    assert SCHEMA_META_VERSION == 26


def test_facts_has_kind_and_value_norm(pg_conn):
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'facts'").fetchall()}
    assert "kind" in cols and "value_norm" in cols


def test_kind_defaults_to_scalar(pg_conn):
    pg_conn.execute(
        "INSERT INTO facts (entity, attribute, entity_norm, attribute_norm, "
        "value, confidence, status, asserted_at, last_confirmed) "
        "VALUES ('proj', 'lang', 'proj', 'lang', 'python', 0.9, 'current', "
        "extract(epoch from now()), extract(epoch from now()))")
    pg_conn.commit()
    row = pg_conn.execute(
        "SELECT kind, value_norm FROM facts WHERE entity = 'proj'").fetchone()
    assert row[0] == "scalar"
    assert row[1] is None


def test_current_uniqueness_split_by_kind(pg_conn):
    idx = {r[0] for r in pg_conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'facts'").fetchall()}
    assert "facts_slot_current_scalar_uq" in idx
    assert "facts_member_current_uq" in idx
    assert "facts_slot_current_uq" not in idx


def test_scalar_current_uniqueness_enforced(pg_conn):
    """Two scalar 'current' rows for the same slot must collide -- the
    scalar-scoped index preserves the pre-v26 one-live-row-per-slot
    invariant."""
    _insert_fact(pg_conn, entity="proj", attribute="lang", value="python")
    pg_conn.commit()
    with pg_conn.cursor():
        try:
            _insert_fact(pg_conn, entity="proj", attribute="lang", value="rust")
            pg_conn.commit()
            assert False, "second concurrent scalar-current row must be rejected"
        except psycopg.errors.UniqueViolation:
            pg_conn.rollback()


def test_member_current_uniqueness_allows_multiple_values_same_slot(pg_conn):
    """Member facts on the same (entity, attribute) slot but different
    values may both be 'current' -- that is the whole point of the split."""
    _insert_fact(pg_conn, entity="proj", attribute="language", value="python",
                 kind="member", value_norm="python")
    _insert_fact(pg_conn, entity="proj", attribute="language", value="rust",
                 kind="member", value_norm="rust")
    pg_conn.commit()

    rows = pg_conn.execute(
        "SELECT value FROM facts WHERE entity = 'proj' "
        "AND attribute = 'language' AND status = 'current' ORDER BY value"
    ).fetchall()
    assert [r[0] for r in rows] == ["python", "rust"]


def test_member_current_uniqueness_still_rejects_same_value_twice(pg_conn):
    """The member index dedupes on VALUE too -- two current rows for the
    identical (entity, attribute, value_norm) must still collide."""
    _insert_fact(pg_conn, entity="proj", attribute="language", value="python",
                 kind="member", value_norm="python")
    pg_conn.commit()
    with pg_conn.cursor():
        try:
            _insert_fact(pg_conn, entity="proj", attribute="language",
                         value="Python", kind="member", value_norm="python")
            pg_conn.commit()
            assert False, "duplicate current member (same value_norm) must be rejected"
        except psycopg.errors.UniqueViolation:
            pg_conn.rollback()
