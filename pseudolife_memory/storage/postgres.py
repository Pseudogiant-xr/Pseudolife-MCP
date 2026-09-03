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
from typing import Any, Sequence

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
    # v35: the write-time label pair — nullable, NULL = unlabelled.
    "authority", "distortion_tolerance",
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
    "entity_id", "object_entity_id", "freshness_class",
    # v26 (set-valued slots): kind partitions the current-uniqueness
    # constraint (scalar vs member) — omitting it from an insert defaults
    # every row to 'scalar' and member rows collide on
    # facts_slot_current_scalar_uq instead of landing in the member index.
    # value_norm is the member dedup identity; NULL on scalar rows.
    "kind", "value_norm",
    # v29: epistemic stance — nullable, NULL = asserted plainly.
    "stance",
    # v35: the write-time label pair — nullable, NULL = unlabelled.
    "authority", "distortion_tolerance",
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
    # Curation review (2026-07-26) proposes this for a source file paired with
    # its own bare stem, so it has to be seeded: an unregistered suggestion is
    # rejected by `graph_relate` with an empty `suggestions` list.
    ("implements", "src (a source file) realizes concept/role dst", False, None),
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
        ensure_schema(self._conn)
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
            elif c == "freshness_class" and v is None:
                v = "evergreen"  # v23; same NOT NULL DEFAULT rule as version
            elif c == "kind" and v is None:
                v = "scalar"     # v26; same NOT NULL DEFAULT rule as version
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
                elif c == "freshness_class" and v is None:
                    # NOT NULL DEFAULT, but the default differs per store:
                    # personal facts are durable, world facts rot.
                    v = "volatile" if table == "world_facts" else "evergreen"
                elif c == "kind" and v is None:
                    v = "scalar"  # v26; NOT NULL DEFAULT — facts table only
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
        not per retrieval, to keep reads cheap. One unnest'd UPDATE rather
        than per-row executemany: the autosave holds the service lock while
        this runs, and a single round trip keeps that hold O(1) in bank
        size (2026-09-01, live-sized 1,135 rows: 30.5ms as per-row
        statements vs 9.8ms unnest'd; the gap scales with row count). The
        ``<>`` guard keeps a no-op save from rewriting every row."""
        if not pairs:
            return
        with self._txn():
            self.conn.execute(
                "UPDATE entries e SET access_count = v.ac "
                "FROM (SELECT unnest(%s::bigint[]) AS id, "
                "             unnest(%s::int[]) AS ac) v "
                "WHERE e.id = v.id AND e.access_count <> v.ac",
                ([i for i, _ in pairs], [c for _, c in pairs]),
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

    # ── entity kinds (schema v24) ───────────────────────────────────────

    def load_entity_kinds(self) -> dict[str, str]:
        """entity_norm -> kind. Small (order 1k rows); loaded once and cached
        by the service, so a plain full read is right here."""
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT entity_norm, kind FROM entity_kinds").fetchall()}

    def upsert_entity_kinds(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self._txn(), self.conn.cursor() as cur:
            for r in rows:
                cur.execute(
                    "INSERT INTO entity_kinds "
                    "(entity_norm, kind, origin, confidence, decided_at) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (entity_norm) DO UPDATE SET "
                    "kind=EXCLUDED.kind, origin=EXCLUDED.origin, "
                    "confidence=EXCLUDED.confidence, "
                    "decided_at=EXCLUDED.decided_at",
                    (r["entity_norm"], r["kind"], r["origin"],
                     r.get("confidence"), r["decided_at"]),
                )
        return len(rows)

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

    # ── retrieval events (append-only search log; learned-reranker Phase 0) ─

    def add_retrieval_event(self, query_text: str, served: list[dict],
                            origin: str = "search",
                            session_id: str | None = None,
                            episode_id: str | None = None,
                            params: dict | None = None,
                            now: float | None = None) -> int:
        """One row per search that served entries.

        ``served`` is the ranked list as dicts (``entry_id``/``score``/
        ``rank``/``via``/``bank``/``components``). Entry ids carry no FK —
        entries are evictable, and a training join tolerates dangling ids.
        ``params`` (v32) is the per-query knob snapshot the fusion ran
        under; None for callers that don't rank (and for v31 rows).
        """
        t = time.time() if now is None else float(now)
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO retrieval_events "
                "(query_text, origin, session_id, episode_id, served, "
                "params, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (query_text, origin, session_id, episode_id, Jsonb(served),
                 None if params is None else Jsonb(params), t),
            ).fetchone()
        return int(row[0])

    def record_retrieval_use(self, entry_id: int, session_id: str | None,
                             used_via: str, window_s: float,
                             now: float | None = None) -> int:
        """Implicit relevance label: the most recent event in this session's
        window that served ``entry_id`` gains a use row. Session match is
        strict (``IS NOT DISTINCT FROM`` — a None session only labels
        None-session events, never another session's). Idempotent per
        (event, entry, via). Returns rows written (0 or 1)."""
        t = time.time() if now is None else float(now)
        with self._txn():
            row = self.conn.execute(
                "SELECT id FROM retrieval_events "
                "WHERE session_id IS NOT DISTINCT FROM %s "
                "AND created_at >= %s AND served @> %s "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (session_id, t - float(window_s),
                 Jsonb([{"entry_id": int(entry_id)}])),
            ).fetchone()
            if row is None:
                return 0
            cur = self.conn.execute(
                "INSERT INTO retrieval_uses "
                "(event_id, entry_id, used_via, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (int(row[0]), int(entry_id), used_via, t),
            )
        return cur.rowcount

    def attach_served_facts(self, event_id: int,
                            served_facts: list[dict]) -> int:
        """Record the cortex slots a search's cortex-first block served
        (schema v34), on the exact event row the search returned the id
        of. A plain UPDATE by primary key — no session-window guessing.
        Returns rows updated (0 = the event was pruned or the id is bogus;
        the caller surfaces that, since a silent zero would be invisible
        exactly the way a dead log is)."""
        with self._txn():
            cur = self.conn.execute(
                "UPDATE retrieval_events SET served_facts = %s "
                "WHERE id = %s",
                (Jsonb(served_facts), int(event_id)))
        return cur.rowcount

    def graduation_report(self, window_days: float = 30.0,
                          min_sessions: int = 8, min_share: float = 0.6,
                          limit: int = 10,
                          now: float | None = None) -> list[dict]:
        """Entries served in a high share of recent distinct sessions —
        static-context ("graduate to CLAUDE.md") candidates: an entry the
        retrieval layer re-serves in nearly every session is effectively
        standing context being paid for per query. Heuristic defaults, not
        measured constants: 30-day window matches a working set's horizon,
        the 8-session floor keeps a young log from nominating on noise,
        0.6 share means "most sessions". Sessions with a NULL session_id
        are excluded (no identity to count). Entries whose row is gone
        (evicted) are dropped — nothing to promote.

        Semantics caveats, per the log's own shape: "served" means ranked
        into the event log, which is written BEFORE the MCP handler's
        cortex-dedup — an entry repeatedly dropped from responses for
        restating a fact still accrues share, so vet a candidate against
        the cortex before promoting it. ``sessions_total`` counts distinct
        sessions that SEARCHED (zero-result searches log too), not
        sessions that were served anything — this biases share downward,
        the conservative direction. Read-only; cost is one scan of the
        WINDOW (the 30-day filter rides retrieval_events_created_idx —
        retention only bounds the table, not this query), but the LATERAL
        jsonb expansion detoasts every windowed event's served blob, and
        like read_audit this runs under the service lock on every
        ``memory_stats`` / ``/api/overview`` call — shrink the window
        before blaming the bank if it ever shows in a latency profile."""
        t = time.time() if now is None else float(now)
        rows = self.conn.execute(
            """
            WITH ev AS (
              SELECT id, session_id, served FROM retrieval_events
              WHERE created_at >= %s AND session_id IS NOT NULL
            ),
            tot AS (SELECT COUNT(DISTINCT session_id) AS n FROM ev),
            hits AS (
              SELECT DISTINCT (elem->>'entry_id')::bigint AS entry_id,
                     ev.session_id
              FROM ev, LATERAL jsonb_array_elements(ev.served) AS elem
              WHERE elem ? 'entry_id'
            )
            SELECT h.entry_id, COUNT(*) AS sessions_served, t.n,
                   e.source, e.access_count, e.text
            FROM hits h
            CROSS JOIN tot t
            JOIN entries e ON e.id = h.entry_id
            WHERE t.n >= %s
            GROUP BY h.entry_id, t.n, e.source, e.access_count, e.text
            HAVING COUNT(*) >= %s * t.n
            ORDER BY COUNT(*) DESC, h.entry_id
            LIMIT %s
            """,
            (t - float(window_days) * 86400, int(min_sessions),
             float(min_share), int(limit)),
        ).fetchall()
        return [
            {"entry_id": int(r[0]), "sessions_served": int(r[1]),
             "sessions_total": int(r[2]),
             "share": round(float(r[1]) / float(r[2]), 3),
             "source": r[3], "access_count": int(r[4]),
             "text": (r[5] or "")[:200]}
            for r in rows
        ]

    def prune_retrieval_events(self, older_than_ts: float) -> int:
        """Delete events older than the cutoff (their use labels CASCADE),
        so the log can't grow unbounded."""
        with self._txn():
            cur = self.conn.execute(
                "DELETE FROM retrieval_events WHERE created_at < %s",
                (float(older_than_ts),),
            )
        return cur.rowcount

    def retrieval_events_window(self, since_ts: float = 0.0,
                                limit: int = 1000) -> list[dict]:
        """Events (oldest first) with use labels aggregated — the training
        export read. Read-only."""
        rows = self.conn.execute(
            "SELECT e.id, e.query_text, e.origin, e.session_id, "
            "e.episode_id, e.served, e.served_facts, e.params, "
            "e.created_at, "
            "COALESCE(json_agg(json_build_object("
            "'entry_id', u.entry_id, 'used_via', u.used_via, "
            "'at', u.created_at)) "
            "FILTER (WHERE u.entry_id IS NOT NULL), '[]') AS uses "
            "FROM retrieval_events e "
            "LEFT JOIN retrieval_uses u ON u.event_id = e.id "
            "WHERE e.created_at >= %s "
            "GROUP BY e.id ORDER BY e.created_at, e.id LIMIT %s",
            (float(since_ts), int(limit)),
        ).fetchall()
        cols = ("id", "query_text", "origin", "session_id", "episode_id",
                "served", "served_facts", "params", "created_at", "uses")
        return [dict(zip(cols, r)) for r in rows]

    def retrieval_log_health(self) -> dict:
        """Row counts + newest event timestamp for ``memory_stats``.

        Both log-write paths are exception-guarded, so a broken log is
        otherwise invisible (zero rows, green /health). Two aggregate
        queries, computed on demand rather than cached: the MAX is O(1) off
        ``retrieval_events_created_idx``, but the COUNTs are honestly
        O(rows) (an index-only scan at best — PG has no cheap exact count).
        Retention is the only bound on that cost: at the 365-day default a
        heavily-searched bank reaches six figures of rows, and this runs
        under the service lock on every ``memory_stats`` /
        ``/api/overview`` call. Lower ``retention_days`` (or trade the
        count for a ``pg_class.reltuples`` estimate) if it ever shows up in
        a latency profile.
        """
        ev = self.conn.execute(
            "SELECT COUNT(*), MAX(created_at) FROM retrieval_events"
        ).fetchone()
        uses = self.conn.execute(
            "SELECT COUNT(*) FROM retrieval_uses").fetchone()
        return {
            "events": int(ev[0]),
            "last_event_at": None if ev[1] is None else float(ev[1]),
            "uses": int(uses[0]),
        }

    def read_audit(self, now: float | None = None) -> dict:
        """Never-read fractions and read/write balance for ``memory_stats``
        (schema v33) — the audit that distinguishes a bank that is consulted
        from one that only accumulates. Age buckets are fixed at 14/45 days:
        young entries are expected to be unread, so only the old-and-unread
        tail is a hygiene signal. Like ``retrieval_log_health`` this is
        computed on demand; the entries scans are O(rows) and run under the
        service lock on every ``memory_stats`` call — fine at 10^3..10^4
        entries, revisit if a bank grows past that."""
        t = time.time() if now is None else float(now)
        agg = self.conn.execute(
            "SELECT count(*), count(*) FILTER (WHERE access_count = 0), "
            "COALESCE(sum(access_count), 0), "
            "COALESCE(percentile_cont(0.5) WITHIN GROUP "
            "(ORDER BY access_count), 0) "
            "FROM entries").fetchone()
        total, never, reads_total, median = (
            int(agg[0]), int(agg[1]), int(agg[2]), float(agg[3]))
        by_age = {}
        for key, lo, hi in (("lt_14d", t - 14 * 86400, None),
                            ("d14_45", t - 45 * 86400, t - 14 * 86400),
                            ("gt_45d", None, t - 45 * 86400)):
            cond, params = [], []
            if lo is not None:
                cond.append("ts >= %s")
                params.append(lo)
            if hi is not None:
                cond.append("ts < %s")
                params.append(hi)
            row = self.conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE access_count = 0) "
                f"FROM entries WHERE {' AND '.join(cond)}",
                tuple(params)).fetchone()
            by_age[key] = {"n": int(row[0]), "never_read": int(row[1])}
        worst = [
            {"source": r[0], "n": int(r[1]), "never_read": int(r[2]),
             "never_read_pct": round(100.0 * r[2] / r[1], 1)}
            for r in self.conn.execute(
                "SELECT source, count(*) AS n, "
                "count(*) FILTER (WHERE access_count = 0) AS never "
                "FROM entries GROUP BY source HAVING count(*) >= 5 "
                "ORDER BY count(*) FILTER (WHERE access_count = 0)::float "
                "/ count(*) DESC LIMIT 5").fetchall()
        ]
        top_share = self.conn.execute(
            "SELECT COALESCE(sum(access_count), 0) FROM ("
            "SELECT access_count FROM entries ORDER BY access_count DESC "
            "LIMIT GREATEST(1, (SELECT count(*) / 10 FROM entries))) d"
        ).fetchone()[0]
        slots = self.conn.execute(
            "SELECT (SELECT count(DISTINCT (entity_norm, attribute_norm)) "
            "FROM facts WHERE status = 'current'), "
            "(SELECT count(*) FROM slot_reads), "
            "(SELECT COALESCE(sum(read_count), 0) FROM slot_reads)"
        ).fetchone()
        reinf = self.conn.execute(
            "SELECT COALESCE(sum(reinforcements), 0), "
            "COALESCE(sum(explicit_reinforcements), 0) FROM entries"
        ).fetchone()
        return {
            "entries": {
                "total": total,
                "never_read": never,
                "never_read_pct": round(100.0 * never / total, 1) if total else 0.0,
                "reads_total": reads_total,
                "reads_median": median,
                "top_decile_read_share": (
                    round(float(top_share) / reads_total, 3)
                    if reads_total else None),
                "by_age": by_age,
            },
            "worst_sources": worst,
            "slots": {
                "current_slots": int(slots[0]),
                "slots_read": int(slots[1]),
                "reads_total": int(slots[2]),
            },
            "reinforcements": {
                "total": int(reinf[0]),
                "explicit": int(reinf[1]),
            },
        }

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
        is advisory, so a later conflicting hint must not silently retype).

        A fresh mint also repairs the fact/lesson cross-index for its name —
        see :meth:`_relink_orphaned_rows`. Mint-only (``xmax = 0`` is zero
        exactly when ON CONFLICT took the INSERT arm), so the repeated-upsert
        hot path (every ``memory_outcome`` log) pays nothing."""
        with self._txn():
            row = self.conn.execute(
                """
                INSERT INTO entities (canonical, display, etype, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (canonical) DO UPDATE
                  SET etype = COALESCE(entities.etype, EXCLUDED.etype)
                RETURNING id, (xmax = 0) AS minted
                """,
                (canonical, display or canonical, etype, time.time()),
            ).fetchone()
            if row[1]:
                self._relink_orphaned_rows(int(row[0]), canonical)
        return int(row[0])

    def _relink_orphaned_rows(self, entity_id: int, canonical: str) -> None:
        """Re-link orphaned fact/lesson rows to a freshly minted entity.

        :meth:`delete_entity` NULLs ``facts``/``lessons`` ``entity_id`` and
        ``object_entity_id`` (no cascade), and nothing else re-links those
        rows until their slot is next written — so a deleted-then-re-minted
        name under-counts everywhere the FK is read (fold-direction ranking,
        ``backfill_entity_sources``, ``lesson_entity_ids``; the #177 damage).

        Norm bridging, deliberately conservative: candidates are matched by
        ``norm_name(raw stored text) == canonical`` — the exact rule
        ``sync._link_entity_ids`` applies on slot writes (minus aliases,
        which a fresh mint cannot have) — and each UPDATE matches the exact
        stored text. The stored ``entity_norm`` column is the CORTEX norm
        (``_norm_key``) and is never consulted: the two spaces disagree
        (``"G:"`` → ``"g"`` vs ``"g:"``), and a cross-space match would
        mis-link the cross-index.

        The ILIKE prefilter is a sound NECESSARY condition: ``norm_name``
        only lowercases and folds separator/hyphen runs to ``-``, so the
        characters inside any hyphen-free segment of the canonical were
        contiguous in the raw text — every true match contains the longest
        such segment as a case-insensitive literal substring. PG vs Python
        case-folding (non-C collation, exotic unicode) can only DROP a
        match — which heals on the next slot write — never add one; the
        Python ``norm_name`` check below stays the authority.

        Cost, measured 2026-08-25 (local PG16 in Docker, synthetic 120-char
        values): ~50 ms per mint at 5k facts, ~65 ms at 50k with the ILIKE
        prefilter (~120 ms and ~420 ms without it — the object-side columns
        are unlinked on almost every row, so those two scans see the whole
        table and every distinct value shipped to Python; regexp_replace
        prefilters measured no better than Python, ILIKE substring search
        does). The live bank is ~750 entries today; mints are
        create-miss-only, so bulk mints (a dream pass, a bench bank build)
        pay per fresh name — if banks outgrow ~50k facts, escalate to a
        pg_trgm index on the text columns or a partial expression index
        over a SQL-side norm (schema bump), not to a looser match. Runs
        inside ensure_entity's transaction, mint-only, under the service
        lock; the enlarged transaction can newly hit the connection's 5s
        ``lock_timeout`` on facts/lessons row locks (external writers only —
        the daemon is single-writer), which aborts and rolls back the whole
        mint rather than leaving a partial repair."""
        from pseudolife_memory.graph import norm_name
        if not canonical:
            return
        seg = max(canonical.split("-"), key=len)
        # "%"/"_"/"\" escaped for LIKE; only "%" can survive norm_name, the
        # other two are folded to "-", but escape all three for robustness.
        like = ("%" + seg.replace("\\", r"\\").replace("%", r"\%")
                         .replace("_", r"\_") + "%")
        for table, id_col, text_col in (
            ("facts", "entity_id", "entity"),
            ("facts", "object_entity_id", "value"),
            ("lessons", "entity_id", "entity"),
            ("lessons", "object_entity_id", "about"),
        ):
            texts = [r[0] for r in self.conn.execute(
                f"SELECT DISTINCT {text_col} FROM {table} "
                f"WHERE {id_col} IS NULL AND {text_col} IS NOT NULL "
                f"AND {text_col} ILIKE %s",
                (like,),
            ).fetchall()]
            for t in texts:
                if norm_name(t) != canonical:
                    continue
                self.conn.execute(
                    f"UPDATE {table} SET {id_col} = %s "
                    f"WHERE {id_col} IS NULL AND {text_col} = %s",
                    (entity_id, t))

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

    def canonical_names_among(self, names: Sequence[str]) -> set[str]:
        """The subset of ``names`` that are some entity's canonical. One
        indexed probe (``entities.canonical`` is UNIQUE); no query for an
        empty list. Lets a caller holding a node's alias list drop the
        aliases ``find_entity`` would never resolve to that node — it
        resolves canonical-first, so an alias row that coincides with any
        canonical is dead for resolution (``graph.alias_canonical_map``
        applies the same drop from a loaded graph)."""
        names = [n for n in names if n]
        if not names:
            return set()
        rows = self.conn.execute(
            "SELECT canonical FROM entities WHERE canonical = ANY(%s)",
            (names,)).fetchall()
        return {r[0] for r in rows}

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

    def delete_entity_audited(self, entity_id: int, display: str, status: str,
                              reason: str, decided_by: str, now: float) -> bool:
        """``delete_entity`` plus its ``merge_decisions`` audit row in ONE
        transaction — an unattended deletion whose audit row a crash could
        separate from the delete would be an invisible deletion."""
        with self._txn():
            for tbl in ("facts", "lessons"):
                self.conn.execute(f"UPDATE {tbl} SET entity_id = NULL WHERE entity_id = %s", (entity_id,))
                self.conn.execute(f"UPDATE {tbl} SET object_entity_id = NULL WHERE object_entity_id = %s", (entity_id,))
            row = self.conn.execute(
                "DELETE FROM entities WHERE id = %s RETURNING id", (entity_id,)).fetchone()
            if row is None:
                return False
            self.conn.execute(
                "INSERT INTO merge_decisions "
                "(proposal_id, entity_display, into_display, status, score, "
                " reason, decided_by, decided_at) "
                "VALUES (NULL, %s, NULL, %s, NULL, %s, %s, %s)",
                (display, status, reason, decided_by, now))
        return True

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
        capped 0.99) and keeps the higher-ranked origin claim
        (user > action > agent > none): a dream re-extraction
        (origin='agent') must not downgrade an edge a human blessed
        (origin='user') or a review verdict confirmed (origin='action') —
        a downgrade would let rescore_edges recompute its confidence and
        dubious_edges re-flag a human-settled edge. ``revive=True``
        (explicit/human assertion) clears a prior supersession;
        ``revive=False`` (agent re-extraction, e.g. the dream) leaves a
        superseded edge superseded — a human removal must be sticky
        against the extractor re-planting the same triple."""
        with self._txn():
            row = self.conn.execute(
                """
                INSERT INTO edges
                  (src_id, relation, dst_id, confidence, origin, asserted_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (src_id, relation, dst_id) DO UPDATE SET
                  confidence = LEAST(
                    0.99, GREATEST(EXCLUDED.confidence, edges.confidence + 0.05)),
                  origin = CASE WHEN
                      CASE edges.origin WHEN 'user' THEN 3
                           WHEN 'action' THEN 2 WHEN 'agent' THEN 1
                           ELSE 0 END
                      >
                      CASE EXCLUDED.origin WHEN 'user' THEN 3
                           WHEN 'action' THEN 2 WHEN 'agent' THEN 1
                           ELSE 0 END
                    THEN edges.origin ELSE EXCLUDED.origin END,
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

    # ── dream-run audit + pre-image journal (schema v27) ─────────────────
    _DREAM_RUN_COLS = ("id", "started_at", "finished_at", "cursor_before",
                       "cursor_after", "pulled", "claims", "tallies",
                       "status", "extractor", "writer_id", "rolled_back_at")
    _DREAM_SLOT_COLS = ("id", "run_id", "seq", "entity", "attribute",
                        "entity_norm", "attribute_norm", "kind", "op",
                        "prev_kind", "prev_value", "prev_status",
                        "prev_confidence", "prev_support", "new_value",
                        "action", "src_entry_id", "at",
                        "chronicle_event_id")

    def start_dream_run(self, started_at: float, cursor_before: float,
                        pulled: int, extractor: str | None = None,
                        writer_id: str | None = None) -> int:
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO dream_runs (started_at, cursor_before, pulled, "
                "status, extractor, writer_id) "
                "VALUES (%s, %s, %s, 'running', %s, %s) RETURNING id",
                (started_at, cursor_before, pulled, extractor, writer_id),
            ).fetchone()
        return int(row[0])

    def finish_dream_run(self, run_id: int, *, status: str,
                         finished_at: float, cursor_after: float | None,
                         claims: int, tallies: dict) -> None:
        with self._txn():
            self.conn.execute(
                "UPDATE dream_runs SET status=%s, finished_at=%s, "
                "cursor_after=%s, claims=%s, tallies=%s WHERE id=%s",
                (status, finished_at, cursor_after, claims,
                 Jsonb(tallies), run_id))

    def update_dream_run_tallies(self, run_id: int, extra: dict) -> None:
        """Merge ``extra`` into an existing run row's tallies JSONB. Exists
        for counters produced AFTER the commit stamp (lesson synthesis runs
        post-commit by design, so its tallies would otherwise live only in
        the transient dream-run result)."""
        with self._txn():
            self.conn.execute(
                "UPDATE dream_runs SET "
                "tallies = COALESCE(tallies, '{}'::jsonb) || %s WHERE id=%s",
                (Jsonb(extra), run_id))

    def add_dream_run_slot(self, run_id: int, row: dict) -> None:
        """One pre-image journal row, written immediately after its claim's
        write lands (crash-durable, mirroring add_trace's per-claim
        pattern) — a buffered journal would lose exactly the rows whose
        writes already committed."""
        with self._txn():
            self.conn.execute(
                "INSERT INTO dream_run_slots (run_id, seq, entity, "
                "attribute, entity_norm, attribute_norm, kind, op, "
                "prev_kind, prev_value, prev_status, prev_confidence, "
                "prev_support, new_value, action, src_entry_id, at, "
                "chronicle_event_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s, %s)",
                (run_id, row["seq"], row["entity"], row["attribute"],
                 row["entity_norm"], row["attribute_norm"], row["kind"],
                 row.get("op"), row.get("prev_kind"), row.get("prev_value"),
                 row.get("prev_status"), row.get("prev_confidence"),
                 row.get("prev_support"), row.get("new_value"),
                 row["action"], row.get("src_entry_id"), row["at"],
                 row.get("chronicle_event_id")))

    def recent_dream_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._DREAM_RUN_COLS)} FROM dream_runs "
            "ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
        return [dict(zip(self._DREAM_RUN_COLS, r)) for r in rows]

    def dream_run_journal(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._DREAM_SLOT_COLS)} FROM dream_run_slots "
            "WHERE run_id = %s ORDER BY seq", (run_id,)).fetchall()
        return [dict(zip(self._DREAM_SLOT_COLS, r)) for r in rows]

    def latest_committed_dream_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {', '.join(self._DREAM_RUN_COLS)} FROM dream_runs "
            "WHERE status = 'committed' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(zip(self._DREAM_RUN_COLS, row)) if row else None

    def dream_runs_newer_than(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {', '.join(self._DREAM_RUN_COLS)} FROM dream_runs "
            "WHERE id > %s ORDER BY id", (run_id,)).fetchall()
        return [dict(zip(self._DREAM_RUN_COLS, r)) for r in rows]

    def mark_dream_run_rolled_back(self, run_id: int, at: float) -> None:
        with self._txn():
            self.conn.execute(
                "UPDATE dream_runs SET status='rolled_back', "
                "rolled_back_at=%s WHERE id=%s", (at, run_id))

    def prune_dream_runs(self, keep: int, *,
                         stale_running_seconds: float = 86400.0) -> int:
        """Keep the newest ``keep`` runs (CASCADE removes their journals).
        Also flips ``running`` rows older than ``stale_running_seconds`` to
        ``failed`` — a process death mid-loop leaves ``running`` forever,
        and rollback must be able to refuse it honestly."""
        with self._txn():
            self.conn.execute(
                "UPDATE dream_runs SET status='failed' "
                "WHERE status='running' AND started_at < %s",
                (time.time() - stale_running_seconds,))
            cur = self.conn.execute(
                "DELETE FROM dream_runs WHERE id NOT IN "
                "(SELECT id FROM dream_runs ORDER BY id DESC LIMIT %s)",
                (max(0, int(keep)),))
        return cur.rowcount

    # ── chronicle events (schema v28) ────────────────────────────────────
    # Additive-only: writes insert or exact-dedup; contradiction handling
    # sets invalidated_at, never deletes; the only delete is dream-run
    # rollback, safe precisely because nothing ever updates these rows.

    def add_chronicle_event(self, row: dict) -> tuple[int, str]:
        """Insert one event, deduping on exact (actor_norm,
        description_norm, occurred_at) among live rows — IS NOT DISTINCT
        FROM so two undated statements of the same occurrence also match.
        Returns ``(id, "inserted"|"duplicate")``."""
        with self._txn():
            dup = self.conn.execute(
                "SELECT id FROM chronicle_events WHERE actor_norm = %s "
                "AND description_norm = %s AND occurred_at IS NOT DISTINCT "
                "FROM %s::timestamptz AND invalidated_at IS NULL "
                "ORDER BY id LIMIT 1",
                (row["actor_norm"], row["description_norm"],
                 row.get("occurred_at"))).fetchone()
            if dup is not None:
                return int(dup[0]), "duplicate"
            new = self.conn.execute(
                "INSERT INTO chronicle_events (occurred_at, occurred_phrase, "
                "recorded_at, actor, actor_norm, description, "
                "description_norm, episode, src_entry_id, hlc_phys, "
                "hlc_logical, writer_id) VALUES (%s::timestamptz, %s, %s, "
                "%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (row.get("occurred_at"), row.get("occurred_phrase"),
                 row["recorded_at"], row["actor"], row["actor_norm"],
                 row["description"], row["description_norm"],
                 row.get("episode"), row.get("src_entry_id"),
                 row.get("hlc_phys"), row.get("hlc_logical"),
                 row.get("writer_id"))).fetchone()
        return int(new[0]), "inserted"

    def chronicle_search(self, query: str,
                         limit: int = 6) -> list[dict[str, Any]]:
        """Live events lexically matching ``query``, chronologically
        ascending — undated rows (phrase-only) trail dated ones and order
        among themselves by when they were recorded. ANY-term match
        (plainto_tsquery's AND rebuilt with OR): a "when did X happen"
        question should surface the related events around X, not only the
        rows carrying every query token. The rebuilt string is parsed
        with the 'simple' config: its terms are already-stemmed English
        lexemes, and to_tsquery('english', ...) would stem them a second
        time ('releas' -> 'relea'), silently matching nothing for any
        word whose stem is itself stemmable."""
        lex = self.conn.execute(
            "SELECT plainto_tsquery('english', %s)::text",
            (query,)).fetchone()[0]
        if not lex:
            return []
        rows = self.conn.execute(
            "SELECT id, to_char(occurred_at, 'YYYY-MM-DD'), occurred_phrase, "
            "recorded_at, actor, description, episode, src_entry_id "
            "FROM chronicle_events "
            "WHERE invalidated_at IS NULL AND "
            "to_tsvector('english', description) @@ to_tsquery('simple', %s) "
            "ORDER BY occurred_at ASC NULLS LAST, recorded_at ASC LIMIT %s",
            (lex.replace(" & ", " | "), limit)).fetchall()
        cols = ("id", "occurred_date", "occurred_phrase", "recorded_at",
                "actor", "description", "episode", "src_entry_id")
        return [dict(zip(cols, r)) for r in rows]

    def invalidate_chronicle_event(self, event_id: int, at: float) -> bool:
        with self._txn():
            cur = self.conn.execute(
                "UPDATE chronicle_events SET invalidated_at = %s "
                "WHERE id = %s AND invalidated_at IS NULL", (at, event_id))
        return cur.rowcount > 0

    def delete_chronicle_event(self, event_id: int) -> bool:
        """Rollback-only removal (see class of methods above)."""
        with self._txn():
            cur = self.conn.execute(
                "DELETE FROM chronicle_events WHERE id = %s", (event_id,))
        return cur.rowcount > 0

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
                "rationale", "source", "created_at", "status",
                "judge_verdict", "judge_confidence", "judge_note",
                "judge_model", "judged_at", "judge_relation")
        rows = self.conn.execute(
            "SELECT p.id, p.src_id, p.relation, p.dst_id, p.confidence, p.similarity, "
            "       p.rationale, p.source, p.created_at, p.status, "
            "       p.judge_verdict, p.judge_confidence, p.judge_note, "
            "       p.judge_model, p.judged_at, p.judge_relation, "
            "       s.display, d.display "
            "FROM edge_proposals p "
            "JOIN entities s ON s.id = p.src_id JOIN entities d ON d.id = p.dst_id "
            "WHERE p.status = 'pending' ORDER BY p.confidence DESC, p.id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r[:16]))
            d["src"], d["dst"] = r[16], r[17]
            out.append(d)
        return out

    def set_proposal_judgment(self, proposal_id: int, *, verdict: str,
                              confidence: float | None, note: str | None,
                              model: str | None, relation: str | None,
                              at: float) -> bool:
        """Record the link judge's verdict on a PENDING edge proposal (an
        opinion, not a decision — schema v35 mirrors the v30 merge
        contract). ``relation`` is the corrected relation of a retype."""
        with self._txn():
            cur = self.conn.execute(
                "UPDATE edge_proposals SET judge_verdict=%s, "
                "judge_confidence=%s, judge_note=%s, judge_model=%s, "
                "judged_at=%s, judge_relation=%s "
                "WHERE id=%s AND status='pending'",
                (verdict, confidence, note, model, at, relation, proposal_id))
        return cur.rowcount > 0

    def record_curation_judgment(self, store: str, a_key: str, b_key: str, *,
                                 verdict: str, keep: str | None,
                                 fold: str | None, confidence: float | None,
                                 note: str | None, model: str | None,
                                 at: float) -> bool:
        """Memo the store-curation judge's verdict on a lesson/world slot
        pair (keys stored sorted, like dismissed_pairs); a re-judge
        overwrites in place. Returns True once written."""
        a, b = sorted((a_key, b_key))
        with self._txn():
            self.conn.execute(
                "INSERT INTO curation_judgments (store, a_key, b_key, verdict, "
                " keep, fold, confidence, note, model, judged_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (store, a_key, b_key) DO UPDATE SET "
                " verdict = EXCLUDED.verdict, keep = EXCLUDED.keep, "
                " fold = EXCLUDED.fold, confidence = EXCLUDED.confidence, "
                " note = EXCLUDED.note, model = EXCLUDED.model, "
                " judged_at = EXCLUDED.judged_at",
                (store, a, b, verdict, keep, fold, confidence, note, model, at))
        return True

    # ── store decisions: retire/restore audit for lessons + world (v37) ───

    _STORE_DECISION_COLS = ("id", "store", "entity_norm", "attribute_norm",
                            "action", "decided_by", "reason", "record",
                            "decided_at")

    def record_store_decision(self, store: str, entity_norm: str,
                              attribute_norm: str, action: str, *,
                              decided_by: str | None, reason: str | None,
                              record: dict | None, now: float) -> int:
        """Durable, FK-free audit row for a lesson/world retire or restore.
        ``record`` is the verbatim slot record (embedding excluded) — the
        restore fallback once compaction has purged the retired row."""
        with self._txn():
            row = self.conn.execute(
                "INSERT INTO store_decisions (store, entity_norm, "
                " attribute_norm, action, decided_by, reason, record, "
                " decided_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (store, entity_norm, attribute_norm, action, decided_by,
                 reason, Jsonb(record) if record is not None else None,
                 now)).fetchone()
        return int(row[0])

    def store_decisions(self, store: str, *, entity_norm: str | None = None,
                        attribute_norm: str | None = None,
                        limit: int = 500) -> list[dict]:
        """Retire/restore audit rows for ``store``, newest first."""
        cols = self._STORE_DECISION_COLS
        sql = f"SELECT {', '.join(cols)} FROM store_decisions WHERE store = %s"
        params: list = [store]
        if entity_norm is not None:
            sql += " AND entity_norm = %s"
            params.append(entity_norm)
        if attribute_norm is not None:
            sql += " AND attribute_norm = %s"
            params.append(attribute_norm)
        sql += " ORDER BY decided_at DESC, id DESC LIMIT %s"
        params.append(int(limit))
        return [dict(zip(cols, r))
                for r in self.conn.execute(sql, params).fetchall()]

    def retired_slots(self, store: str, *, entity_norm: str | None = None,
                      limit: int = 100) -> list[dict]:
        """Slots whose LATEST decision is a retire — still retired, whether
        or not compaction has since purged the row. Newest first."""
        cols = self._STORE_DECISION_COLS
        sql = (f"SELECT DISTINCT ON (entity_norm, attribute_norm) "
               f"{', '.join(cols)} FROM store_decisions WHERE store = %s")
        params: list = [store]
        if entity_norm is not None:
            sql += " AND entity_norm = %s"
            params.append(entity_norm)
        sql += " ORDER BY entity_norm, attribute_norm, decided_at DESC, id DESC"
        rows = [dict(zip(cols, r))
                for r in self.conn.execute(sql, params).fetchall()]
        out = [d for d in rows if d["action"] == "retire"]
        out.sort(key=lambda d: (-float(d["decided_at"]), -int(d["id"])))
        return out[: max(0, int(limit))]

    def curation_judgments(self, store: str) -> dict[tuple[str, str], dict]:
        """``{(a_key, b_key): {verdict, keep, fold, confidence, note, model,
        judged_at}}`` for one store, keys sorted."""
        rows = self.conn.execute(
            "SELECT a_key, b_key, verdict, keep, fold, confidence, note, "
            "model, judged_at FROM curation_judgments WHERE store = %s",
            (store,)).fetchall()
        return {(r[0], r[1]): {"verdict": r[2], "keep": r[3], "fold": r[4],
                               "confidence": r[5], "note": r[6],
                               "model": r[7], "judged_at": r[8]}
                for r in rows}

    def get_proposal(self, proposal_id: int) -> dict | None:
        cols = ("id", "src_id", "relation", "dst_id", "confidence", "similarity",
                "rationale", "source", "created_at", "status", "decided_by",
                "decided_at", "judge_verdict", "judge_confidence",
                "judge_relation")
        r = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM edge_proposals WHERE id = %s", (proposal_id,)
        ).fetchone()
        return dict(zip(cols, r)) if r else None

    def set_proposal_status(self, proposal_id: int, status: str, *,
                            decided_by: str | None = None,
                            decided_at: float | None = None) -> bool:
        with self._txn():
            cur = self.conn.execute(
                "UPDATE edge_proposals SET status = %s, "
                "decided_by = COALESCE(%s, decided_by), "
                "decided_at = COALESCE(%s, decided_at) WHERE id = %s",
                (status, decided_by, decided_at, proposal_id))
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
        cols = ("id", "kind", "entity_id", "into_id", "score", "reason",
                "status", "created_at", "judge_verdict", "judge_confidence",
                "judge_note", "judge_model", "judged_at",
                "judge2_verdict", "judge2_confidence", "judge2_model",
                "judged2_at")
        rows = self.conn.execute(
            "SELECT p.id, p.kind, p.entity_id, p.into_id, p.score, p.reason, p.status, "
            "       p.created_at, p.judge_verdict, p.judge_confidence, "
            "       p.judge_note, p.judge_model, p.judged_at, "
            "       p.judge2_verdict, p.judge2_confidence, p.judge2_model, "
            "       p.judged2_at, "
            "       e.display, i.display "
            "FROM entity_proposals p "
            "JOIN entities e ON e.id = p.entity_id "
            "LEFT JOIN entities i ON i.id = p.into_id "
            "WHERE p.status = 'pending' ORDER BY p.kind, p.score DESC NULLS LAST, p.id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(cols, r[:17]))
            d["entity"], d["into"] = r[17], r[18]
            out.append(d)
        return out

    def set_entity_proposal_second_judgment(self, proposal_id: int, *,
                                            verdict: str,
                                            confidence: float | None,
                                            model: str | None, at: float,
                                            note: str | None = None) -> bool:
        """Record the merge judge's SECOND opinion on a still-pending row
        (schema v35); ``note`` replaces the combined judge_note when given."""
        with self._txn():
            cur = self.conn.execute(
                "UPDATE entity_proposals SET judge2_verdict=%s, "
                "judge2_confidence=%s, judge2_model=%s, judged2_at=%s, "
                "judge_note = COALESCE(%s, judge_note) "
                "WHERE id=%s AND status='pending'",
                (verdict, confidence, model, at, note, proposal_id))
        return cur.rowcount > 0

    def entity_fact_rows(self, entity_id: int, canonical: str,
                         limit: int = 3) -> list[tuple[str, str]]:
        """``(attribute, value)`` of the current facts an entity carries,
        by id or by name — evidence for the junk judge."""
        rows = self.conn.execute(
            "SELECT attribute, value FROM facts "
            "WHERE (entity_id = %s OR entity_norm = %s) "
            "  AND superseded_at IS NULL ORDER BY id LIMIT %s",
            (entity_id, canonical, int(limit))).fetchall()
        return [(r[0], r[1]) for r in rows]

    def zero_evidence_entities(self, min_age_seconds: float) -> list[dict]:
        """Entities carrying NO structural evidence — no edge (superseded
        rows count: edge history is evidence), no current fact by id or by
        cortex norm, no lesson reference, no alias, no scope, no proposal
        on either side — created before ``now - min_age_seconds``. The
        orphan sweep's candidate list; the service still checks the
        entries for a mention before deleting."""
        cutoff = time.time() - float(min_age_seconds)
        rows = self.conn.execute(
            "SELECT e.id, e.display, e.canonical, e.created_at FROM entities e "
            "WHERE e.created_at < %s "
            "  AND NOT EXISTS (SELECT 1 FROM edges x "
            "                  WHERE x.src_id = e.id OR x.dst_id = e.id) "
            "  AND NOT EXISTS (SELECT 1 FROM facts f "
            "                  WHERE (f.entity_id = e.id OR f.object_entity_id = e.id "
            "                         OR f.entity_norm = e.canonical) "
            "                    AND f.superseded_at IS NULL) "
            "  AND NOT EXISTS (SELECT 1 FROM lessons l "
            "                  WHERE l.entity_id = e.id OR l.object_entity_id = e.id "
            "                     OR l.entity_norm = e.canonical) "
            "  AND NOT EXISTS (SELECT 1 FROM world_facts w "
            "                  WHERE w.entity_norm = e.canonical "
            "                    AND w.status = 'current') "
            "  AND NOT EXISTS (SELECT 1 FROM entity_aliases a WHERE a.entity_id = e.id) "
            "  AND NOT EXISTS (SELECT 1 FROM entity_sources s WHERE s.entity_id = e.id) "
            "  AND NOT EXISTS (SELECT 1 FROM edge_proposals p "
            "                  WHERE p.src_id = e.id OR p.dst_id = e.id) "
            "  AND NOT EXISTS (SELECT 1 FROM entity_proposals q "
            "                  WHERE q.entity_id = e.id OR q.into_id = e.id) "
            "ORDER BY e.id", (cutoff,)).fetchall()
        return [{"id": r[0], "display": r[1], "canonical": r[2],
                 "created_at": r[3]} for r in rows]

    def set_entity_proposal_judgment(self, proposal_id: int, *,
                                     verdict: str, confidence: float | None,
                                     note: str | None, model: str | None,
                                     at: float) -> bool:
        """Record the autonomous judge's shadow verdict on a pending
        proposal (an opinion, not a decision — see schema v30)."""
        with self._txn():
            cur = self.conn.execute(
                "UPDATE entity_proposals SET judge_verdict=%s, "
                "judge_confidence=%s, judge_note=%s, judge_model=%s, "
                "judged_at=%s WHERE id=%s AND status='pending'",
                (verdict, confidence, note, model, at, proposal_id))
        return cur.rowcount > 0

    def get_entity_proposal(self, proposal_id: int) -> dict | None:
        cols = ("id", "kind", "entity_id", "into_id", "score", "reason", "status",
                "created_at", "decided_by", "decided_at", "judge_verdict",
                "judge_confidence", "judge2_verdict", "judge2_confidence")
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

    def junk_accepted_displays(self) -> list[str]:
        """Displays of every entity ever deleted as junk (reviewed or
        dream-auto) — the junk TOMBSTONES. Junk rows are the accepted
        merge_decisions with no fold target; the proposal row itself
        CASCADEs away with the entity, so this denormalized table is the
        only durable record a verdict happened."""
        rows = self.conn.execute(
            "SELECT DISTINCT entity_display FROM merge_decisions "
            "WHERE status = 'accepted' AND into_display IS NULL").fetchall()
        return [r[0] for r in rows]

    def max_entity_id(self) -> int:
        """High-water mark of the entities serial — the deep-dream need
        signal's watermark (id-based, not count-based: merges and junk
        deletions shrink counts and would mask growth)."""
        row = self.conn.execute(
            "SELECT coalesce(max(id), 0) FROM entities").fetchone()
        return int(row[0])

    def entities_above(self, entity_id: int) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM entities WHERE id > %s",
            (int(entity_id),)).fetchone()
        return int(row[0])

    def merge_decision_stats(self) -> dict:
        """Accept/reject tallies over merge_decisions — the direct measure of
        the dedup detector's precision (the 2026-08-11 triage ran 38/153 and
        the number previously vanished into the audit log). Junk deletions —
        auto (``decided_by='dream-auto'``) and reviewed alike — are recorded
        to the same table with ``into_display IS NULL``; they are not merge
        verdicts and are excluded."""
        rows = self.conn.execute(
            "SELECT status, count(*) FROM merge_decisions "
            "WHERE decided_by <> 'dream-auto' "
            "  AND into_display IS NOT NULL "
            "  AND status IN ('accepted', 'rejected') "
            "GROUP BY status").fetchall()
        counts = {status: int(n) for status, n in rows}
        accepted = counts.get("accepted", 0)
        rejected = counts.get("rejected", 0)
        total = accepted + rejected
        return {"accepted": accepted, "rejected": rejected, "total": total,
                "accept_rate": round(accepted / total, 3) if total else None}

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

    def traces_for_slots(self, slot_keys) -> dict[tuple[str, str], list[int]]:
        """:meth:`traces_for_slot` for MANY slots in one round trip.

        The per-slot form is right where the caller is already doing per-row
        work (a cortex lookup, or one row of a top_k search page). ``recall``
        is not that shape: it serves the facts of up to ``max_entities``
        nodes at once, so the per-slot form would put an unbounded query
        count on a single call. Keys are the normalized ``(entity,
        attribute)`` pairs, unnested into a join rather than an ``IN`` list
        of tuples so one plan serves any batch size.

        Input keys are de-duplicated first: a repeated pair would join N×M
        and hand the caller the same ``entry_id`` several times, which for
        a caller that COUNTS the hits (the ``re_verify`` reason names how
        many source memories moved) is a wrong answer rather than a slow
        one."""
        keys = list(dict.fromkeys((str(e), str(a)) for e, a in (slot_keys or [])))
        if not keys:
            return {}
        out: dict[tuple[str, str], list[int]] = {}
        for e, a, eid in self.conn.execute(
                "SELECT t.entity_norm, t.attribute_norm, t.entry_id "
                "FROM memory_traces t "
                "JOIN unnest(%s::text[], %s::text[]) AS k(e, a) "
                "  ON t.entity_norm = k.e AND t.attribute_norm = k.a "
                "ORDER BY t.entity_norm, t.attribute_norm, t.entry_id",
                ([k[0] for k in keys], [k[1] for k in keys])).fetchall():
            out.setdefault((e, a), []).append(int(eid))
        return out

    def superseded_evidence(self, entry_ids) -> dict[int, float]:
        """``{entry_id: superseded_at}`` for those of these entries that
        carry a supersession mark.

        The TIMESTAMP, not just the fact of it: the cross-index is keyed on
        the SLOT, so it returns every entry that ever formed that slot
        across its whole supersession history. A caller asking "is my
        evidence still good?" has to compare WHEN the evidence was
        retracted against when the standing value was last confirmed, or it
        flags every slot that ever had a corrected contributor and never
        stops.

        Scoped to the ids asked about and served by the primary key, never a
        full-table scan — the caller is a read surface annotating exactly
        the source entries the facts it is serving cite (a handful on a
        lookup, the whole cited set on a dump)."""
        ids = [int(i) for i in (entry_ids or []) if i is not None]
        if not ids:
            return {}
        return {int(r[0]): float(r[1]) for r in self.conn.execute(
            "SELECT id, superseded_at FROM entries "
            "WHERE id = ANY(%s) AND superseded_at IS NOT NULL",
            (ids,)).fetchall()}

    def slots_for_entries(self, entry_ids) -> list[dict]:
        """The engram cross-index read in the RETRACT direction: given source
        entries, every cortex slot they helped form.

        :meth:`traces_for_slot` walks the same edge forwards. Correcting a
        source memory needs it backwards — that is what scopes a repair to
        the derivations that actually stood on the retracted evidence,
        instead of leaving them current (arXiv 2608.10502) or resetting the
        store. Served by ``memory_traces_entry_idx``.
        """
        ids = [int(i) for i in (entry_ids or []) if i is not None]
        if not ids:
            return []
        cols = ("entity_norm", "attribute_norm", "entry_id")
        return [dict(zip(cols, r)) for r in self.conn.execute(
            "SELECT entity_norm, attribute_norm, entry_id FROM memory_traces "
            "WHERE entry_id = ANY(%s) "
            "ORDER BY entity_norm, attribute_norm, entry_id",
            (ids,)).fetchall()]

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

    def lesson_entity_ids(self) -> set[int]:
        """Entity ids referenced by any lesson row (subject or about-object).
        graph_review excludes these from the unattributed finding — lesson-
        minted nodes whose prefers/avoids edges were pruned have no edge
        signal left to identify them by."""
        rows = self.conn.execute(
            "SELECT entity_id FROM lessons WHERE entity_id IS NOT NULL "
            "UNION "
            "SELECT object_entity_id FROM lessons "
            "WHERE object_entity_id IS NOT NULL").fetchall()
        return {r[0] for r in rows}

    def entity_fact_counts(self) -> dict[int, int]:
        """Current-fact count per entity — the evidence signal that merge-
        direction ranking uses alongside degree, so a contentless node cannot
        become the target that absorbs a fact-rich one (2026-07-26)."""
        return {eid: int(n) for eid, n in self.conn.execute(
            "SELECT entity_id, COUNT(*) FROM facts "
            "WHERE status = 'current' AND entity_id IS NOT NULL "
            "GROUP BY entity_id").fetchall()}

    def current_fact_counts_by_entity_text(self) -> dict[str, int]:
        """Current-fact count per RAW subject text — the cross-index-free
        companion to :meth:`entity_fact_counts`, which counts by
        ``facts.entity_id`` and therefore reads zero for facts orphaned by
        an earlier ``delete_entity`` (it NULLs the FK and nothing re-links
        it). Returns the raw text so the caller normalizes into its own
        space: ``facts.entity_norm`` is the cortex norm (``_norm_key``) and
        the graph's ``norm_name`` is a different one — ``G:`` normalizes to
        ``g:`` in the first and ``g`` in the second."""
        return {str(ent): int(n) for ent, n in self.conn.execute(
            "SELECT entity, COUNT(*) FROM facts WHERE status = 'current' "
            "GROUP BY entity").fetchall()}

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
            # Purge contaminated derived rows: excluded sources (immortal
            # otherwise — upserts refresh them but nothing removed them,
            # 2026-07-19) and legacy mixed-case keys (new writes are always
            # case-folded, so a mixed-case row would shadow forever as a
            # separate scope). Benign stale derived rows are deliberately
            # KEPT: attribution must not decay when retention prunes the
            # entries it was derived from. Manual rows are never touched.
            self.conn.execute(
                "DELETE FROM entity_sources WHERE origin = 'derived' AND "
                "(LOWER(TRIM(source)) = ANY(%s) OR source <> LOWER(source))",
                (list(excl),))
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
        cols = ("id", "text", "source", "ts", "reinforcements",
                "explicit_reinforcements", "access_count")
        row = self.conn.execute(
            "SELECT id, text, source, ts, reinforcements, "
            "explicit_reinforcements, access_count "
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

    def bump_reinforcements(self, entry_id: int, delta: int,
                            explicit: bool = False) -> None:
        """``explicit=True`` (the v33 split) additionally bumps
        ``explicit_reinforcements`` in the SAME statement — one transaction,
        so a connection loss cannot commit one counter without the other.
        The dream's trace path calls this with the default, so the shared
        counter (the retention formula's input) keeps its pre-v33 meaning."""
        extra = (", explicit_reinforcements = explicit_reinforcements + %s"
                 if explicit else "")
        params = (delta, delta, entry_id) if explicit else (delta, entry_id)
        with self._txn():
            self.conn.execute(
                f"UPDATE entries SET reinforcements = reinforcements + %s"
                f"{extra} WHERE id = %s",
                params)

    def bump_slot_reads(self, slots: list[tuple[str, str]],
                        now: float | None = None) -> None:
        """Count one serve for each ``(entity_norm, attribute_norm)`` slot.
        Upsert-increment; a slot row exists only once something read it."""
        if not slots:
            return
        t = time.time() if now is None else float(now)
        with self._txn(), self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO slot_reads "
                "(entity_norm, attribute_norm, read_count, last_read_at) "
                "VALUES (%s, %s, 1, %s) "
                "ON CONFLICT (entity_norm, attribute_norm) DO UPDATE SET "
                "read_count = slot_reads.read_count + 1, "
                "last_read_at = EXCLUDED.last_read_at",
                [(e, a, t) for e, a in slots])

    def delete_slot_reads(self, entity_norm: str,
                          attribute_norm: str | None = None) -> None:
        """Drop read counters for a forgotten entity (or one exact slot) —
        called by ``cortex_forget`` so an orphaned counter cannot make slot
        coverage exceed 100% or leak a stale count into a re-created slot."""
        with self._txn():
            if attribute_norm is None:
                self.conn.execute(
                    "DELETE FROM slot_reads WHERE entity_norm = %s",
                    (entity_norm,))
            else:
                self.conn.execute(
                    "DELETE FROM slot_reads WHERE entity_norm = %s "
                    "AND attribute_norm = %s",
                    (entity_norm, attribute_norm))

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
