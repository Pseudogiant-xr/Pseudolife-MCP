"""Database state of a disposable bank, as comparable JSON.

``dump`` captures the catalog (extensions, schemas, tables, columns, types,
defaults, constraints, indexes, sequences, functions, triggers, views) and
every row of every table, ordered by its primary key (or by every column
when a table has none). ``diff`` compares two dumps after the declared
normalizers, which replace nondeterministic values by rule, never by hand:

* With ``PL_HARNESS_BASE_DATABASE`` (a restored real bank every disposable
  database copies, ``pgdisposable.base_database``), a table's dump holds
  only the rows the base does not hold, byte for byte (``md5(row::text)``),
  and ``base_missing`` counts and digests the base rows that are gone
  (deleted or rewritten). A whole real bank per step would not fit in
  memory: on a 284 MB bank two full dumps took the harness past 9.6 GiB
  (daemon box, 2026-11-07). Base rows are identical on both sides by
  construction, so the comparison loses nothing but their display.
* ``NONDETERMINISTIC`` names ``(table, column)`` pairs whose values are
  wall-clock times or random identifiers. A rule ``"clock"`` keeps only
  null-or-not (two inserts may share a clock tick on one side and not the
  other); any other rule also keeps equality between values in the same
  dump, so a write that changes WHICH rows share an identifier still shows.
  An existing row's clock is compared exactly, except a base row's clock
  inside the arm's captured clock window (``normalize``'s ``windows``).
* ``event_scores_free`` compares the scores a retrieval event's served
  entries carry within ``EVENT_SCORE_TOL``, as the search harnesses do.
"""
from __future__ import annotations

import contextlib
from contextvars import ContextVar
import hashlib
import json
from collections import Counter
import math
import os
import re
import time as clock
import uuid
from datetime import date, datetime, time
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

# (table, column) -> why its value is not reproducible across two runs.
NONDETERMINISTIC: dict[tuple[str, str], str] = {}

# Tables whose rows the Python oracle writes and the Rust daemon does not
# yet, with the batch that ports the writer. Each is dumped with its catalog
# and no rows, and is a declared divergence in rust/daemon/divergences.md;
# the porting batch deletes its entry and proves the rows instead.
UNPORTED_ROWS: dict[str, str] = {
    # Both daemons' autosave and clean-exit flush write the counters row (D2,
    # schema v57; Rust since B4), each on its own autosave clock, so a
    # per-step dump cannot compare it. w2e.py's counters scenarios compare the
    # row at fixed points instead (its `counters` op).
    "cms_counters": "B4",
    # Both daemons' gate activity (schema v58): each refresher writes it on
    # its own once-a-minute clock with wall-clock times, so a per-step dump
    # cannot compare it either. The writer is ported (principal_activity.rs);
    # its sequences are compared against the Python recorder's goldens
    # (goldens/principal-activity.json) and its SQL by its database tests.
    "principal_activity": "v58-clock",
    "rejected_bearers": "v58-clock",
}

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
    if isinstance(value, (Decimal, uuid.UUID)):
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


# A dump's column lists and primary keys, read once after every table is
# locked (no ALTER lands until the dump ends): per table they cost two
# catalog queries per dump, 90 ms of a 516 ms dump over a bank-sized copy
# (this PC, bench Postgres, 46 tables, 2026-11-09).
_DUMP_COLUMNS: ContextVar[dict | None] = ContextVar("_DUMP_COLUMNS", default=None)


def _columns_by_table(conn) -> dict:
    """Every table's (column, data type) list in ordinal order and its
    primary key columns in key order, as the per-table queries read them."""
    cols: dict = {}
    for s, t, c, d in conn.execute(
            "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY table_schema, table_name, ordinal_position"):
        cols.setdefault((s, t), []).append((c, d))
    pks: dict = {}
    for s, t, a in conn.execute(
            "SELECT n.nspname, c.relname, a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
            "WHERE i.indisprimary ORDER BY n.nspname, c.relname, k.ord"):
        pks.setdefault((s, t), []).append(a)
    return {"cols": cols, "pks": pks}


def _order_columns(conn, schema: str, table: str) -> list[str]:
    prefetched = _DUMP_COLUMNS.get()
    if prefetched is not None:
        return list(prefetched["pks"].get((schema, table))
                    or [c for c, _ in prefetched["cols"].get((schema, table), [])])
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


