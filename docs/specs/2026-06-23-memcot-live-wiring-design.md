# `memory_recall` — Live MemCoT Iterative Retrieval Tool (Design)

- **Date:** 2026-06-23
- **Status:** approved (brainstorming) — pending implementation plan
- **Branch:** `feat/memory-recall-tool`
- **Author:** agent (Claude) + Pseudogiant
- **Predecessor:** the measurement harness `evals/memcot_bench.py` (merged `55d576e`) and its design `docs/specs/2026-06-23-memcot-retrieval-loop-design.md`.

## Motivation

The MemCoT measurement harness proved that an iterative search→graph-expand→re-query
loop unlocks multi-hop recall the single-shot `memory_search` cannot reach: on the
hardened bench corpus single-shot scored **0.333** (1-hop only) vs **1.0** for
loop+graph, with the lift concentrated in **graph traversal** (`lift_from_graph`
+0.556 ≫ `lift_from_looping` +0.111). The harness was measurement-only. This spec
productionises the winning arm as a live, read-only MCP tool so multi-hop queries
against the real bank actually benefit.

A secondary harness finding shapes the design: a retrieval-**confidence** gate
cannot decide when to loop (`gate_would_fire = 0/9` — single-shot is *confidently
wrong* on multi-hop). So we do **not** auto-trigger the loop; we expose a **distinct
tool** the agent invokes for relational/multi-hop questions. Agent tool-choice = the
"relational-query detection" the bench said we'd need.

## Goal & non-goals

**Goal.** A new read-only `memory_recall` MCP tool (+ backing `service.recall()`)
that runs the mechanical iterative loop over the live bank and returns the bridging
context single-shot search misses. Deploy it live alongside the already-merged GAM #2
graph-population (one daemon rebuild).

**Non-goals.**
- No change to `memory_search` or any existing retrieval path (zero regression by
  construction — we only add a tool).
- No write path — `recall` is strictly read-only.
- No new graph schema; GAM #2 (relation extraction in the dream) is already in
  `master`, just not yet in the live daemon image.
- No scale-indexing of the entity vocabulary (load-and-match is fine at current
  scale; flagged as future work, not built — YAGNI).

## Current state (reconciled 2026-06-23)

- `master` **already contains** graph-foundation (AGE removed, `graph_store.py`,
  `GraphStore` port) **and** GAM #2 graph-from-text (`extract_relations`,
  `_link_dream_relations`, `_dream_extract_relations` in `dream.py`/`service.py`).
  Commits `f0862e3..207eeef`.
- The **live daemon predates GAM #2**: probed live edges carry `origin="action"`
  (hand-created via `memory_graph_relate`), not the `origin="agent"` the dream
  relation-extractor writes. The live graph already has real multi-hop structure,
  e.g. `pseudolife-mcp ──stores-data-in──▶ postgres ──runs-on──▶ docker-desktop`
  (+ derived inverse `docker-desktop ──hosts──▶ postgres`).
- Tools register via `@mcp.tool()` functions in `mcp_server.py` delegating to the
  `service` singleton. Adding a tool ⇒ a daemon rebuild/redeploy.

## Architecture & files

- **`pseudolife_memory/memory/recall.py`** *(new)* — the loop engine and controllers,
  promoted/adapted from the harness. Pure orchestration over injected callables
  (`search_fn`, `graph_fn`, `entity_vocab`), so it is unit-testable without a daemon
  or DB:
  - `RecallState` (dataclass): `seeds`, `entities`, `texts`, `facts`, `edges`,
    `paths`, `iterations`, `low_confidence`.
  - `RecallController` (Protocol): `seed_entities(query, hits, vocab) -> list[str]`;
    `next_queries(query, newly) -> list[str]`.
  - `MechanicalController` — deterministic: seeds = vocab entities word-matched in
    the query + top hits; `next_queries` = `query + " " + name` per newly discovered
    entity.
  - `LLMController` — real-but-minimal: reuses an injected `DreamExtractor`
    (`OpenAICompatExtractor`) to name seed/next entities from query+context. Injected,
    so unit-tested with a **stub** extractor (no served model in tests). Off by default.
  - `run_recall(search_fn, graph_fn, entity_vocab, query, controller, *, hops, top_k, max_entities) -> RecallState`.
- **`pseudolife_memory/service.py`** — `recall(query, hops=None, top_k=None, driver=None) -> dict`:
  loads the live entity vocabulary from the graph store once, builds the configured
  controller, and calls `run_recall` wiring `self.search` / `self.graph_neighborhood`.
  Holds **no lock** (composes the already-locked public methods; no re-entrancy).
- **`pseudolife_memory/mcp_server.py`** — `@mcp.tool() memory_recall(query, hops=3, top_k=5)`
  → `service.recall(...)`. Docstring is the gate (see Gating).
