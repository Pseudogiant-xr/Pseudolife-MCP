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
  would corrupt the bank; the daemon must be stopped first. A probe that
  TIMES OUT counts as "answers" too — a socket that accepts the connection
  but never responds is a daemon that is up, just hung, not one that is
  absent (see ``_daemon_reachable``).
* The migration connection sets ``lock_timeout`` (``_LOCK_TIMEOUT``, 10s):
  if something is still holding an open transaction against one of these
  tables despite the health check above, the ``ALTER`` fails loudly with
  ``LockNotAvailable`` instead of queuing behind it forever. Already-
  migrated tables stay committed; re-run after clearing the blocker.
* Migration order is ``facts, world_facts, lessons, entries`` — ENTRIES
  LAST, deliberately (see ``_TABLES``): the daemon's dimension guard
  checks only ``entries.embedding``, so entries must be the last column to
  change dimension for that guard to keep refusing through every partial
  state a crash could leave behind.
* Each of the four tables is migrated in its OWN transaction: drop NOT
  NULL where present (``entries`` only — the other three columns are
  already nullable), ``ALTER COLUMN embedding TYPE vector(1024) USING
  NULL`` (a straight cast from vector(384) to vector(1024) is not defined,
  so the old values are discarded as part of the type change and every row
  is re-embedded immediately after, inside the same transaction, fetched
  AFTER the ALTER so the ACCESS EXCLUSIVE lock rules out a row sneaking in
  between fetch and cast), restore NOT NULL on ``entries``. No vector index
  is created or rebuilt — there is currently none: ``ensure_schema`` drops
  ``entries_embedding_idx`` unconditionally on every boot (2026-07-02
  zombie sweep) because all similarity search happens in Python over the
  hydrated bands, not via a SQL vector query.
* ``SCHEMA_META_VERSION`` (the live constant -- this script always stamps
  whatever the current build's value is, not a value fixed at the time this
  migration was written for v25) is stamped in ``meta`` LAST, only after
  all four tables have migrated without error — a bank that failed partway
  through must not claim to be on the current schema version.

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

# The four embedding-carrying tables, in migration order. ENTRIES MUST BE
# LAST. The daemon's dimension guard
# (schema.py::_refuse_on_embedding_dim_mismatch) checks entries.embedding
# ONLY -- "one column is a sufficient sentinel... all four move in
# lockstep" is that guard's premise, and the premise only holds while
# entries is the LAST column to change dimension. Each table below is its
# own transaction (see migrate_table), so a crash partway through this
# script is the normal case to design for, not an edge case: migrating
# entries first would leave a partial bank where entries already reports
# vector(1024) while facts/world_facts/lessons are still vector(384) -- the
# guard checks entries, sees the target dimension, and PASSES, so a
# restarted daemon boots healthy and then throws on the very first cortex
# read/write against a still-384 table. With entries last, every partial
# state (crash after 0, 1, 2, or 3 tables) leaves entries un-migrated, so
# the guard keeps refusing to boot until the migration actually finishes --
# the sentinel stays armed through every partial state.
_TABLES = ("facts", "world_facts", "lessons", "entries")

