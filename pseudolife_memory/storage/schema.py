"""Schema v11 DDL — entries / episodes / meta / cortex facts / world facts /
lessons + outcome signals / graph tables.

Everything is ``CREATE TABLE IF NOT EXISTS`` so :func:`ensure_schema` is
idempotent and safe to run on every daemon start. The graph tables
(entities / entity_aliases / relations / edges) are created in Phase 1 so
the schema is complete, but only consumed from Phase 2 onward.

The ``vector`` extension is REQUIRED. Apache AGE is no longer used or probed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SCHEMA_META_VERSION = 25

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
  closed_by_new_start BOOLEAN NOT NULL DEFAULT FALSE,
  session_key TEXT,
  parent_id TEXT
);

CREATE TABLE IF NOT EXISTS entries (
  id BIGSERIAL PRIMARY KEY,
  band TEXT NOT NULL,
  text TEXT NOT NULL,
  embedding vector(1024) NOT NULL,
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
-- v22: the UNIQUE(src_id, relation, dst_id) constraint index covers
-- src_id-leading lookups, but dst_id-only lookups (merge_entity's
-- dst-side dedup/repoint, any "what points to X" traversal) had no
-- supporting index and fell back to a sequential scan.
CREATE INDEX IF NOT EXISTS edges_dst_idx ON edges (dst_id);

CREATE TABLE IF NOT EXISTS edge_proposals (
  id BIGSERIAL PRIMARY KEY,
  src_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  relation TEXT NOT NULL REFERENCES relations(name),
  dst_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  confidence REAL NOT NULL,
  similarity REAL,
  rationale TEXT,
  source TEXT NOT NULL DEFAULT 'deep-dream',
  created_at DOUBLE PRECISION NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  UNIQUE (src_id, relation, dst_id)
);

CREATE TABLE IF NOT EXISTS entity_proposals (
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  into_id BIGINT REFERENCES entities(id) ON DELETE CASCADE,
  score REAL,
  reason TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS entity_proposals_merge_uq ON entity_proposals
  (LEAST(entity_id, into_id), GREATEST(entity_id, into_id)) WHERE kind = 'merge';
CREATE UNIQUE INDEX IF NOT EXISTS entity_proposals_junk_uq ON entity_proposals
  (entity_id) WHERE kind = 'junk';

-- v24 (freshness policy input): one kind per entity_norm -- artifact |
-- system | concept. Keyed on entity_norm, NOT entity_id: that is what
-- cortex slots key on, a third of cortex entities have no graph node at
-- all, and a graph merge would otherwise silently retarget the kind.
CREATE TABLE IF NOT EXISTS entity_kinds (
  entity_norm TEXT PRIMARY KEY,
  kind        TEXT NOT NULL,
  origin      TEXT NOT NULL,
  confidence  REAL,
  decided_at  DOUBLE PRECISION NOT NULL
);

-- v20 (2026-07-02 review fix 3): human-dismissed duplicate findings. The
-- duplicate analyzer is stateless token-Jaccard, so its false positives
-- (postgres vs postgres.py) re-flagged forever; a dismissed pair is stored
-- normalized with a_norm < b_norm and skipped on every later analysis. Kept
-- by name (no entity FK) so a dismissal survives entity churn.
CREATE TABLE IF NOT EXISTS dismissed_pairs (
  a_norm TEXT NOT NULL,
  b_norm TEXT NOT NULL,
  dismissed_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (a_norm, b_norm)
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
  embedding vector(1024),
  entity_id BIGINT REFERENCES entities(id),
  object_entity_id BIGINT REFERENCES entities(id),
  -- Read-time currency (schema v23), same curve as world_facts. Defaults to
  -- 'evergreen' — NOT 'volatile' like world_facts — because personal facts
  -- are mostly durable; defaulting to volatile would silently re-rank an
  -- existing bank on an unmeasured assumption. A writer marks the transient
  -- ones (deployment status, "current" version) and only those decay.
  freshness_class TEXT NOT NULL DEFAULT 'evergreen'
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
  embedding vector(1024),
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
  embedding vector(1024),
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

-- v12 community tables (graph-insight Track B). Persisted per dream sweep;
-- entity_communities links each entity to its community (CASCADE on entity delete).
CREATE TABLE IF NOT EXISTS communities (
  id          BIGINT PRIMARY KEY,
  label       TEXT,
  size        INTEGER NOT NULL,
  cohesion    DOUBLE PRECISION NOT NULL,
  computed_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_communities (
  entity_id    BIGINT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
  community_id BIGINT NOT NULL,
  computed_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS entity_communities_cid_idx ON entity_communities (community_id);

-- v13 engram cross-index (provenance-as-link). Keyed on the STABLE canonical
-- slot (entity_norm, attribute_norm) — NOT facts.id, which is regenerated on
-- every cortex snapshot save. entry_id keeps a CASCADE FK (entries.id is stable),
-- so an evicting episode auto-removes its traces.
CREATE TABLE IF NOT EXISTS memory_traces (
  entity_norm    TEXT   NOT NULL,
  attribute_norm TEXT   NOT NULL,
  entry_id       BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  created_at     DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (entity_norm, attribute_norm, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_traces_entry_idx ON memory_traces (entry_id);

-- v16 additive: per-entity project/topic attribution. Denormalized cache of
-- entity_id -> source(s). 'derived' rows are recomputed from
-- facts.entity_id ⋈ memory_traces ⋈ entries; 'manual' rows are user overrides
-- and are never auto-overwritten.
CREATE TABLE IF NOT EXISTS entity_sources (
  entity_id  BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source     TEXT   NOT NULL,
  count      INTEGER NOT NULL DEFAULT 1,
  origin     TEXT   NOT NULL DEFAULT 'derived',
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (entity_id, source)
);
CREATE INDEX IF NOT EXISTS entity_sources_source_idx ON entity_sources (source);
"""

