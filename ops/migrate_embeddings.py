"""Schema v25 embedding migration: vector(384) -> vector(1024)
(Qwen3-Embedding-0.6B backbone swap).

Human-gated, dry-run by default, backup-first — same discipline as the
other one-time `ops/*.py` maintenance scripts (``dedup_cortex.py``,
``retire_by_writer.py``). See
docs/superpowers/specs/2026-07-28-embedding-backbone-v25-design.md
("The migration script") for the full design rationale.

WHY THIS EXISTS
----------------
``ensure_schema`` (``pseudolife_memory/storage/schema.py``) is additive-only
and REFUSES to boot the daemon against a bank whose ``entries.embedding`` is
not already ``vector(1024)`` — see
:func:`pseudolife_memory.storage.schema._refuse_on_embedding_dim_mismatch`.
This script is the one deliberate, human-run exception: it takes a live
v24 bank (four ``vector(384)`` columns) to v25 (``vector(1024)``) offline,
re-embedding every row through the SAME ``EmbeddingPipeline`` and the SAME
text construction the write paths use (single-copy rule — nothing here is
a re-implementation of ``MemoryService.cortex_write`` / ``world_write`` /
``lesson_write``'s claim-text shape, it is that shape, cited below).

SAFETY
------
* Dry-run is the DEFAULT: prints the plan (per-table row counts + current
  vs target dimension) and writes nothing. Pass ``--apply`` to actually run
  the migration.
* ``--apply`` REFUSES without ``--backup-verified`` — back up the bank
  (``ops/backup.ps1``) first and confirm it, then pass the flag.
* ``--apply`` REFUSES while the daemon answers its health endpoint
  (default ``http://127.0.0.1:8765/health``, override with
  ``--health-url`` — tests point this at a throwaway local server). A
  live writer re-embedding underneath the daemon's own in-memory state
  would corrupt the bank; the daemon must be stopped first.
* Each of the four tables (``entries``, ``facts``, ``world_facts``,
  ``lessons``) is migrated in its OWN transaction: drop NOT NULL where
  present (``entries`` only — the other three columns are already
  nullable), ``ALTER COLUMN embedding TYPE vector(1024) USING NULL``
  (a straight cast from vector(384) to vector(1024) is not defined, so
  the old values are discarded as part of the type change and every row
  is re-embedded immediately after, inside the same transaction), restore
  NOT NULL on ``entries``. No vector index is created or rebuilt — there
  is currently none: ``ensure_schema`` drops ``entries_embedding_idx``
  unconditionally on every boot (2026-07-02 zombie sweep) because all
  similarity search happens in Python over the hydrated bands, not via a
  SQL vector query.
* ``SCHEMA_META_VERSION`` (25) is stamped in ``meta`` LAST, only after all
  four tables have migrated without error — a bank that failed partway
  through must not claim to be on schema v25.

SLOT EMBEDDINGS — NOT MIGRATED, AND NOT MISSED
------------------------------------------------
``CortexRecord.slot_embedding`` (the value-free ``"{entity} {attribute}"``
vector used for paraphrase-robust slot matching — ``cortex_dedup``,
``_resolve_dream_slot``) has NO column in ``facts`` (see ``_FACT_COLS`` /
the ``facts`` table DDL in ``storage/schema.py``, and
``storage/sync.py::_record_to_row`` / ``hydrate_cortex``, neither of which
read or write it). It is a pure in-memory, lazily-recomputed field:
``hydrate_cortex`` always sets it to ``None`` for every restored record,
and the two call sites that need it backfill on demand
(``MemoryService.cortex_dedup``, service.py ~line 2576;
``MemoryService._resolve_dream_slot``, service.py ~line 2605) via
``encode_single(f"{entity} {attribute}".strip())`` against whatever
embedder is live at the time. This already happens on every daemon
restart today, dimension change or not — there is nothing stored to
migrate, and the existing lazy path self-heals against the post-
migration 1024-d pipeline the first time either call site runs.

ROLLBACK
--------
There is no in-place downgrade path (a ``vector(1024)`` column cannot be
cast back to a ``vector(384)`` with the old values intact — they were
discarded by the ``USING NULL`` cast the moment the column type changed).
If the migration fails partway, or the re-embedded bank looks wrong,
restore from the pre-migration backup (``ops/backup.ps1`` output) and
redeploy the pre-migration image tag. Do not attempt to "fix forward" a
partially-migrated bank by hand.

USAGE
-----
    python ops/migrate_embeddings.py                          # dry-run report
    python ops/migrate_embeddings.py --apply --backup-verified
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

import psycopg
from pgvector.psycopg import register_vector

from pseudolife_memory.memory.embedding import EmbeddingPipeline
from pseudolife_memory.storage.postgres import _embedding_in
from pseudolife_memory.storage.schema import (
    SCHEMA_META_VERSION,
    _EXPECTED_EMBEDDING_DIM as TARGET_DIM,
)
from pseudolife_memory.utils.config import EmbeddingConfig

_DEFAULT_DSN = "postgresql://pseudolife:pseudolife@127.0.0.1:5433/pseudolife_memory"
_DEFAULT_HEALTH_URL = "http://127.0.0.1:8765/health"

# The four embedding-carrying tables, in migration order. Order doesn't
# matter functionally (each is its own transaction, none reference another
# table's embedding column), but entries first mirrors the write paths'
# rough write frequency (most churny table first, so a failure surfaces
# early rather than after the smaller tables already committed).
_TABLES = ("entries", "facts", "world_facts", "lessons")


def _live_dim(conn, table: str) -> int | None:
    """``atttypmod`` on ``{table}.embedding`` — the declared vector
    dimension verbatim (see schema.py's ``_refuse_on_embedding_dim_mismatch``
    docstring for why this is the reliable way to read it). ``None`` if the
    table doesn't exist."""
    row = conn.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = to_regclass(%s) AND attname = 'embedding' "
        "AND attnum > 0 AND NOT attisdropped",
        (f"public.{table}",),
    ).fetchone()
    return row[0] if row else None


