"""Schema v8 DDL — entries / episodes / meta / cortex facts / graph tables.

Everything is ``CREATE TABLE IF NOT EXISTS`` so :func:`ensure_schema` is
idempotent and safe to run on every daemon start. The graph tables
(entities / entity_aliases / relations / edges) are created in Phase 1 so
the schema is complete, but only consumed from Phase 2 onward.

The ``vector`` extension is REQUIRED; ``age`` is probed and optional —
its absence only disables the Phase 2 Cypher layer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SCHEMA_META_VERSION = 9

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  hint TEXT,
  started_at DOUBLE PRECISION NOT NULL,
  ended_at DOUBLE PRECISION,
  closed_by_new_start BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS entries (
  id BIGSERIAL PRIMARY KEY,
  band TEXT NOT NULL,
  text TEXT NOT NULL,
  embedding vector(384) NOT NULL,
  surprise REAL NOT NULL DEFAULT 0,
  ts DOUBLE PRECISION NOT NULL,
  access_count INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT '',
  superseded_at DOUBLE PRECISION,
  superseded_by_text TEXT,
  last_logical_turn INTEGER,
  -- Denormalized episode stamp (id + title travel with the entry); no FK
  -- so entry inserts never depend on episode-row ordering and episodes
  -- can be pruned independently.
  episode_id TEXT,
  episode_title TEXT,
  tags JSONB NOT NULL DEFAULT '[]',
  slots JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS entries_band_idx ON entries (band);
CREATE INDEX IF NOT EXISTS entries_ts_idx ON entries (ts);
CREATE INDEX IF NOT EXISTS entries_source_idx ON entries (source);
CREATE INDEX IF NOT EXISTS entries_embedding_idx
  ON entries USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS entities (
  id BIGSERIAL PRIMARY KEY,
  canonical TEXT NOT NULL UNIQUE,
  display TEXT NOT NULL,
  etype TEXT,
  created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_aliases (
  alias TEXT PRIMARY KEY,
  entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relations (
  name TEXT PRIMARY KEY,
  description TEXT NOT NULL,
  src_type TEXT,
  dst_type TEXT,
  transitive BOOLEAN NOT NULL DEFAULT FALSE,
  inverse_of TEXT REFERENCES relations(name),
  builtin BOOLEAN NOT NULL DEFAULT FALSE,
  created_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
  id BIGSERIAL PRIMARY KEY,
  src_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  relation TEXT NOT NULL REFERENCES relations(name),
  dst_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  confidence REAL NOT NULL DEFAULT 0.8,
  origin TEXT,
  asserted_at DOUBLE PRECISION NOT NULL,
  superseded_at DOUBLE PRECISION,
  UNIQUE (src_id, relation, dst_id)
);

CREATE TABLE IF NOT EXISTS facts (
  id BIGSERIAL PRIMARY KEY,
  entity TEXT NOT NULL,
  attribute TEXT NOT NULL,
  entity_norm TEXT NOT NULL,
  attribute_norm TEXT NOT NULL,
  value TEXT NOT NULL,
  polarity TEXT NOT NULL DEFAULT '+',
  status TEXT NOT NULL,
  confidence REAL NOT NULL,
  origin TEXT,
  support JSONB NOT NULL DEFAULT '[]',
  provenance JSONB NOT NULL DEFAULT '[]',
  asserted_at DOUBLE PRECISION NOT NULL,
  last_confirmed DOUBLE PRECISION NOT NULL,
  supersedes_value TEXT,
  superseded_by_value TEXT,
  superseded_at DOUBLE PRECISION,
  embedding vector(384),
  entity_id BIGINT REFERENCES entities(id),
  object_entity_id BIGINT REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS facts_slot_idx
  ON facts (entity_norm, attribute_norm, status);

-- World-knowledge cortex (schema v9, additive). Same slot-keyed shape as `facts`
-- so the cortex write/supersede/key-norm logic is reused, but PHYSICALLY SEPARATE
-- for blast-radius isolation (a runaway research ingest can be truncated without
-- touching the user/project `facts`). World provenance/freshness columns hold the
-- per-fact citation (quote + url, NOT the full page) and the read-time decay anchor.
CREATE TABLE IF NOT EXISTS world_facts (
  id BIGSERIAL PRIMARY KEY,
  entity TEXT NOT NULL,
  attribute TEXT NOT NULL,
  entity_norm TEXT NOT NULL,
  attribute_norm TEXT NOT NULL,
  value TEXT NOT NULL,
  polarity TEXT NOT NULL DEFAULT '+',
  status TEXT NOT NULL,
  confidence REAL NOT NULL,
  origin TEXT,                              -- 'source' for v1 (external-but-cited)
  support JSONB NOT NULL DEFAULT '[]',
  provenance JSONB NOT NULL DEFAULT '[]',
  asserted_at DOUBLE PRECISION NOT NULL,
  last_confirmed DOUBLE PRECISION NOT NULL,
  supersedes_value TEXT,
  superseded_by_value TEXT,
  superseded_at DOUBLE PRECISION,
  embedding vector(384),
  -- world provenance + freshness (spec 2026-06-13, D5 quote-not-page)
  source_url TEXT,
  source_quote TEXT,
  retrieved_at DOUBLE PRECISION,
  freshness_class TEXT NOT NULL DEFAULT 'volatile',
  content_hash TEXT,
  source_doc_id BIGINT                      -- nullable; set only for opt-in full-doc corpus
);
CREATE INDEX IF NOT EXISTS world_facts_slot_idx
  ON world_facts (entity_norm, attribute_norm, status);
"""


def ensure_schema(conn) -> dict:
    """Create extensions + tables idempotently. Returns capability flags.

    ``vector`` is required (raises if unavailable); ``age`` is optional —
    returns ``{"age_available": bool}`` so callers can gate the Cypher
    layer. Records ``schema_version`` in ``meta`` (insert-or-keep).
    """
    with conn.cursor() as cur:
        # Bound every DDL statement so a stray lock holder surfaces as an
        # error instead of an indefinite hang (the v0.1 lesson, applied to
        # the new storage layer).
        cur.execute("SET lock_timeout = '5s'; SET statement_timeout = '30s';")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        age_available = True
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS age;")
        except Exception as exc:  # noqa: BLE001
            age_available = False
            conn.rollback()
            logger.info("AGE extension unavailable (%s) — Cypher layer off.", exc)
        cur.execute(SCHEMA_SQL)
        # One-time upgrade: drop the old episode FK only when it's actually
        # present. Guarding avoids taking an ACCESS EXCLUSIVE lock on every
        # init (which could block behind any open transaction on entries).
        cur.execute(
            "SELECT 1 FROM pg_constraint WHERE conname = 'entries_episode_id_fkey'"
        )
        if cur.fetchone() is not None:
            cur.execute(
                "ALTER TABLE entries DROP CONSTRAINT entries_episode_id_fkey"
            )
        cur.execute(
            """
            INSERT INTO meta (key, value) VALUES ('schema_version', %s::jsonb)
            ON CONFLICT (key) DO NOTHING
            """,
            (str(SCHEMA_META_VERSION),),
        )
    conn.commit()
    return {"age_available": age_available}