_BASE_HASHES: dict[str, dict[str, Counter]] = {}
_BASE_KEYS: dict[str, dict[str, set[str]]] = {}
_BASE_INFO: dict[str, str] = {}
# Per base and table, where each base row sits: ``ctids`` maps a row's ctid
# to its (xmin, md5), with the table's file, columns and the xmins that wrote
# its rows, for ``_fast_delta``. ``md5`` is the table's ``_BASE_HASHES``
# Counter itself, so a caller's Counter shows which base it came from.
_BASE_ROWS: dict[str, dict[str, dict]] = {}
# The session settings PostgreSQL's output functions read, as the base was
# hashed: a session rendering any differently can leave the canary row's
# text alone (a float that prints the same) while other rows change. Every
# positive extra_float_digits prints the shortest exact float (PostgreSQL
# 12+), so they compare as one: the board's dump sets 3, the base read 1.
_BASE_RENDER: dict[str, str] = {}
_RENDER = ("SELECT json_build_array(" + ", ".join(
    f"current_setting('{g}')" for g in (
        "DateStyle", "IntervalStyle", "TimeZone", "bytea_output", "lc_monetary",
        "standard_conforming_strings", "quote_all_identifiers", "search_path", "xmloption"))
    + ", CASE WHEN current_setting('extra_float_digits')::int > 0 THEN 'shortest' "
      "ELSE current_setting('extra_float_digits') END)::text")
# A directory where one run's harness processes share each base's hashes
# (``mirror/forced.py`` sets it): a scenario is one process, and one hash
# pass over a real bank's copy took 6.75 s (daemon box during a forced run,
# 2026-11-09), so each process paid that before its first dump.
BASE_CACHE_ENV = "PL_HARNESS_BASE_CACHE"
_CACHE_FORMAT = 4
# The catalog rows ``t::text`` depends on: a column added, dropped or retyped
# changes rows' text without rewriting their tuples, and so does an enum label
# renamed or a composite type's attribute added, dropped or retyped (of the
# column's type, its element type or a domain's base type), which may reach
# only some rows (a NULL composite prints the same).
_ATTRS = ("SELECT coalesce(json_agg(json_build_array(a.attnum, a.attname, a.atttypid::int, "
          "a.atttypmod, a.attisdropped, a.atthasmissing, "
          "(SELECT json_agg(e.enumlabel ORDER BY e.enumtypid, e.enumsortorder) FROM pg_type ty "
          "JOIN pg_enum e ON e.enumtypid IN (ty.oid, ty.typelem, ty.typbasetype) "
          "WHERE ty.oid = a.atttypid), "
          "(SELECT json_agg(json_build_array(ca.attrelid::int, ca.attnum, ca.attname, "
          "ca.atttypid::int, ca.atttypmod, ca.attisdropped) ORDER BY ca.attrelid, ca.attnum) "
          "FROM pg_type ty JOIN pg_type ct ON ct.oid IN (ty.oid, ty.typelem, ty.typbasetype) "
          "JOIN pg_attribute ca ON ca.attrelid = ct.typrelid AND ca.attnum > 0 "
          "WHERE ty.oid = a.atttypid)) ORDER BY a.attnum), '[]')::text "
          "FROM pg_attribute a WHERE a.attrelid = %(rel)s::regclass AND a.attnum > 0")


def _regclass(schema: str, table: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in (schema, table))


def _table_stamp(conn, schema: str, table: str) -> dict:
    """What a base table's cached rows rest on: its file, its columns and how
    many of its rows each transaction wrote. Any write to the base adds an
    xmin or changes a count, a rewrite changes the file, an ALTER the
    columns; freezing keeps the raw xmin (PostgreSQL 9.4+)."""
    rel = _regclass(schema, table)
    filenode = conn.execute("SELECT pg_relation_filenode(%s::regclass)", (rel,)).fetchone()[0]
    attrs = conn.execute(_ATTRS, {"rel": rel}).fetchone()[0]
    census = conn.execute(sql.SQL("SELECT xmin::text, count(*) FROM {} GROUP BY 1 ORDER BY 1").format(
        sql.Identifier(schema, table))).fetchall()
    return {"filenode": filenode, "attrs": attrs, "census": [[x, n] for x, n in census]}