def _row_count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 — fixed table names, not user input


def build_plan(conn) -> dict[str, dict]:
    """Per-table {dim, rows} snapshot — printed in both dry-run and apply."""
    return {t: {"dim": _live_dim(conn, t), "rows": _row_count(conn, t)}
            for t in _TABLES}


def print_plan(plan: dict[str, dict], target_dim: int) -> None:
    print(f"Target dimension: vector({target_dim})")
    for table, info in plan.items():
        dim = info["dim"]
        if dim is None:
            print(f"  {table:12s}  MISSING (table does not exist)")
        elif dim == target_dim:
            print(f"  {table:12s}  {info['rows']:6d} rows  already vector({dim})")
        else:
            print(f"  {table:12s}  {info['rows']:6d} rows  vector({dim}) -> vector({target_dim})")


def _daemon_reachable(health_url: str, timeout: float = 2.0) -> bool:
    """True iff something answers ``health_url`` at all — any HTTP status
    counts (an unhealthy-but-listening daemon is still a live writer).
    Connection refused / timeout / DNS failure means nothing is listening,
    which is the only condition under which ``--apply`` may proceed."""
    try:
        urllib.request.urlopen(health_url, timeout=timeout)  # noqa: S310 — fixed localhost health probe, not user-controlled
        return True
    except urllib.error.HTTPError:
        return True  # the server answered, just with a non-2xx status
    except urllib.error.URLError:
        return False
    except OSError:
        return False


def _claim_text(entity: str, attribute: str, value: str) -> str:
    """The EXACT claim-text shape every canonical-store write path commits
    (single-copy rule — cited, not re-derived):

    * ``MemoryService.cortex_write``  (service.py ~line 1509): facts
    * ``MemoryService.world_write``   (service.py ~line 1706): world_facts
    * ``MemoryService.lesson_write``  (service.py ~line 1828, task/aspect/
      lesson map onto entity/attribute/value — see
      ``storage/sync.py::_lesson_record_to_row``): lessons

    All three build ``f"{entity} {attribute} {value}".strip()`` from the
    row's own (entity, attribute, value) columns. One helper, one shape.
    """
    return f"{entity} {attribute} {value}".strip()


def _fetch_rows(conn, table: str) -> list[dict]:
    if table == "entries":
        rows = conn.execute("SELECT id, text FROM entries ORDER BY id").fetchall()
        return [{"id": r[0], "text": r[1]} for r in rows]
    rows = conn.execute(
        f"SELECT id, entity, attribute, value FROM {table} ORDER BY id"  # noqa: S608
    ).fetchall()
    return [{"id": r[0], "entity": r[1], "attribute": r[2], "value": r[3]} for r in rows]


def _row_text(table: str, row: dict) -> str:
    if table == "entries":
        # Document-side, verbatim — the exact text MemoryService.store()
        # embedded via encode_single(text) (service.py ~line 800). No
        # reconstruction needed: the stored text IS the document.
        return row["text"]
    return _claim_text(row["entity"], row["attribute"], row["value"])


