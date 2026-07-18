"""PostgresStorage — schema v8 write-through backend (spec §4).

One synchronous connection per instance. The daemon is the single
writer and ``MemoryService``'s coarse lock already serializes calls,
so no pooling is needed. Every mutating method commits before
returning — a store that returned to the caller is durable.

Embeddings ride pgvector (numpy float32 in/out via ``register_vector``).
``tags`` / ``slots`` / ``support`` / ``provenance`` are JSONB.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Any

import numpy as np
import psycopg
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from pseudolife_memory.storage.schema import ensure_schema

logger = logging.getLogger(__name__)

_ENTRY_COLS = (
    "band", "text", "embedding", "surprise", "ts", "access_count", "source",
    "superseded_at", "superseded_by_text", "last_logical_turn",
    "episode_id", "episode_title", "tags", "slots",
)
_ENTRY_JSONB = {"tags", "slots"}

# v11 writer-aware temporal/provenance stamp — shared by every canonical store.
_STAMP_COLS = (
    "tx_time", "valid_time", "hlc_phys", "hlc_logical",
    "writer_id", "session_id", "version",
)

_FACT_COLS = (
    "entity", "attribute", "entity_norm", "attribute_norm", "value",
    "polarity", "status", "confidence", "origin", "support", "provenance",
    "asserted_at", "last_confirmed", "supersedes_value",
    "superseded_by_value", "superseded_at", "embedding",
    "entity_id", "object_entity_id",
) + _STAMP_COLS
_FACT_JSONB = {"support", "provenance"}

# World-knowledge cortex columns (schema v9). Same slot-keyed shape as facts plus
# per-fact citation + freshness; no entity_id/object_entity_id (world facts are not
# graph-linked in v1). support/provenance are JSONB like the personal cortex.
_WORLD_FACT_COLS = (
    "entity", "attribute", "entity_norm", "attribute_norm", "value",
    "polarity", "status", "confidence", "origin", "support", "provenance",
    "asserted_at", "last_confirmed", "supersedes_value",
    "superseded_by_value", "superseded_at", "embedding",
    "source_url", "source_quote", "retrieved_at", "freshness_class",
    "content_hash", "source_doc_id",
) + _STAMP_COLS

# Procedural / outcome memory columns (schema v10). Same slot-keyed shape as facts
# plus `outcome` (success|failure|correction); graph-linked like the personal cortex
# (entity_id -> task-type entity, object_entity_id -> the tool/source the lesson is
# about). support/provenance are JSONB (provenance = episode + signal ids).
_LESSON_COLS = (
    "entity", "attribute", "entity_norm", "attribute_norm", "value", "about",
    "polarity", "outcome", "status", "confidence", "origin", "support",
    "provenance", "asserted_at", "last_confirmed", "supersedes_value",
    "superseded_by_value", "superseded_at", "embedding",
    "entity_id", "object_entity_id",
) + _STAMP_COLS
_SIGNAL_COLS = (
    "task", "outcome", "about", "detail", "polarity", "origin",
    "episode_id", "created_at",
)

# Ontology-lite builtin relations (spec §5.3) — the closed vocabulary a
# weak model starts from. Referenced inverses must come first (FK).
_BUILTIN_RELATIONS = (
    # (name, description, transitive, inverse_of)
    ("depends-on", "src requires dst to function", True, None),
    ("part-of", "src is a component of dst", True, None),
    ("hosts", "src is the host/platform for dst", False, None),
    ("runs-on", "src executes on host/platform dst", False, "hosts"),
    ("uses", "src makes use of dst", False, None),
    ("configures", "src sets configuration for dst", False, None),
    ("stores-data-in", "src persists its data in dst", False, None),
    ("related-to", "untyped catch-all association", False, None),
    # Procedural / outcome memory (schema v10): a task-type entity prefers/avoids
    # the tool/source a lesson is about. Untyped like the other builtins.
    ("prefers", "src (a task-type) prefers approach/tool dst (positive lesson)",
     False, None),
    ("avoids", "src (a task-type) should avoid dead-end dst (negative lesson)",
     False, None),
)

# Mutable entry fields update_entry accepts — everything else is identity.
_ENTRY_UPDATABLE = {
    "band", "surprise", "access_count", "superseded_at",
    "superseded_by_text", "last_logical_turn", "episode_id",
    "episode_title", "tags", "slots",
}


def _embedding_in(value: Any):
    """Accept numpy / torch / list; hand pgvector a float32 numpy array."""
    if value is None:
        return None
    if hasattr(value, "detach"):  # torch.Tensor without importing torch here
        value = value.detach().cpu().numpy()
    return np.asarray(value, dtype=np.float32)


def _embedding_out(value: Any):
    """Normalize a vector column read to a float32 numpy array. pgvector
    <0.5 hands psycopg reads back as numpy arrays; 0.5+ returns ``Vector``
    objects, which ``np.asarray`` cannot coerce (TypeError)."""
    if value is None:
        return None
    if hasattr(value, "to_numpy"):  # pgvector.Vector (0.5+ psycopg reads)
        value = value.to_numpy()
    return np.asarray(value, dtype=np.float32)


class PostgresStorage:
    """Durable layer under the in-memory bands / cortex (single writer)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn = self._connect()
        self.capabilities = ensure_schema(self._conn)
        register_vector(self._conn)
        self._seed_relations()

    def _connect(self) -> psycopg.Connection:
        """Open + session-configure a connection (shared by init and the
        reconnect path).

        Autocommit (H4, 2026-07-02 review): a bare read must never leave an
        implicit transaction open — that pinned the xmin horizon (blocking
        autovacuum on the churny canonical tables) and held ACCESS SHARE
        locks that blocked any concurrent DDL. Mutations get explicit
        transaction blocks via :meth:`_txn`."""
        conn = psycopg.connect(self.dsn, connect_timeout=10, autocommit=True)
        # Never block forever on a lock — a stuck/orphaned writer should
        # raise here, not hang the whole daemon. (Session-level GUCs; they
        # apply immediately under autocommit.)
        conn.execute("SET lock_timeout = '5s'")
        # Pin the namespace to public BEFORE ensure_schema runs. The DB role is
        # `pseudolife`, which can clash with schema names, so the cluster default
        # ("$user", public) search_path could shadow the real bank. Pinning to
        # public makes SCHEMA_SQL creation + every read/write target the real tables.
        conn.execute("SET search_path TO public")
        return conn

    @property
    def conn(self) -> psycopg.Connection:
        """The live shared connection — transparently re-established when a
        Postgres restart closed/broke the previous one (2026-07-02 review
        fix: there was no reconnect anywhere, so a PG restart poisoned the
        daemon until manual restart). Heal-on-next-use: the call that hits
        the dead connection still raises; the *next* one reconnects.
        Schema is NOT re-ensured (it exists); the vector adapter is
        per-connection and must be re-registered."""
        c = self._conn
        if c.closed or c.broken:
            logger.warning("postgres connection lost (closed=%s broken=%s); "
                           "reconnecting", c.closed, c.broken)
            self._conn = self._connect()
            register_vector(self._conn)
        return self._conn

    def ping(self) -> bool:
        """Cheap liveness probe for /health on a DEDICATED short-lived
        connection, so it can't interleave with — or leave an idle
        transaction on — the shared connection another thread is using.
        Raises on an unreachable server."""
        with psycopg.connect(self.dsn, connect_timeout=2) as c:
            c.execute("SELECT 1")
        return True

    def _seed_relations(self) -> None:
        with self._txn(), self.conn.cursor() as cur:
            for name, desc, transitive, inverse in _BUILTIN_RELATIONS:
                cur.execute(
                    """
                    INSERT INTO relations
                      (name, description, transitive, inverse_of, builtin,
                       created_at)
                    VALUES (%s, %s, %s, %s, TRUE, %s)
                    ON CONFLICT (name) DO NOTHING
                    """,
                    (name, desc, transitive, inverse, time.time()),
                )

    def close(self) -> None:
        try:
            self._conn.close()  # raw: never reconnect just to close
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def _txn(self):
        """Every mutating method funnels through here. The connection runs
        autocommit (reads never leave an idle transaction — H4), so mutations
        open an explicit psycopg transaction block: commit on success,
        rollback on any exception. Without this, a single failed statement
        (lock timeout, FK violation, ...) would poison subsequent calls.
        Nested use degrades safely to savepoints (the old manual
        commit/rollback would have committed the outer work mid-way).

        Commit check (2026-07-04): psycopg's Transaction.__exit__ silently
        SKIPS the COMMIT when the connection broke during the block
        (pgconn.status != OK) — the block exits cleanly while the server
        rolls the work back. Left undetected, insert_entry hands out a
        RETURNING id for a row that never committed, and the stale db_id
        later stalls the dream on memory_traces FK violations."""
        with self.conn.transaction() as tx:
            yield
        if tx.status is not tx.Status.COMMITTED:
            raise psycopg.OperationalError(
                f"transaction did not commit (status={tx.status.name}); "
                "connection lost during the block")

    # ── entries ─────────────────────────────────────────────────────────

    def insert_entry(self, e: dict) -> int:
        values = []
        for c in _ENTRY_COLS:
            v = e.get(c)
            if c == "embedding":
                v = _embedding_in(v)
            elif c in _ENTRY_JSONB:
                v = Jsonb(v if v is not None else [])
            values.append(v)
        with self._txn():
            row = self.conn.execute(
                f"INSERT INTO entries ({', '.join(_ENTRY_COLS)}) "
                f"VALUES ({', '.join(['%s'] * len(_ENTRY_COLS))}) RETURNING id",
                values,
            ).fetchone()
        return int(row[0])

    def update_entry(self, entry_id: int, **fields) -> None:
        unknown = set(fields) - _ENTRY_UPDATABLE
        if unknown:
            raise ValueError(f"update_entry: non-updatable fields {sorted(unknown)}")
        if not fields:
            return
        sets, values = [], []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            values.append(Jsonb(v) if k in _ENTRY_JSONB else v)
        values.append(entry_id)
        with self._txn():
            self.conn.execute(
                f"UPDATE entries SET {', '.join(sets)} WHERE id = %s", values,
            )

    def delete_entry_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._txn():
            cur = self.conn.execute("DELETE FROM entries WHERE id = ANY(%s)", (ids,))
        return cur.rowcount

    def load_entries(self) -> list[dict]:
        cols = ("id",) + _ENTRY_COLS + ("reinforcements",)
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM entries ORDER BY id",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            d["embedding"] = _embedding_out(d["embedding"])
            out.append(d)
        return out

    # ── episodes ────────────────────────────────────────────────────────

    def upsert_episode(self, ep: dict) -> None:
        with self._txn():
            self.conn.execute(
                """
                INSERT INTO episodes (id, title, hint, started_at, ended_at,
                                      closed_by_new_start, session_key, parent_id)
                VALUES (%(id)s, %(title)s, %(hint)s, %(started_at)s,
                        %(ended_at)s, %(closed_by_new_start)s, %(session_key)s,
                        %(parent_id)s)
                ON CONFLICT (id) DO UPDATE SET
                  title = EXCLUDED.title,
                  hint = EXCLUDED.hint,
                  started_at = EXCLUDED.started_at,
                  ended_at = EXCLUDED.ended_at,
                  closed_by_new_start = EXCLUDED.closed_by_new_start,
                  session_key = EXCLUDED.session_key,
                  parent_id = EXCLUDED.parent_id
                """,
                ep,
            )

    def load_episodes(self) -> list[dict]:
        cols = ("id", "title", "hint", "started_at", "ended_at",
                "closed_by_new_start", "session_key", "parent_id")
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM episodes ORDER BY started_at",
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def delete_episode(self, episode_id: str) -> None:
        with self._txn():
            self.conn.execute("DELETE FROM episodes WHERE id = %s", (episode_id,))

    def retarget_episode_refs(
        self, old_ids: list[str], new_id: str, new_title: str,
    ) -> int:
        """Re-point every row stamped with one of ``old_ids`` (entries +
        outcome signals) at ``new_id`` — the episode-merge bulk pass. Returns
        the number of entry rows moved."""
        if not old_ids:
            return 0
        with self._txn():
            cur = self.conn.execute(
                "UPDATE entries SET episode_id = %s, episode_title = %s "
                "WHERE episode_id = ANY(%s)",
                (new_id, new_title, list(old_ids)),
            )
            self.conn.execute(
                "UPDATE outcome_signals SET episode_id = %s "
                "WHERE episode_id = ANY(%s)",
                (new_id, list(old_ids)),
            )
        return cur.rowcount or 0

    # ── cortex facts ────────────────────────────────────────────────────

    def upsert_fact(self, f: dict) -> int:
        values = []
        for c in _FACT_COLS:
            v = f.get(c)
            if c == "embedding":
                v = _embedding_in(v)
            elif c in _FACT_JSONB:
                v = Jsonb(v if v is not None else [])
            elif c == "version" and v is None:
                v = 1            # NOT NULL DEFAULT 1; never insert explicit NULL
            values.append(v)
        if f.get("id") is not None:
            sets = ", ".join(f"{c} = %s" for c in _FACT_COLS)
            with self._txn():
                self.conn.execute(
                    f"UPDATE facts SET {sets} WHERE id = %s", values + [f["id"]],
                )
            return int(f["id"])
        with self._txn():
            row = self.conn.execute(
                f"INSERT INTO facts ({', '.join(_FACT_COLS)}) "
                f"VALUES ({', '.join(['%s'] * len(_FACT_COLS))}) RETURNING id",
                values,
            ).fetchone()
        return int(row[0])

    def _insert_canonical_rows(
        self, cur, table: str, cols: tuple[str, ...], rows: list[dict],
    ) -> None:
        """Shared row-insert loop for the three canonical stores
        (facts / world_facts / lessons)."""
        for f in rows:
            values = []
            for c in cols:
                v = f.get(c)
                if c == "embedding":
                    v = _embedding_in(v)
                elif c in _FACT_JSONB:
                    v = Jsonb(v if v is not None else [])
                elif c == "version" and v is None:
                    v = 1            # NOT NULL DEFAULT 1; never insert explicit NULL
                values.append(v)
            cur.execute(
                f"INSERT INTO {table} ({', '.join(cols)}) "
                f"VALUES ({', '.join(['%s'] * len(cols))})",
                values,
            )

    def _replace_slot_rows(
        self, table: str, cols: tuple[str, ...],
        slots: set[tuple[str, str]] | list[tuple[str, str]], rows: list[dict],
    ) -> None:
        """Per-slot rewrite (2026-07-02 P1): delete + reinsert ONLY the given
        ``(entity_norm, attribute_norm)`` slots, one transaction. The
        full-table snapshot rewrite was O(total rows) per write — quadratic
        over a dream sweep — and reassigned every row id, which also blocked
        the dormant OCC seam."""
        slot_list = sorted(set(slots))
        if not slot_list:
            return
        placeholders = ", ".join(["(%s, %s)"] * len(slot_list))
        params = [x for s in slot_list for x in s]
        with self._txn(), self.conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {table} "
                f"WHERE (entity_norm, attribute_norm) IN ({placeholders})",
                params,
            )
            self._insert_canonical_rows(cur, table, cols, rows)

    def replace_slot_facts(self, slots, rows: list[dict]) -> None:
        self._replace_slot_rows("facts", _FACT_COLS, slots, rows)

    def replace_slot_world_facts(self, slots, rows: list[dict]) -> None:
        self._replace_slot_rows("world_facts", _WORLD_FACT_COLS, slots, rows)

    def replace_slot_lessons(self, slots, rows: list[dict]) -> None:
        self._replace_slot_rows("lessons", _LESSON_COLS, slots, rows)

    def replace_facts(self, rows: list[dict]) -> None:
        """Snapshot-style cortex persistence: one transaction, full rewrite.

        Retained for restore/migration and the explicit-save resync path;
        the per-write path is :meth:`replace_slot_facts` (2026-07-02 P1).
        """
        with self._txn(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM facts")
            self._insert_canonical_rows(cur, "facts", _FACT_COLS, rows)

    def replace_facts_occ(self, rows: list[dict]) -> None:
        """Optimistic-concurrency cortex persistence (Phase-2 seam, dormant).

        The future multi-process writer topology replaces the snapshot rewrite
        with per-row compare-and-swap on ``version`` (write only if the stored
        version matches the one we read; bump on success; surface a conflict
        otherwise). The schema already carries ``version`` for this, but the
        path itself — CAS, conflict resolution, cache invalidation — is a
        separate plan. ``StorageConfig.write_mode='snapshot'`` is the only live
        path in v0.4; selecting ``occ`` lands here.
        """
        raise NotImplementedError(
            "write_mode=occ (per-row compare-and-swap) is Phase 2 — "
            "the live path is write_mode=snapshot (replace_facts)."
        )

    def update_access_counts(self, pairs: list[tuple[int, int]]) -> None:
        """Bulk-sync (entry_id, access_count) — called on the save cadence,
        not per retrieval, to keep reads cheap."""
        if not pairs:
            return
        with self._txn(), self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE entries SET access_count = %s "
                "WHERE id = %s AND access_count <> %s",
                [(c, i, c) for (i, c) in pairs],
            )

    def delete_fact_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._txn():
            cur = self.conn.execute("DELETE FROM facts WHERE id = ANY(%s)", (ids,))
        return cur.rowcount

    def load_facts(self) -> list[dict]:
        cols = ("id",) + _FACT_COLS
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM facts ORDER BY id",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            d["embedding"] = _embedding_out(d["embedding"])
            out.append(d)
        return out

    # ── world-knowledge cortex (schema v9; same snapshot pattern as facts) ──

    def replace_world_facts(self, rows: list[dict]) -> None:
        """Snapshot-style world cortex persistence: full rewrite. Retained for
        the explicit-save resync; the per-write path is
        replace_slot_world_facts."""
        with self._txn(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM world_facts")
            self._insert_canonical_rows(cur, "world_facts", _WORLD_FACT_COLS, rows)

    def load_world_facts(self) -> list[dict]:
        cols = ("id",) + _WORLD_FACT_COLS
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM world_facts ORDER BY id",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            d["embedding"] = _embedding_out(d["embedding"])
            out.append(d)
        return out

    # ── procedural / outcome memory (schema v10; same snapshot pattern) ─────

    def replace_lessons(self, rows: list[dict]) -> None:
        """Snapshot-style lesson persistence: full rewrite. Retained for the
        explicit-save resync; the per-write path is replace_slot_lessons."""
        with self._txn(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM lessons")
            self._insert_canonical_rows(cur, "lessons", _LESSON_COLS, rows)

    def load_lessons(self) -> list[dict]:
        cols = ("id",) + _LESSON_COLS
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM lessons ORDER BY id",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            d["embedding"] = _embedding_out(d["embedding"])
            out.append(d)
        return out

    # ── outcome signals (append-only log the dream drains into lessons) ─────

    def add_signal(self, task: str, outcome: str, about: str | None = None,
                   detail: str | None = None, polarity: str | None = None,
                   origin: str | None = None, episode_id: str | None = None,
                   now: float | None = None) -> int:
        t = time.time() if now is None else float(now)
        with self._txn():
            row = self.conn.execute(
                f"INSERT INTO outcome_signals ({', '.join(_SIGNAL_COLS)}) "
                f"VALUES ({', '.join(['%s'] * len(_SIGNAL_COLS))}) RETURNING id",
                (task, outcome, about, detail, polarity, origin, episode_id, t),
            ).fetchone()
        return int(row[0])

    def count_signals_for_episodes(self, episode_ids: list[str]) -> int:
        """Total outcome signals (consumed or not) across ``episode_ids`` —
        the auto-inference candidate scan's "already has a signal" check."""
        if not episode_ids:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM outcome_signals WHERE episode_id = ANY(%s)",
            (episode_ids,),
        ).fetchone()
        return int(row[0])

    def pending_signals(self, limit: int | None = None) -> list[dict]:
        cols = ("id",) + _SIGNAL_COLS
        sql = (
            f"SELECT {', '.join(cols)} FROM outcome_signals "
            "WHERE consumed_at IS NULL ORDER BY created_at, id"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self.conn.execute(sql).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def consume_signals(self, ids: list[int], now: float | None = None) -> int:
        """Mark signals consumed (the dream's drain cursor). Idempotent: an
        already-consumed signal is skipped."""
        if not ids:
            return 0
        t = time.time() if now is None else float(now)
        with self._txn():
            cur = self.conn.execute(
                "UPDATE outcome_signals SET consumed_at = %s "
                "WHERE id = ANY(%s) AND consumed_at IS NULL",
                (t, list(ids)),
            )
        return cur.rowcount

    def prune_signals(self, older_than_ts: float) -> int:
        """Delete signals (consumed or not) older than the cutoff, so the log
        can't grow unbounded when no extractor is configured to drain it."""
        with self._txn():
            cur = self.conn.execute(
                "DELETE FROM outcome_signals WHERE created_at < %s",
                (float(older_than_ts),),
            )
        return cur.rowcount

    def loop_health(self, window_s: float, now: float | None = None) -> dict:
        """Windowed loop-activity counts for the Console tile: current vs the
        immediately preceding window of stores + outcome signals, session
        episodes (parent_id IS NULL), pending signals, lesson recency.
        Read-only, all on indexed timestamp columns. Consumed signals still
        count as outcomes — consumption is the dream's drain cursor, not a
        judgement; the caveat is upstream retention (signal_retention_days)
        deleting rows older than its cutoff."""
        t = time.time() if now is None else float(now)
        cutoff, prev_cutoff = t - window_s, t - 2 * window_s

        def _window_counts(table: str, ts_col: str) -> dict:
            row = self.conn.execute(
                f"SELECT COUNT(*) FILTER (WHERE {ts_col} >= %s), "
                f"COUNT(*) FILTER (WHERE {ts_col} >= %s AND {ts_col} < %s) "
                f"FROM {table}", (cutoff, prev_cutoff, cutoff)).fetchone()
            return {"current": row[0], "previous": row[1]}

        stores = _window_counts("entries", "ts")
        outcomes = _window_counts("outcome_signals", "created_at")
        outcomes["by_outcome"] = {
            o: n for o, n in self.conn.execute(
                "SELECT outcome, COUNT(*) FROM outcome_signals "
                "WHERE created_at >= %s GROUP BY outcome", (cutoff,))}
        sessions = self.conn.execute(
            "SELECT COUNT(*) FROM episodes "
            "WHERE started_at >= %s AND parent_id IS NULL",
            (cutoff,)).fetchone()[0]
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM outcome_signals WHERE consumed_at IS NULL"
        ).fetchone()[0]
        last_lesson, lessons_current = self.conn.execute(
            "SELECT MAX(asserted_at), COUNT(*) FILTER (WHERE status = 'current') "
            "FROM lessons").fetchone()
        return {"stores": stores, "outcomes": outcomes, "sessions": sessions,
                "pending_signals": pending, "last_lesson_at": last_lesson,
                "lessons_current": lessons_current}

    # ── meta ────────────────────────────────────────────────────────────

    def meta_set(self, key: str, value: Any) -> None:
        with self._txn():
            self.conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, Jsonb(value)),
            )

    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = %s", (key,),
        ).fetchone()
        return default if row is None else row[0]

    def get_meta(self, key: str):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value) -> None:
        with self._txn():
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, json.dumps(value)),
            )

    # ── graph: entities / aliases ───────────────────────────────────────

    def ensure_entity(
        self, canonical: str, display: str | None = None,
        etype: str | None = None,
    ) -> int:
        """Upsert by canonical name; first non-null etype wins (soft typing
        is advisory, so a later conflicting hint must not silently retype)."""
        with self._txn():
            row = self.conn.execute(
                """
                INSERT INTO entities (canonical, display, etype, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (canonical) DO UPDATE
                  SET etype = COALESCE(entities.etype, EXCLUDED.etype)
                RETURNING id
                """,
                (canonical, display or canonical, etype, time.time()),
            ).fetchone()
        return int(row[0])

    def find_entity(self, name_norm: str) -> dict | None:
        """Resolve a normalized name via canonical first, then aliases."""
        cols = ("id", "canonical", "display", "etype", "created_at")
        row = self.conn.execute(
            "SELECT id, canonical, display, etype, created_at FROM entities "
            "WHERE canonical = %s",
            (name_norm,),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT e.id, e.canonical, e.display, e.etype, e.created_at
                FROM entity_aliases a JOIN entities e ON e.id = a.entity_id
                WHERE a.alias = %s
                """,
                (name_norm,),
            ).fetchone()
        if row is None:
            return None
        d = dict(zip(cols, row))
        d["aliases"] = [
            r[0] for r in self.conn.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id = %s "
                "ORDER BY alias",
                (d["id"],),
            ).fetchall()
        ]
        return d

    def find_fact_slot_entity(self, key_norm: str) -> str | None:
        """Display entity of a CURRENT fact whose slot key — entity_norm and
        attribute_norm hyphen-joined, matching graph.norm_name's separator
        folding — equals ``key_norm``. Small table + create-miss-only calls,
        so the unindexed concat scan is fine."""
        row = self.conn.execute(
            "SELECT entity FROM facts WHERE status = 'current' "
            "AND entity_norm || '-' || attribute_norm = %s LIMIT 1",
            (key_norm,)).fetchone()
        return row[0] if row else None

    def add_alias(self, alias_norm: str, entity_id: int) -> None:
        with self._txn():
            self.conn.execute(
                """
                INSERT INTO entity_aliases (alias, entity_id) VALUES (%s, %s)
                ON CONFLICT (alias) DO UPDATE SET entity_id = EXCLUDED.entity_id
                """,
                (alias_norm, entity_id),
            )

    def delete_entity(self, entity_id: int) -> bool:
        """Remove a graph entity. edges/aliases/sources/community are ON DELETE
        CASCADE; facts/lessons FK have NO cascade, so null those refs first
        (the fact/lesson rows survive, just unlinked from the deleted node)."""
        with self._txn():
            for tbl in ("facts", "lessons"):
                self.conn.execute(f"UPDATE {tbl} SET entity_id = NULL WHERE entity_id = %s", (entity_id,))
                self.conn.execute(f"UPDATE {tbl} SET object_entity_id = NULL WHERE object_entity_id = %s", (entity_id,))
            row = self.conn.execute(
                "DELETE FROM entities WHERE id = %s RETURNING id", (entity_id,)).fetchone()
        return row is not None

    def merge_entity(self, from_id: int, into_id: int) -> bool:
        """Fold `from` into `into`: drop edges that would duplicate or self-loop,
        re-point the rest, re-point fact/lesson refs, carry aliases + sources,
        then delete `from` (CASCADE clears its leftovers). edges UNIQUE
        (src,rel,dst) forces the dedup-before-repoint order."""
        if from_id == into_id:
            return False
        with self._txn():
            c = self.conn
            # 0. Both endpoints must still exist. A chained multi-way merge
            #    (A->B, C->B, C->A applied in order) can present a `from_id`
            #    already deleted by an earlier merge in the same batch; a queued
            #    merge proposal whose `into` entity was junk-deleted before the
            #    merge is accepted presents a stale `into_id`. Either way, treat
            #    it as a no-op (return False) rather than proceeding — a stale
            #    `into_id` would otherwise re-point edges to a nonexistent row
            #    and raise an FK violation (→ rollback + 500). Returning False
            #    lets callers report "target no longer exists" gracefully and
            #    keeps merge counts honest.
            rows = c.execute(
                "SELECT id FROM entities WHERE id IN (%s, %s)",
                (from_id, into_id)).fetchall()
            if {r[0] for r in rows} != {from_id, into_id}:
                return False
            # 1a. drop from-edges that already exist on `into` (src side / dst side)
            c.execute("DELETE FROM edges f WHERE f.src_id = %s AND EXISTS ("
                      "SELECT 1 FROM edges t WHERE t.src_id = %s AND t.relation = f.relation "
                      "AND t.dst_id = f.dst_id)", (from_id, into_id))
            c.execute("DELETE FROM edges f WHERE f.dst_id = %s AND EXISTS ("
                      "SELECT 1 FROM edges t WHERE t.dst_id = %s AND t.relation = f.relation "
                      "AND t.src_id = f.src_id)", (from_id, into_id))
            # 1b. drop edges that would become self-loops (from<->into) plus the
            #     pure from-self-loop (from, from) that re-point would turn into
            #     (into, into), violating UNIQUE if that edge already exists.
            c.execute("DELETE FROM edges WHERE (src_id = %s AND dst_id = %s) "
                      "OR (src_id = %s AND dst_id = %s)",
                      (from_id, into_id, into_id, from_id))
            c.execute("DELETE FROM edges WHERE src_id = %s AND dst_id = %s",
                      (from_id, from_id))
            # 1c. re-point
            c.execute("UPDATE edges SET src_id = %s WHERE src_id = %s", (into_id, from_id))
            c.execute("UPDATE edges SET dst_id = %s WHERE dst_id = %s", (into_id, from_id))
            # 2. fact/lesson refs
            for tbl in ("facts", "lessons"):
                c.execute(f"UPDATE {tbl} SET entity_id = %s WHERE entity_id = %s", (into_id, from_id))
                c.execute(f"UPDATE {tbl} SET object_entity_id = %s WHERE object_entity_id = %s", (into_id, from_id))
            # 3. aliases: from's canonical + its aliases become into's aliases
            frm = c.execute("SELECT canonical FROM entities WHERE id = %s", (from_id,)).fetchone()
            if frm:
                c.execute("INSERT INTO entity_aliases (alias, entity_id) VALUES (%s, %s) "
                          "ON CONFLICT (alias) DO NOTHING", (frm[0], into_id))
            c.execute("UPDATE entity_aliases SET entity_id = %s WHERE entity_id = %s "
                      "AND alias NOT IN (SELECT alias FROM entity_aliases WHERE entity_id = %s)",
                      (into_id, from_id, into_id))
            # 4. sources: carry from's sources onto into (keep existing)
            c.execute("INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                      "SELECT %s, source, count, origin, updated_at FROM entity_sources WHERE entity_id = %s "
                      "ON CONFLICT (entity_id, source) DO NOTHING", (into_id, from_id))
            # 5. delete `from` (CASCADE removes its leftover aliases/sources/community/edges)
            c.execute("DELETE FROM entities WHERE id = %s", (from_id,))
        return True

    def entity_id_map(self) -> dict[str, int]:
        """Every normalized name (canonical + alias) → entity id. Used to
        link fact rows on cortex snapshot; canonical wins on collision."""
        m: dict[str, int] = {}
        for alias, eid in self.conn.execute(
            "SELECT alias, entity_id FROM entity_aliases",
        ).fetchall():
            m[alias] = eid
        for canonical, eid in self.conn.execute(
            "SELECT canonical, id FROM entities",
        ).fetchall():
            m[canonical] = eid
        return m

    # ── graph: relations registry ───────────────────────────────────────

    def load_relations(self) -> list[dict]:
        cols = ("name", "description", "src_type", "dst_type", "transitive",
                "inverse_of", "builtin")
        return [
            dict(zip(cols, r)) for r in self.conn.execute(
                f"SELECT {', '.join(cols)} FROM relations ORDER BY name",
            ).fetchall()
        ]

    def upsert_relation(
        self, name: str, description: str, *,
        src_type: str | None = None, dst_type: str | None = None,
        transitive: bool = False, inverse_of: str | None = None,
    ) -> None:
        with self._txn():
            self.conn.execute(
                """
                INSERT INTO relations
                  (name, description, src_type, dst_type, transitive,
                   inverse_of, builtin, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE, %s)
                ON CONFLICT (name) DO UPDATE SET
                  description = EXCLUDED.description,
                  src_type = EXCLUDED.src_type,
                  dst_type = EXCLUDED.dst_type,
                  transitive = EXCLUDED.transitive,
                  inverse_of = EXCLUDED.inverse_of
                """,
                (name, description, src_type, dst_type, transitive,
                 inverse_of, time.time()),
            )

    # ── graph: edges ────────────────────────────────────────────────────

    def upsert_edge(
        self, src_id: int, relation: str, dst_id: int, *,
        confidence: float = 0.8, origin: str | None = None,
        revive: bool = True,
    ) -> dict:
        """Insert or re-assert. Re-assertion bumps confidence (+0.05,
        capped 0.99) and keeps the stronger origin claim if the new call
        omitted one. ``revive=True`` (explicit/human assertion) clears a
        prior supersession; ``revive=False`` (agent re-extraction, e.g.
        the dream) leaves a superseded edge superseded — a human removal
        must be sticky against the extractor re-planting the same triple."""
        with self._txn():
            row = self.conn.execute(
                """
                INSERT INTO edges
                  (src_id, relation, dst_id, confidence, origin, asserted_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (src_id, relation, dst_id) DO UPDATE SET
                  confidence = LEAST(
                    0.99, GREATEST(EXCLUDED.confidence, edges.confidence + 0.05)),
                  origin = COALESCE(EXCLUDED.origin, edges.origin),
                  superseded_at = CASE WHEN %s THEN NULL
                                       ELSE edges.superseded_at END,
                  asserted_at = EXCLUDED.asserted_at
                RETURNING id, confidence
                """,
                (src_id, relation, dst_id, confidence, origin, time.time(),
                 bool(revive)),
            ).fetchone()
        return {"id": int(row[0]), "confidence": float(row[1])}

    def bless_edge(self, src_id: int, relation: str, dst_id: int, *,
                   confidence: float = 0.8) -> bool:
        """Human 'Keep' on a review-queue edge: raise a LIVE edge to at least
        ``confidence`` and mark it origin='user' so the dubious detector stops
        flagging it. Never creates a missing edge and never revives a
        superseded one — Keep confirms, it doesn't assert."""
        with self._txn():
            cur = self.conn.execute(
                """
                UPDATE edges SET
                  confidence = GREATEST(confidence, %s),
                  origin = 'user',
                  asserted_at = %s
                WHERE src_id = %s AND relation = %s AND dst_id = %s
                  AND superseded_at IS NULL
                """,
                (confidence, time.time(), src_id, relation, dst_id),
            )
        return cur.rowcount > 0

    def supersede_edge(self, src_id: int, relation: str, dst_id: int) -> bool:
        with self._txn():
            cur = self.conn.execute(
                """
                UPDATE edges SET superseded_at = %s
                WHERE src_id = %s AND relation = %s AND dst_id = %s
                  AND superseded_at IS NULL
                """,
                (time.time(), src_id, relation, dst_id),
            )
        return cur.rowcount > 0

    def has_trace(self, entity_norm: str, attribute_norm: str,
                  entry_id: int) -> bool:
        """True iff this source entry already formed this slot once — lets a
        dream batch retry skip re-confirming (and re-ratcheting) the prefix
        it already consolidated."""
        row = self.conn.execute(
            "SELECT 1 FROM memory_traces WHERE entity_norm=%s "
            "AND attribute_norm=%s AND entry_id=%s",
            (entity_norm, attribute_norm, entry_id),
        ).fetchone()
        return row is not None

    def add_trace(self, entity_norm: str, attribute_norm: str,
                  entry_id: int, now: float) -> bool:
        """Link a cortex slot to a source episode. Idempotent on the PK; returns
        True iff a NEW row was inserted (so the caller bumps reinforcements only on
        genuine new formation, never on a re-assert)."""
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO memory_traces (entity_norm, attribute_norm, entry_id, created_at) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (entity_norm, attribute_norm, entry_id) DO NOTHING "
                "RETURNING entry_id",
                (entity_norm, attribute_norm, entry_id, now),
            ).fetchone()
        return row is not None

    def set_edge_confidence(self, edge_id: int, confidence: float) -> None:
        with self._txn():
            self.conn.execute("UPDATE edges SET confidence = %s WHERE id = %s",
                              (float(confidence), edge_id))

    def traces_by_entity_norm(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for ent_norm, entry_id in self.conn.execute(
            "SELECT entity_norm, entry_id FROM memory_traces ORDER BY entity_norm, entry_id"
        ).fetchall():
            out.setdefault(ent_norm, []).append(entry_id)
        return out

    def insert_proposal(self, src_id: int, relation: str, dst_id: int,
                        confidence: float, similarity: float | None,
                        rationale: str | None, source: str, now: float) -> int | None:
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO edge_proposals "
                "(src_id, relation, dst_id, confidence, similarity, rationale, source, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (src_id, relation, dst_id) DO NOTHING RETURNING id",
                (src_id, relation, dst_id, float(confidence),
                 similarity, rationale, source, now),
            ).fetchone()
        return int(row[0]) if row else None

    def dismiss_pair(self, a_norm: str, b_norm: str) -> bool:
        """Persist a human 'these are NOT duplicates' verdict. Stored ordered
        (a < b) so either argument order lands on the same row. Returns True
        iff a new dismissal was recorded."""
        a, b = sorted((a_norm, b_norm))
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO dismissed_pairs (a_norm, b_norm, dismissed_at) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING a_norm",
                (a, b, time.time()),
            ).fetchone()
        return row is not None

    def dismissed_pairs(self) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self.conn.execute(
            "SELECT a_norm, b_norm FROM dismissed_pairs").fetchall()}

    def pending_proposals(self) -> list[dict]:
        cols = ("id", "src_id", "relation", "dst_id", "confidence", "similarity",
                "rationale", "source", "created_at", "status")
        rows = self.conn.execute(
            "SELECT p.id, p.src_id, p.relation, p.dst_id, p.confidence, p.similarity, "
            "       p.rationale, p.source, p.created_at, p.status, s.display, d.display "
            "FROM edge_proposals p "
            "JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id "
            "WHERE p.status = 'pending' ORDER BY p.confidence DESC, p.id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r[:10]))
            d["src"], d["dst"] = r[10], r[11]
            out.append(d)
        return out

    def get_proposal(self, proposal_id: int) -> dict | None:
        cols = ("id", "src_id", "relation", "dst_id", "confidence", "similarity",
                "rationale", "source", "created_at", "status")
        r = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM edge_proposals WHERE id = %s", (proposal_id,)
        ).fetchone()
        return dict(zip(cols, r)) if r else None

    def set_proposal_status(self, proposal_id: int, status: str) -> bool:
        with self._txn():
            cur = self.conn.execute(
                "UPDATE edge_proposals SET status = %s WHERE id = %s", (status, proposal_id))
        return cur.rowcount > 0

    def insert_entity_proposal(self, kind: str, entity_id: int, into_id: int | None,
                               score: float | None, reason: str | None, now: float) -> int | None:
        try:
            with self._txn():
                row = self.conn.execute(
                    "INSERT INTO entity_proposals (kind, entity_id, into_id, score, reason, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
                    (kind, entity_id, into_id, score, reason, now),
                ).fetchone()
            return int(row[0]) if row else None
        except psycopg.errors.ForeignKeyViolation:
            # endpoint was deleted (e.g. auto-merged) this pass — skip the
            # proposal; _txn already rolled back.
            return None

    def entity_proposal_keys(self) -> set[tuple]:
        """Keys of ALL entity_proposals rows (any status), shaped like the
        dedupe unique indexes: ``("junk", entity_id)`` and ``("merge",
        least_id, greatest_id)`` — so a dry-run can tell which previews the
        apply path will silently skip."""
        rows = self.conn.execute(
            "SELECT kind, entity_id, into_id FROM entity_proposals").fetchall()
        out: set[tuple] = set()
        for kind, eid, into in rows:
            if kind == "merge" and into is not None:
                out.add(("merge", min(eid, into), max(eid, into)))
            else:
                out.add((kind, eid))
        return out

    def dump_graph_tables(self) -> dict[str, list[dict]]:
        """Plain-dict dump of the five graph tables the deep dream mutates —
        the pre-apply snapshot payload."""
        out: dict[str, list[dict]] = {}
        for t in ("entities", "edges", "entity_aliases",
                  "edge_proposals", "entity_proposals"):
            cur = self.conn.execute(f"SELECT * FROM {t} ORDER BY 1")  # noqa: S608 — fixed table list
            cols = [c.name for c in cur.description]
            out[t] = [dict(zip(cols, r)) for r in cur.fetchall()]
        return out

    def pending_entity_proposals(self) -> list[dict]:
        cols = ("id", "kind", "entity_id", "into_id", "score", "reason", "status", "created_at")
        rows = self.conn.execute(
            "SELECT p.id, p.kind, p.entity_id, p.into_id, p.score, p.reason, p.status, "
            "       p.created_at, e.display, i.display "
            "FROM entity_proposals p "
            "JOIN entities e ON e.id = p.entity_id "
            "LEFT JOIN entities i ON i.id = p.into_id "
            "WHERE p.status = 'pending' ORDER BY p.kind, p.score DESC NULLS LAST, p.id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r[:8]))
            d["entity"], d["into"] = r[8], r[9]
            out.append(d)
        return out

    def get_entity_proposal(self, proposal_id: int) -> dict | None:
        cols = ("id", "kind", "entity_id", "into_id", "score", "reason", "status", "created_at")
        r = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM entity_proposals WHERE id = %s", (proposal_id,)
        ).fetchone()
        return dict(zip(cols, r)) if r else None

    def set_entity_proposal_status(self, proposal_id: int, status: str, *,
                                   decided_by: str | None = None,
                                   decided_at: float | None = None) -> bool:
        with self._txn():
            cur = self.conn.execute(
                "UPDATE entity_proposals SET status = %s, "
                "decided_by = COALESCE(%s, decided_by), "
                "decided_at = COALESCE(%s, decided_at) WHERE id = %s",
                (status, decided_by, decided_at, proposal_id))
        return cur.rowcount > 0

    def record_merge_decision(self, proposal_id: int | None, entity_display: str,
                              into_display: str | None, status: str,
                              score: float | None, reason: str | None,
                              decided_by: str, decided_at: float) -> int:
        """Durable audit row for a merge decision. Denormalized on purpose:
        an accepted merge deletes the folded entity (and its proposal row via
        CASCADE), so the audit must not reference either."""
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO merge_decisions "
                "(proposal_id, entity_display, into_display, status, score, "
                " reason, decided_by, decided_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (proposal_id, entity_display, into_display, status, score,
                 reason, decided_by, decided_at)).fetchone()
        return int(row[0])

    def recent_entity_decisions(self, limit: int = 20) -> list[dict]:
        """Merge decisions newest-first — the audit trail behind Atlas
        'recent merge decisions'. Reads merge_decisions (durable), not
        entity_proposals (accepted rows CASCADE away with the merge)."""
        cols = ("id", "proposal_id", "entity", "into", "status", "score",
                "reason", "decided_by", "decided_at")
        rows = self.conn.execute(
            "SELECT id, proposal_id, entity_display, into_display, status, "
            "       score, reason, decided_by, decided_at "
            "FROM merge_decisions "
            "ORDER BY decided_at DESC, id DESC LIMIT %s",
            (int(limit),)).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def traces_for_slot(self, entity_norm: str, attribute_norm: str) -> list[int]:
        return [r[0] for r in self.conn.execute(
            "SELECT entry_id FROM memory_traces "
            "WHERE entity_norm = %s AND attribute_norm = %s ORDER BY entry_id",
            (entity_norm, attribute_norm)).fetchall()]

    def upsert_entity_source(self, entity_id: int, source: str,
                             origin: str, now: float) -> None:
        """Attribute an entity to a project/source. A 'derived' upsert never
        downgrades an existing 'manual' row; it bumps count + updated_at. A
        'manual' upsert always wins."""
        with self._txn():
            self.conn.execute(
                "INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                "VALUES (%s, %s, 1, %s, %s) "
                "ON CONFLICT (entity_id, source) DO UPDATE SET "
                "  count = entity_sources.count + 1, "
                "  updated_at = EXCLUDED.updated_at, "
                "  origin = CASE WHEN entity_sources.origin = 'manual' "
                "                THEN 'manual' ELSE EXCLUDED.origin END",
                (entity_id, source, origin, now))

    def sources_for_entity(self, entity_id: int) -> list[dict]:
        cols = ("source", "count", "origin")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            "SELECT source, count, origin FROM entity_sources "
            "WHERE entity_id = %s ORDER BY count DESC, source", (entity_id,)).fetchall()]

    def entries_for_entity(self, entity_id: int, *, limit: int = 20) -> list[dict]:
        """The MIRAS source entries behind an entity, newest-first. Bridges
        facts.entity_id (graph FK) -> facts.entity_norm (cortex norm) -> the
        memory_traces engram cross-index -> entries. Keying through facts avoids
        the graph/cortex norm mismatch (mirrors backfill_entity_sources). A
        graph-only node with no current fact returns []."""
        cols = ("id", "band", "source", "ts", "text", "episode_title")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            "SELECT DISTINCT en.id, en.band, en.source, en.ts, en.text, "
            "en.episode_title "
            "FROM facts f "
            "JOIN memory_traces t ON t.entity_norm = f.entity_norm "
            "JOIN entries en ON en.id = t.entry_id "
            "WHERE f.entity_id = %s AND f.status = 'current' "
            "ORDER BY en.ts DESC LIMIT %s",
            (entity_id, int(limit))).fetchall()]

    def entity_sources_map(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for eid, source in self.conn.execute(
            "SELECT entity_id, source FROM entity_sources ORDER BY entity_id, source"
        ).fetchall():
            out.setdefault(eid, []).append(source)
        return out

    def backfill_entity_sources(self, now: float, *,
                                rollup: dict[str, str] | None = None,
                                exclude: frozenset[str] | None = None) -> int:
        """Derive entity->source attribution from the fact-provenance link:
        facts.entity_id is the authoritative FK to entities; facts.entity_norm
        shares the cortex normalization with memory_traces.entity_norm; entries
        carry the source. Keying by entity_id avoids the graph/cortex norm
        mismatch. Writes/refreshes origin='derived'; never overwrites 'manual'.
        Idempotent: count is recomputed from DISTINCT entries.

        Scope keys are case-folded ('Pseudolife' and 'pseudolife' are one
        scope). Sources in ``exclude`` (case-insensitive) never become
        projects — meta tags like status/claude leak in otherwise. A source in
        ``rollup`` ALSO writes its umbrella scope (both rows kept, so the
        family view and the fine-grained filter coexist)."""
        rows = self.conn.execute(
            "SELECT m.entity_id, en.source, COUNT(DISTINCT t.entry_id) AS cnt "
            "FROM (SELECT DISTINCT entity_id, entity_norm FROM facts "
            "      WHERE entity_id IS NOT NULL AND status = 'current') m "
            "JOIN memory_traces t ON t.entity_norm = m.entity_norm "
            "JOIN entries en ON en.id = t.entry_id "
            "WHERE en.source <> '' "
            "GROUP BY m.entity_id, en.source"
        ).fetchall()
        excl = {str(s).strip().lower() for s in (exclude or ())}
        roll = {str(k).strip().lower(): str(v).strip().lower()
                for k, v in (rollup or {}).items()}
        agg: dict[tuple[int, str], int] = {}
        for entity_id, source, cnt in rows:
            key = str(source).strip().lower()
            if not key or key in excl:
                continue
            agg[(entity_id, key)] = agg.get((entity_id, key), 0) + int(cnt)
            umb = roll.get(key)
            if umb and umb != key and umb not in excl:
                agg[(entity_id, umb)] = agg.get((entity_id, umb), 0) + int(cnt)
        n = 0
        with self._txn():
            for (entity_id, source), cnt in agg.items():
                self.conn.execute(
                    "INSERT INTO entity_sources (entity_id, source, count, origin, updated_at) "
                    "VALUES (%s, %s, %s, 'derived', %s) "
                    "ON CONFLICT (entity_id, source) DO UPDATE SET "
                    "  count = EXCLUDED.count, updated_at = EXCLUDED.updated_at, "
                    "  origin = CASE WHEN entity_sources.origin = 'manual' "
                    "                THEN 'manual' ELSE 'derived' END",
                    (entity_id, source, cnt, now))
                n += 1
        return n

    def project_source_counts(self) -> list[dict]:
        cols = ("source", "entities")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            "SELECT source, COUNT(DISTINCT entity_id) AS entities "
            "FROM entity_sources GROUP BY source ORDER BY entities DESC, source"
        ).fetchall()]

    def facts_for_entry(self, entry_id: int) -> list[dict]:
        # The (entity, attribute) slot is the stable handle; facts.id is ephemeral
        # (snapshot-rewrite reassigns it on every cortex write), so we neither
        # return it nor order by it — order by the stable slot for determinism.
        cols = ("entity", "attribute", "value")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            f"SELECT f.{', f.'.join(cols)} FROM facts f "
            "JOIN memory_traces t ON f.entity_norm = t.entity_norm "
            "AND f.attribute_norm = t.attribute_norm "
            "WHERE t.entry_id = %s AND f.status = 'current' "
            "ORDER BY f.entity_norm, f.attribute_norm", (entry_id,)).fetchall()]

    def get_entry(self, entry_id: int) -> dict | None:
        cols = ("id", "text", "source", "ts", "reinforcements", "access_count")
        row = self.conn.execute(
            "SELECT id, text, source, ts, reinforcements, access_count "
            "FROM entries WHERE id = %s",
            (entry_id,)).fetchone()
        return dict(zip(cols, row)) if row else None

    def existing_entry_ids(self, ids) -> set[int]:
        """The subset of ``ids`` that still have entries rows — lets the dream
        verify its in-memory db_id mapping after a connection loss rolled back
        inserts whose ids were already handed out."""
        ids = [int(i) for i in ids]
        if not ids:
            return set()
        rows = self.conn.execute(
            "SELECT id FROM entries WHERE id = ANY(%s)", (ids,)).fetchall()
        return {int(r[0]) for r in rows}

    def bump_reinforcements(self, entry_id: int, delta: int) -> None:
        with self._txn():
            self.conn.execute(
                "UPDATE entries SET reinforcements = reinforcements + %s WHERE id = %s",
                (delta, entry_id))

    def bump_access_count(self, entry_id: int, delta: int) -> None:
        with self._txn():
            self.conn.execute(
                "UPDATE entries SET access_count = access_count + %s WHERE id = %s",
                (delta, entry_id))

    def load_graph(self) -> dict:
        """Whole live graph (entities + aliases + non-superseded edges) —
        small by design, loaded per query for on-read inference."""
        ent_cols = ("id", "canonical", "display", "etype", "created_at")
        entities = [
            dict(zip(ent_cols, r)) for r in self.conn.execute(
                "SELECT id, canonical, display, etype, created_at FROM entities "
                "ORDER BY id",
            ).fetchall()
        ]
        aliases: dict[int, list[str]] = {}
        for alias, eid in self.conn.execute(
            "SELECT alias, entity_id FROM entity_aliases ORDER BY alias",
        ).fetchall():
            aliases.setdefault(eid, []).append(alias)
        edge_cols = ("id", "src_id", "relation", "dst_id", "confidence",
                     "origin", "asserted_at")
        edges = [
            dict(zip(edge_cols, r)) for r in self.conn.execute(
                f"SELECT {', '.join(edge_cols)} FROM edges "
                "WHERE superseded_at IS NULL ORDER BY id",
            ).fetchall()
        ]
        return {"entities": entities, "aliases": aliases, "edges": edges}

    def replace_communities(self, assignment: dict[int, int],
                            summaries: list[dict], computed_at: float) -> None:
        """Wholesale replace the community partition (truncate + bulk insert).
        The shared entities hub is never touched."""
        with self._txn(), self.conn.cursor() as cur:
            cur.execute("DELETE FROM entity_communities")
            cur.execute("DELETE FROM communities")
            if summaries:
                cur.executemany(
                    "INSERT INTO communities (id, label, size, cohesion, computed_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(s["id"], s["label"], s["size"], s["cohesion"], computed_at)
                     for s in summaries],
                )
            if assignment:
                cur.executemany(
                    "INSERT INTO entity_communities (entity_id, community_id, computed_at) "
                    "VALUES (%s, %s, %s)",
                    [(eid, cid, computed_at) for eid, cid in assignment.items()],
                )

    def load_communities(self) -> dict:
        assignment = {
            eid: cid for eid, cid in self.conn.execute(
                "SELECT entity_id, community_id FROM entity_communities").fetchall()
        }
        cols = ("id", "label", "size", "cohesion", "computed_at")
        communities = [
            dict(zip(cols, r)) for r in self.conn.execute(
                f"SELECT {', '.join(cols)} FROM communities ORDER BY id").fetchall()
        ]
        return {"assignment": assignment, "communities": communities}
