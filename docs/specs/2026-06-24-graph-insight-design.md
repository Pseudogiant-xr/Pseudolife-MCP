# Graph insight layer — design (Track B v1)

**Date:** 2026-06-24
**Branch:** `feat/graph-insight` (off `master`)
**Status:** design approved, pending implementation plan
**Provenance:** Track B of the graphify competitive evaluation (2026-06-24).
Track A (recall hub-gating) shipped; this builds the topology insight layer
graphify's `analyze.py`/`cluster.py` demonstrate, adapted to Pseudolife's data
model (provenance tiers, contested facts) rather than graphify's code-specific
signals.

## Problem

Pseudolife has a knowledge graph (Postgres `entities` hub + NetworkX read-model)
but no topology analytics. `dream` consolidates memories into edges, but nothing
surfaces the *shape* of what the graph knows: which entities are central, which
connections are surprising, what is worth verifying. graphify computes
communities, "god nodes", "surprising connections", and "suggested questions"
over a static code graph; we want the same insight over our living memory graph,
computed during `dream` and persisted so the Console can use it and communities
stay stable across sessions.

## Goals / non-goals

**Goals (v1)**
- Community detection over the entity graph, persisted with stable IDs across
  sweeps.
- God-nodes, surprising-connections, and suggested-questions analytics, adapted
  to our provenance (`origin`, `derived`, `confidence`) and contested-fact
  systems.
- Compute everything inside the `dream` sweep; persist communities (tables) +
  a digest snapshot (`meta` JSON). Surface via read-only MCP tools and enrich
  `memory_graph` with per-node community.
- Keep the analytics a pure, DB-free, unit-testable module (the `recall.py`
  pattern). No new mandatory dependency (Louvain via the already-vendored
  NetworkX).

**Non-goals (deferred — see Deferred Experiments)**
- Compute-on-read freshness mode.
- graspologic Leiden as the default algorithm.
- LLM community labeling.
- Cortex Console UI work (graph coloring + digest panel).
- Wiring surprises/questions into the recall prefetch.

## Architecture

### Module layout
- **`pseudolife_memory/memory/graph_insight.py`** (NEW) — pure analytics over
  `(edges, entities, prior_communities)`, DB-free and unit-testable like
  `recall.py`/`graph.py`: `detect_communities`, `remap_to_previous`,
  `cohesion_score`, `god_nodes`, `surprising_connections`, `suggest_questions`,
  `build_digest`. Louvain via NetworkX; optional graspologic Leiden auto-used if
  importable.
- **`storage/schema.py`** — add the `communities` + `entity_communities` tables;
  bump `SCHEMA_META_VERSION` 11→12.
- **`storage/postgres.py`** (the `GraphStore` impl) —
  `replace_communities(rows, labels)`, `load_communities()`, and digest get/set
  helpers over `meta`.
- **`service.py`** — `_refresh_graph_insight()` (called inside `dream_run`),
  read methods backing the tools, and community enrichment on
  `graph_neighborhood` nodes.
- **`mcp_server.py`** — `memory_digest`, `memory_communities` tools.
- **`utils/config.py`** — a `GraphInsightConfig` block under `MemoryConfig`.

The analytics engine is a pure module the service calls; the service owns
persistence and the dream hook. Same separation that kept Track A clean.

### Persistence (schema v12 — additive)
All DDL is `CREATE TABLE IF NOT EXISTS`, so the migration is appending DDL + the
version bump (consistent with `ensure_schema`):

```sql
CREATE TABLE IF NOT EXISTS communities (
  id          BIGINT PRIMARY KEY,        -- stable id, 0 = largest community
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
```

The **digest artifact** (god-nodes / surprises / questions + `computed_at`) is a
single JSON row in the existing `meta` table (`key='graph_digest'`) — no third
table; it is a read-once snapshot, not something we join on. Communities get
real tables because the Console and `memory_graph` join/filter on community.

### Recompute model
`_refresh_graph_insight()` runs inside `dream_run` after
`_dream_extract_relations` writes edges, gated by `config.graph_insight.enabled`
and a non-empty graph:

1. `load_graph()` → edges + entities.
2. `detect_communities` → new partition.
3. `remap_to_previous(new, load_communities())` → stable IDs (greedy-overlap).
4. `replace_communities(...)` → transactional truncate + bulk-write of both
   tables. The shared `entities` hub is never UPDATEd (no FK-churn/lock
   pressure).
5. `build_digest(...)` → write `meta['graph_digest']`.

Wrapped so any failure logs and never breaks the dream (same discipline as the
extractor try/except). Because step 4 is one transaction, a mid-refresh failure
rolls back and the prior assignment survives. The `dream_run` summary gains
`graph_insight: {communities: N, refreshed: bool}`.

## The analytics (`graph_insight.py`)

### A — Community detection
Build an undirected `nx.Graph` from asserted edges. Partition with
`nx.community.louvain_communities(seed=42, resolution=cfg.resolution)`
(deterministic); optional graspologic Leiden if `algorithm="leiden"` and
importable, else fall back to Louvain (log once). Post-process: split oversized
communities (> `max_community_fraction` of nodes) with a second partition pass;
give isolates singleton communities; re-index by size-desc with a
`tuple(sorted(ids))` tiebreak for a total, reproducible order. `cohesion_score`
= intra-community edge density (`actual / possible`).

`remap_to_previous(new, prior)`: greedy overlap matching (largest intersection
first, one-to-one) assigns each new community the prior ID it most overlaps;
unmatched communities get fresh IDs in deterministic order. First run (no prior)
→ fresh IDs by size.

