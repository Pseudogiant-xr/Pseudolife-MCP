"""``pseudolife-mcp export`` / ``pseudolife-mcp import`` — logical bank
transfer.

pg_dump (``ops/backup.*`` on the Docker tier, ``pseudolife-mcp backup`` on
the pip tiers) stays the *physical* backup: engine-exact, restored with psql
into a matching server. This pair is the *logical* layer: the bank's
knowledge as a ZIP of JSONL files (one per table) plus a manifest —
decoupled from Postgres major version, deployment tier, and (additively)
schema version. Use it to move a bank between installs or tiers, and to
keep a human-readable copy so nothing is stranded inside the database.

Contract, pinned by ``tests/test_transfer_cli.py``:

* Export runs live-safe: one REPEATABLE READ snapshot, read-only.
* Every schema table is explicitly classified — knowledge tables in
  ``EXPORTED_TABLES``, operational telemetry/journals in
  ``EXCLUDED_TABLES`` — and the roster must cover ``BENCH_RESET_TABLES``
  exactly, so a future table forces a decision here.
* Import fills a FRESH bank only (refuses non-empty), inside one
  transaction, refuses while other connections hold the database (a
  running daemon — stop it first), preserves ids/HLC stamps/embeddings
  verbatim, and advances the id sequences past the imported rows.
* Column mismatches are asymmetric on purpose: an export *missing* a
  column loads fine (the target's DDL default applies — old export, new
  build), while an export *carrying* an unknown column refuses (new
  export, old build — importing would silently drop data).
* Build-owned and transient ``meta`` keys never travel: ``schema_version``
  and extension lineage markers (``*_schema_version``) belong to the
  target build, while the active-session pointer is session state.

The module stays torch-free: embeddings move verbatim as pgvector text (the
manifest pins the dimension; import refuses a mismatch), so neither command
needs the embedding model.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from typing import Mapping

import psycopg

from pseudolife_memory.backup_cli import _default_data_dir
from pseudolife_memory.storage import embedded_pg
from pseudolife_memory.storage.schema import BENCH_RESET_TABLES, ensure_schema

FORMAT_VERSION = 1

# FK-safe insert order: parents before children (entities before aliases /
# edges / proposals / sources, relations before edges, entries before
# memory_traces). Together with EXCLUDED_TABLES this must partition
# BENCH_RESET_TABLES — the roster test is the forcing function that makes a
# future table pick a side.
EXPORTED_TABLES = (
    "meta", "episodes", "entries", "entities", "entity_aliases",
    "relations", "edges", "edge_proposals", "entity_proposals",
    "entity_kinds", "dismissed_pairs", "facts", "world_facts", "lessons",
    "outcome_signals", "communities", "entity_communities",
    "memory_traces", "entity_sources", "merge_decisions",
    "chronicle_events",
    # v35: the store-curation judge's verdict memo travels like
    # dismissed_pairs — a settled pair stays settled on the target bank.
    "curation_judgments",
)

# Operational, not knowledge: the dream pre-image journal only exists to
# roll back runs against THIS bank's row ids, and the retrieval/read
# telemetry is training signal tied to this deployment's serving history.
EXCLUDED_TABLES = (
    "dream_runs", "dream_run_slots", "retrieval_events", "retrieval_uses",
    "slot_reads",
)

# meta keys that must not travel: the target build owns its schema_version
# and any extension lineage marker (the `*_schema_version` convention —
# see docs/guide/configuration.md#extension-schemas), and the
# active-session pointer is transient session state.
_META_SKIP_KEYS = {"schema_version", "active_session_pointer"}


def _skip_meta_key(key) -> bool:
    return key in _META_SKIP_KEYS or str(key).endswith("_schema_version")

# The freshness check import runs. Derived, not listed: every exported
# table must be empty except the two a daemon-initialized bank legitimately
# populates — meta (schema_version) and relations (the builtin vocabulary,
# merged on name at import). Deriving it means a future exported table is
# covered by construction instead of silently exempt.
_EMPTINESS_EXEMPT = ("meta", "relations")
_MUST_BE_EMPTY = tuple(
    t for t in EXPORTED_TABLES if t not in _EMPTINESS_EXEMPT)

# Import inserts this many rows per executemany flush — bounds peak memory
# so a large bank streams instead of materializing whole tables.
_BATCH_ROWS = 500


class TransferError(RuntimeError):
    """A refused export/import — message is the user-facing explanation."""


def _json_default(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__} for export")


def _connect(dsn: str) -> psycopg.Connection:
    # Autocommit like PostgresStorage: reads never leave an idle
    # transaction, and explicit conn.transaction() blocks stay real
    # transactions (not savepoints inside an implicit one).
    conn = psycopg.connect(dsn, connect_timeout=10, autocommit=True)
    conn.execute("SET search_path TO public")
    return conn


def _embedding_dim(conn) -> int | None:
    """The live entries.embedding dimension (atttypmod IS the dimension on
    a pgvector column — same probe schema.py's dim guard uses)."""
    row = conn.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = to_regclass('public.entries') "
        "AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped"
    ).fetchone()
    return row[0] if row and row[0] > 0 else None


