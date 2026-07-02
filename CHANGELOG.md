# Changelog

All notable changes to PseudoLife-MCP are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed (2026-07-02 review, final item — MCP tool-surface consolidation)
- **BREAKING: the MCP surface shrank from 55 tools to 32** (the manifest is
  agent context every session: description payload dropped ~37.0k → ~15.0k
  chars, ~60%). Three verb-dispatched tools replace fifteen:
  - `memory_dream(action=...)` — `status` / `pull` / `commit` / `run` /
    `deep` (replaces `memory_dream_status/pull/commit/run` +
    `memory_deep_dream`).
  - `memory_forget(scope=...)` — `memory` / `fact` / `world` / `lesson`
    (replaces `memory_delete`, `memory_fact_forget`, `memory_world_forget`,
    `memory_lesson_forget`). Scope `memory` now returns a structured
    `{error: "filter_required"}` on a filterless call instead of a raw
    ToolError.
  - `memory_graph_review(action=...)` — `list` / `propose` / `accept_link` /
    `reject_link` / `accept_merge` / `accept_junk` / `reject_entity`
    (replaces `memory_graph_propose_links` + the five accept/reject tools;
    `list` newly exposes `service.graph_review` over MCP).
- **Removed from MCP** (Console REST + CLI cover them; service methods and
  `/api` routes unchanged): `memory_facts`, `memory_world_facts`,
  `memory_lessons`, `memory_list_sources`, `memory_list_tags`,
  `memory_episode_list`, `memory_communities`, `memory_digest`,
  `memory_briefing` (the SessionStart hook uses `pseudolife-mcp briefing`),
  `memory_path` (use `memory_graph(to=...)`), `memory_save` (autosave loop +
  exit flush already cover durability).
- **Every remaining docstring rewritten terse** — first line says what the
  tool does, when-to-use guidance kept only where it changes behaviour.
  `tests/test_tool_consolidation.py` pins the budget (≤1600 chars/tool,
  ≤18k total) plus dispatch/validation contracts for the three merged tools.
- Core-tier membership (`PSEUDOLIFE_MCP_TOOLSET=core`) is unchanged — all 15
  core tools kept their names, as did every tool referenced by the global
  CLAUDE.md workflow. `/dream` command and deep-dream runbook updated to the
  new verbs.

### Fixed/Changed (2026-07-02 review P3 — surface polish + zombie sweep)
- **Tokenless `/api` is now browser-hardened** (review H2, live exposure —
  the daemon runs without a token): foreign `Origin` → 403 (CSRF, covers
  bodyless POSTs), foreign `Host` → 403 (DNS rebinding), and any POST with
  a body must be `application/json` → 415 (a cross-site form can't send
  that without a failing CORS preflight). Non-browser clients send neither
  header and pass; with a token set the host gates are skipped (the
  Authorization header already proves intent, so LAN use stays legitimate).
- **Console Stream view repaired**: search/recent entries now carry the
  storage row `id` (the engram-trace button finally renders live, and
  agents can pair hits with `memory_get`/`memory_reinforce`); the "Explain
  ranking" drawer reads the real trace keys (`name`/candidate lists/
  `text_preview`) instead of fixture-invented ones that rendered
  "undefined" and "[object Object]" in production.
- **Fixture-vs-serializer contract test** (`tests/test_fixture_contract.py`)
  pins the exact keys the Stream view consumes against both the real trace
  and the devserver fixtures — fixture drift now fails CI instead of QA.
- **ReferenceBank similarity math**: ChromaDB cosine distance is `1 − cos`,
  so similarity is `1 − dist` — the old `1 − dist/2` scored orthogonal
  chunks 0.5, above the retrieval floor, appending unrelated documents to
  essentially every search once any document existed.
- **BM25 tokenizer keeps standalone integers** (`port 8080`, `error 404`,
  `RTX 4090`) — the numeric pattern required a dot, silently gutting the
  exact-token channel for the very tokens it exists to catch.
- **Zombie sweep**: removed the never-called `ContrastiveUpdater` /
  `ContextBuilder` daemon wiring, the dead `AuthHealthASGI` wrapper, the
  chat-product config blocks (`backend`/`claude`/`gemini`/`lmstudio`) and
  `HydeConfig`; `NLIConfig.enabled` now defaults False with an honest
  "not wired" docstring; the HNSW index on `entries.embedding` is dropped
  (maintained on every insert, queried by nothing — similarity runs in
  Python over the hydrated bands).
