# Graph foundation — one source of truth + swappable `GraphStore` (drop AGE) — design

**Date:** 2026-06-22 · **Status:** approved (design), pending plan
**Sub-project:** #1 of 3 on the GAM track (#2 graph-from-text, #3 two-tier
episodic/semantic GAM). This is the foundation the other two build on.
**Background:** fresh-eyes review `docs/2026-06-21-fresh-eyes-review.md` (F4 tool
surface, F5 schema drift); the v0.4 schema-collision incident (the AGE graph was
renamed `pseudolife_graph` after the role-name collision caused a shadow schema).

## Goal

Collapse today's **three** graph representations into **one source of truth + one
derived read-model**, with all graph access behind a **swappable `GraphStore`
port** so an AGE / dedicated-graph-DB backend can be slotted in later (enterprise
scale) without touching the dream, cortex, or retrieval. Apache AGE is removed.

Today the graph exists in three places:
1. **Postgres relational tables** (`entities` / `edges` / `relations`) — the
   canonical store (hold confidence / origin / HLC / supersession).
2. **NetworkX** (`graph.py`) — derive-on-read: transitive closure, inverse
   mirroring, subgraph + shortest path. The actual reasoning engine.
3. **Apache AGE** — a best-effort, property-less *mirror* (bare nodes + unlabeled
   edges) read by exactly one power-tool (`memory_graph_query`).

AGE is the redundant third wheel: it duplicates the edge data, holds none of the
edge properties, performs none of the reasoning, and is the component that caused
the v0.4 role/schema-collision incident. After this change the graph is a clean
**write-model (Postgres `entities` hub) + derived read-model (NetworkX)** behind a
single interface.

**Why:** the "one source of truth" the project wants *already exists* — the
Postgres `entities` table is the shared hub that `facts`, `world_facts`,
`lessons`, **and** `edges` all foreign-key into. Promoting AGE to canonical would
*re-create* a split (entities would have to leave that hub or be duplicated) and
reimplement tested derivation/provenance logic on AGE's incomplete Cypher.
Dropping AGE achieves coherence *better* than promoting it, removes a heavy
Postgres-extension dependency (a productization win for the standalone CPU
build), and shrinks the agent tool surface (fresh-eyes F4).

**Why an interface (not just deletion):** enterprise multi-agent scale (tens–
hundreds of writers) is a *possible* future. In-memory NetworkX derive-on-read is
the one graph component that would not scale to a very large graph. Putting graph
access behind a `GraphStore` port makes the engine a **reversible, contained swap**
(NetworkX-now → AGE/graph-DB-later) — the same "design for both" move already used
for v0.4 writer-keying — so choosing the simple engine now is *not* "building the
wrong framework."