def migrate_table(conn, pipeline: EmbeddingPipeline, table: str) -> dict:
    """Migrate one table's embedding column to ``vector(TARGET_DIM)`` in a
    single transaction: drop NOT NULL (entries only), ALTER the column type
    (discarding old values — USING NULL), re-embed every row DOCUMENT-side
    through the real pipeline (``encode`` — never ``encode_query``: these
    are stored vectors, not retrieval probes), write the vectors back,
    restore NOT NULL (entries only). Creates no index."""
    rows = _fetch_rows(conn, table)
    texts = [_row_text(table, r) for r in rows]

    with conn.transaction():
        if table == "entries":
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
        conn.execute(
            f"ALTER TABLE {table} ALTER COLUMN embedding "  # noqa: S608
            f"TYPE vector({TARGET_DIM}) USING NULL"
        )
        if texts:
            vectors = pipeline.encode(texts, normalize=True)
            with conn.cursor() as cur:
                cur.executemany(
                    f"UPDATE {table} SET embedding = %s WHERE id = %s",  # noqa: S608
                    [(_embedding_in(vec), r["id"]) for r, vec in zip(rows, vectors)],
                )
        if table == "entries":
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding SET NOT NULL")

    return {"table": table, "rows": len(rows)}


def stamp_schema_version(conn, version: int) -> None:
    """Stamp ``meta.schema_version`` — mirrors schema.py's own upsert
    exactly. Called ONLY after every table above has migrated without
    raising (see module docstring: "only after all four tables have
    migrated without error")."""
    with conn.transaction():
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', %s::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(version),),
        )


def _build_pipeline() -> EmbeddingPipeline:
    cfg = EmbeddingConfig()
    if cfg.batch_size < 32:
        cfg.batch_size = 32  # batch encodes, not one row at a time
    return EmbeddingPipeline(cfg)


def run(args: argparse.Namespace) -> int:
    # Autocommit + explicit conn.transaction() blocks, mirroring
    # PostgresStorage._connect()/_txn() (storage/postgres.py) — plain
    # reads (build_plan, _fetch_rows) never leave an idle transaction open,
    # and each table's migration gets its own real transaction.
    conn = psycopg.connect(args.dsn, connect_timeout=10, autocommit=True)
    # Pin the namespace the same way PostgresStorage._connect() does: the DB
    # role (`pseudolife`) can clash with schema names under the cluster
    # default ("$user", public) search_path, which would otherwise resolve
    # this script's bare table names to the wrong schema.
    conn.execute("SET search_path TO public")
    register_vector(conn)
    try:
        plan = build_plan(conn)
        print_plan(plan, TARGET_DIM)

        pending = [t for t, info in plan.items()
                   if info["dim"] is not None and info["dim"] != TARGET_DIM]
        if not pending:
            print("\nNothing to do: every table already reports "
                  f"vector({TARGET_DIM}).")
            return 0

        if not args.apply:
            print(f"\nDRY RUN — {len(pending)} table(s) would be migrated: "
                  f"{', '.join(pending)}. Re-run with --apply "
                  "(after --backup-verified) to write.")
            return 0

        if not args.backup_verified:
            print("\nREFUSING to apply: pass --backup-verified after running "
                  "ops/backup.ps1 and confirming the backup is good. "
                  "This migration discards the old-dimension embedding "
                  "columns as part of the ALTER (USING NULL) — there is no "
                  "in-place downgrade path.", file=sys.stderr)
            return 1

        if _daemon_reachable(args.health_url):
            print(f"\nREFUSING to apply: the daemon answers {args.health_url}. "
                  "Stop it first — a live writer re-embedding underneath the "
                  "daemon's own in-memory state corrupts the bank. Override "
                  "--health-url only for tests against a throwaway server.",
                  file=sys.stderr)
            return 1

        # Only constructed once every refusal gate above has cleared --
        # it loads the model, which is wasted work on the dry-run/refusal
        # paths (and, for tests, would otherwise force every refusal test
        # to pay the load cost too).
        pipeline = _build_pipeline()

        print(f"\nAPPLY — migrating {len(pending)} table(s): "
              f"{', '.join(pending)}\n")
        t0 = time.monotonic()
        for table in pending:
            result = migrate_table(conn, pipeline, table)
            print(f"  {table:12s}  {result['rows']:6d} rows re-embedded "
                  f"-> vector({TARGET_DIM})")
        stamp_schema_version(conn, SCHEMA_META_VERSION)
        elapsed = time.monotonic() - t0
        print(f"\nDone in {elapsed:.1f}s. meta.schema_version stamped "
              f"{SCHEMA_META_VERSION}.")
        return 0
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get(
        "PSEUDOLIFE_MCP_DATABASE_URL", _DEFAULT_DSN))
    ap.add_argument("--apply", action="store_true",
                    help="commit the migration (default: dry-run report only)")
    ap.add_argument("--backup-verified", action="store_true",
                    help="required with --apply: confirms a fresh backup exists")
    ap.add_argument("--health-url", default=_DEFAULT_HEALTH_URL,
                    help="refuse --apply while this URL answers "
                         f"(default {_DEFAULT_HEALTH_URL}; override for tests)")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
