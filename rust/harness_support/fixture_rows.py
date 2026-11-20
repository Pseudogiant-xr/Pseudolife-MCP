"""Insert captured rows only when they match the live disposable schema."""

from psycopg import sql
from psycopg.types.json import Json


def insert_fixture_rows(conn, table: str, rows: list[dict]) -> None:
    """Insert complete public-table rows inside a guarded fixture transaction."""
    if not rows:
        return
    ident = sql.Identifier("public", table)
    # Keep the checked columns stable until INSERT. This is the same lock
    # mode INSERT takes, acquired before reading the catalog.
    conn.execute(sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(ident))
    columns = {name for (name,) in conn.execute(
        "SELECT a.attname FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped",
        (table,),
    )}
    for index, row in enumerate(rows, start=1):
        if row.keys() != columns:
            missing = ", ".join(sorted(columns - row.keys())) or "none"
            unexpected = ", ".join(sorted(row.keys() - columns)) or "none"
            raise ValueError(f"public.{table} fixture row {index} column mismatch "
                             f"(missing {missing}, unexpected {unexpected})")
    conn.execute(sql.SQL("INSERT INTO {} SELECT * FROM json_populate_recordset(NULL::{}, %s)")
                 .format(ident, ident), (Json(rows),))
