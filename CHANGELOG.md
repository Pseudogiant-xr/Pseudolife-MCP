# Changelog

All notable changes to PseudoLife-MCP are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

- In-process stdio MCP server exposing PseudoLife's memory layer: the MIRAS
  8-tier continuum (working → forever), ChromaDB reference bank, supersession,
  and contrastive learning. File-mode persistence (`cms_state.pt` + ChromaDB);
  no daemon, no Postgres. `memory_store` / `memory_search` / `memory_recent` /
  `memory_supersede` / `memory_delete` / `memory_stats` / `memory_save` plus the
  document RAG tools.

[0.2.0]: https://github.com/Pseudogiant-xr/PseudoLife-MCP/releases/tag/v0.2.0