### B — God-nodes
Top-N entities by degree (reuses Track A's `graph.degree_counts`). graphify's
noise filters are code-domain (file/JSON/builtin nodes) and do not apply; this
is degree-rank with an optional config `etype` exclude set (empty by default).
Output `[{entity_id, display, degree}]`.

### C — Surprising connections (provenance-adapted)
graphify scores by EXTRACTED/INFERRED/AMBIGUOUS + cross-file-type + cross-repo +
semantic-similarity. We have none of those edge dimensions; we score on the
signals our edges *do* carry. The analytics run over **asserted** edges
(`storage.load_graph`, the same source as Track A's degree), which carry
`relation`, `confidence`, `origin` but **no `derived` flag** (derived/closure/
inverse edges are re-derived on read in `build_subgraph`, never stored):
- **Uncertainty** — `confidence < 0.6` or `origin == "agent"` (the origin the
  dream writes for auto-extracted edges) add weight (our analogue of
  AMBIGUOUS/INFERRED).
- **Cross-community** — the edge bridges two communities (the core signal).
- **Peripheral→hub** — a low-degree entity (degree ≤ 2) reaching a god-node (a
  top-N-degree entity).

The weighting cutoffs (`confidence < 0.6`, peripheral degree ≤ 2) are module
constants, not config knobs, to keep `GraphInsightConfig` lean; the plan pins
their exact values. Dedup by community-pair so one hub cannot dominate. Output
top-N
`[{src, dst, relation, confidence, origin, score, why}]`, where `why`
is a human reason (e.g. "agent-inferred bridge between community 2 and 5").

### D — Suggested questions (Pseudolife signals)
- **contested_fact** — from `cortex_records` with `contested == True`: *"Which
  value of `{attr}` for `{entity}` is correct — `{current}` or `{contender}`?"*
  (we carry `contender_value`/`contender_origin`). Uses our real contested-fact
  system rather than graphify's ambiguous-edge guess.
- **bridge_entity** — high-betweenness entity spanning ≥2 communities: *"Why
  does `{entity}` connect `{commA}` to `{commB}`?"*
- **verify_inferred** — a god-node with many `origin == "agent"` (dream-inferred)
  edges: *"Are the inferred relationships involving `{entity}` correct?"*
- **isolated_entity** — degree ≤ 1: *"What connects `{entity}` to the rest?"*
- **low_cohesion** — community with cohesion below a module-constant threshold
  (≈ 0.15) and size ≥ a small minimum (≈ 5): *"Should `{community}` be split?"*

Betweenness (the one expensive call) is `k`-sampled above
`cfg.betweenness_sample` nodes and runs only here, inside `dream` — never
on-read. Output top-N `[{type, question, why}]`.

### Digest (`build_digest`)
Assembles `{computed_at, communities: [{id, label, size, cohesion}], god_nodes,
surprises, questions, totals: {entities, edges, communities}}` →
`meta['graph_digest']`. Community **labels** are mechanical for v1: a community
is named after its highest-degree member's display (e.g. "postgres"). LLM
labeling is a deferred experiment (keeps the offline ethos).

## Surfacing (MCP tools + enrichment)
- **`memory_digest()`** — returns the persisted digest; `{available: false,
  reason}` when `dream` has not produced one yet.
- **`memory_communities(community_id=None)`** — lists communities `(id, label,
  size, cohesion)`; with an id, that community's members. Reads the two tables.
- **`memory_graph` enrichment** — `graph_neighborhood` nodes gain a `community`
  field joined from `entity_communities` (Console coloring + per-node context).
- **`memory_stats`** — add a `communities` count + last `computed_at`.

## Config (`GraphInsightConfig`)
```python
enabled: bool = True
algorithm: str = "louvain"            # "louvain" | "leiden" (leiden needs graspologic; falls back)
resolution: float = 1.0
max_community_fraction: float = 0.25
god_nodes_top_n: int = 10
surprises_top_n: int = 10
questions_top_n: int = 7
betweenness_sample: int = 200         # k-sample betweenness above this node count (0 = exact)
```

## Error handling / edge cases
- Empty graph / no edges → skip refresh; prior digest/communities untouched.
- `algorithm="leiden"` without graspologic → fall back to Louvain, log once.
- Refresh failure → logged, dream continues; transactional truncate+write rolls
  back, prior assignment survives.
- First run (no prior) → fresh IDs by size.
- Large graph → `k`-sampled betweenness bounds cost.
- Deleted entity → `ON DELETE CASCADE`; the next refresh rewrites regardless.

## Testing
- **`test_graph_insight.py`** (pure, no DB): two-cluster graph → two communities;
  `remap_to_previous` stability (relabeled identical partition keeps IDs);
  `cohesion_score`; god-node ranking; surprise scoring (cross-community +
  derived + peripheral→hub, dedup-by-pair); each question type (contested /
  bridge / isolated / low-cohesion) from synthetic inputs; digest assembly +
  mechanical labels.
- **Schema/migration test**: `ensure_schema` creates both tables;
  `schema_version == 12`.
- **PG integration** (`build_service` pattern, bench Postgres): a `dream_run` on
  a seeded multi-community graph persists `communities` + `entity_communities` +
  `meta` digest; `graph_neighborhood` returns community per node; tools read it
  back; a no-change re-run keeps community IDs stable.
- **Tool tests** (monkeypatched `service`): `memory_digest` /
  `memory_communities` shapes.

## Success criteria
On a synthetic graph with K obvious clusters: `detect_communities` returns ~K
communities; god-nodes are the actual hubs; surprises include the cross-community
bridge; questions include a contested fact when one exists. A second `dream_run`
with no graph change keeps community IDs stable (remap verified). `dream_run`
persists all three (communities, entity_communities, meta digest); tools read
them back. Existing suite green; no schema-version regression.

## Deferred experiments (flagged for later)
1. **Compute-on-read freshness mode** (vs persist-during-dream) — a flag to
   recompute the cheap analytics live between sweeps.
2. **graspologic Leiden as default** (vs Louvain) — quality upgrade; needs the
   dependency in the baked image.
3. **LLM community labeling** (vs mechanical highest-degree label).
4. **Cortex Console UI** — graph coloring by community + a digest panel.
5. **Recall prefetch integration** — inject a "## What your memory is unsure
   about" block (surprises/questions) into the context, mirroring the
   world-knowledge block.

## Files touched
- `pseudolife_memory/memory/graph_insight.py` (new — pure analytics).
- `pseudolife_memory/storage/schema.py` (two tables, version bump).
- `pseudolife_memory/storage/postgres.py` (community read/write + digest helpers).
- `pseudolife_memory/service.py` (`_refresh_graph_insight`, dream hook, read
  methods, `graph_neighborhood` community enrichment, `stats`).
- `pseudolife_memory/mcp_server.py` (`memory_digest`, `memory_communities`).
- `pseudolife_memory/utils/config.py` (`GraphInsightConfig`).
- Tests: `tests/test_graph_insight.py`, plus schema/migration + PG integration +
  tool tests in the existing files.