def _read_base_table(conn, schema: str, table: str) -> tuple[list, list]:
    """A base table's rows as [ctid, xmin, md5] and its first-column keys,
    in one pass."""
    first = _select_list(conn, schema, table)[0].split(", ", 1)[0]
    rows, keys = [], []
    for r in conn.execute(f'SELECT ctid::text, xmin::text, md5(t::text){", " + first if first else ""} '
                          f'FROM "{schema}"."{table}" t'):
        rows.append([r[0], r[1], r[2]])
        if first:
            keys.append(json.dumps(_plain(r[3]), sort_keys=True))
    return rows, keys


def _load_base(conn, base: str) -> dict[str, dict]:
    """Every base table's rows, stamps and keys: from the run's cache when
    every stamp still matches the base, else read and cached again."""
    oid = conn.execute("SELECT oid::int FROM pg_database WHERE datname = current_database()").fetchone()[0]
    render = conn.execute(_RENDER).fetchone()[0]
    stamps = {f"{s}.{t}": _table_stamp(conn, s, t) for s, t in _tables(conn)}
    cache_dir = os.environ.get(BASE_CACHE_ENV)
    path = os.path.join(cache_dir, f"{base}.json") if cache_dir else None
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("format") == _CACHE_FORMAT and cached.get("base") == base \
                    and cached.get("oid") == oid and cached.get("render") == render \
                    and {q: v["stamp"] for q, v in cached["tables"].items()} == stamps:
                return cached["tables"]
        except (OSError, ValueError, KeyError, TypeError):
            pass  # unreadable or stale: read the base again
    tables = {}
    for q, stamp in stamps.items():
        schema, table = q.split(".", 1)
        rows, keys = _read_base_table(conn, schema, table)
        tables[q] = {"stamp": stamp, "rows": rows, "keys": keys}
    # Stamped again: a base whose file, columns or census changed while it
    # was read is not cached. (A row added and deleted meanwhile leaves the
    # census; this process then uses the same read as the cache would.)
    if path and {f"{s}.{t}": _table_stamp(conn, s, t) for s, t in _tables(conn)} == stamps:
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                      encoding="utf-8") as f:
                json.dump({"format": _CACHE_FORMAT, "base": base, "oid": oid, "render": render,
                           "tables": tables}, f)
            os.replace(tmp, path)
        except OSError:  # the cache only saves time: this process has its read
            with contextlib.suppress(OSError):
                os.remove(tmp)
    return tables


def _base_hashes(dsn: str) -> dict[str, Counter] | None:
    """Every base table's row hashes, read once per process (the base never
    changes while a run copies it), or None without a base. With
    ``PL_HARNESS_BASE_CACHE`` a run's processes share one read."""
    base = os.environ.get("PL_HARNESS_BASE_DATABASE")
    if not base:
        return None
    if base not in _BASE_HASHES:
        from pseudolife_memory.storage.schema import refuse_production_database
        refuse_production_database(base)
        if not re.fullmatch(r"pl_cf_mir_[a-z0-9_]{1,40}", base):
            raise ValueError(f"refusing base database {base!r}: expected pl_cf_mir_*")
        info = psycopg.conninfo.make_conninfo(dsn, dbname=base)
        out, keys, located = {}, {}, {}
        with psycopg.connect(info, autocommit=True) as conn:
            _BASE_RENDER[base] = conn.execute(_RENDER).fetchone()[0]
            for qualified, body in _load_base(conn, base).items():
                out[qualified] = Counter(r[2] for r in body["rows"])
                keys[qualified] = set(body["keys"])
                located[qualified] = {
                    "md5": out[qualified], "ctids": {r[0]: (r[1], r[2]) for r in body["rows"]},
                    "xmins": sorted({r[1] for r in body["rows"]}),
                    "filenode": body["stamp"]["filenode"], "attrs": body["stamp"]["attrs"]}
        _BASE_HASHES[base], _BASE_KEYS[base], _BASE_INFO[base] = out, keys, info
        _BASE_ROWS[base] = located
    return _BASE_HASHES[base]


def base_keys(qualified: str) -> set[str]:
    """The base's first-column values of a table (as ``normalize`` keys a
    row), or none without a base: a base row a run rewrites is an existing
    row, whose clocks are compared exactly outside the arm's clock window."""
    return _BASE_KEYS.get(os.environ.get("PL_HARNESS_BASE_DATABASE") or "", {}).get(qualified, set())


