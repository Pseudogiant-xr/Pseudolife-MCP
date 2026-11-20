"""Insert captured rows only when they match the live disposable schema."""

from psycopg import sql
from psycopg.types.json import Json


def checked_fixture_insert(conn, table: str, rows: list[dict]):
    """Prepare an INSERT for execution within the same guarded transaction."""
    ident = sql.Identifier("public", table)
    # Keep the checked columns stable until INSERT. This is the same lock
    # mode INSERT takes, acquired before reading the catalog.
    conn.execute(sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(ident))
    # Use the locked relation's row descriptor. An earlier REPEATABLE READ
    # snapshot can still see old rows in pg_attribute.
    with conn.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(ident)) as cursor:
        columns = {column.name for column in cursor.description}
    for index, row in enumerate(rows, start=1):
        if row.keys() != columns:
            missing = ", ".join(sorted(columns - row.keys())) or "none"
            unexpected = ", ".join(sorted(row.keys() - columns)) or "none"
            raise ValueError(f"public.{table} fixture row {index} column mismatch "
                             f"(missing {missing}, unexpected {unexpected})")
    statement = sql.SQL("INSERT INTO {} SELECT * FROM json_populate_recordset(NULL::{}, %s)")
    return statement.format(ident, ident), (Json(rows),)