# SET on the migration connection so a queued ACCESS EXCLUSIVE ALTER fails
# loudly instead of blocking the world behind a hung daemon's locks (see
# _daemon_reachable and the LockNotAvailable handling in run()). Its own
# name/constant so tests can pass a short override without waiting out the
# real one.
_LOCK_TIMEOUT = "10s"


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
    """True iff something answers ``health_url`` OR the probe times out —
    any HTTP status counts (an unhealthy-but-listening daemon is still a
    live writer), and so does a hang: a socket that accepts the TCP
    connection but never answers is a daemon that is UP, just stuck, and
    treating that as "absent" is the failure mode this function exists to
    avoid (a --apply that proceeds would then queue its ACCESS EXCLUSIVE
    ALTER behind the hung daemon's locks — see ``_LOCK_TIMEOUT`` for the
    backstop if this check is ever bypassed). Only connection refused / DNS
    failure — nothing listening at all — clears this gate.

    Checked BEFORE the ``URLError``/``OSError`` handlers below: urllib wraps
    a connect- or read-timeout in ``URLError(reason=TimeoutError(...))`` on
    every platform observed so far, but the bare ``TimeoutError`` branch is
    kept as defense in case a future stdlib/platform lets it escape
    unwrapped."""
    try:
        urllib.request.urlopen(health_url, timeout=timeout)  # noqa: S310 — fixed localhost health probe, not user-controlled
        return True
    except urllib.error.HTTPError:
        return True  # the server answered, just with a non-2xx status
    except TimeoutError:
        return True  # accepted the connection, never answered -- UP, not absent
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return True  # same case, wrapped
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
    restore NOT NULL (entries only). Creates no index.

    The row fetch happens AFTER the ALTER, inside this same transaction —
    not before it. The ALTER takes an ACCESS EXCLUSIVE lock, which blocks
    concurrent inserts into ``table`` for the rest of this transaction, so
    a fetch taken right after it is a snapshot of exactly the rows the
    ``USING NULL`` cast just nulled: nothing can have been inserted in
    between. Fetching before the transaction (the original bug) left a
    window where a row inserted after the fetch but before the ALTER got
    silently nulled by ``USING NULL`` and was never in the fetched set, so
    it was never re-embedded.
    """
    with conn.transaction():
        if table == "entries":
            conn.execute("ALTER TABLE entries ALTER COLUMN embedding DROP NOT NULL")
        conn.execute(
            f"ALTER TABLE {table} ALTER COLUMN embedding "  # noqa: S608
            f"TYPE vector({TARGET_DIM}) USING NULL"
        )
        rows = _fetch_rows(conn, table)
        texts = [_row_text(table, r) for r in rows]
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


def _apply_lock_timeout(conn, timeout: str = _LOCK_TIMEOUT) -> None:
    """SET lock_timeout on the migration connection: an ACCESS EXCLUSIVE
    ALTER queued behind another session's open transaction on the same
    table (a hung-but-listening daemon that slipped past ``_daemon_reachable``,
    or any other stray client) fails loudly with ``psycopg.errors.
    LockNotAvailable`` instead of blocking indefinitely. ``timeout`` is a
    parameter (not just the module constant inlined) so tests can pass a
    short override and prove the mechanism without waiting out the real
    default."""
    conn.execute(f"SET lock_timeout = '{timeout}'")  # noqa: S608 — fixed literal / test override, not user input


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
    _apply_lock_timeout(conn)
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

        if args.assume_daemon_stopped:
            print("\n--assume-daemon-stopped: SKIPPING the health probe on "
                  "your word. If the daemon is actually running, this "
                  "migration will corrupt its in-memory state.")
        elif _daemon_reachable(args.health_url):
            print(f"\nREFUSING to apply: the daemon answers {args.health_url} "
                  "— or the port neither answered nor refused (on some hosts "
                  "a CLOSED loopback port times out instead of refusing, "
                  "which this gate deliberately reads as 'up': fail-safe). "
                  "Stop the daemon (docker stop pseudolife-mcp-daemon), "
                  "verify with 'docker ps', and if the probe still refuses, "
                  "re-run with --assume-daemon-stopped.", file=sys.stderr)
            return 1

        # Only constructed once every refusal gate above has cleared --
        # it loads the model, which is wasted work on the dry-run/refusal
        # paths (and, for tests, would otherwise force every refusal test
        # to pay the load cost too).
        pipeline = _build_pipeline()

        print(f"\nAPPLY — migrating {len(pending)} table(s): "
              f"{', '.join(pending)}\n")
        t0 = time.monotonic()
        try:
            for table in pending:
                result = migrate_table(conn, pipeline, table)
                print(f"  {table:12s}  {result['rows']:6d} rows re-embedded "
                      f"-> vector({TARGET_DIM})")
            stamp_schema_version(conn, SCHEMA_META_VERSION)
        except psycopg.errors.LockNotAvailable:
            print(
                "\nREFUSING: timed out waiting for a lock "
                f"(lock_timeout={_LOCK_TIMEOUT}) while migrating. Something "
                "else is holding an open transaction against these tables "
                "-- stop the daemon (and any other client with an open "
                "transaction) and re-run; already-migrated tables are "
                "committed and will be skipped.", file=sys.stderr)
            return 1
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
    ap.add_argument("--assume-daemon-stopped", action="store_true",
                    help="Skip the daemon health probe. Needed on hosts where "
                         "a closed loopback port times out instead of "
                         "refusing (the gate reads a timeout as 'daemon up', "
                         "fail-safe) -- only after verifying with docker ps "
                         "that the daemon is genuinely stopped.")
    ap.add_argument("--health-url", default=_DEFAULT_HEALTH_URL,
                    help="refuse --apply while this URL answers "
                         f"(default {_DEFAULT_HEALTH_URL}; override for tests)")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