- **Session titles no longer mis-attribute on POSIX** (found by CI run #1):
  a Windows-style client cwd parsed as one relative segment on Linux, so
  the git walk could title a session after the daemon's own repo.

### Added (2026-07-02 review P2 — quality infrastructure)
- **CI (GitHub Actions).** `.github/workflows/ci.yml` runs the full suite on
  every master push and PR: pgvector/pg16 service container, CPU-torch
  install mirroring the daemon image, cached pip + HuggingFace models, then
  the documented offline invocation. Budget-guarded for a free-plan private
  repo: master+PR triggers only, cancel-in-progress concurrency, no
  artifacts (warm run ≈ 4-6 min of the 2,000 free minutes/month).
- **Retrieval golden set** (`tests/test_retrieval_golden.py`): 50 realistic
  memory/paraphrase-query pairs asserting recall@5 ≥ 0.92 and MRR ≥ 0.85 on
  the dense path plus top-3 ≥ 0.85 on BM25-fused identifier queries
  (measured baseline: 1.000 / 0.990 / 1.000) — the first thing on master
  that can catch a *ranking* regression, in under a second.
- **`ops/restore.ps1`** — the restore path is now a rehearsed procedure, not
  a code comment. Default mode restores the newest backup into a scratch
  database, reports per-table row counts against the live bank, and drops
  the scratch (live bank untouched); `-Apply` does the real restore with a
  pre-restore safety dump, daemon stop/start, and a health gate. Rehearsed
  2026-07-02 against the latest backup: PASSED.
- **Off-disk backup mirror**: `ops/backup.ps1` copies each artifact to
  `PSEUDOLIFE_BACKUP_MIRROR` (or `-MirrorDir`) with the same retention when
  set — point it at a folder on another physical disk. Mirror failure warns
  but never aborts (the primary backup already succeeded).

### Changed (2026-07-02 review H4 — autocommit connection)
- **Reads no longer leave the shared connection idle-in-transaction.** The
  storage connection runs autocommit; every mutation opens an explicit
  psycopg transaction block (`_txn` → `conn.transaction()`, nesting degrades
  to savepoints). Pre-fix, a bare read opened an implicit transaction that
  stayed open until the next mutator committed — pinning the xmin horizon
  overnight (blocking autovacuum) and holding ACCESS SHARE locks that
  blocked any concurrent DDL (the root cause of the test-suite
  lock-timeout ordering flake). `ensure_schema` now wraps its DDL in one
  `conn.transaction()` block so it stays atomic under both connection modes.

### Changed (2026-07-02 review P1 — per-slot persistence, schema v19)
- **The full-table snapshot rewrite is gone from the write path.** Every
  cortex/world/lesson write used to `DELETE FROM <table>` and reinsert every
  row (embeddings included) — O(claims × total rows) per dream sweep,
  permanent id churn and autovacuum pressure, and a structural blocker for
  the dormant OCC seam. The stores now track `dirty_slots`; saves persist
  only the mutated `(entity_norm, attribute_norm)` slots in one transaction
  (`replace_slot_facts` / `_world_facts` / `_lessons`,
  `sync_cortex_slots` / `sync_world_slots` / `sync_lesson_slots`). The
  supersession log + dream cursor ride a `meta_dirty` flag instead of being
  rewritten every save. Full snapshots remain for explicit `memory_save`,
  exit flush, and restore/migration — a belt-and-braces resync.
- **Schema v19:** partial unique indexes enforce one `current` row per slot
  on facts/world_facts/lessons (+ at-most-one `contested` on facts) — the
  invariant previously lived only in Python, so an additive `restore_from_pt`
  could silently create duplicate current rows. `ensure_schema` heals
  pre-existing duplicates first (keeps the most recently confirmed, demotes
  the rest — mirroring `CortexStore._reindex_current`).
- **HLC re-seeds from stored stamps at hydrate** (`hlc.observe` of the
  bank's high-water mark): a wall-clock step-back across restarts (NTP,
  resume) no longer lets history outrank new writes and park user
  corrections as contenders.
- **Auto-promoted facts are stamped** (`_promote_slots` now passes HLC +
  writer/session like `cortex_write`): unstamped rows could never supersede
  stamped ones and were retro-labeled `writer_id='legacy'` by the v11
  backfill on every boot.

### Fixed (2026-07-02 review P0 — six correctness fires)
- **MCP tools no longer block the daemon's event loop.** The SDK invokes sync
  tools inline on the uvicorn loop, so one long call (`memory_dream_run`,
  `document_ingest`, first-call model init) froze every other session,
  `/health`, and the Console — a Docker healthcheck could kill the daemon
  mid-dream. Every registered tool is now an async wrapper that dispatches
  its sync body via `anyio.to_thread.run_sync` (one change in `_tool()`;
  module-level fns stay sync for the Console/tests). Contextvars (writer /
  session attribution) propagate into the worker thread.
- **Postgres reconnect + honest `/health`.** A PG restart used to poison the
  daemon permanently (single connection, no reconnect anywhere) while
  `/health` — which never touched the DB — kept saying "ok". `storage.conn`
  is now a heal-on-next-use property; `/health` pings the DB on a dedicated
  short-lived connection and reports 503 `status:degraded` when it's
  unreachable. `_txn` rollback on a dead connection no longer masks the
  original exception. `ensure_schema`'s DDL timeouts are now `SET LOCAL` —
  the old session-wide `SET` silently capped every runtime query at 30s.
- **`access_count` now counts returned results, not candidates.** Bands
  bumped every band-local top-k candidate (up to 8 per band per query,
  pre-filter), corrupting promotion, MTT retention, and eviction scoring at
  the source. The bump moved to the final merged top-k in `cms.retrieve`.
- **Eviction prefers superseded entries.** A correction arrives with
  near-zero surprise while the stale fact it replaced keeps a decayed-but-
  larger one, so surprise-driven eviction destroyed corrections and kept the
  stale facts. Superseded entries now score 0.05× — always the cheaper loss.
- **Graph ingestion gated at the source** (the junk root cause, previously
  patched detection-side only): dream relations drop endpoints matching the
  known junk classes (`junk_name_reason`: concat-artifacts, bare numbers,
  status words) before entity creation; fact-write subject nodes get the same
  gate; `dream.min_relation_confidence` default 0.0 → **0.2** (hard
  type-violations score ≤0.175 and are now dropped, not written-then-cleaned);
  and `upsert_edge(revive=False)` on the dream path makes human removals
  sticky — an agent re-assertion no longer resurrects a superseded edge.
- **Dream poison-pill quarantine + idempotent re-dreams.** A deterministically
  failing entry used to stall consolidation forever (same batch retried every
  sweep) while each retry re-confirmed the batch prefix, ratcheting agent-
  guess confidence toward ~0.98. Now: three strikes per entry → quarantine
  (cursor advances past it), and an already-traced (slot, source-entry) pair
  is skipped on re-extraction instead of re-confirmed.
- **User config.yaml keys are respected.** `_apply_mcp_defaults` clobbered
  five knobs unconditionally after load (`surprise_threshold`,
  `meta_filter.enabled`, `recency_base_half_life_s`, `traces.retention_boost`,
  `embedding.batch_size`) — the YAML knobs were dead. Defaults now overlay
  only keys the user left unset; `load_config` also gained the missing
  `memory.traces` / `memory.deep_dream` sections.
- **Lesson signals survive empty synthesis.** `synthesize_lessons` consumed
  the outcome-signal queue even when the extraction wrote nothing — silently
  losing the only feeder for procedural memory. Signals are now consumed only
  when at least one lesson landed.

### Added
- **Session-scoped episodes (correct attribution + clean names).** Episodes are now
  keyed to a **stable per-session id** instead of a single global `current_id`, so a
  new session (e.g. a different project) no longer auto-closes another's open episode
  and each `memory_store` is stamped to *its own* session's episode even under
  concurrency (`EpisodeManager` tracks one open episode per `session_key`). The session
  id is the transport's `mcp-session-id` — **stable per session for a direct-HTTP
  client** (the daemon's shipped transport), or a stdio shim's `X-PL-Session`
  (`writer_context` prefers it). **Lifecycle is daemon-owned:** because a direct-HTTP
  client has no shim/hook in the path, the daemon **lazily opens** a session episode on
  the first store of a new session id (so empty sessions never create a husk) and an
  **idle reaper** closes it once inactive — firing the end-of-session dream, or pruning
  it if empty (`PSEUDOLIFE_SESSION_IDLE_SECONDS`, default 30 min). The
  `SessionStart`/`SessionEnd` `episode-start`/`episode-end` hooks are therefore obsolete
  (removed; the legacy CLI + shim path remain for stdio clients). Titles are
  `{project} - {YYYY-MM-DD HH:MM}` (shim, from cwd) or `session - {YYYY-MM-DD HH:MM}`
  (daemon, generic — direct-HTTP carries no project signal; set `TZ` in `ops/.env` for
  local-time titles). New `storage.delete_episode` +
  `service.episode_prune_empty(include_open=False)` + `POST /api/episodes/prune` provide
  a one-shot cleanup for the empty/spurious husks the old single-pointer model
  accumulated. New `memory_session_title(title)` tool lets an agent name its
  session episode (since the daemon can't see the client's project dir); the
  shim no longer titles GUI-client sessions after a system dir (`system32` →
  generic `session`).
- **Atlas review queue: granular per-item bulk actions.** The `dubious_edge` (Prune),
  `unattributed` (Assign) and `test_artifact` (Delete) findings — previously
  all-or-nothing over the whole list — now render a filterable, capped-scroll checkbox
  list with "select all (filtered)" / "clear" and a live count on the action button
  (opt-in: nothing selected by default), so you act on exactly the chosen subset. The
  `orphan` finding is now actionable too (Delete + Assign on the selection). Pure
  frontend (`atlas_review.js` `selectableList`) — the findings already carried their
  full lists and the handlers already post per item.
- **Atlas review queue: entity provenance.** New `GET /api/graph/entity-provenance`
  (`service.entity_provenance` + `storage.entries_for_entity`) returns an entity's
  project attribution (`entity_sources`: source · count · origin) and the MIRAS
  source entries behind its facts (band · source · ts · text), bridging
  `facts.entity_id → entity_norm → memory_traces → entries`. In the Atlas Review
  panel every entity name is now clickable to lazy-load a provenance drawer, so a
  human can judge a merge/junk/link finding from real evidence, not names alone.
  (Source entries carry no user/action/agent tier — that lives on facts/edges — so
  the drawer shows band + `entity_sources` origin, not a per-entry tier.)
- **Session-start briefing (P1.7).** New `memory_briefing` tool + `pseudolife-mcp
  briefing` CLI assemble a "what your memory is unsure about" (graph surprises +
  questions) + "lessons from past work" (avoid/prefer) block. Wire the CLI to a
  SessionStart hook (README) to auto-inject it; it never auto-starts the daemon
  and prints nothing on a cold bank.
- **Easy hook install + safe updates.** `pseudolife-mcp briefing --hook-json`
  emits the SessionStart `additionalContext` payload; `ops/install-hook.ps1` wires
  it idempotently alongside existing hooks (backs up `settings.json` first);
  `ops/update.ps1` does a backup-first, daemon-only (`--no-deps`) rebuild that
  never touches Postgres/the extractor or runs `down -v`.

### Changed
- **Dream cadence: faster post-activity consolidation.** `memory.dream.idle_seconds`
  default 1800 → 600, so the cortex consolidates ~10 min after you go quiet (still
  never mid-session — any store resets idle). The quiescence gate logic is
  unchanged. The README "Dreaming" section now documents the concrete cadence
  (8 / 600s / 600s, daemon-only) and the on-demand `memory_fact_set` /
  `memory_dream_run` paths.
- **Tool-surface gate + redundancy trim.** `PSEUDOLIFE_MCP_TOOLSET=core` exposes a
  lean 15-tool core set (default `full` = unchanged). Folded `memory_trace` into
  `memory_search(explain=True)` and dropped `get_neighbors` (its `relation_filter`
  moved onto `memory_graph`); `memory_path` retained. 48 → 46 tools at the time
  of this change (the surface has since grown again with the deep-dream /
  entity-consolidation additions below — see README for the current count).
- **Retention bench made honest (P1.6).** `evals/retention_bench.py` now models a
  heavy-tailed reinforcement workload with `access_count` coupled to reinforcement
  (reinforcing *is* accessing). The honest re-derivation keeps `retention_boost=1.0`
  (the largest boost with ~no recency displacement) but shows it's a modest nudge on
  top of the automatic access-coupling — not the dramatic knee the prior synthetic
  bench implied. Default unchanged.
- **Right-sized the continuum bands.** The default `continuum` preset's total
  capacity drops 44,000 → ~5,250 (e.g. `slow` 8000→1500), all still well above a
  personal bank's fill — so eviction/curation engages in ~1 year (the `slow`
  band) instead of ~decades, with no data loss on existing personal banks. Raise
  the caps (or use `preset: custom`) for high-volume / multi-agent deployments.

### Fixed
- **Atlas review queue rendered deep-dream findings unusably.** The panel predated
  the deep-dream proposal shapes: `merge_candidate` (data in `f.merges`),
  `proposed_link` (`f.links`), and `junk_candidate` (`f.entities` as objects) showed
  no detail or literal `[object Object]`, and their action buttons were dead (read
  `f.entities` → `[undefined, undefined]`) or posted malformed bodies. The renderer
  (`atlas_review.js`) now understands all finding shapes and surfaces the
  already-computed signals (jaccard / similarity / confidence / reason / rationale);
  the handler (`views/atlas.js`) dispatches per item to the id-keyed
  `accept-entity-merge` / `accept-entity-junk` / `reject-entity-proposal` /
  `accept-proposal` / `reject-proposal` endpoints. `graph_review.proposed_links` now
  carries the `edge_proposals` id so links are accept/reject-able.
- **"Merge duplicate entities" modal clipped long names.** The footer put the full
  entity name in each button (`Keep "<name>"`); long path-like names overflowed the
  fixed-width modal (`overflow:hidden`) and were cut off. The modal now shows both
  full names (labelled A/B, wrapping) in the body and uses short, middle-ellipsised
  button labels; `.modal-foot` also wraps (`flex-wrap`) as a safety net.
- **Deep-dream merge proposals were noisy; `A<->B` artifacts were unhandled.** The
  entity-merge classifier proposed a merge whenever one name's token set was a subset
  of the other's, so single generic tokens drove false merges (`memory_graph→Graph`,
  `bank→live bank`, `LIVE→live daemon`) and real entities were merged *into*
  concatenated extraction artifacts (`Phase 2 plan → Phase 1 plan<->Phase 2 plan`).
  `_name_contains` now requires the contained token set to have ≥2 tokens and excludes
  any concat-artifact endpoint; a new degree-agnostic `concat-artifact` junk rule
  (`_is_concat_artifact`) surfaces the `A<->B` nodes for deletion instead.
