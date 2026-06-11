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

_FACT_COLS = (
    "entity", "attribute", "entity_norm", "attribute_norm", "value",
    "polarity", "status", "confidence", "origin", "support", "provenance",
    "asserted_at", "last_confirmed", "supersedes_value",
    "superseded_by_value", "superseded_at", "embedding",
    "entity_id", "object_entity_id",
)
_FACT_JSONB = {"support", "provenance"}

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


class PostgresStorage:
    """Durable layer under the in-memory bands / cortex (single writer)."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.conn = psycopg.connect(dsn, connect_timeout=10)
        # Never block forever on a lock — a stuck/orphaned writer should
        # raise here, not hang the whole daemon.
        self.conn.execute("SET lock_timeout = '5s'")
        self.conn.commit()
        self.capabilities = ensure_schema(self.conn)
        register_vector(self.conn)
        self._seed_relations()

    def _seed_relations(self) -> None:
        with self.conn.cursor() as cur:
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
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

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
        row = self.conn.execute(
            f"INSERT INTO entries ({', '.join(_ENTRY_COLS)}) "
            f"VALUES ({', '.join(['%s'] * len(_ENTRY_COLS))}) RETURNING id",
            values,
        ).fetchone()
        self.conn.commit()
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
        self.conn.execute(
            f"UPDATE entries SET {', '.join(sets)} WHERE id = %s", values,
        )
        self.conn.commit()

    def delete_entry_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        cur = self.conn.execute("DELETE FROM entries WHERE id = ANY(%s)", (ids,))
        self.conn.commit()
        return cur.rowcount

    def load_entries(self) -> list[dict]:
        cols = ("id",) + _ENTRY_COLS
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM entries ORDER BY id",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            d["embedding"] = np.asarray(d["embedding"], dtype=np.float32)
            out.append(d)
        return out

    # ── episodes ────────────────────────────────────────────────────────

    def upsert_episode(self, ep: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO episodes (id, title, hint, started_at, ended_at,
                                  closed_by_new_start)
            VALUES (%(id)s, %(title)s, %(hint)s, %(started_at)s,
                    %(ended_at)s, %(closed_by_new_start)s)
            ON CONFLICT (id) DO UPDATE SET
              title = EXCLUDED.title, hint = EXCLUDED.hint,
              started_at = EXCLUDED.started_at, ended_at = EXCLUDED.ended_at,
              closed_by_new_start = EXCLUDED.closed_by_new_start
            """,
            ep,
        )
        self.conn.commit()

    def load_episodes(self) -> list[dict]:
        cols = ("id", "title", "hint", "started_at", "ended_at",
                "closed_by_new_start")
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM episodes ORDER BY started_at",
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    # ── cortex facts ────────────────────────────────────────────────────

    def upsert_fact(self, f: dict) -> int:
        values = []
        for c in _FACT_COLS:
            v = f.get(c)
            if c == "embedding":
                v = _embedding_in(v)
            elif c in _FACT_JSONB:
                v = Jsonb(v if v is not None else [])
            values.append(v)
        if f.get("id") is not None:
            sets = ", ".join(f"{c} = %s" for c in _FACT_COLS)
            self.conn.execute(
                f"UPDATE facts SET {sets} WHERE id = %s", values + [f["id"]],
            )
            self.conn.commit()
            return int(f["id"])
        row = self.conn.execute(
            f"INSERT INTO facts ({', '.join(_FACT_COLS)}) "
            f"VALUES ({', '.join(['%s'] * len(_FACT_COLS))}) RETURNING id",
            values,
        ).fetchone()
        self.conn.commit()
        return int(row[0])

    def replace_facts(self, rows: list[dict]) -> None:
        """Snapshot-style cortex persistence: one transaction, full rewrite.

        The cortex is small by design (one current record per slot plus
        audit history), so a transactional truncate+insert is simpler and
        safer than row diffing.
        """
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM facts")
            for f in rows:
                values = []
                for c in _FACT_COLS:
                    v = f.get(c)
                    if c == "embedding":
                        v = _embedding_in(v)
                    elif c in _FACT_JSONB:
                        v = Jsonb(v if v is not None else [])
                    values.append(v)
                cur.execute(
                    f"INSERT INTO facts ({', '.join(_FACT_COLS)}) "
                    f"VALUES ({', '.join(['%s'] * len(_FACT_COLS))})",
                    values,
                )
        self.conn.commit()

    def update_access_counts(self, pairs: list[tuple[int, int]]) -> None:
        """Bulk-sync (entry_id, access_count) — called on the save cadence,
        not per retrieval, to keep reads cheap."""
        if not pairs:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "UPDATE entries SET access_count = %s "
                "WHERE id = %s AND access_count <> %s",
                [(c, i, c) for (i, c) in pairs],
            )
        self.conn.commit()

    def delete_fact_ids(self, ids: list[int]) -> int:
        if not ids:
            return 0
        cur = self.conn.execute("DELETE FROM facts WHERE id = ANY(%s)", (ids,))
        self.conn.commit()
        return cur.rowcount

    def load_facts(self) -> list[dict]:
        cols = ("id",) + _FACT_COLS
        rows = self.conn.execute(
            f"SELECT {', '.join(cols)} FROM facts ORDER BY id",
        ).fetchall()
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            if d["embedding"] is not None:
                d["embedding"] = np.asarray(d["embedding"], dtype=np.float32)
            out.append(d)
        return out

    # ── meta ────────────────────────────────────────────────────────────

    def meta_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, Jsonb(value)),
        )
        self.conn.commit()

    def meta_get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = %s", (key,),
        ).fetchone()
        return default if row is None else row[0]

    # ── graph: entities / aliases ───────────────────────────────────────

    def ensure_entity(
        self, canonical: str, display: str | None = None,
        etype: str | None = None,
    ) -> int:
        """Upsert by canonical name; first non-null etype wins (soft typing
        is advisory, so a later conflicting hint must not silently retype)."""
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
        self.conn.commit()
        return int(row[0])

    def find_entity(self, name_norm: str) -> dict | None:
        """Resolve a normalized name via canonical first, then aliases."""
        cols = ("id", "canonical", "display", "etype")
        row = self.conn.execute(
            "SELECT id, canonical, display, etype FROM entities "
            "WHERE canonical = %s",
            (name_norm,),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT e.id, e.canonical, e.display, e.etype
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

    def add_alias(self, alias_norm: str, entity_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO entity_aliases (alias, entity_id) VALUES (%s, %s)
            ON CONFLICT (alias) DO UPDATE SET entity_id = EXCLUDED.entity_id
            """,
            (alias_norm, entity_id),
        )
        self.conn.commit()

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
        self.conn.commit()

    # ── graph: edges ────────────────────────────────────────────────────

    def upsert_edge(
        self, src_id: int, relation: str, dst_id: int, *,
        confidence: float = 0.8, origin: str | None = None,
    ) -> dict:
        """Insert or re-assert. Re-assertion bumps confidence (+0.05,
        capped 0.99), revives a superseded edge, and keeps the stronger
        origin claim if the new call omitted one."""
        row = self.conn.execute(
            """
            INSERT INTO edges
              (src_id, relation, dst_id, confidence, origin, asserted_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (src_id, relation, dst_id) DO UPDATE SET
              confidence = LEAST(
                0.99, GREATEST(EXCLUDED.confidence, edges.confidence + 0.05)),
              origin = COALESCE(EXCLUDED.origin, edges.origin),
              superseded_at = NULL,
              asserted_at = EXCLUDED.asserted_at
            RETURNING id, confidence
            """,
            (src_id, relation, dst_id, confidence, origin, time.time()),
        ).fetchone()
        self.conn.commit()
        return {"id": int(row[0]), "confidence": float(row[1])}

    def supersede_edge(self, src_id: int, relation: str, dst_id: int) -> bool:
        cur = self.conn.execute(
            """
            UPDATE edges SET superseded_at = %s
            WHERE src_id = %s AND relation = %s AND dst_id = %s
              AND superseded_at IS NULL
            """,
            (time.time(), src_id, relation, dst_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def load_graph(self) -> dict:
        """Whole live graph (entities + aliases + non-superseded edges) —
        small by design, loaded per query for on-read inference."""
        ent_cols = ("id", "canonical", "display", "etype")
        entities = [
            dict(zip(ent_cols, r)) for r in self.conn.execute(
                "SELECT id, canonical, display, etype FROM entities "
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
