"""Schema v10 DDL — entries / episodes / meta / cortex facts / world facts /
lessons + outcome signals / graph tables.

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

SCHEMA_META_VERSION = 11

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

-- Procedural / outcome memory ("lessons", schema v10, additive). Slot-keyed like
-- `facts`, but the slot is (task-type, aspect) and each lesson carries an `outcome`
-- (success|failure|correction) alongside `polarity` (+ do-this / - avoid). Kept
-- PHYSICALLY SEPARATE from `facts`/`world_facts` for blast-radius isolation. Graph-
-- linked like the personal cortex: `entity_id` -> the task-type entity,
-- `object_entity_id` -> the tool/source the lesson is about (the `prefers`/`avoids`
-- edge endpoint). Written solely by the dream (single-writer); see
-- docs/specs/2026-06-20-procedural-outcome-memory-design.md.
CREATE TABLE IF NOT EXISTS lessons (
  id BIGSERIAL PRIMARY KEY,
  entity TEXT NOT NULL,
  attribute TEXT NOT NULL,
  entity_norm TEXT NOT NULL,
  attribute_norm TEXT NOT NULL,
  value TEXT NOT NULL,
  about TEXT,                                 -- the tool/source the lesson is about
  polarity TEXT NOT NULL DEFAULT '+',
  outcome TEXT NOT NULL DEFAULT 'success',   -- success | failure | correction
  status TEXT NOT NULL,
  confidence REAL NOT NULL,
  origin TEXT,
  support JSONB NOT NULL DEFAULT '[]',
  provenance JSONB NOT NULL DEFAULT '[]',     -- contributing episode + signal ids
  asserted_at DOUBLE PRECISION NOT NULL,
  last_confirmed DOUBLE PRECISION NOT NULL,
  supersedes_value TEXT,
  superseded_by_value TEXT,
  superseded_at DOUBLE PRECISION,
  embedding vector(384),
  entity_id BIGINT REFERENCES entities(id),
  object_entity_id BIGINT REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS lessons_slot_idx
  ON lessons (entity_norm, attribute_norm, status);

-- In-session outcome signals: a cheap, append-only log the dream drains into
-- lessons. `consumed_at` is the dream's drain cursor (NULL = pending). Never a
-- user-visible memory; pruned by age so it can't grow unbounded when no extractor
-- is configured to synthesise lessons.
CREATE TABLE IF NOT EXISTS outcome_signals (
  id BIGSERIAL PRIMARY KEY,
  task TEXT NOT NULL,
  outcome TEXT NOT NULL,                      -- success | failure | correction
  about TEXT,
  detail TEXT,
  polarity TEXT,
  origin TEXT,
  episode_id TEXT,
  created_at DOUBLE PRECISION NOT NULL,
  consumed_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS outcome_signals_pending_idx
  ON outcome_signals (consumed_at, created_at);

-- v11 writer-aware temporal/provenance stamp (additive; backfilled from
-- asserted_at). tx_time = wall-clock record time (DISPLAY only); valid_time =
-- event time (when it became true); (hlc_phys, hlc_logical) = the ordering
-- authority (a hybrid logical clock, immune to wall-clock steps); writer_id /
-- session_id = who wrote this version; version = per-slot OCC counter (dormant
-- until storage.write_mode='occ'). See
-- docs/specs/2026-06-21-writer-aware-temporal-memory-design.md.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['facts','world_facts','lessons','edges'] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tx_time DOUBLE PRECISION', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS valid_time DOUBLE PRECISION', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS hlc_phys BIGINT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS hlc_logical INT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS writer_id TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS session_id TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS version INT NOT NULL DEFAULT 1', t);
    EXECUTE format('UPDATE %I SET tx_time = asserted_at WHERE tx_time IS NULL', t);
    EXECUTE format('UPDATE %I SET valid_time = asserted_at WHERE valid_time IS NULL', t);
    EXECUTE format('UPDATE %I SET writer_id = ''legacy'' WHERE writer_id IS NULL', t);
  END LOOP;
END $$;
"""


def ensure_schema(conn) -> dict:
    """Create extensions + tables idempotently. Returns capability flags.

    ``vector`` is required (raises if unavailable); ``age`` is optional —
    returns ``{"age_available": bool}`` so callers can gate the Cypher
    layer. Records ``schema_version`` in ``meta`` (upsert to the current value,
    so an upgraded bank reports its real version, not the first-init one).
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
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (str(SCHEMA_META_VERSION),),
        )
    conn.commit()
    return {"age_available": age_available}
