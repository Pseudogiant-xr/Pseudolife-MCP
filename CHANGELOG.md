# Changelog

All notable changes to PseudoLife-MCP are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  `public`, and a guarded backup-first migration (`ops/migrate_v04.py`) renames
  legacy graphs + drops shadow tables. `ops/retire_by_writer.py` supersedes a
  rogue writer's rows. Design + plan:
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
