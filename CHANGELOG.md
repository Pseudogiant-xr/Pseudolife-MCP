# Changelog

All notable changes to PseudoLife-MCP are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  moved onto `memory_graph`); `memory_path` retained. 48 → 46 tools.

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