- **`pseudolife_memory/utils/config.py`** — `RecallConfig`:
  `driver: str = "mechanical"`, `default_hops: int = 3`, `default_top_k: int = 5`,
  `max_entities: int = 50`. Env override `PSEUDOLIFE_RECALL_DRIVER`.
- **`tests/test_recall.py`** *(new)* — unit (controllers w/ stub extractor, live
  entity-spotting, cap enforcement, `low_confidence`) + PG-integration (seed a known
  multi-hop graph; assert `recall` bridges where `search` misses; 1-hop no-regress).
- Docs: this spec + `CHANGELOG`/README note.

## The recall loop (live)

Per call:
1. **Load vocab** once: entity display-names + aliases from the graph store.
2. **Seed:** `search(query, top_k)`; controller resolves seed entities by
   word-boundary matching the query + top-hit texts against the vocab. If none
   resolve → return `low_confidence=True` (agent falls back to `memory_search`).
3. **Expand (per iteration, depth-1):** for each frontier entity,
   `graph_neighborhood(entity, depth=1)` → fold connected entities (structural) and
   their current facts into state; new entities join the frontier.
4. **Re-query:** controller's `next_queries` re-run `search` to pull supporting
   snippets for newly discovered entities.
5. **Stop:** no new entities, or `hops` reached, or `max_entities` reached.

Depth-1/iteration (N hops = N iterations) matches the benched, reviewed engine.

## Return shape

```json
{ "query": "...", "seeds": ["..."],
  "entities": [{"entity": "...", "facts": [{"attribute": "...", "value": "..."}]}],
  "edges": [{"src": "...", "relation": "...", "dst": "...", "derived": false}],
  "paths": [["a", "b", "c"]],
  "texts": ["...search-hit snippets..."],
  "iterations": 3, "hops": 3, "low_confidence": false }
```

The `edges`/`facts`/`paths` are the payload single-shot can't produce.

## Gating (how the agent knows when to use it)

No auto-trigger (confidence can't gate — bench finding). The tool's **docstring**
directs use: call `memory_recall` for relational / multi-hop questions ("what does X
ultimately run on?", "what's connected to Y?", "how does A reach C?"); use
`memory_search` for direct lookups. `low_confidence=True` signals fall-back.

## Cost caps & safety

`hops` default 3 (hard max 5), `top_k` default 5, `max_entities` default 50 (bounds
hub-node blowup). Strictly **read-only** — no writes, cannot corrupt the bank.
Returns bounded by caps.

## Testing

- **Unit** (`recall.py` via injected stubs, no DB/model): `MechanicalController`
  seeding + expansion; `LLMController` with a stub extractor; live entity-spotting
  word-boundary correctness; `hops`/`max_entities` caps; `low_confidence` on no-seed.
- **PG-integration** (`service.recall` against the bench DB): seed a known
  multi-hop chain, assert `recall` surfaces the terminal that `search` alone misses;
  assert a 1-hop/direct query returns correctly and isn't degraded.
- The harness (`evals/memcot_bench.py`) already validates the core loop mechanics
  against the real service; these tests cover the live-vocab + tool wiring + caps.

## Gated deploy (one rebuild lands GAM #2 + recall)

After build + tests + merge to master, a **gated** deploy (user approves before the
rebuild):
1. **`ops/backup.ps1` first** (pg dump of the live bank).
2. Rebuild the daemon image from master (`docker compose -f ops/docker-compose.yml
   build daemon`), preserving **both** external volumes — **never** `down -v`.
3. Restart; `/health` check (schema unchanged; GAM #2 + recall additive).
4. **Live smoke:** (a) `memory_recall` on `pseudolife-mcp → postgres →
   docker-desktop` returns `docker-desktop`; (b) `memory_search` output unchanged;
   (c) trigger a `dream_run` and confirm new `origin=agent` relation edges appear
   (GAM #2 now live).

Deploy gotchas (from prior AGE-removal): preserve `ops_pseudolife_pgdata` +
`ops_pseudolife_data`; backup-first; the daemon is a baked image (rebuild required to
pick up code).

## Resolved decisions

1. Location: **server-side MCP tool** (no provider layer in this standalone repo).
2. Tool shape: **new `memory_recall`** (not a `memory_search` flag) — zero regression.
3. Driver: **mechanical default + `recall.driver` config flag**; `LLMController`
   real-but-minimal, injected, stub-tested, off by default.
4. Scope: build + test + **gated deploy** that also lands GAM #2 graph-population
   (one daemon rebuild from master).
5. Gating: agent tool-choice (docstring-directed) + `low_confidence` fallback; no
   confidence auto-gate.

## Out of scope / future

- Entity-vocab indexing for enterprise scale (load-and-match suffices now).
- Auto-routing `memory_search` → `recall` on detected relational queries.
- Tuning the `LLMController` prompt / adopting it as default.