**Non-goals (explicitly out of scope):**
- Graph-from-text extraction in the dream (sub-project #2).
- The two-tier episodic event-graph / semantic-graph GAM architecture and
  graph-guided multi-factor retrieval (sub-project #3).
- Enterprise multi-agent work: activating the dormant OCC seam, tenancy /
  per-bank isolation, a scalable embedding/extraction tier. The near-term
  shared-bank-with-a-handful-of-trusted-writers case (user + brother + their two
  agents) needs **no** work here — the single daemon's coarse lock
  serializes writes and v0.4 writer-keying attributes them; it is only a problem
  at hundreds of writers.
- Building an actual AGE/graph-DB backend implementation — only the port + the
  default Postgres/NetworkX impl ship now.

## The boundary (key decision)

**Entities are pinned to the Postgres hub; edges + traversal + the relation
registry are swappable.** Rationale: `facts` / `lessons` / `world_facts`
foreign-key into `entities(id)`, so entities cannot move to a graph backend
without breaking those references — they stay in Postgres regardless of graph
engine. Nothing foreign-keys *into* `edges`, so edges (which only reference entity
ids) and all traversal can live behind the port and be re-homed later.

```
service.py ─▶ GraphStore (Protocol)              entities (Postgres hub — PINNED)
                 │                                   ▲ FK: facts / lessons / world
   default:  PostgresNetworkxGraphStore  ───────────┘
     • edge writes:     upsert_edge / supersede_edge
     • relation vocab:  load_relations / upsert_relation
     • reads/traversal: neighborhood(root, depth, to, include_facts)
                        → transitive/inverse/path derivation (graph.py + NetworkX)
   future (NOT built):  AgeGraphStore / Neo4jGraphStore — same Protocol
```

`graph.py` (`norm_name`, `resolve_relation`, `derive_edges`, `build_subgraph`) is
the read-model — it is retained and moves *behind* the default impl. Entity
resolution (`ensure_entity` / `find_entity` / `add_alias`) stays on the Postgres
storage object (the hub), **not** on the `GraphStore` port.

### `GraphStore` Protocol (sketch — finalized in the plan)

```python
class GraphStore(Protocol):
    # writes (entity ids resolved by the caller via the Postgres hub)
    def upsert_edge(self, src_id, relation, dst_id, *, confidence, origin) -> dict: ...
    def supersede_edge(self, src_id, relation, dst_id) -> bool: ...
    # relation registry (closed vocabulary + guardrail)
    def load_relations(self) -> list[dict]: ...
    def upsert_relation(self, name, description, *, src_type, dst_type,
                        transitive, inverse_of) -> None: ...
    # reads / traversal (does derivation internally; returns plain data)
    def neighborhood(self, root_canonical, *, depth, to_canonical=None,
                     include_facts=True) -> dict: ...
```

The default `PostgresNetworkxGraphStore` is a thin adapter over the existing
`PostgresStorage` edge/relation methods + `graph.py`. No behavior change — it is
the current code paths, relocated behind the Protocol.

## Components & changes

### Remove (delete)
- `pseudolife_memory/storage/age.py` (entire `AgeGraph` mirror).
- `service.py`: the ~22 `_age_mirror(...)` call-sites, `_age_mirror` itself, the
  `age_sync()` method, and the `age_available` capability gate (service.py:371).
- `mcp_server.py`: the `memory_graph_query` tool (mcp_server.py:1146).
- `cli.py`: the `age-sync` mode (and its help text / usage line).
- `storage/schema.py`: the `age_available` probe + `CREATE EXTENSION age` /
  `create_graph` gating; `ensure_schema` returns without the AGE flag.
- Dockerfile / ops: the Apache AGE build/extension steps.

### Keep (behavior unchanged)
- `entities` / `edges` / `relations` tables and all their `PostgresStorage`
  methods (`upsert_edge`, `supersede_edge`, `load_graph`, `load_relations`,
  `upsert_relation`, `ensure_entity`, `find_entity`, `add_alias`).
- `graph.py` derivation (transitive/inverse + `derived: true` / `via: [...]`
  provenance) and the closed-vocab guardrail (`resolve_relation` + `related-to`
  fallback).
- The 5 retained graph tools: `memory_graph_relate`, `memory_graph_unrelate`,
  `memory_alias`, `memory_relation_define`, `memory_graph` (neighborhood).
- The dream's lesson-graph writer (`_link_lesson_graph`) — minus its
  `_age_mirror` call; it keeps writing `prefers` / `avoids` edges to Postgres.

### Add
- `GraphStore` Protocol + `PostgresNetworkxGraphStore` default impl (new module,
  e.g. `pseudolife_memory/memory/graph_store.py`). `service.py` calls graph ops
  through this rather than reaching into storage + `graph.py` directly.

## Migration & deploy

The live bank has an AGE `pseudolife_graph` schema. **Edges already live in the
relational `edges` table (the source of truth), so dropping the AGE graph is zero
data loss.** Procedure (follows the standard "recreate only the daemon, never
`down -v`" rule):
1. `ops/backup.ps1` (pg_dump → `data/backups/`) **first**.
2. Idempotent, guarded migration step: `DROP` the `pseudolife_graph` AGE graph
   and `DROP EXTENSION IF EXISTS age CASCADE` (no-op if already absent).
3. Bump `SCHEMA_META_VERSION`; and (fresh-eyes F5 quick win folded in) derive the
   `/health` `schema` value from `SCHEMA_META_VERSION` instead of the hardcoded
   `8`, so the three schema numbers stop drifting.
4. Rebuild the daemon image **without** the AGE extension; `up -d` only the
   daemon (Postgres + extractor untouched).

## Breaking changes (acceptable)
- **`memory_graph_query` (raw read-only Cypher) removed.** It is a strong-model
  power-tool that weak-model deployments already refuse to expose (weak-model
  footgun), and removing it shrinks the 42-tool surface (fresh-eyes F4). Noted in
  CHANGELOG; multi-hop questions are served by `memory_graph` (neighborhood +
  derived edges + path), which needs no Cypher.
- **`pseudolife-mcp age-sync` CLI mode removed.** No agent-facing impact.

## Testing
- Route the existing graph tests through the `GraphStore` port — behavior is
  unchanged, so they pass as-is once `service.py` calls the port.
- Add a **`GraphStore` contract test**: a backend-agnostic suite (upsert →
  neighborhood → derived/inverse edges → supersede) that the default impl passes
  and any future backend must pass. This is the swap-point guarantee.
- Delete the live-AGE round-trip test; add a guard test asserting no `age`
  import / `cypher(` call remains in `pseudolife_memory/`.
- Full suite green (HF offline env for determinism, per the test gotcha).

## Success criteria (verifiable)
1. Full test suite green (minus deleted AGE tests); `GraphStore` contract test
   passes.
2. `rg -i 'ag_catalog|AgeGraph|cypher|age-sync|age_available'` over
   `pseudolife_memory/` returns no live code paths.
3. Daemon image builds + the daemon boots + all graph tools work against a
   Postgres **without** the AGE extension installed.
4. `memory_graph` returns identical neighborhoods / derived edges / paths as
   before the change (covered by the retained tests).
