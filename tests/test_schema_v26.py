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
    assert SCHEMA_META_VERSION == 28


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


def test_ensure_schema_healing_is_kind_aware(pg_conn):
    """The v19 duplicate-healing pass (facts/current) runs on EVERY
    ensure_schema call, i.e. every daemon start. Without a kind-aware
    predicate it would partition member rows by the same
    (entity_norm, attribute_norm) as scalar rows and demote all but the
    newest member on a slot to 'superseded' on every restart -- silently
    destroying a set-valued slot's membership over time. Seeds two current
    MEMBER rows (distinct value_norm) on one slot, and a genuine duplicate
    current SCALAR pair on another slot, then re-runs ensure_schema:
    the members must both survive; the scalar duplicate must still heal to
    exactly one 'current' row (the newest), same as pre-v26 behaviour."""
    from pseudolife_memory.storage.schema import ensure_schema

    # Two legitimately distinct current members on the same slot -- not a
    # duplicate, this is the whole point of the member split.
    _insert_fact(pg_conn, entity="proj", attribute="tags", value="alpha",
                 kind="member", value_norm="alpha")
    _insert_fact(pg_conn, entity="proj", attribute="tags", value="beta",
                 kind="member", value_norm="beta")

    # A genuine scalar duplicate on a different slot -- bypass the scalar
    # unique index the fixture's own ensure_schema already built, exactly
    # like test_slot_persistence.py's pre-existing healing test does.
    pg_conn.execute("DROP INDEX IF EXISTS facts_slot_current_scalar_uq")
    _insert_fact(pg_conn, entity="proj", attribute="lang", value="older",
                 kind="scalar")
    _insert_fact(pg_conn, entity="proj", attribute="lang", value="newer",
                 kind="scalar")
    pg_conn.commit()

    ensure_schema(pg_conn)

    member_rows = {r[0] for r in pg_conn.execute(
        "SELECT value FROM facts WHERE entity_norm = 'proj' "
        "AND attribute_norm = 'tags' AND status = 'current'").fetchall()}
    assert member_rows == {"alpha", "beta"}, (
        "kind-aware healing must leave BOTH current members untouched")

    scalar_rows = {r[0]: r[1] for r in pg_conn.execute(
        "SELECT value, status FROM facts WHERE entity_norm = 'proj' "
        "AND attribute_norm = 'lang'").fetchall()}
    assert scalar_rows["newer"] == "current"
    assert scalar_rows["older"] == "superseded"
