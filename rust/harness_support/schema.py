"""Execute the daemon's committed schema plan on a disposable connection.

No DDL lives here. The cli-startup schema cells compare this executor with
the daemon's schema-only probe, including the complete catalog and rows.
"""
import json
from pathlib import Path

from psycopg import RawCursor

from .database_guard import assert_disposable_database

STORAGE = Path(__file__).resolve().parents[1] / "daemon" / "src" / "storage"
OPERATIONS = {"version_guard", "embedding_guard", "base_schema", "sql", "remember", "if"}


def _test(predicate, last, variables):
    if set(predicate) == {"not"}:
        return not _test(predicate["not"], last, variables)
    if set(predicate) == {"variable"}:
        return variables[predicate["variable"]]
    if predicate == {"last": "row"}:
        return last is not None
    if predicate == {"last": "value"}:
        return last is not None and last[0] is not None
    raise ValueError("unknown schema predicate")


def _first(conn, statement):
    with conn.execute(statement) as cursor:
        while True:
            if cursor.description is not None:
                row = cursor.fetchone()
                if row is not None:
                    return row
            if not cursor.nextset():
                return None


def _validate(steps):
    for step in steps:
        if step["op"] not in OPERATIONS:
            raise ValueError("unknown schema operation: " + step["op"])
        if step["op"] == "if":
            _validate(step["then"])
            _validate(step["else"])


def load_plan():
    """Read UTF-8 artifacts, using Python's universal-newline normalization."""
    plan = json.loads((STORAGE / "schema_plan.json").read_text(encoding="utf-8"))
    header, sql = (STORAGE / "schema.sql").read_text(encoding="utf-8").split("\n", 1)
    if plan["format"] != 1 or header != f'-- schema_version {plan["schema_version"]}':
        raise ValueError("unsupported or inconsistent schema artifacts")
    _validate(plan["steps"])
    return plan, sql


def ensure_schema(conn):
    """Initialize/repair schema only; caller commits any existing transaction.

    The transaction context rolls back all our DDL on failure, or uses one
    savepoint when the caller already owns the transaction (as psycopg does).
    """
    plan, sql = load_plan()
    pending, variables, last = list(reversed(plan["steps"])), {}, None
    with conn.transaction():
        assert_disposable_database(conn)
        conn.execute("SET LOCAL search_path TO public")
        while pending:
            step = pending.pop()
            op = step["op"]
            if op == "version_guard":
                exists = _first(conn, "SELECT to_regclass('public.meta')")
                if exists is not None and exists[0] is not None:
                    row = _first(conn, "SELECT value::text FROM public.meta WHERE key = 'schema_version'")
                    if row is not None and row[0] is not None:
                        version = json.loads(row[0])
                        if type(version) is not int or not 1 <= version <= plan["schema_version"]:
                            raise RuntimeError("refusing unsupported bank schema_version; bank left unchanged")
            elif op == "embedding_guard":
                row = _first(conn, plan["embedding_probe"])
                dim = int(row[0]) if row is not None and row[0] is not None else -1
                if dim > 0 and dim != plan["embedding_dimension"]:
                    raise RuntimeError(plan["embedding_refusal"].replace("{live_dim}", str(dim)))
            elif op == "base_schema":
                last = _first(conn, sql)
            elif op == "sql":
                if step["parameters"]:
                    # RawCursor retains the plan's PostgreSQL $n placeholders.
                    with RawCursor(conn) as cursor:
                        cursor.execute(step["sql"], step["parameters"])
                    last = None
                else:
                    last = _first(conn, step["sql"])
            elif op == "remember":
                variables[step["name"]] = _test(step["test"], last, variables)
            elif op == "if":
                branch = "then" if _test(step["test"], last, variables) else "else"
                pending.extend(reversed(step[branch]))
            else:
                raise ValueError("unknown schema operation: " + op)