def _column_types(conn, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, udt_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return {name: udt for name, udt in rows}


# ---------------------------------------------------------------- export


def perform_export(dsn: str, out: Path | str) -> dict:
    """Write a logical export of the bank at ``dsn`` to the ZIP ``out``.
    Read-only; safe against a live daemon (single snapshot)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(out) + ".part")
    counts: dict[str, int] = {}
    try:
        with _connect(dsn) as conn, conn.transaction():
            conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            # Shortest-round-trip float rendering regardless of what the
            # server/pooler pins extra_float_digits to — float4 embedding
            # components need 9 significant digits; the PG >=12 default (1)
            # provides them, but a pooler pinning 0 would silently truncate
            # every vector/confidence/ts. Same insurance pg_dump takes.
            conn.execute("SET LOCAL extra_float_digits = 3")
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as zf:
                for table in EXPORTED_TABLES:
                    counts[table] = _export_table(conn, zf, table)
                manifest = {
                    "format_version": FORMAT_VERSION,
                    "created_at": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(),
                    "schema_version": version_row[0] if version_row else None,
                    "embedding_dim": _embedding_dim(conn),
                    "pseudolife_version": _package_version(),
                    "counts": counts,
                    "excluded_tables": list(EXCLUDED_TABLES),
                }
                zf.writestr(
                    "manifest.json", json.dumps(manifest, indent=2))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(out)
    return {"path": out, "counts": counts}


def _export_table(conn, zf: zipfile.ZipFile, table: str) -> int:
    n = 0
    # Named (server-side) cursor: rows stream in itersize batches instead
    # of materializing the whole table client-side — entries embeddings are
    # ~12KB of text each, so a large bank would otherwise gulp gigabytes.
    # force_zip64 keeps an over-4GB member from failing at close time.
    with conn.cursor(name=f"pl_export_{table}") as cur, \
            zf.open(f"{table}.jsonl", "w", force_zip64=True) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="\n")
        # Table names come from EXPORTED_TABLES (a module literal), never
        # caller input, so the f-string identifier is safe. ORDER BY the
        # first column (the PK or its leading column on every exported
        # table) keeps output stable across heap reordering — and load
        # order matters for relations, whose self-referential inverse_of
        # FK the import defers regardless (belt and braces).
        cur.execute(f"SELECT * FROM {table} ORDER BY 1")
        cols = [d.name for d in cur.description]
        for row in cur:
            rec = dict(zip(cols, row))
            if table == "meta" and _skip_meta_key(rec.get("key")):
                continue
            text.write(json.dumps(
                rec, default=_json_default, ensure_ascii=False) + "\n")
            n += 1
        text.flush()
        text.detach()
    return n


def _package_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("pseudolife-mcp")
    except Exception:  # noqa: BLE001 — best-effort provenance only
        return None


# ---------------------------------------------------------------- import


def perform_import(dsn: str, zip_path: Path | str, force: bool = False) -> dict:
    """Load the logical export at ``zip_path`` into the FRESH bank at
    ``dsn``. One transaction; refuses a non-empty bank, other live
    connections (unless ``force``), unknown columns, and an embedding
    dimension mismatch."""
    zip_path = Path(zip_path)
    counts: dict[str, int] = {}
    with zipfile.ZipFile(zip_path) as zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            raise TransferError(
                f"{zip_path} carries no manifest.json — not a "
                "pseudolife-mcp export.") from None
        if manifest.get("format_version") != FORMAT_VERSION:
            raise TransferError(
                f"export format {manifest.get('format_version')!r} is not "
                f"the format {FORMAT_VERSION} this build reads — upgrade "
                "pseudolife-mcp and retry.")
        names = set(zf.namelist())
        with _connect(dsn) as conn:
            # Connection guard FIRST — before ensure_schema's DDL, whose
            # ACCESS EXCLUSIVE requests would otherwise queue behind a
            # running daemon and block its queries while this tool waits
            # to fail.
            _refuse_other_connections(conn, force)
            ensure_schema(conn)
            _refuse_dim_mismatch(conn, manifest)
            with conn.transaction():
                # Lock out concurrent writers (a daemon auto-spawned by a
                # shim mid-import) for the whole transaction, then check
                # emptiness UNDER the lock so "fresh bank" is atomic, not
                # check-then-act. EXCLUSIVE still allows reads.
                conn.execute("SET LOCAL lock_timeout = '5s'")
                conn.execute(
                    "LOCK TABLE " + ", ".join(EXPORTED_TABLES)
                    + " IN EXCLUSIVE MODE")
                _refuse_nonempty(conn)
                for table in EXPORTED_TABLES:
                    if f"{table}.jsonl" not in names:
                        continue  # an older export without this table
                    with zf.open(f"{table}.jsonl") as raw:
                        # newline="\n" on read as on write: only real
                        # newlines split records — text carrying U+2028/
                        # U+0085/U+2029 (which str.splitlines() would
                        # treat as line breaks) stays one record.
                        lines = io.TextIOWrapper(
                            raw, encoding="utf-8", newline="\n")
                        if table == "meta":
                            counts[table] = _import_meta(conn, lines)
                        else:
                            counts[table] = _import_table(
                                conn, table, lines)
                _advance_sequences(conn)
    return {"counts": counts}


def _refuse_other_connections(conn, force: bool) -> None:
    n = conn.execute(
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND backend_type = 'client backend'"
    ).fetchone()[0]
    if n and not force:
        raise TransferError(
            f"{n} other connection(s) hold this database — a running "
            "daemon? Stop it first (Docker tier: `docker compose -f "
            "ops/docker-compose.yml stop pseudolife-daemon`; pip tiers: "
            "stop the serve process), then retry. Pass --force only if "
            "you know the connections are inert.")


def _refuse_nonempty(conn) -> None:
    held = {}
    for table in _MUST_BE_EMPTY:
        n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if n:
            held[table] = n
    if held:
        detail = ", ".join(f"{t}={n}" for t, n in held.items())
        raise TransferError(
            f"target bank is not empty ({detail}) — import only fills a "
            "fresh bank. Point PSEUDOLIFE_MCP_DATABASE_URL at a new "
            "database (or start a fresh lite data dir) and retry.")


def _refuse_dim_mismatch(conn, manifest: dict) -> None:
    live = _embedding_dim(conn)
    exported = manifest.get("embedding_dim")
    if live and exported and live != exported:
        raise TransferError(
            f"export embeddings are {exported}-dimensional but this bank's "
            f"columns are vector({live}) — the vectors cannot load "
            "verbatim. Import on a matching build, or re-embed via the "
            "migration path (ops/migrate_embeddings.py) after importing "
            "there.")


def _import_meta(conn, lines) -> int:
    n = 0
    with conn.cursor() as cur:
        for line in lines:
            line = line.rstrip("\n")
            if not line:
                continue
            rec = json.loads(line)
            unknown = set(rec) - {"key", "value"}
            if unknown:
                raise TransferError(
                    f"meta rows in the export carry column(s) "
                    f"{sorted(unknown)} this build does not know — the "
                    "export came from a newer pseudolife-mcp; upgrade "
                    "this install and retry.")
            if _skip_meta_key(rec.get("key")):
                continue
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (rec["key"], json.dumps(rec["value"])),
            )
            n += 1
    return n


def _placeholder(udt: str) -> str:
    if udt == "vector":
        return "%s::vector"
    if udt == "jsonb":
        return "%s::jsonb"
    if udt == "timestamptz":
        return "%s::timestamptz"
    return "%s"


def _encode(value, udt: str):
    if value is None:
        return None
    if udt == "jsonb":
        return json.dumps(value)
    return value


def _import_table(conn, table: str, lines) -> int:
    types = _column_types(conn, table)
    # Builtin relations are (re-)seeded by every daemon start; an export
    # naturally carries them, so collisions on name are expected identity,
    # not data loss.
    on_conflict = (
        " ON CONFLICT (name) DO NOTHING" if table == "relations" else "")
    inserted = 0
    # Rows from one export usually share a column set, but group by key set
    # anyway so a hand-edited or cross-version file still loads. Flushed
    # every _BATCH_ROWS so a large table streams instead of materializing.
    groups: dict[tuple[str, ...], list[tuple]] = {}
    pending = 0
    # relations.inverse_of is a self-referential, non-deferrable FK, and
    # heap order (what the export read, ORDER BY name notwithstanding) has
    # no reason to put a referenced inverse first — insert every relation
    # with inverse_of NULL, then set the inverses once all names exist.
    deferred_inverses: list[tuple[str, str]] = []

    with conn.cursor() as cur:

        def flush() -> None:
            nonlocal inserted, pending
            for cols, params in groups.items():
                cur.executemany(
                    f'INSERT INTO {table} ({", ".join(cols)}) VALUES '
                    f'({", ".join(_placeholder(types[c]) for c in cols)})'
                    f"{on_conflict}",
                    params,
                )
                # rowcount is cumulative across an executemany: conflicts
                # relations skipped are not counted as imported.
                inserted += max(cur.rowcount, 0)
            groups.clear()
            pending = 0

        for line in lines:
            line = line.rstrip("\n")
            if not line:
                continue
            rec = json.loads(line)
            unknown = set(rec) - set(types)
            if unknown:
                raise TransferError(
                    f"table {table!r} in the export carries column(s) "
                    f"{sorted(unknown)} this build does not know — the "
                    "export came from a newer pseudolife-mcp; upgrade this "
                    "install and retry (importing here would silently drop "
                    "that data).")
            if table == "relations" and rec.get("inverse_of") is not None:
                deferred_inverses.append((rec["inverse_of"], rec["name"]))
                rec = {**rec, "inverse_of": None}
            cols = tuple(sorted(rec))
            groups.setdefault(cols, []).append(
                tuple(_encode(rec[c], types[c]) for c in cols))
            pending += 1
            if pending >= _BATCH_ROWS:
                flush()
        flush()
        for inverse, name in deferred_inverses:
            cur.execute(
                "UPDATE relations SET inverse_of = %s WHERE name = %s",
                (inverse, name),
            )
    return inserted


def _advance_sequences(conn) -> None:
    """Move each serial id sequence past the imported rows so fresh writes
    extend the bank instead of colliding."""
    for table in EXPORTED_TABLES:
        if "id" not in _column_types(conn, table):
            continue
        seq = conn.execute(
            "SELECT pg_get_serial_sequence(%s, 'id')", (table,)
        ).fetchone()[0]
        if not seq:
            continue  # e.g. communities: BIGINT PK without a sequence
        conn.execute(
            f"SELECT setval(%s, (SELECT MAX(id) FROM {table}), true) "
            f"WHERE EXISTS (SELECT 1 FROM {table})",
            (seq,),
        )


# ------------------------------------------------------------------ CLI


def _resolve_dsn(
    data_dir: Path, environ: Mapping[str, str] = os.environ,
):
    """Mirror backup_cli's resolution: the explicit DSN env var, else the
    lite tier's embedded instance (attached, or started for the duration —
    never initialized: transfer against a bank that does not exist yet is
    an error, not a reason to create one)."""
    dsn = environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
    own_instance = None
    if not dsn and embedded_pg.available():
        if (Path(data_dir) / "embedded_pg" / "PG_VERSION").exists():
            dsn, own_instance = embedded_pg.attach_or_start(Path(data_dir))
    return dsn, own_instance


def run_transfer(mode: str) -> None:
    parser = argparse.ArgumentParser(
        prog=f"pseudolife-mcp {mode}",
        description=(
            "Write a portable logical export of the bank (ZIP of JSONL "
            "tables + manifest)." if mode == "export" else
            "Load a logical export into a fresh, empty bank."),
    )
    if mode == "export":
        parser.add_argument(
            "--out", type=Path, default=None,
            help="destination .zip (default: ./pseudolife-export-<ts>.zip)")
    else:
        parser.add_argument("archive", type=Path, help="the export .zip")
        parser.add_argument(
            "--force", action="store_true",
            help="import even while other connections hold the database")
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="bank data dir for the lite tier (default: "
             "PSEUDOLIFE_MCP_DATA_DIR, the lite per-user dir, or ./data)")
    args = parser.parse_args(sys.argv[2:])

    data_dir = args.data_dir or _default_data_dir(os.environ)
    dsn, own_instance = _resolve_dsn(data_dir)
    if not dsn:
        print(
            "no database configured — set PSEUDOLIFE_MCP_DATABASE_URL "
            "(Docker tier: postgresql://pseudolife:<POSTGRES_PASSWORD from "
            "ops/.env>@127.0.0.1:5433/pseudolife_memory) or use a lite "
            "data dir that holds a bank.",
            file=sys.stderr)
        sys.exit(1)
    try:
        if mode == "export":
            out = args.out or Path.cwd() / (
                f"pseudolife-export-{time.strftime('%Y%m%d-%H%M%S')}.zip")
            result = perform_export(dsn, out)
            print(f"exported to: {result['path']}")
        else:
            result = perform_import(dsn, args.archive, force=args.force)
            print(f"imported: {args.archive}")
        for table, n in result["counts"].items():
            if n:
                print(f"  {table}: {n}")
    except TransferError as exc:
        print(f"{mode} refused: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if own_instance is not None:
            own_instance.stop()
