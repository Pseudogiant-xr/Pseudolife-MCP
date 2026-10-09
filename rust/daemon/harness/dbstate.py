"""Database state of a disposable bank, as comparable JSON.

``dump`` captures the catalog (extensions, schemas, tables, columns, types,
defaults, constraints, indexes, sequences, functions, triggers, views) and
every row of every table, ordered by its primary key (or by every column
when a table has none). ``diff`` compares two dumps after the declared
normalizers, which replace nondeterministic values by rule, never by hand:

* ``NONDETERMINISTIC`` names ``(table, column)`` pairs whose values are
  wall-clock times or random identifiers. A rule ``"clock"`` keeps only
  null-or-not (two inserts may share a clock tick on one side and not the
  other); any other rule also keeps equality between values in the same
  dump, so a write that changes WHICH rows share an identifier still shows.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal

import psycopg

# (table, column) -> why its value is not reproducible across two runs.
NONDETERMINISTIC: dict[tuple[str, str], str] = {}

_CATALOG = {
    "extensions": "SELECT extname, extversion, n.nspname FROM pg_extension e "
                  "JOIN pg_namespace n ON n.oid = e.extnamespace ORDER BY extname",
    "schemas": "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
               "AND nspname <> 'information_schema' ORDER BY 1",
    "columns": "SELECT table_schema, table_name, ordinal_position, column_name, data_type, "
               "udt_name, character_maximum_length, numeric_precision, is_nullable, "
               "column_default, is_identity, identity_generation, is_generated, "
               "generation_expression FROM information_schema.columns "
               "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
               "ORDER BY 1, 2, 3",
    "constraints": "SELECT n.nspname, c.relname, con.conname, con.contype, "
                   "pg_get_constraintdef(con.oid, true) FROM pg_constraint con "
                   "JOIN pg_class c ON c.oid = con.conrelid "
                   "JOIN pg_namespace n ON n.oid = c.relnamespace "
                   "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2, 3",
    "indexes": "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
               "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2, 3",
    "sequences": "SELECT schemaname, sequencename, data_type::text, start_value, increment_by, "
                 "last_value FROM pg_sequences "
                 "WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2",
    "functions": "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), "
                 "md5(pg_get_functiondef(p.oid)) FROM pg_proc p "
                 "JOIN pg_namespace n ON n.oid = p.pronamespace "
                 "LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e' "
                 "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                 "AND d.objid IS NULL AND p.prokind IN ('f', 'p') ORDER BY 1, 2, 3",
    "triggers": "SELECT event_object_schema, event_object_table, trigger_name, "
                "action_timing, event_manipulation, action_statement "
                "FROM information_schema.triggers ORDER BY 1, 2, 3, 5",
    "views": "SELECT table_schema, table_name, md5(view_definition) FROM information_schema.views "
             "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2",
    "column_types": "SELECT n.nspname, c.relname, a.attnum, a.attname, "
                    "format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity::text "
                    "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p') "
                    "AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
                    "ORDER BY 1, 2, 3",
    "sequence_bounds": "SELECT schemaname, sequencename, min_value, max_value, cache_size, cycle "
                       "FROM pg_sequences WHERE schemaname NOT IN ('pg_catalog', 'information_schema') "
                       "ORDER BY 1, 2",
    "owners_and_comments": "SELECT n.nspname, c.relname, c.relkind::text, "
                           "pg_get_userbyid(c.relowner), "
                           "obj_description(c.oid, 'pg_class') FROM pg_class c "
                           "JOIN pg_namespace n ON n.oid = c.relnamespace "
                           "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast') "
                           "ORDER BY 1, 2",
}


def _plain(value):
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _tables(conn) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname NOT IN ('pg_catalog', 'information_schema', "
        "'pg_toast') ORDER BY 1, 2").fetchall()
    return [(s, t) for s, t in rows]


def _order_columns(conn, schema: str, table: str) -> list[str]:
    pk = conn.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        "WHERE i.indisprimary AND n.nspname = %s AND c.relname = %s ORDER BY k.ord",
        (schema, table)).fetchall()
    if pk:
        return [r[0] for r in pk]
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = %s ORDER BY ordinal_position", (schema, table)).fetchall()
    return [r[0] for r in cols]


def dump(dsn: str) -> dict:
    out: dict = {"catalog": {}, "rows": {}}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for key, query in _CATALOG.items():
            out["catalog"][key] = [_plain(list(r)) for r in conn.execute(query).fetchall()]
        for schema, table in _tables(conn):
            order = _order_columns(conn, schema, table)
            # Vectors and other non-JSON types read as text: exact and comparable.
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (schema, table)).fetchall()
            select = ", ".join(
                f'"{c}"::text' if t == "USER-DEFINED" else f'"{c}"' for c, t in cols)
            ordering = ", ".join(f'"{c}"::text' for c in order) or "1"
            rows = conn.execute(
                f'SELECT {select} FROM "{schema}"."{table}" ORDER BY {ordering}').fetchall()
            out["rows"][f"{schema}.{table}"] = {
                "columns": [c for c, _ in cols],
                "rows": [_plain(list(r)) for r in rows],
            }
    return out


def normalize(state: dict, rules: dict[tuple[str, str], str] | None = None,
              before: dict | None = None) -> dict:
    """Replace every declared nondeterministic value with a shape token.

    With ``before`` (the bank's state before the run), a row that already
    existed keeps its exact values: a clock column may change only on rows
    the run created, so a rewritten existing timestamp still diffs. A new
    clock value must be a finite number, or it is left raw and diffs."""
    rules = NONDETERMINISTIC if rules is None else rules
    state = json.loads(json.dumps(state))
    for qualified, table in state["rows"].items():
        name = qualified.split(".", 1)[1]
        prior = set()
        if before is not None:
            prior = {json.dumps(r[0], sort_keys=True)
                     for r in before["rows"].get(qualified, {}).get("rows", [])}
        for idx, col in enumerate(table["columns"]):
            if (name, col) not in rules:
                continue
            seen: dict[str, str] = {}
            clock = rules[(name, col)].startswith("clock")
            for row in table["rows"]:
                value = row[idx]
                if value is None:
                    continue
                if clock:
                    if json.dumps(row[0], sort_keys=True) in prior:
                        continue  # an existing row: compared exactly
                    if isinstance(value, (int, float)) and not isinstance(value, bool) \
                            and value == value and abs(value) != float("inf"):
                        row[idx] = f"<{col}>"
                    continue
                key = json.dumps(value, sort_keys=True)
                row[idx] = seen.setdefault(key, f"<{col}#{len(seen)}>")
    return state


def diff(a: dict, b: dict, path: str = "") -> list[str]:
    """Differences between two normalized states, as readable strings."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in b:
                out.append(f"{path}/{k}: only in python")
            elif k not in a:
                out.append(f"{path}/{k}: only in rust")
            else:
                out += diff(a[k], b[k], f"{path}/{k}")
    elif isinstance(a, list) and isinstance(b, list) and path.endswith("/rows"):
        sa = {json.dumps(r, sort_keys=True) for r in a}
        sb = {json.dumps(r, sort_keys=True) for r in b}
        for r in sorted(sa - sb)[:10]:
            out.append(f"{path}: row only in python: {r[:300]}")
        for r in sorted(sb - sa)[:10]:
            out.append(f"{path}: row only in rust: {r[:300]}")
        if not out and a != b:
            out.append(f"{path}: same rows in a different order")
    elif type(a) is not type(b) or a != b:
        out.append(f"{path}: python {json.dumps(a)[:300]} vs rust {json.dumps(b)[:300]}")
    return out
