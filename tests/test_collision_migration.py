"""Schema-collision migration (v0.4 T7) — needs the AGE-enabled dev Postgres.

Seeds the exact role/AGE-schema collision: an AGE graph named ``pseudolife``
(== the DB role name, so AGE makes a schema called ``pseudolife``) plus a shadow
relational table inside that schema. Proves ``ops/migrate_v04`` renames the
graph to ``pseudolife_graph``, drops the shadow, bumps meta — and that the
dry-run mutates nothing. Skips cleanly without a PG/AGE server.
"""

from __future__ import annotations

import pytest

from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)


def _age_available(conn) -> bool:
    try:
        conn.execute("LOAD 'age'")
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        conn.rollback()
        return False


def _graph_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (name,)
    ).fetchone() is not None


def _regclass(conn, qualified: str):
    return conn.execute("SELECT to_regclass(%s)", (qualified,)).fetchone()[0]


@pytest.fixture()
def age_conn(pg_conn):
    if not _age_available(pg_conn):
        pytest.skip("AGE not available on the test server")
    return pg_conn


def _seed_collision(conn):
    """Old AGE graph named after the role + a shadow table in its schema."""
    conn.execute("LOAD 'age'")
    conn.execute("SET search_path TO public, ag_catalog")
    if not _graph_exists(conn, "pseudolife"):
        conn.execute("SELECT ag_catalog.create_graph('pseudolife')")  # makes schema
    conn.execute("CREATE TABLE IF NOT EXISTS pseudolife.entries (id BIGINT)")
    conn.execute("DELETE FROM pseudolife.entries")
    conn.execute("INSERT INTO pseudolife.entries (id) VALUES (1)")
    conn.commit()


def test_dry_run_mutates_nothing(age_conn):
    from ops import migrate_v04

    _seed_collision(age_conn)
    # "Mutates nothing" → state after dry-run equals state before. (The new graph
    # may already exist from a prior run in the shared test DB; assert no change,
    # not absolute absence.)
    new_before = _graph_exists(age_conn, "pseudolife_graph")
    plan = migrate_v04.run(age_conn, apply=False)

    assert plan["apply"] is False
    assert _graph_exists(age_conn, "pseudolife")            # old graph untouched
    assert _graph_exists(age_conn, "pseudolife_graph") == new_before  # unchanged
    assert _regclass(age_conn, "pseudolife.entries") is not None  # shadow intact


def test_apply_renames_graph_drops_shadow_bumps_meta(age_conn):
    from ops import migrate_v04
    from pseudolife_memory.storage.schema import SCHEMA_META_VERSION

    _seed_collision(age_conn)
    migrate_v04.run(age_conn, apply=True)

    # (a) new graph exists + a Cypher round-trip works (rebuilt from truth)
    assert _graph_exists(age_conn, "pseudolife_graph")
    age_conn.execute("SET search_path TO ag_catalog, public")
    rows = age_conn.execute(
        "SELECT * FROM ag_catalog.cypher('pseudolife_graph', "
        "$age$ MATCH (n) RETURN count(n) $age$) AS (c agtype)"
    ).fetchall()
    age_conn.commit()
    assert rows and rows[0] is not None

    # (b) old colliding graph is gone
    assert not _graph_exists(age_conn, "pseudolife")

    # (c) the shadow table is gone
    age_conn.execute("SET search_path TO public, ag_catalog")
    age_conn.commit()
    assert _regclass(age_conn, "pseudolife.entries") is None

    # (d) meta.schema_version is current
    row = age_conn.execute(
        "SELECT value::text FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    assert int(row[0].strip('"')) == SCHEMA_META_VERSION