- **A failed statement could wedge the whole daemon (connection poisoning).** The
  daemon holds one long-lived psycopg connection, but only 3 of ~30 mutating methods
  in `storage/postgres.py` rolled back on error — so any raised statement (lock
  timeout, FK violation) left the connection `InFailedSqlTransaction`, breaking every
  subsequent tool call until a restart. Every mutator now funnels through one shared
  `_txn()` context manager (commit on success, rollback on any exception). The
  deep-dream apply loop is unaffected by design (each op is idempotent + re-runnable).
- **`world_cortex` / `lessons` supersession ignored HLC ordering.** Both stores
  superseded on value-difference alone, unlike the personal cortex; they now gate on
  the HLC (an out-of-order write with an older stamp can't clobber a newer value).
  Dormant under the shipped single-writer (every write gets a fresh monotonic tick),
  live under the future multi-writer path — parity with `cortex._should_supersede`.
- **`exact_duplicate_pairs` could auto-merge (no review) two `A<->B` concat
  artifacts**, and `merge_entity` over-counted / FK-crashed on a stale endpoint. The
  auto-merge path now excludes concat artifacts, and `merge_entity` returns a graceful
  no-op when either endpoint no longer exists instead of raising.

### Security
- **Stored-XSS via world-fact `source_url`.** A citation URL is agent/LLM-authored
  (often distilled from fetched web content), so a prompt-injected `javascript:` /
  `data:` scheme could execute when an operator clicked the "source" link in the
  Cortex Console. Now blocked at both ends: `service.world_write` rejects any non-
  `http(s)` scheme at write time (`{"action":"rejected"}`) so the payload never lands,
  and `views/world.js` allowlists `http(s)` at render time (bad URLs show as inert text).
- **`ops/restore_from_pt.py` unpickled snapshots with `weights_only=False`** (CWE-502),
  inconsistent with `storage/migrate.py`'s own guard on the same file format. Now
  `weights_only=True`, so restoring a stale/tampered `.pt` bank can't execute code.

## [0.6.0] — 2026-06-25 — graph foundation

### Added
- **Provenance-as-link (Phase 1)** — the dream now links each consolidated fact-slot
  to the dense episodes it came from (`memory_traces`, keyed on the stable slot);
  facts surface `source_entries`, and new `memory_get` / `memory_reinforce` tools
  dereference and strengthen them.
- **Cortex Console (web UI)** — an operator dashboard served by the daemon at
  `/ui/` (new `pseudolife_memory/web/`: a pure-ASGI `/api` REST layer 1:1 over
  `MemoryService` + a no-build vanilla SPA). Tabs: Observatory (health/stats,
  MIRAS band continuum, dream gauges), Cortex (fact review with provenance +
  version-history timeline + contested-fact resolve), World, Lessons, Episodes,
  Stream (search + ranking-trace debugger), Graph (force-directed visualiser +
  table view), and a Console config editor (28 knobs, live-vs-restart, atomic
  save with backup). `/api/*` is bearer-gated like `/mcp`; `/ui` + `/health`
  stay open. Offline-first (vendored OFL fonts, no CDN, no build step). A
  fixture-backed `pseudolife_memory.web.devserver` renders the UI without
  Postgres for development.
- **Graph insight layer** — `dream` now computes graph communities (persisted),
  god-nodes, surprising connections, and suggested questions. New read-only
  tools `memory_digest` and `memory_communities`; `memory_graph` nodes carry a
  `community` field.
- **`memory_recall`** — read-only multi-hop graph-traversal retrieval (MemCoT
  loop). Seeds from the query, walks the knowledge graph up to `hops`
  iterations (max 5), and returns entities, edges, paths, and supporting texts.
  Mechanical seed driver by default (deterministic, no LLM call); set
  `PSEUDOLIFE_RECALL_DRIVER=llm` to use the dream endpoint for seed resolution.
  `low_confidence: true` signals no seed matched — fall back to `memory_search`.
- **`memory_path` / `get_neighbors`** — two focused read-only graph MCP tools.
  `memory_path` returns the shortest path between two entities (targeted
  bidirectional search over the read-model, `max_hops` cutoff); `get_neighbors`
  returns an entity's 1-hop neighbours with an optional relation filter.

### Changed
- `memory_recall` mechanical seeder is now **query-first** — it seeds the
  question's subject(s) and uses search-hit matches only as a fallback,
  eliminating cross-talk noise on populous banks (bench: seed precision 1.0 vs
  0.262, zero answer-recall loss, ~4× fewer graph calls). `recall.driver=llm`
  unchanged.
- `memory_recall` now **hub-gates** graph expansion (graphify-derived) — high-degree
  hub entities are still returned as results but are not expanded *through*, with
  degree-aware frontier ordering and a per-hop budget. Cuts blast radius on
  hub-adjacent queries with no recall loss (bench: mean −118 tokens/q, −6.7
  entities/q, zero recall regression). Adaptive threshold
  (`recall.hub_percentile` / `recall.hub_floor`); disable via `recall.hub_gate=false`.
- The graph-insight digest now also refreshes on a `dream_run` with **no memory
  backlog**, so manual graph edits (cleanup, direct `graph_relate`) are reflected
  in `memory_digest` / `memory_communities` without waiting for a memory-bearing
  dream.
- Dream `exclude_sources` default now also skips `"status"` and `"log"` — store
  verbose status/log dumps under those sources to keep them searchable (in the
  bands) without the dream mining them into knowledge-graph clutter.
- Graph layer: single source of truth (Postgres `entities` hub + NetworkX
  read-model) behind a swappable `GraphStore` port. Apache AGE removed.
- **Dream extractor default → Gemma 4 E2B QAT (UD-Q4_K_XL).** Switched the baked
  sidecar model (`ops/Dockerfile.extractor` `MODEL_URL`) from PTQ Q4_K_M to the
  quantization-aware-trained UD-Q4_K_XL — smaller (2.44 vs ~2.9 GB) and faster on
  CPU at identical quality. Quant ladder (2026-06-24, `evals/`): facts gold 1.0 /
  stale 0.0, relations F1 0.75 (separate), lessons 5/6 — all equal to the old
  Q4_K_M, ~17–40% faster to consolidate. Lighter GGUF quants are dominated:
  UD-Q3_K_XL regresses relations (F1 0.62) and is bigger+slower; UD-Q2_K_XL
  craters lesson synthesis (3/6 — inverts polarity/outcome) and is the slowest.
  GGUF size floor is ~2.2 GB; genuine sub-1 GB needs the LiteRT 2-bit/mmap mobile
  build (a separate runtime, not wired here).

### Removed
- `memory_graph_query` (raw read-only Cypher) MCP tool and the `pseudolife-mcp
  age-sync` CLI mode. Multi-hop queries are served by `memory_graph`
  (neighborhood + derived/inverse edges + shortest path). The Postgres image no
  longer requires the Apache AGE extension. Run `ops/migrate_drop_age.py` once
  (back up first) to drop the AGE graph + extension from an existing bank — it
  supersedes the v0.4 `ops/migrate_v04.py` collision-fix migration.

## [0.5.1] — dream resilience

### Fixed
- **The dream stopped skipping memories on a failed extraction.** The extractor
  masked failures (timeout / network / malformed response) as an empty `[]`
  result, so `dream_run` advanced its cursor past those memories permanently —
  on the live CPU Gemma sidecar this skipped every dream during a too-short
  timeout window. `OpenAICompatExtractor` now **raises `ExtractorError`** on
  failure; `dream_run` **holds the cursor** (returns `extractor_failed`) so the
  memories are retried next sweep, and `synthesize_lessons` already leaves its
  signals pending. A genuine empty result (a successful call with no canonical
  facts) still writes nothing and advances, as before.
- **Extractor timeout was too short for CPU inference.** The default CPU sidecar
  (Gemma E2B Q4) generates at ~30 tok/s, so a full generation easily exceeded the
  old 20s timeout → `claims:0`. `extractor_timeout_seconds` default 20s → **240s**
  and `extractor_max_tokens` 1024 → **2048** (headroom for dense batches + slower
  end-user laptops). Both are now env-overridable —
  `PSEUDOLIFE_DREAM_TIMEOUT_SECONDS` / `PSEUDOLIFE_DREAM_MAX_TOKENS` (set in
  `ops/docker-compose.yml`) — alongside the existing `_BASE_URL` / `_MODEL` /
  `_API_KEY`.

### Added
- **`ops/wslconfig.example`** — a `.wslconfig` template that caps Docker
  Desktop's WSL2 VM (the stack needs ~2–4 GB resident; WSL2 otherwise balloons to
  ~50% of host RAM and caches without releasing). Copy to `%USERPROFILE%\.wslconfig`
  and `wsl --shutdown` to apply.