def prior_rows(before: dict, qualified: str, current: list) -> list:
    """``before``'s rows of a table plus, under a base, the base's version of
    each row in ``current`` whose key the base holds and ``before`` does
    not, since a dump leaves out rows equal to the base. A consumer that
    compares a row with its earlier self reads this, not ``before`` alone."""
    rows = list(before["rows"].get(qualified, {}).get("rows", []))
    base = os.environ.get("PL_HARNESS_BASE_DATABASE") or ""
    keys = base_keys(qualified)
    if not keys:
        return rows
    have = {json.dumps(r[0], sort_keys=True) for r in rows}
    wanted = {json.dumps(r[0], sort_keys=True): r[0] for r in current}
    missing = [v for k, v in wanted.items() if k in keys and k not in have]
    if not missing:
        return rows
    schema, table = qualified.split(".", 1)
    with psycopg.connect(_BASE_INFO[base], autocommit=True) as conn:
        select, first = _select_list(conn, schema, table)
        rows += [_plain(list(r)) for r in conn.execute(
            f'SELECT {select} FROM "{schema}"."{table}" WHERE "{first}"::text = ANY(%s)',
            ([v if isinstance(v, str) else json.dumps(v) for v in missing],))]
    return rows


def _select_list(conn, schema: str, table: str) -> tuple[str, str]:
    """The dump's select list for a table (non-JSON types as text) and the
    name of its first column."""
    cols = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table)).fetchall()
    select = ", ".join(f'"{c}"::text' if t == "USER-DEFINED" else f'"{c}"' for c, t in cols)
    return select, (cols[0][0] if cols else "")


def _base_missing(known: Counter, present: Counter) -> dict:
    """The base rows gone, counted as a multiset (a table without a key may
    hold the same row twice)."""
    gone = sorted((known - present).elements())
    return {"count": len(gone), "digest": hashlib.sha256("".join(gone).encode()).hexdigest()}


FAST_ENV = "PL_HARNESS_DUMP_FAST"      # "0": always hash every row
VERIFY_ENV = "PL_HARNESS_DUMP_VERIFY"  # "1": run both paths and require one answer


class DeltaMismatch(AssertionError):
    """The fast delta and the full hash disagreed (``PL_HARNESS_DUMP_VERIFY``)."""


def base_delta(conn, schema: str, table: str, known: Counter, select: str,
               ordering: str = "1") -> tuple[list[str], list[tuple], dict]:
    """A table's rows the base does not hold, byte for byte, with
    ``select`` read per row in ``ordering``, as (column names, row tuples,
    ``base_missing``). ``_fast_delta`` answers when it can vouch for the
    table; otherwise every row is hashed (``_full_delta``). Both give the
    same answer, which ``PL_HARNESS_DUMP_VERIFY=1`` checks on every call."""
    fast = None if os.environ.get(FAST_ENV) == "0" else _fast_delta(
        conn, schema, table, known, select, ordering)
    if fast is not None and os.environ.get(VERIFY_ENV) != "1":
        return fast
    full = _full_delta(conn, schema, table, known, select, ordering)
    if fast is not None and fast != full:
        raise DeltaMismatch(f"{schema}.{table}: fast delta {fast[1:]!r:.600} vs full {full[1:]!r:.600}")
    return full


