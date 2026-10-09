-- schema_version 55

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
  slots JSONB NOT NULL DEFAULT '[]',
  -- v35 (write-time label pair, arXiv 2608.01679 + 2608.22752):
  -- authority = the speech act of the text ('directive' | 'observation'
  -- | 'quoted'), distortion_tolerance = how exactly it must survive
  -- consolidation ('constraint' | 'procedural' | 'belief' |
  -- 'preference' | 'episodic'). Both nullable: NULL = observation /
  -- unlabelled, exactly the pre-v35 reading, so the migration is a
  -- no-op on an existing bank. Carried through supersede/consolidate.
  authority TEXT,
  distortion_tolerance TEXT,
  -- v38: durable dream acknowledgement. NULL is reserved for rows written
  -- before this column existed and is classified once at service startup.
  dream_state TEXT DEFAULT 'pending'
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

-- v51: entry-level support for dream edges. Legacy and explicit edges have
-- no rows here and are never inferred to belong to a forgotten entry.
CREATE TABLE IF NOT EXISTS edge_evidence (
  edge_id BIGINT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
  entry_id BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  PRIMARY KEY (edge_id, entry_id)
);
CREATE INDEX IF NOT EXISTS edge_evidence_entry_idx ON edge_evidence (entry_id);

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
-- by name (no entity FK) so a dismissal survives entity churn. Namespaces
-- sharing the table (norm names strip ":" so they never collide):
-- "lesson:<key>" / "world:<key>" slot-pair dismissals (store curation), and
-- since v37 the "junk:<canonical>" SELF-pair — a junk proposal rejected as
-- "keep", written here because the rejected entity_proposals row CASCADEs
-- away with its entity and a re-mint of the same name was re-filed and
-- re-judged as if no verdict existed. Merge rejects write the canonical
-- pair here too (same reason), whoever decided them.
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
  freshness_class TEXT NOT NULL DEFAULT 'evergreen',
  -- v26 (set-valued slots): 'scalar' | 'member'. Partitions the
  -- current-uniqueness constraint below -- a scalar slot keeps its old
  -- one-live-row-per-(entity,attribute) invariant, a member slot instead
  -- dedupes per (entity,attribute,VALUE) so several members can be
  -- concurrently current on the same slot.
  kind TEXT NOT NULL DEFAULT 'scalar',
  -- v26: the member identity the member index dedupes on. NULL on scalar
  -- rows (not part of their uniqueness key).
  value_norm TEXT,
  -- v29 (epistemic stance): the source's own hedge words ("probably",
  -- "unconfirmed", "per the runbook"), kept verbatim and SEPARATE from
  -- value so consolidation cannot silently turn a hedged claim into a
  -- confident canonical fact. NULL = asserted plainly (every pre-v29
  -- row). Reader metadata only — never an input to confidence, ranking,
  -- or supersession.
  stance TEXT,
  -- v35 (write-time label pair): the SOURCE's speech act and fidelity
  -- class, inherited from the entry the dream derived the fact from and
  -- kept through supersession unless a write restates them. NULL =
  -- observation / unlabelled. distortion_tolerance = 'constraint' is
  -- the one label recall ranks on (pinned ahead of cosine when the
  -- query names the entity); neither feeds confidence or supersession.
  authority TEXT,
  distortion_tolerance TEXT
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
-- v44: what the outcome's used_ids became. {"credited", "unmatched",
-- "served_elsewhere"}: entry-id lists from the one labelling statement;
-- {"unchecked", "reason"} when that statement failed. NULL = the outcome
-- named no ids, the retrieval log was off, the row predates v44, or this
-- best-effort write itself failed (counted in the log's write errors).
-- Kept out of portable exports (transfer_cli.EXCLUDED_COLUMNS).
ALTER TABLE outcome_signals ADD COLUMN IF NOT EXISTS used_ids JSONB;

-- v44: one row per memory_lesson_search call: the query, the caller, and
-- the lessons served, by slot key (lesson row ids are regenerated by
-- snapshot saves). Deliberately NOT retrieval_events: the retrieval replay,
-- the telemetry review and the graph ablation re-run every row there as a
-- memory_search. FK-free; pruned with the retrieval log's retention.
CREATE TABLE IF NOT EXISTS lesson_search_events (
  id BIGSERIAL PRIMARY KEY,
  query_text TEXT NOT NULL,
  session_id TEXT,
  episode_id TEXT,
  served JSONB NOT NULL DEFAULT '[]',
  created_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS lesson_search_events_session_idx
  ON lesson_search_events (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS lesson_search_events_created_idx
  ON lesson_search_events (created_at);

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

-- v39 durable source-supersession events. The source entry is deliberately
-- NOT a foreign key: this row exists so a correction warning survives the
-- capacity eviction or explicit deletion that removes entries + live traces.
-- facts.id is equally unsuitable because cortex snapshots regenerate it.
CREATE TABLE IF NOT EXISTS memory_trace_invalidations (
  entity_norm     TEXT             NOT NULL,
  attribute_norm  TEXT             NOT NULL,
  source_entry_id BIGINT           NOT NULL,
  invalidated_at  DOUBLE PRECISION NOT NULL,
  cause           TEXT             NOT NULL,
  PRIMARY KEY (entity_norm, attribute_norm, source_entry_id)
);

-- v41 append-only decisions for explicitly reinstating one retired entry.
-- No FK: the audit must survive later entry eviction or deletion.
CREATE TABLE IF NOT EXISTS entry_reinstatement_decisions (
  operation_id                     UUID PRIMARY KEY,
  entry_id                         BIGINT NOT NULL,
  request_sha256                   TEXT NOT NULL,
  entry_text_sha256                TEXT NOT NULL,
  entry_source_sha256              TEXT NOT NULL,
  prior_superseded_at              DOUBLE PRECISION NOT NULL,
  prior_superseded_by_text         TEXT NOT NULL,
  prior_superseded_by_text_sha256  TEXT NOT NULL,
  evidence_packet_sha256           TEXT NOT NULL,
  reviewer_ids                     JSONB NOT NULL,
  reason                           TEXT NOT NULL,
  decided_by                       TEXT NOT NULL,
  decided_at                       DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS entry_reinstatement_decisions_entry_idx
  ON entry_reinstatement_decisions (entry_id, decided_at DESC);

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

-- v43 durable client-session registrations: one row per session key the
-- daemon registered (the SessionStart hook, or POST /api/episode/start from
-- the stdio shim and the CLI episode hooks). A session root is deleted when
-- it ends holding no entry (prune-on-empty), which left the searches and
-- outcomes of read-only sessions naming nothing; this row is never pruned.
-- started_at is the first registration and never moves; ended_at and
-- end_reason are the most recent close ('end' = SessionEnd or shim exit,
-- 'idle' = the idle reaper), cleared when the session registers again or a
-- store or handle reopens its root.
-- policy_variant is the startup memory policy the hook assigned (NULL on
-- the api path, which serves none); principal is the bearer's principal
-- name (NULL when the registration arrived outside a request). Both, and
-- registered_via, keep their first non-NULL value. episode_ids is a JSON
-- array of every root episode id the session was given, so a row stamped
-- with a pruned root still attributes; start_times is every registration's
-- time (a resumed client starts a new shim near the latest one). No FK: it
-- outlives the episodes it names.
CREATE TABLE IF NOT EXISTS client_sessions (
  session_key    TEXT PRIMARY KEY,
  registered_via TEXT NOT NULL,
  principal      TEXT,
  started_at     DOUBLE PRECISION NOT NULL,
  ended_at       DOUBLE PRECISION,
  end_reason     TEXT,
  policy_variant TEXT,
  episode_ids    JSONB NOT NULL DEFAULT '[]',
  start_times    JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS client_sessions_started_idx
  ON client_sessions (started_at);

CREATE TABLE IF NOT EXISTS coordination_agents (
    agent_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    credential_hash TEXT,
    label TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    episode TEXT NOT NULL DEFAULT '',
    capabilities JSONB NOT NULL DEFAULT '{}',
    wake_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at DOUBLE PRECISION NOT NULL,
    last_activity DOUBLE PRECISION NOT NULL,
    lifecycle TEXT NOT NULL DEFAULT 'registered',
    next_sequence BIGINT NOT NULL DEFAULT 0,
    attachment_id TEXT,
    generation BIGINT NOT NULL DEFAULT 0,
    lease_until DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS coordination_agents_scope_idx
    ON coordination_agents (project, task);
CREATE TABLE IF NOT EXISTS coordination_messages (
    message_id TEXT PRIMARY KEY,
    sender_agent_id TEXT NOT NULL REFERENCES coordination_agents(agent_id),
    recipient_agent_id TEXT NOT NULL REFERENCES coordination_agents(agent_id),
    sender_principal TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    text TEXT,
    reply_to TEXT,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    recipient_sequence BIGINT NOT NULL,
    hlc TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    attempt_at DOUBLE PRECISION,
    attempt_generation BIGINT,
    attempts INTEGER NOT NULL DEFAULT 0,
    acknowledged_at DOUBLE PRECISION,
    UNIQUE (recipient_agent_id, recipient_sequence)
);
CREATE INDEX IF NOT EXISTS coordination_messages_pending_idx
    ON coordination_messages (recipient_agent_id, recipient_sequence)
    WHERE acknowledged_at IS NULL;
CREATE INDEX IF NOT EXISTS coordination_messages_sender_time_idx
    ON coordination_messages (sender_agent_id, created_at);
CREATE INDEX IF NOT EXISTS coordination_messages_expiry_idx
    ON coordination_messages (expires_at);
-- v42: when the recipient was first served each message, by either path.
ALTER TABLE coordination_messages ADD COLUMN IF NOT EXISTS first_read_at DOUBLE PRECISION;
-- v42: the board's append-only audit log, one row per mutation, written in
-- the mutation's own transaction. No foreign keys: it outlives the agent and
-- message rows it describes. seq is dense and allocated under a transaction
-- advisory lock; hash = sha256(prev_hash || canonical row). payload is the
-- canonical JSON text that was hashed, kept as TEXT because JSONB would
-- renormalize it.
CREATE TABLE IF NOT EXISTS coordination_events (
    seq BIGINT PRIMARY KEY,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    principal TEXT NOT NULL DEFAULT '',
    agent_id TEXT NOT NULL DEFAULT '',
    recipient_agent_id TEXT,
    project TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    message_id TEXT,
    payload TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    hlc TEXT NOT NULL DEFAULT '',
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS coordination_events_time_idx
    ON coordination_events (created_at);
-- v52: participant pages, exact peer filters and per-message lifecycle reads.
-- Pages seek each direction independently before combining their bounded rows.
CREATE INDEX IF NOT EXISTS coordination_events_send_sender_idx
    ON coordination_events (agent_id, principal, seq) WHERE event='send';
CREATE INDEX IF NOT EXISTS coordination_events_send_recipient_idx
    ON coordination_events (recipient_agent_id, seq) WHERE event='send';
CREATE INDEX IF NOT EXISTS coordination_events_send_pair_idx
    ON coordination_events (agent_id, recipient_agent_id, seq) WHERE event='send';
CREATE INDEX IF NOT EXISTS coordination_events_send_principal_idx
    ON coordination_events (principal, seq) WHERE event='send';
CREATE INDEX IF NOT EXISTS coordination_events_principal_idx
    ON coordination_events (principal, seq);
CREATE INDEX IF NOT EXISTS coordination_events_expire_idx
    ON coordination_events (seq) WHERE event='expire';
CREATE INDEX IF NOT EXISTS coordination_events_message_idx
    ON coordination_events (message_id, event, created_at);
-- v45: named leases on shared resources, one row per name. The row outlives
-- each hold so the fence keeps rising across grants. No foreign keys, like
-- the audit log: prune drops a departed waiter's row itself and never
-- removes a live holder, and the fixtures' TRUNCATE of the agent table must
-- not need a CASCADE.
CREATE TABLE IF NOT EXISTS coordination_leases (
    name TEXT PRIMARY KEY,
    holder_agent_id TEXT,
    holder_principal TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    fence BIGINT NOT NULL DEFAULT 0,
    acquired_at DOUBLE PRECISION,
    expires_at DOUBLE PRECISION,
    expect INTEGER,
    expected_end DOUBLE PRECISION,
    freed_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS coordination_leases_holder_idx
    ON coordination_leases (holder_agent_id) WHERE holder_agent_id IS NOT NULL;
-- v45: one sequence hands out every lease's fences, so a fence never repeats
-- for a name even after prune forgets its row.
CREATE SEQUENCE IF NOT EXISTS coordination_lease_fence;
-- v45: each lease's queue, served in ticket (arrival) order.
CREATE TABLE IF NOT EXISTS coordination_lease_waiters (
    name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    ticket BIGSERIAL,
    principal TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    ttl INTEGER NOT NULL,
    expect INTEGER,
    enqueued_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (name, agent_id)
);
CREATE INDEX IF NOT EXISTS coordination_lease_waiters_order_idx
    ON coordination_lease_waiters (name, ticket);
CREATE INDEX IF NOT EXISTS coordination_lease_waiters_agent_idx
    ON coordination_lease_waiters (agent_id);
-- v45: when an agent's current status stops being true, if it said.
ALTER TABLE coordination_agents ADD COLUMN IF NOT EXISTS status_expires_at DOUBLE PRECISION;
-- v46: a send event's body and a random salt, both outside the row hash. Its
-- hashed payload commits to sha256(salt || body) instead, and not its length,
-- so an operator can redact the body (body and salt NULL, behind a chained
-- redact event), the chain still verifies, and with the salt gone nothing is
-- left to test a guess of a short body against. NULL on every other event,
-- and on sends written before v46, whose body stays inside the hashed
-- payload. Added only when missing: ADD COLUMN IF NOT EXISTS takes an ACCESS
-- EXCLUSIVE lock even when the column exists, and board-audit export/verify
-- hold a read lock on this table for their whole snapshot, so on every
-- daemon start an open export would fail the schema pass at its 5 s lock
-- timeout (review, 2026-09-26).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_events'::regclass
                     AND attname = 'body' AND attnum > 0 AND NOT attisdropped) THEN
        -- IF NOT EXISTS still: two first starts racing both reach here.
        ALTER TABLE coordination_events ADD COLUMN IF NOT EXISTS body TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_events'::regclass
                     AND attname = 'body_salt' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_events ADD COLUMN IF NOT EXISTS body_salt TEXT;
    END IF;
END $$;
-- v47: the subagents an orchestrating session runs under its own address, as
-- [{label, since}]. A subagent shares its parent's shim and so its board
-- identity; the parent names them here instead of giving them addresses.
-- Guarded like the v46 columns, so a routine start takes no ACCESS EXCLUSIVE
-- lock on the table once the column exists.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_agents'::regclass
                     AND attname = 'children' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_agents
            ADD COLUMN IF NOT EXISTS children JSONB NOT NULL DEFAULT '[]';
    END IF;
END $$;
-- v48: one send may reach several recipients (``to: "project:<name>"`` or
-- ``"all"``) under one request id, one row each, so the sender's request key
-- admits the recipient. The unique index is created first, so the table is
-- never without a request key. The pre-v48 key is found by its columns
-- (exactly sender_agent_id and request_id), not by the name Postgres gave
-- it, and dropped only where it exists: a pass over a migrated bank then
-- adds no ALTER TABLE of its own.
CREATE UNIQUE INDEX IF NOT EXISTS coordination_messages_request_idx
    ON coordination_messages (sender_agent_id, request_id, recipient_agent_id);
DO $$
DECLARE
    stale_key name;
BEGIN
    FOR stale_key IN
        SELECT c.conname FROM pg_constraint c
        WHERE c.conrelid = 'coordination_messages'::regclass AND c.contype = 'u'
          AND (SELECT array_agg(a.attname::text ORDER BY a.attname::text)
               FROM pg_attribute a
               WHERE a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey))
              = ARRAY['request_id', 'sender_agent_id']
    LOOP
        EXECUTE format('ALTER TABLE coordination_messages DROP CONSTRAINT %I', stale_key);
    END LOOP;
END $$;
-- v49: the park record, a session's standing statement of why it stopped
-- and what would clear it (maintainer decision 2026-09-28). park_reason
-- NULL means not parked; the other fields describe the park. Guarded like
-- the v46 and v47 columns: one probe, then the ALTER only when missing.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_agents'::regclass
                     AND attname = 'park_reason' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_agents
            ADD COLUMN IF NOT EXISTS park_reason TEXT,
            ADD COLUMN IF NOT EXISTS park_needs TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS park_clear_by TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS park_resume TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS park_expires DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS park_set_at DOUBLE PRECISION;
    END IF;
    -- The wake decision a send returned, kept on the message so a retry
    -- repeats it; NULL on messages sent before v49.
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_messages'::regclass
                     AND attname = 'wake' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_messages ADD COLUMN IF NOT EXISTS wake JSONB;
    END IF;
END $$;
-- v49: every ring the daemon decided at send (rung or nudged), for the
-- caps (per recipient per hour, urgent per sender per hour, the nightly
-- total), the fan-out stagger (ring_at) and the hand-off to the shim
-- (served_at: the recipient's next attach or heartbeat carried it). No
-- foreign keys, like the audit log: a ring outlives the mail it was for.
CREATE TABLE IF NOT EXISTS coordination_wakes (
    wake_id BIGSERIAL PRIMARY KEY,
    recipient_agent_id TEXT NOT NULL,
    sender_agent_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    urgent BOOLEAN NOT NULL DEFAULT FALSE,
    ring_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    served_at DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS coordination_wakes_recipient_idx
    ON coordination_wakes (recipient_agent_id, created_at);
CREATE INDEX IF NOT EXISTS coordination_wakes_sender_idx
    ON coordination_wakes (sender_agent_id, created_at);
CREATE INDEX IF NOT EXISTS coordination_wakes_time_idx
    ON coordination_wakes (created_at);
-- v50: a subagent's link to its parent (maintainer decision 2026-09-30:
-- subagents are their parent's children, not peers). parent_thread is the
-- parent's Codex thread id a native child (collaboration.spawn_agent)
-- registered with, set once at register; NULL on every other row, and a
-- row that has one sends no mail. parent_agent_id is the row that thread
-- resolved to under the same principal, filled at register or when the
-- parent registers later, and cleared when prune removes the parent.
-- Guarded like the v47 and v49 columns: one probe, then the ALTER only
-- when missing, so a routine start takes no ACCESS EXCLUSIVE lock.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_agents'::regclass
                     AND attname = 'parent_agent_id' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_agents
            ADD COLUMN IF NOT EXISTS parent_agent_id TEXT,
            ADD COLUMN IF NOT EXISTS parent_thread TEXT;
    END IF;
END $$;
-- v54: maintainer messages (spec 2026-10-02-maintainer-wake-design.md and
-- its 2026-10-04 addendum). A message's origin ('agent', or 'maintainer'
-- for one the daemon verified from a passkey signature in the Console), the
-- proof kept so a stored message can be re-verified, and when the
-- maintainer withdrew it. Guarded like the v49 and v50 columns.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_messages'::regclass
                     AND attname = 'repudiated_at' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_messages
            ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'agent',
            ADD COLUMN IF NOT EXISTS maintainer_proof JSONB,
            ADD COLUMN IF NOT EXISTS repudiated_at DOUBLE PRECISION;
    END IF;
END $$;
-- v55: the name a row shows on the board (maintainer request 2026-10-02:
-- nearly every row read "claude-code" or "codex", the label being set once
-- at register). name_source says who set it, by precedence: 'agent'
-- (memory_agents update, so a session can correct a stale title) over
-- 'harness' (the shim, from the title the harness already shows) over
-- 'title' (a memory_session_title rename of the row's session); '' means
-- unnamed. name_set_at is when it last changed. harness_name keeps the
-- newest harness title while an agent name stands, so clearing the agent
-- name brings it back. Guarded like the v47, v49 and v50 columns: one
-- probe, then the ALTER only when missing.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'coordination_agents'::regclass
                     AND attname = 'harness_name' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE coordination_agents
            ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS name_source TEXT NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS name_set_at DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS harness_name TEXT NOT NULL DEFAULT '';
    END IF;
END $$;
-- v55: a session retitle names the rows registered under that session.
CREATE INDEX IF NOT EXISTS coordination_agents_episode_idx
    ON coordination_agents (episode) WHERE episode <> '';

CREATE TABLE IF NOT EXISTS principals (
    principal        TEXT PRIMARY KEY,
    token_hash       TEXT UNIQUE,
    tier             TEXT,
    board            BOOLEAN NOT NULL DEFAULT TRUE,
    code_hash        TEXT UNIQUE,
    code_expires_at  DOUBLE PRECISION,
    paired_code_hash TEXT,
    created_at       DOUBLE PRECISION NOT NULL,
    paired_at        DOUBLE PRECISION,
    revoked_at       DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS maintainer_passkeys (
    credential_id TEXT PRIMARY KEY,
    public_key    BYTEA NOT NULL,
    alg           INTEGER NOT NULL,
    sign_count    BIGINT NOT NULL DEFAULT 0,
    label         TEXT NOT NULL,
    enrolled_by   TEXT NOT NULL,
    state         TEXT NOT NULL,
    active_from   DOUBLE PRECISION,
    created_at    DOUBLE PRECISION NOT NULL,
    last_used_at  DOUBLE PRECISION,
    revoked_at    DOUBLE PRECISION,
    revoked_by    TEXT,
    flagged_at    DOUBLE PRECISION
);
CREATE TABLE IF NOT EXISTS maintainer_bootstrap (
    code_hash       TEXT PRIMARY KEY,
    expires_at      DOUBLE PRECISION NOT NULL,
    used_at         DOUBLE PRECISION,
    credential_id   TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS maintainer_nonces (
    nonce      TEXT PRIMARY KEY,
    expires_at DOUBLE PRECISION NOT NULL
);
-- Wrong-code guesses against the live code, which burn it at the limit;
-- added during v54 development, so a bank created before it gains the
-- column here (guarded like the v49 and v50 columns).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = 'maintainer_bootstrap'::regclass
                     AND attname = 'failed_attempts' AND attnum > 0 AND NOT attisdropped) THEN
        ALTER TABLE maintainer_bootstrap
            ADD COLUMN IF NOT EXISTS failed_attempts INTEGER NOT NULL DEFAULT 0;
    END IF;
END $$;