## [0.5.0] — cosine spine

### Changed
- **Removed the test-time-trained neural memory; bands are now plain cosine
  vector stores.** An A/B eval ([`docs/2026-06-21-neural-memory-investigation.md`])
  showed the MIRAS neural-retrieval blend *underperformed* pure cosine at every
  scale (the L2-self-reconstruction MLP over frozen embeddings is a regime
  mismatch for standalone retrieval — TITANS/HOPE are end-to-end sequence
  models). `band.retrieve` is now pure cosine; the store gate uses **novelty**
  surprise (`1 − max cos(x, existing)`). Deleted `memory/miras/objectives.py`,
  `update_rules.py`, `modules.py`, the HOPE chained-read, the neural-blend
  config (`neural_blend_weight` / `neural_warmup_updates`), `chain_residual`,
  and the dead `MemoryMLP` / `TitansMemoryBank`. The contrastive feature keeps
  suppression (drops the band-MLP step). `memory_stats` per-band fields no
  longer include `objective` / `update_rule` / `base_lr` / `memory_module`.
  `weights.pt` now persists only counters (no MLP weights); legacy state with
  weight blocks loads tolerantly (entries restored, weights ignored). The full
  neural machinery is archived on the `archive/neural-memory-titans` branch
  for a future sequence-model experiment. `MIRASBandSpec` keeps only capacity /
  cadence / promotion / eviction.