def _fast_delta(conn, schema: str, table: str, known: Counter, select: str,
                ordering: str) -> tuple[list[str], list[tuple], dict] | None:
    """``_full_delta``'s answer without hashing the rows the copy still
    shares with the base, or None when it cannot vouch for the table.

    A disposable database is a file copy of the base, so a base row nobody
    wrote is the same tuple at the same ctid with the same xmin, and its
    bytes are the base's. Every insert or update makes a new tuple with a
    new xmin, a rewrite to identical bytes included, and a delete removes
    the tuple. So only the rows whose xmin wrote no base row are hashed
    (``candidates``); a base tuple gone is found by ctid when fewer old-xmin
    rows remain than the base holds. The multisets then give exactly the
    full hash's surplus and ``base_missing``: the candidates' hashes minus
    the gone rows', and the other way round.

    It declines (None) for a table the base did not hash or another base
    hashed, a file rewritten (VACUUM FULL, CLUSTER, TRUNCATE, a rewriting
    retype: ctids no longer locate base rows), a catalog change to what
    ``t::text`` renders (a column added, dropped or retyped without a
    rewrite, an enum label renamed: ``_ATTRS``), a session rendering values
    differently from the base read (``_RENDER``), or an unchanged row whose
    text no longer hashes as the base's: a backstop for a rendering change
    neither shows (a type's output function replaced), which no test can
    stage without one of the other guards seeing it first.

    Freezing and wraparound cannot make a written row look like a base row:
    since PostgreSQL 9.4 freezing sets a flag and keeps the raw xmin, which
    ``xmin`` reads (checked on PostgreSQL 18.6, the bench's and the
    mirror's), and a transaction in the copy could reuse a base row's xid
    only after about 2**31 more transactions on the cluster, which no run
    approaches. The base's rows were written by one or two transactions per
    table (mirror seed, quiet and curation copies, 2026-11-09), so the
    ``= ANY`` test is a short list."""
    base = os.environ.get("PL_HARNESS_BASE_DATABASE") or ""
    info = _BASE_ROWS.get(base, {}).get(f"{schema}.{table}")
    if info is None or info["md5"] is not known:
        return None
    old = info["xmins"]
    rel = f'"{schema}"."{table}"'
    with conn.cursor(row_factory=tuple_row) as cur:
        # One statement per table: the scan, then (uncorrelated subqueries,
        # run once) the session's rendering settings, the table's file and
        # catalog, and one row the copy shares with the base. The statement
        # locks the table before it runs, so no rewrite lands between the
        # scan and the file it reports, nor until the dump ends.
        kept, candidates, render, filenode, attrs, canary = cur.execute(
            f"SELECT count(*) FILTER (WHERE xmin = ANY(%(old)s::xid[])), "
            f"coalesce(array_agg(ARRAY[ctid::text, md5(t::text)]) "
            f"FILTER (WHERE NOT xmin = ANY(%(old)s::xid[])), '{{}}'), "
            f"({_RENDER}), (SELECT pg_relation_filenode(%(rel)s::regclass)), ({_ATTRS}), "
            f"(SELECT ARRAY[t.ctid::text, t.xmin::text, md5(t::text)] FROM {rel} t "
            f"WHERE t.xmin = ANY(%(old)s::xid[]) LIMIT 1) FROM {rel} t",
            {"old": old, "rel": _regclass(schema, table)}).fetchone()
        if render != _BASE_RENDER.get(base):
            return None
        if filenode != info["filenode"]:
            return None
        if attrs != info["attrs"]:
            return None
        if kept:
            # One row the copy shares with the base must hash as the base's,
            # or every unchanged row's text has changed, and only the full
            # hash can tell which rows the base still holds.
            if canary is None or info["ctids"].get(canary[0]) != (canary[1], canary[2]):
                return None
        base_rows = sum(known.values())
        gone = Counter()
        if kept != base_rows:
            present = set(cur.execute(f"SELECT ctid::text, xmin::text FROM {rel} t "
                                      f"WHERE xmin = ANY(%s::xid[])", (old,)).fetchall())
            if len(present) != kept or kept > base_rows:
                return None
            gone = Counter(digest for ctid, (xmin, digest) in info["ctids"].items()
                           if (ctid, xmin) not in present)
            if sum(gone.values()) != base_rows - kept:
                return None
        added = Counter(digest for _, digest in candidates)
        # known - present and present - known, with present = known - gone + added.
        surplus = added - gone
        wanted = [ctid for ctid, digest in candidates if digest in surplus]
        fetched = cur.execute(
            f"SELECT md5(t::text), {select} FROM {rel} t WHERE ctid = ANY(%s::tid[]) "
            f"ORDER BY {ordering}", (wanted,)).fetchall()
        columns = [d.name for d in cur.description[1:]]
    rows, owed = [], Counter(surplus)
    for row in fetched:
        if owed[row[0]] > 0:
            owed[row[0]] -= 1
            rows.append(row[1:])
    return columns, rows, _base_missing(gone, added)


def _full_delta(conn, schema: str, table: str, known: Counter, select: str,
                ordering: str = "1") -> tuple[list[str], list[tuple], dict]:
    """Every row's hash crosses the wire, and the differing rows are
    fetched by hash."""
    with conn.cursor(row_factory=tuple_row) as cur:
        present = Counter(r[0] for r in cur.execute(
            f'SELECT md5(t::text) FROM "{schema}"."{table}" t'))
        # New rows, plus the extra copies of a row the base holds fewer times.
        surplus = present - known
        fetched = cur.execute(
            f'SELECT md5(t::text), {select} FROM "{schema}"."{table}" t '
            f'WHERE md5(t::text) = ANY(%s) ORDER BY {ordering}', (list(surplus),)).fetchall()
        columns = [d.name for d in cur.description[1:]]
    rows, owed = [], Counter(surplus)
    for row in fetched:
        if owed[row[0]] > 0:
            owed[row[0]] -= 1
            rows.append(row[1:])
    return columns, rows, _base_missing(known, present)