# The dimension every embedding column is declared at (schema v25). Not
# derived from EmbeddingConfig on purpose: ensure_schema must refuse based
# on what THIS BUILD's schema.py demands, independent of whatever model a
# caller happens to have configured.
_EXPECTED_EMBEDDING_DIM = 1024


def _refuse_on_embedding_dim_mismatch(cur) -> None:
    """Refuse to start the write path if the live bank's embedding columns
    were declared at a different dimension than this build's schema.

    ``ensure_schema`` is additive-only — ``CREATE TABLE IF NOT EXISTS`` /
    ``ADD COLUMN IF NOT EXISTS`` never touch an existing column's TYPE, and
    that must stay true for dimension changes too: a pgvector dimension
    change needs every existing row RE-EMBEDDED (a batch job through the
    real ``EmbeddingPipeline``), not a DDL statement. Without this guard, a
    daemon started against an old-dimensioned bank after a model swap would
    either silently keep running against a bank that can never store a
    correctly-shaped vector again, or — worse, if ``ensure_schema`` ever
    grew an in-place ALTER — half-migrate the four embedding tables at
    startup with no re-embedding, corrupting the bank. Refusing is the only
    safe additive behaviour; the actual migration
    (``ops/migrate_embeddings.py``) is a deliberate, human-gated,
    backup-first step, never something the daemon does on its own at boot.

    Checked on ``entries.embedding`` only — all four embedding columns move
    together in lockstep (one model, one dimension), so one column is a
    sufficient sentinel for the other three.

    ``atttypmod`` on a pgvector column IS the declared dimension verbatim
    (unlike e.g. ``varchar``, which offsets it) — confirmed against a live
    server: ``vector(1024)`` reports ``atttypmod = 1024``. Resolved via
    ``to_regclass`` rather than a bare ``'entries'::regclass`` cast so a
    missing table (fresh install — nothing to refuse) returns zero rows
    instead of raising.
    """
    cur.execute(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid = to_regclass('public.entries') "
        "AND attname = 'embedding' AND attnum > 0 AND NOT attisdropped"
    )
    row = cur.fetchone()
    if row is None:
        return  # fresh install: entries doesn't exist yet
    live_dim = row[0]
    if live_dim > 0 and live_dim != _EXPECTED_EMBEDDING_DIM:
        raise RuntimeError(
            f"Refusing to start: entries.embedding is vector({live_dim}) "
            f"but this build's schema expects vector({_EXPECTED_EMBEDDING_DIM}). "
            "ensure_schema is additive-only and will NOT alter vector "
            "dimensions in place -- that would either half-migrate four "
            f"tables at startup or write {_EXPECTED_EMBEDDING_DIM}-d vectors "
            f"into a {live_dim}-d column, corrupting the bank. Run the "
            "human-gated migration first: `python ops/migrate_embeddings.py` "
            f"(backs up, requires the daemon stopped, re-embeds every row "
            f"through the real embedder, and moves all four embedding "
            f"columns from vector({live_dim}) to vector({_EXPECTED_EMBEDDING_DIM})). "
            "Never run the daemon against a bank you have not migrated."
        )