### Fixed
- **Durable-save failures no longer silent (F3).** A failed cortex/world/lessons
  snapshot used to be swallowed with a `logger.warning` while the tool call
  returned success — silent data loss in a memory system. The saves now raise
  `PersistenceError` (surfaced to the caller on tool paths; the background
  autosave/flush threads already catch, so they survive) and bump a
  health-visible `persist_errors` counter. The AGE *mirror* stays best-effort
  (rebuildable via age-sync) — only content persistence is hardened.
- **Version/schema drift (F5).** `pyproject` version `0.2.0 → 0.4.0`; `/health`
  now reports `schema` from `SCHEMA_META_VERSION` (was hardcoded `8`) plus the
  new `persist_errors`; the `mcp_server.py` header rewritten to describe the
  HTTP-daemon + auth architecture (was the obsolete v0.1 stdio/no-auth model);
  clarified that `cortex.SCHEMA_VERSION` is the file-mode snapshot format number,
  distinct from the Postgres `SCHEMA_META_VERSION`.

### Added
- **Writer-aware temporal memory (schema v11).** Every canonical write (cortex,
  world, lessons) now carries a temporal/provenance stamp: `tx_time` (write
  time), `valid_time` (event time — a lesson inherits its source signal's
  observation time, not the dream's write time: bitemporal), an
  `(hlc_phys, hlc_logical)` **Hybrid Logical Clock** that is the ordering
  authority for supersession (monotonic, immune to wall-clock steps — "newer
  wins" no longer depends on jittery wall time), and `writer_id`/`session_id`.
  The daemon reads an `X-PL-Writer` header per request (the shim forwards
  `PSEUDOLIFE_WRITER_ID`) and a per-connection `session_id`, so concurrent
  sessions/agents are distinguishable. Reads surface the stamp + a human `age`;
  new `memory_history(entity, attribute)` returns the per-slot version timeline.
  A dormant `write_mode=occ` seam (`version` column + `replace_facts_occ` stub)
  is laid for a future multi-process writer (Phase 2; raises `NotImplementedError`).
  **Collision fix:** the AGE graph is renamed off the DB role name
  (`pseudolife` → `pseudolife_graph`), every connection pins `search_path` to
  `public`, and a guarded backup-first migration (`ops/migrate_v04.py`, later
  superseded by `ops/migrate_drop_age.py` when AGE was removed) renames legacy
  graphs + drops shadow tables. `ops/retire_by_writer.py` supersedes a rogue
  writer's rows. Design + plan:
  `docs/specs/2026-06-21-writer-aware-temporal-memory-{design,plan}.md`.
- **Procedural / outcome memory — "lessons" (schema v10).** A fourth memory
  layer beside the personal and world cortex that learns from the agent's *own
  work*: what worked, what was a dead end, and what the user corrected. Keyed by
  `(task-type, aspect)`, each lesson carries an `outcome`
  (`success`/`failure`/`correction`) and a `polarity` (`+` do / `-` avoid) in its
  own `lessons` table (blast-radius isolated). Capture is cheap and in-session
  (`memory_outcome` logs a *signal*; user-tier `memory_fact_set` corrections are
  auto-tagged); synthesis is **single-writer** — the dream's LLM extractor distils
  accumulated `outcome_signals` into lessons (`extract_lessons`), with no
  deterministic floor (no extractor ⇒ no lessons, signals retained + age-pruned).
  Lessons are **graph-traversable**: a task-type becomes an `etype='task-type'`
  entity and each lesson adds a `prefers`/`avoids` edge (two new builtin
  relations) to the tool/source it concerns. New tools: `memory_outcome`,
  `memory_lesson_search` (embedding-on-query), `memory_lessons`,
  `memory_lesson_forget`. Config under `memory.lessons`. The auto-injected
  "lessons from past work" prompt block, an outcome-coloured graph view, and a
  Cypher-side AGE edge-property upgrade are deferred follow-ons. Design:
  `docs/specs/2026-06-20-procedural-outcome-memory-design.md`.
- **Dream consolidation (Tiers 0–2).** Pull recent associative memories, extract
  canonical `(entity, attribute, value)` facts, write them to the cortex, and
  advance a monotonic cursor so each memory is consolidated once (session-agnostic
  — no "session finished" event needed). A pluggable `DreamExtractor`
  (`memory/dream.py`) feeds one shared `service.dream_run` driver that owns cursor
  discipline. (Single-writer cortex — see Changed — makes the LLM dream the sole
  automatic writer; the regex is opt-in only.) Three tiers:
  - **Tier 0** — `memory_dream_run` (regex floor, headless, no LLM, on-box/free).
  - **Tier 1** — agent-driven via `memory_dream_pull` / `memory_dream_status` /
    `memory_dream_commit` and a copy-in `/dream` command
    (`examples/commands/dream.md`).
  - **Tier 2** — `OpenAICompatExtractor` + a daemon background sweep that fires on
    a configurable backlog+quiescence trigger, pointed at any OpenAI-compatible
    endpoint (Ollama, LM Studio, Haiku, OpenRouter, self-hosted) via
    `PSEUDOLIFE_DREAM_BASE_URL` / `_MODEL` / `_API_KEY`.

  Eligible sources and the trigger thresholds are configurable under
  `memory.dream`. Design: `docs/specs/2026-06-15-pluggable-dream-extractor-design.md`.
- **Abstention signal.** `memory_search` now returns `low_confidence` — `True`
  when the top score falls below `memory.search_confidence_floor` (default `0.0`
  = off), so the agent can choose to abstain rather than answer from a weak
  match. A cortex hit always overrides it (a canonical fact *is* the answer).
- **One-shot dream sweep.** `memory_dream_run(limit=…)` consolidates the whole
  eligible backlog in a single call (omit for the configured batch size).
- **Opt-in CPU LLM extractor sidecar.** A llama.cpp `compose --profile extractor`
  service (`ops/Dockerfile.extractor`, Gemma 4 E2B baked in) exposes an
  OpenAI-compatible endpoint for higher-quality dream consolidation, off by
  default. Plus `evals/` — an extractor-ladder benchmark that picks the minimum
  viable model (verdict: Gemma 4 E2B clears; see `evals/README.md`).
- **Tunable cortex abstention guard.** `memory.cortex.guard_min_score` (default
  `0.3` = prior hard-coded behaviour) sets the score at/above which a cortex fact
  counts as a confident answer and suppresses `low_confidence`. Raising it lets
  weak topically-adjacent facts stop blocking abstention. Calibrated as a pair
  with `search_confidence_floor`; the `evals/` guard sweep recommends
  `guard_min_score = 0.65` + `search_confidence_floor = 0.70` (doubles abstention
  recall at zero false-abstain). Behaviour-preserving at the default.
- **Dream slot resolver (off by default).** `memory.cortex.dream_slot_match_threshold`
  (default `0.0` = off) lets the dream pass map a paraphrased `(entity, attribute)`
  onto an existing slot (value-free `slot_embedding`, schema v8, additive) before
  writing, to catch small-model supersession forks. Calibration found no
  measurable benefit on the benchmark (stale-leak flat, a false-merge at `0.80`):
  the residual fragmentation traces to the deterministic regex auto-promote, not
  paraphrase — see `docs/specs/2026-06-19-single-writer-cortex-design.md` for the
  structural fix. Shipped off; enable only with the false-merge risk in mind.

### Changed
- **Single-writer cortex.** The LLM **dream** pass is now the sole *automatic*
  writer of canonical facts (plus explicit `memory_fact_set`). The deterministic
  regex auto-promote on `store` (`memory.cortex.auto_promote`) now defaults
  **off**, and the `dream_run` regex fallback is removed — an extractor that
  yields nothing writes nothing. Rationale: the regex mis-splits compound entity
  names (`"payments database host"` → `payments` / `database host`) and, running
  alongside the LLM dream, fragments one fact across sibling slots — the real
  cause of the residual stale-leak, not small-model paraphrase. New
  `NoOpExtractor` is the default when no extractor LLM is configured; the daemon
  logs a startup warning in that case. Behaviour change: a plain `store()` no
  longer populates the cortex. Design:
  `docs/specs/2026-06-19-single-writer-cortex-design.md`.
- **Extractor sidecar default-on.** `ops/docker-compose.yml` now starts the Gemma
  CPU extractor with the stack (dropped its `profiles` gate) and routes dream
  consolidation to it. Clearer names (anti-PEBKAC): compose project `pseudolife-mcp`
  (was the folder default `ops`); containers `pseudolife-mcp-{postgres,daemon,extractor}`;
  new-install volumes default to `pseudolife-mcp-{bank,state}`, env-overridable so
  existing installs keep `ops_pseudolife_*` via `ops/.env`.

### Added (cleanup tooling)
- **`ops/dedup_cortex.py`.** One-time, dry-run-first, reversible cleanup that
  collapses paraphrase sibling slots left by past auto-promotes
  (`MemoryService.cortex_dedup` / `CortexStore.dedup_siblings`): clusters current
  slots by value-free slot-embedding cosine, keeps the canonical (provenance tier,
  then recency), retires the rest (audit trail kept). Back up before `--apply`.

### Fixed
- **Reasoning models in `OpenAICompatExtractor`.** Thinking models (Qwen3, etc.)
  spent the entire token budget on a `<think>` trace and returned empty content,
  silently falling back to the regex floor. The extractor now sends
  `chat_template_kwargs:{enable_thinking:false}` and tolerantly parses the
  outermost JSON object (stripping ```json fences / leading prose). Non-thinking
  templates (e.g. Gemma) ignore the kwarg; extraction got both faster and more
  accurate across the board.

## [0.2.0] - 2026-06-14

The v0.2 line moves the bank off local files and onto a single-writer daemon
backed by Postgres, and adds a canonical-fact cortex and a typed knowledge
graph on top of the associative continuum.

### Added
- **Daemon + shim architecture.** A long-lived memory daemon owns the bank and
  serves MCP over HTTP on `127.0.0.1:8765`; every Claude Code session attaches
  through a torch-free stdio shim (`pseudolife-mcp`) that auto-starts the daemon
  if absent. Three CLI modes: `serve` (daemon), default (shim), `embedded`
  (the v0.1 in-process server — no daemon, no Postgres).
- **Postgres source of truth.** Postgres 16 + pgvector (bundled
  `ops/docker-compose.yml`, host port `5433`, external volume so `down -v` can't
  wipe the bank) is now durable storage; the in-memory MIRAS bands are a
  write-through cache hydrated at startup. Single writer = concurrent sessions
  can't clobber each other; entries are transactional.
- **Cortex (canonical facts).** Slot-keyed `(entity, attribute) -> current value`
  store with supersession-not-decay: `memory_fact_get` / `memory_fact_set` /
  `memory_fact_resolve` / `memory_fact_forget` / `memory_facts`. Slot-shaped
  facts in any `memory_store` auto-promote at a 0.5 confidence floor.
- **Provenance contenders.** Cortex facts carry a tier (`user > action > agent`);
  a weaker-tier write that conflicts with a stronger-tier fact is parked as a
  contender (surfaced in get/search) rather than silently overwriting, and
  settled with `memory_fact_resolve`.
- **Knowledge graph.** Typed entity graph (`memory_graph`, `memory_graph_relate`,
  `memory_graph_unrelate`, `memory_relation_define`, `memory_alias`) with a
  closed relation vocabulary, soft type hints, and transitive/inverse closure
  computed on read. Apache AGE mirror enables read-only openCypher via
  `memory_graph_query`.
- **World-knowledge cortex.** Durable cited/dated facts about external reality,
  persisted in Postgres and exposed through the daemon's MCP tools.
- **Tier C** (carried from late 0.1): episodes (`memory_episode_*`),
  multi-valued tags, and the consolidation workflow
  (`memory_consolidation_candidates` + `memory_consolidate`).
- **Optional retrieval boosters:** cross-encoder reranker (`rerank=True`) and a
  stdlib BM25 hybrid lexical pool (`bm25=True`), both off by default.
- **LAN sharing.** Run the daemon with `PSEUDOLIFE_MCP_HOST=0.0.0.0` and a
  `PSEUDOLIFE_MCP_TOKEN`; the daemon refuses to bind a non-loopback host without
  a token, and Postgres stays loopback-only.
- **Ops:** `ops/install-autostart.ps1` (Task Scheduler logon task),
  `ops/backup.ps1` (rotating `pg_dump`), `age-sync` to heal a drifted AGE mirror.

### Fixed
- **Alias-aware cortex lookup.** `memory_fact_get` / `cortex_lookup` now resolve
  entity aliases through the graph before reporting a miss, so a fact stored
  under a canonical name (e.g. `dev-box`) is reachable via any bound alias
  (e.g. `4090`) — honouring the documented contract that every fact lookup
  resolves aliases first.
- **Test isolation against the AGE schema.** PG-backed test fixtures now pin
  `search_path` to `public` before schema/truncate work and reap leaked
  backends. Previously, once a test created the AGE graph (whose schema name
  `pseudolife` equals the DB role), unqualified table names resolved to
  graph-schema shadow tables and `TRUNCATE` cleared the wrong ones — rows leaked
  across tests and `pytest tests/` showed order-dependent failures. The full
  suite (300 tests) is now green on repeat runs.

### Migration
- On first daemon run, a pre-v8 `cms_state.pt` in `PSEUDOLIFE_MCP_DATA_DIR` is
  auto-migrated into Postgres; the originals are renamed `*.pre-v8.bak` (never
  deleted). The MCP build is not save-compatible with the desktop PseudoLife app.

## [0.1.0] - Initial release

- In-process stdio MCP server exposing the neural memory layer: the MIRAS
  8-tier continuum (working → forever), ChromaDB reference bank, supersession,
  and contrastive learning. File-mode persistence (`cms_state.pt` + ChromaDB);
  no daemon, no Postgres. `memory_store` / `memory_search` / `memory_recent` /
  `memory_supersede` / `memory_delete` / `memory_stats` / `memory_save` plus the
  document RAG tools.

[0.2.0]: https://github.com/Pseudogiant-xr/PseudoLife-MCP/releases/tag/v0.2.0