class _Busy(Exception):
    """A dump attempt gave way to a concurrent lock holder; retry it."""


def dump(dsn: str, timeout: float = 60.0) -> dict:
    """Every row and catalog query reads one REPEATABLE READ snapshot, so a
    write committing mid-dump (a prune cascading from dream_runs to
    dream_run_slots) cannot show one table before it and the next after.

    The snapshot's locks last until the dump ends, and a daemon's startup
    schema transaction takes ACCESS EXCLUSIVE locks in its own order, so a
    dump that waited while holding a lock could deadlock it. Every table is
    therefore locked NOWAIT before the snapshot exists, any other wait is
    cut at 100 ms, and a conflict releases everything and retries: the dump
    gives way, holding its locks through a wait of at most 100 ms (catalog
    reads of objects it did not lock). Sequence ``last_value``
    and the ``pg_get_*def`` functions are not transactional and can still be
    newer than the rows."""
    deadline = clock.monotonic() + timeout
    while True:
        try:
            return _dump_once(dsn)
        except (_Busy, psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected,
                psycopg.errors.UndefinedTable):
            if clock.monotonic() > deadline:
                raise
            clock.sleep(0.05)


def _dump_once(dsn: str) -> dict:
    out: dict = {"catalog": {}, "rows": {}}
    base = _base_hashes(dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        tables = _tables(conn)
    with psycopg.connect(dsn) as conn:
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        # Neither statement takes the snapshot; the first catalog query does,
        # after every table lock is held.
        conn.execute("SET LOCAL lock_timeout = '100ms'")
        if tables:
            conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE NOWAIT").format(
                sql.SQL(", ").join(sql.Identifier(s, t) for s, t in tables)))
        for key, query in _CATALOG.items():
            out["catalog"][key] = [_plain(list(r)) for r in conn.execute(query).fetchall()]
        if _tables(conn) != tables:
            raise _Busy("tables changed between listing and locking")
        prefetched = _columns_by_table(conn)
        token = _DUMP_COLUMNS.set(prefetched)
        try:
            _dump_tables(conn, tables, base, prefetched, out)
        finally:
            _DUMP_COLUMNS.reset(token)
    return out


def _dump_tables(conn, tables, base, prefetched, out) -> None:
    for schema, table in tables:
        order = _order_columns(conn, schema, table)
        # Vectors and other non-JSON types read as text: exact and comparable.
        cols = prefetched["cols"].get((schema, table), [])
        select = ", ".join(
            f'"{c}"::text' if t == "USER-DEFINED" else f'"{c}"' for c, t in cols)
        ordering = ", ".join(f'"{c}"::text' for c in order) or "1"
        key = f"{schema}.{table}"
        extra = {}
        if schema == "public" and table in UNPORTED_ROWS:
            rows = []
        elif base is not None and key in base:
            _, rows, extra["base_missing"] = base_delta(
                conn, schema, table, base[key], select, ordering)
        else:
            rows = conn.execute(
                f'SELECT {select} FROM "{schema}"."{table}" ORDER BY {ordering}').fetchall()
        out["rows"][key] = {
            "columns": [c for c, _ in cols],
            "rows": [_plain(list(r)) for r in rows],
            **extra,
        }


def _finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clock_windows(windows) -> list[tuple[float, float]]:
    """``windows`` (None, or (start, end) pairs) checked: two finite
    times each, start first."""
    out = []
    for pair in windows or ():
        start, end = pair
        if not (_finite_number(start) and _finite_number(end)) or start > end:
            raise ValueError(f"a clock window is two finite times, start first: {pair!r}")
        out.append((float(start), float(end)))
    return out