def ensure_schema(conn) -> dict:
    """Create extensions + tables idempotently. Returns capability flags.

    ``vector`` is required (raises if unavailable). Records
    ``schema_version`` in ``meta`` (upsert to the current value, so an
    upgraded bank reports its real version, not the first-init one).

    Refuses outright (``RuntimeError``, before any DDL runs) if the live
    bank's ``entries.embedding`` is already dimensioned but at a different
    size than this build expects — see :func:`_refuse_on_embedding_dim_mismatch`.
    """
    # conn.transaction(): one atomic DDL transaction whether the caller's
    # connection is autocommit (the daemon's storage conn, H4) or classic
    # (test fixtures) — psycopg begins/commits for real on an idle
    # connection in either mode.
    with conn.transaction(), conn.cursor() as cur:
        # Checked FIRST, before any DDL (including the lock/statement
        # timeouts below) — a dim mismatch must abort before touching
        # anything, not merely before the CREATE TABLE calls.
        _refuse_on_embedding_dim_mismatch(cur)
        # Bound every DDL statement so a stray lock holder surfaces as an
        # error instead of an indefinite hang (the v0.1 lesson, applied to
        # the new storage layer). SET LOCAL: these guards are for THIS
        # transaction only — a plain SET leaked the 30s statement_timeout
        # into the whole session, so every later runtime query silently ran
        # under a 30s abort (2026-07-02 review fix).
        cur.execute(
            "SET LOCAL lock_timeout = '5s'; "
            "SET LOCAL statement_timeout = '30s';")
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(SCHEMA_SQL)
        # v13 additive: reinforcement counter on entries (tracks how many times
        # the dream has re-linked an episode via memory_traces).
        cur.execute(
            "ALTER TABLE entries ADD COLUMN IF NOT EXISTS reinforcements "
            "INTEGER NOT NULL DEFAULT 0"
        )
        # v14 additive: per-session idempotency key for hook-driven episodes.
        cur.execute(
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS session_key TEXT"
        )
        # v15 additive: parent episode id for nested sub-episodes.
        cur.execute(
            "ALTER TABLE episodes ADD COLUMN IF NOT EXISTS parent_id TEXT"
        )
        # v21 additive: decision audit on entity proposals (who folded /
        # rejected a near-duplicate, and when) — the human's post-hoc window
        # onto deep-dream merge triage. Stamps live on the proposal row while
        # it exists; the durable audit is merge_decisions below, because an
        # ACCEPTED merge deletes the folded entity and the proposal row
        # CASCADEs away with it.
        cur.execute(
            "ALTER TABLE entity_proposals ADD COLUMN IF NOT EXISTS decided_by TEXT"
        )
        cur.execute(
            "ALTER TABLE entity_proposals ADD COLUMN IF NOT EXISTS "
            "decided_at DOUBLE PRECISION"
        )
        # v23 additive: read-time currency on personal cortex facts, mirroring
        # world_facts. DEFAULT 'evergreen', not world_facts' 'volatile' — an
        # existing bank of durable project facts must not start decaying on an
        # unmeasured assumption. Backfills every existing row as evergreen,
        # i.e. exactly today's behaviour, so this migration is a no-op until a
        # writer marks a fact transient.
        cur.execute(
            "ALTER TABLE facts ADD COLUMN IF NOT EXISTS freshness_class "
            "TEXT NOT NULL DEFAULT 'evergreen'"
        )
        # v24 additive: per-entity kind, the input to the freshness policy.
        # Keyed on entity_norm (what cortex slots key on), not entity_id --
        # a third of cortex entities have no graph node, and a graph merge
        # would otherwise silently retarget the kind.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS entity_kinds ("
            "entity_norm TEXT PRIMARY KEY, kind TEXT NOT NULL, "
            "origin TEXT NOT NULL, confidence REAL, "
            "decided_at DOUBLE PRECISION NOT NULL)"
        )
        # v21 additive: durable, denormalized merge-decision audit (no FKs —
        # must outlive the entities and proposal rows it describes).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS merge_decisions (
              id BIGSERIAL PRIMARY KEY,
              proposal_id BIGINT,
              entity_display TEXT,
              into_display TEXT,
              status TEXT NOT NULL,
              score REAL,
              reason TEXT,
              decided_by TEXT,
              decided_at DOUBLE PRECISION NOT NULL
            )
            """
        )
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
        # 2026-07-02 zombie sweep: the HNSW index was maintained on every
        # entries insert but nothing ever ran a vector query in SQL — all
        # similarity happens in Python over the hydrated bands. Drop it;
        # recreate consciously if retrieval ever moves into PG.
        cur.execute("DROP INDEX IF EXISTS entries_embedding_idx")
        # v19 (2026-07-02 P1): DB-enforced one-live-row-per-slot for the three
        # canonical stores — the invariant previously lived only in Python
        # (CortexStore._current), so an additive restore could silently create
        # duplicate current rows. Heal pre-existing duplicates first (keep the
        # most recently confirmed, demote the rest — mirroring
        # CortexStore._reindex_current), then add the partial unique indexes.
        # Both steps are cheap no-ops on a clean bank.
        for table, status in (("facts", "current"), ("facts", "contested"),
                              ("world_facts", "current"), ("lessons", "current")):
            cur.execute(
                f"""
                UPDATE {table} SET status = 'superseded',
                       superseded_at = COALESCE(superseded_at,
                                                EXTRACT(EPOCH FROM now()))
                WHERE id IN (
                  SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                      PARTITION BY entity_norm, attribute_norm
                      ORDER BY last_confirmed DESC, id DESC) AS rn
                    FROM {table} WHERE status = %s) d
                  WHERE d.rn > 1)
                """,
                (status,),
            )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS facts_slot_current_uq "
            "ON facts (entity_norm, attribute_norm) WHERE status = 'current'"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS facts_slot_contested_uq "
            "ON facts (entity_norm, attribute_norm) WHERE status = 'contested'"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS world_facts_slot_current_uq "
            "ON world_facts (entity_norm, attribute_norm) "
            "WHERE status = 'current'"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS lessons_slot_current_uq "
            "ON lessons (entity_norm, attribute_norm) WHERE status = 'current'"
        )
        cur.execute(
            """
            INSERT INTO meta (key, value) VALUES ('schema_version', %s::jsonb)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (str(SCHEMA_META_VERSION),),
        )
    return {}
