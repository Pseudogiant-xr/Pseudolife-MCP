from tests.pg_fixtures import pg_conn, pg_url  # noqa: F401  (fixtures)
from pseudolife_memory.storage.schema import SCHEMA_META_VERSION


def test_schema_version_is_current():
    assert SCHEMA_META_VERSION == 21


def test_entity_sources_table_present(pg_conn):
    assert pg_conn.execute(
        "SELECT to_regclass('public.entity_sources')").fetchone()[0]
    cols = {r[0] for r in pg_conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='entity_sources'").fetchall()}
    assert {"entity_id", "source", "count", "origin", "updated_at"} <= cols