def normalize(state: dict, rules: dict[tuple[str, str], str] | None = None,
              before: dict | None = None, windows=None) -> dict:
    """Replace every declared nondeterministic value with a shape token.

    With ``before`` (the bank's state before the run), a row that already
    existed keeps its exact values: a clock column may change only on rows
    the run created, so a rewritten existing timestamp still diffs. A new
    clock value must be a finite number, or it is left raw and diffs.

    Under a base, a row whose key the base holds existed too, but a step may
    rewrite it with a fresh clock (a reaper's close, a slot read's
    ``last_read_at``), and each daemon writes its own now(). ``windows``
    lists the arm's captured clock windows, ``(start, end)`` in wall-clock
    seconds, from its daemon's first start until its dump: a base row's
    clock inside one was written during the run and becomes a new row's
    token; outside every window it stays raw and is compared exactly, so a
    base clock one arm keeps and the other restamps still diffs. A row only
    ``before`` holds (a synthetic seed) is compared exactly, as without
    windows."""
    rules = NONDETERMINISTIC if rules is None else rules
    spans = _clock_windows(windows)
    state = json.loads(json.dumps(state))
    for qualified, table in state["rows"].items():
        name = qualified.split(".", 1)[1]
        prior, held = set(), set()
        if before is not None:
            prior = {json.dumps(r[0], sort_keys=True)
                     for r in before["rows"].get(qualified, {}).get("rows", [])}
            held = base_keys(qualified)
            prior |= held  # rows equal to the base are not dumped
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
                    key = json.dumps(row[0], sort_keys=True)
                    if key in prior:
                        # An existing row: compared exactly, unless a base
                        # row's clock was written during the run.
                        if key in held and _finite_number(value) \
                                and any(lo <= value <= hi for lo, hi in spans):
                            row[idx] = f"<{col}>"
                        continue
                    if _finite_number(value):
                        row[idx] = f"<{col}>"
                    continue
                key = json.dumps(value, sort_keys=True)
                row[idx] = seen.setdefault(key, f"<{col}#{len(seen)}>")
    return state


# Search telemetry: a retrieval event's served entries carry their score and
# fusion inputs, which rest on the embeddings and so on float32 arithmetic.
# They compare within w2d_search.py's SCORE_TOL, as w2e.py's events_diff
# already compared them; on a copy of a real bank the arms' dense components
# differed by 1.2e-7 to 1.4e-6 (forced real-data run, reported 2026-11-10).
EVENT_FLOAT_KEYS = frozenset({"score", "dense", "ce", "bm25", "slot", "recency", "margin"})
EVENT_SCORE_TOL = 2e-4
EVENT_TOLERANCES = {k: EVENT_SCORE_TOL for k in EVENT_FLOAT_KEYS}


def pair_floats(x, y, tolerances: dict[str, float]) -> None:
    """In place: wherever ``x`` and ``y`` hold numbers under the same key
    that ``tolerances`` names, within its tolerance of each other, both
    become ``<key>``; everything else is left for an exact comparison."""
    if isinstance(x, dict) and isinstance(y, dict):
        for k in x.keys() & y.keys():
            a, b = x[k], y[k]
            numbers = (isinstance(a, (int, float)) and isinstance(b, (int, float))
                       and not isinstance(a, bool) and not isinstance(b, bool))
            if numbers and k in tolerances and abs(a - b) <= tolerances[k]:
                x[k] = y[k] = f"<{k}>"
            else:
                pair_floats(a, b, tolerances)
    elif isinstance(x, list) and isinstance(y, list):
        for a, b in zip(x, y):
            pair_floats(a, b, tolerances)


def event_scores_free(a: dict, b: dict) -> tuple[dict, dict]:
    """Copies of two normalized dumps in which each retrieval event both
    hold (matched by id) has its served entries' ``EVENT_FLOAT_KEYS``
    within ``EVENT_SCORE_TOL`` replaced by tokens on both sides. Every other
    column, field and table is left for ``diff`` to compare exactly."""
    a, b = json.loads(json.dumps(a)), json.loads(json.dumps(b))
    ea = a.get("rows", {}).get("public.retrieval_events")
    eb = b.get("rows", {}).get("public.retrieval_events")
    if not (isinstance(ea, dict) and isinstance(eb, dict)) or ea.get("columns") != eb.get("columns") \
            or "served" not in (ea.get("columns") or []):
        return a, b
    at = ea["columns"].index("served")
    by_id = {json.dumps(r[0], sort_keys=True): r for r in eb.get("rows", [])}
    for ra in ea.get("rows", []):
        rb = by_id.get(json.dumps(ra[0], sort_keys=True))
        if rb is not None:
            pair_floats(ra[at], rb[at], EVENT_TOLERANCES)
    return a, b


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
