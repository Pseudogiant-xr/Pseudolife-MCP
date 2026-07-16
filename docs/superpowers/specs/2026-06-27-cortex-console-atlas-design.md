# Cortex Console "Atlas" — project-scoped graph review — design

**Status:** approved design (2026-06-27)
**Author:** agent + user (brainstorm, 2026-06-27)
**Builds on:** Cortex Console v2 (`docs/superpowers/specs/2026-06-25-cortex-console-v2-design.md`, shipped), graphify-derived graph insight (Track B), GAM #2 graph-from-text.

## Context

The Cortex Console's Graph tab is **seed-only**. The empty state literally reads
"Enter a seed entity to explore" ([graph.js](../../../pseudolife_memory/web/static/js/views/graph.js)),
and every view (2D force, 3D galaxy) calls `GET /api/graph?entity=<x>`. You
cannot see the graph until you already know an entity's name and type it in.
Cortex (facts) and Graph are separate tabs with no click-through, so reviewing
the graph means a find-it-in-cortex-then-retype-it-in-graph dance. There is no
global/overview view.

A live audit of the bank (2026-06-27) surfaced the deeper problem. The bank is
**multi-project by design** — one shared store, ~6+ distinct projects keyed by
`source`: `pseudolife`/`pseudolife-mcp`, the `hermes-*` family, `gw2-reshade`,
`enshrouded-server`, `llama-turboq`, `homelab-local-models`, `windows-env`,
`gnd-share`, and more. But the **knowledge graph is global and un-scoped**:

- `entries` carries a `source` column; the `entities` and `edges` tables do
  **not** ([schema.py:63-97](../../../pseudolife_memory/storage/schema.py)).
- The dream extracts entities + relations from text and writes them into one
  shared graph, **dropping the per-project provenance**
  (`_link_dream_relations`, [service.py:1403](../../../pseudolife_memory/service.py)
  records `origin="agent"` and a confidence — no `entry_id`, no `source`).

So all projects collapse into one undifferentiated graph. Communities mix
topics; a 16-node `gw2-reshade` cluster (Guild Wars 2 ReShade config) looks like
it "invaded" the project graph when it is simply a different project with nowhere
to live. The audit also found, within the un-scoped graph:

- a **duplicate entity** never aliased — `web frontend ("Pseudolife Cortex
  Console")` (community 1) and `Cortex Console web frontend` (community 2) are
  the same thing, splitting the UI cluster across two communities (the source of
  the "why does X bridge to Y" confusion in the session briefing);
- **dubious low-confidence sidecar edges** — `memory_recall →runs-on→
  docker-desktop`, `memory_recall →stores-data-in→ postgres` (a *tool* does not
  run on a host or store data);
- heavy **fragmentation** — ~20 of 42 communities are isolated 2-node dyads;
- **test/smoke artifacts** in the live graph — `payments/payments-db`,
  `pl-healthcheck-target`, `deploy-smoke-*`, `noise agent`.

The correct fix is not deletion. It is to give the graph the **project/topic
dimension** the rest of the bank already has, then make review-and-correct a
first-class, repeatable workflow in the console.

## Goals

- A **seedless, project-scoped** graph view — open the graph and *see something*
  without typing an entity name.
- **Project/topic as a first-class graph facet** — scope to the current project,
  one project, or "all projects" coloured by project.
- A **review workbench ("Atlas")** that surfaces graph-health findings
  (cross-project / unattributed / duplicate / orphan / dubious-edge / test
  artifact) and lets the user fix them through confirm-gated, backup-first
  actions.
- Stop new pollution at the source: new graph data is **born project-scoped**.

## Non-goals

- No change to retrieval ranking, recall, or the dream's fact-consolidation
  behaviour (relation extraction gains a source stamp only).
- No bulk auto-delete. Destructive actions stay explicit, per-finding,
  confirm-gated, and preceded by a backup.
- No new graph rendering engine — reuse the existing canvas `ForceGraph` and the
  vendored 3D galaxy.

## Decisions (from the brainstorm)

- **Shape:** Approach C — a reviewer's workbench ("Atlas"), absorbing the
  seedless whole-graph map as its centerpiece. (A = seedless map only, B =
  community-first overview were considered.)
- **Attribution:** hybrid, two-tier (below). Not single-column; not
  manual-only.
- **Off-topic reframed:** "belongs to another project → assign", not "delete".
  Hard delete reserved for genuine test artifacts.
- **Delivery:** three independently shippable stages; spec covers all three.

## Design

### A. Project attribution (hybrid, two-tier)

An entity may belong to **several** projects (shared infra such as `postgres`,
`docker-desktop` legitimately spans `pseudolife-mcp` and `hermes-*`). Attribution
is therefore a set, not a single value.

1. **Retroactive (derive from existing provenance).** `memory_traces` links a
   fact-slot `(entity_norm, attribute_norm)` to its source `entry_id`, and
   `entries.source` is the project. So an entity's projects =
   `{entries.source for entry in traces_for entity_norm}`. Entities with no trace
   (graph-only nodes, e.g. most `gw2-reshade` entities, which carry no cortex
   facts) derive **no** project and surface as **"unattributed"** in the review
   queue.

2. **Forward (keep fresh).** Relation extraction is batched (N texts → M
   triples with no triple→text mapping), so an edge cannot be stamped with a
   single source at write time. Instead `entity_sources` is re-derived
   incrementally at the **tail of each dream** (after the per-entry fact traces
   are written), riding the same precise `memory_traces` link the retroactive
   path uses. New entities thus become attributed on the next dream, manual rows
   preserved.

3. **Manual override.** "Assign to project" / "mark shared" actions in Atlas
   write explicit attribution that wins over derivation.

**Schema (v16, additive):**

```sql
CREATE TABLE IF NOT EXISTS entity_sources (
  entity_id BIGINT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  source    TEXT   NOT NULL,
  count     INTEGER NOT NULL DEFAULT 1,   -- support strength (distinct traced entries)
  origin    TEXT   NOT NULL DEFAULT 'derived',  -- 'derived' | 'manual'
  updated_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (entity_id, source)
);
CREATE INDEX IF NOT EXISTS entity_sources_source_idx ON entity_sources (source);
```

`origin='manual'` rows are authoritative and are never overwritten by
derivation. A one-time **backfill** populates `entity_sources` from
`memory_traces ⋈ entries` (origin `derived`); it is idempotent and re-runnable.

### B. Backend surface (small, enumerated additions to `routes.py`)

Read:
- `GET /api/graph?entity=&depth=&scope=<project|all>` — extend the existing
  handler so a **blank entity** returns the whole graph from
  `storage.load_graph()`, filtered to `scope` (default = the console's current
  project). Node payload gains `sources: [..]`. Caps + a node ceiling guard the
  whole-graph case (~365 nodes today renders fine; cap defends growth).
- `GET /api/graph/projects` — list projects (sources) with entity/edge counts,
  for the project switcher.
- `GET /api/graph/review?scope=` — new `graph_review` analyzer (below).

Mutations (each a small, explicit, confirm-gated action; backup-first as with
the existing console write actions):
- `POST /api/graph/assign-scope` — `{entity, source, shared?}` → upsert
  `entity_sources` (`origin='manual'`).
- `POST /api/graph/merge` — `{from, into}` → alias `from` into `into`
  (`add_alias` exists at the storage layer) and re-point edges.
- `POST /api/graph/unrelate` — `{src, relation, dst}` → wraps the existing
  `service.graph_unrelate`.
- `POST /api/graph/delete-entity` — `{entity}` → remove the entity and its
  incident edges (CASCADE). For genuine garbage only.

### C. `graph_review` analyzer (`pseudolife_memory/memory/graph_review.py`)

A read-only pass over `load_graph()` + `entity_sources` + the existing
`graph_insight` outputs, returning findings grouped by type. Each finding has a
stable id, a type, a human label, the member entities/edges, and the suggested
action(s). Detectors:

- **belongs-to-another-project** — a community whose entities' attributed
  project(s) differ from `scope`. Action: *assign / confirm project*.
- **unattributed** — entities with no `entity_sources` row. Action: *assign a
  project*.
- **duplicate-entity candidates** — near-identical display names / normalised
  forms / high alias overlap (e.g. the two Cortex Console frontend nodes).
  Action: *merge*.
- **orphan dyads** — degree-1 isolated pairs. Action: *review / leave*.
- **dubious edges** — low-confidence (`≤0.6`) `origin='agent'` edges whose
  relation is semantically implausible for the endpoint types (e.g. a tool
  `runs-on` a host). Action: *prune*.
- **test artifacts** — entities matching known smoke/test name patterns.
  Action: *delete*.

Reuses `graph_insight.god_nodes / surprising_connections / detect_communities`;
adds only the dup/orphan/attribution detectors.

### D. Frontend — the Atlas view

A new view (`views/atlas.js`) registered under the Structure nav group,
reachable from Graph. Layout (per the approved mockup), compacted for the
~680px console width:

- **Toolbar:** search, a **project switcher** (`scope: <project> | all`) as the
  primary control, and the Atlas/Galaxy/Table toggle. The switcher drives every
  view; Galaxy and Table inherit the scope.
- **Review queue (left):** findings from `/api/graph/review`, grouped by type
  with counts and severity colour.
- **Whole-graph map (center):** the seedless, scoped graph. Reuses the canvas
  `ForceGraph`; coloured by **project** in "all" scope and by community within a
  single project. The selected finding's subgraph is highlighted; flagged
  clusters read as spatially separate.
- **Action panel (below the map):** the selected finding's members + the
  confirm-gated, backup-first actions for its type.

Cortex and Insight rows gain a **"Show in Atlas"** link (`#/atlas?entity=…`) so
find → graph is one click — closing the original UX gap.

### E. Staged delivery

1. **Scoping foundation** — `entity_sources` (v16) + backfill + incremental
   re-derive at the dream tail; `GET /api/graph` seedless+scoped;
   `GET /api/graph/projects`. Data becomes project-aware.
2. **Atlas view** — seedless scoped map + project switcher + "Show in Atlas"
   links. *Ships the visualisation win on its own — kills "type a seed to see
   anything".*
3. **Review queue + actions** — `graph_review` + the four mutation endpoints +
   the queue/action-panel UI. Ships cleanup-through-the-UI.

## Data flow

```
dream tail (after per-entry fact traces written)
   → graph_backfill_sources()
       → re-derive entity_sources from memory_traces ⋈ entries (manual rows kept)

console open Atlas (scope=project)
   → GET /api/graph?scope=project        (seedless, filtered by entity_sources)
   → GET /api/graph/review?scope=project (findings)
   → user picks finding → highlight subgraph
   → user acts → POST /api/graph/{assign-scope|merge|unrelate|delete-entity}
       → backup-first → mutate → re-fetch graph + review
```

## Error handling

- Whole-graph fetch is capped (node ceiling + edge cap); over-cap returns a
  truncation flag the UI shows ("showing N of M — narrow scope").
- `graph_review` is best-effort and isolated: a detector failure returns its
  group empty, never 500s the view (mirrors `_safe_refresh_graph_insight`).
- Every mutation is confirm-gated in the UI and backup-first server-side; a
  mutation failure leaves the graph unchanged and surfaces the error inline.
- Derivation/backfill never blocks a write path; it runs as an explicit
  maintenance step and is idempotent.

## Testing strategy

Backend tests run against the mock `FixtureService` (no torch, no Postgres),
matching `tests/test_web.py`:

- `entity_sources` schema v16 present; backfill is idempotent and derives the
  expected project sets from `memory_traces ⋈ entries`.
- Seedless `GET /api/graph` returns the whole graph filtered by `scope`; node
  payload carries `sources`; truncation flag set over cap.
- `graph_review` detectors: a planted duplicate pair is flagged; an
  unattributed node surfaces; a low-confidence implausible edge is flagged; a
  cross-project cluster is flagged under a foreign scope and not under its own.
- Each mutation dispatches and shapes its result; `merge` aliases + re-points;
  `assign-scope` writes a `manual` row that survives a re-derive.
- Relation extraction threads + stamps `source` (unit test on
  `_link_dream_relations(relations, source)`).
- Frontend smoke via the devserver + chrome-devtools (scoped map renders,
  switcher filters, "Show in Atlas" navigates), zero console errors.

## Migration & rollout

- Schema v16 is additive (`CREATE TABLE IF NOT EXISTS` + bump
  `SCHEMA_META_VERSION`); the daemon applies it on startup.
- Backfill is a one-time explicit pass (CLI or a guarded route), re-runnable.
- Deploy follows the standing procedure: `ops/backup.ps1` first, rebuild +
  `up -d --no-deps pseudolife-daemon` only (pg + extractor untouched, never
  `down -v`), rollback tag retained.

## Risks & open questions

- **Graph-only entities have no retroactive provenance.** Mitigated: they
  surface as "unattributed" for one-click manual assignment; the forward stamp
  fixes new data. Acceptable.
- **Duplicate detection precision.** Start conservative (exact-normalised +
  high alias/string overlap) to avoid bad-merge suggestions; a merge is always
  user-confirmed before it runs.
- **Shared-infra entities** legitimately carry many projects; the map must show
  multi-project membership without treating it as an error.
- **Scope of "current project"** — the console isn't inherently tied to one
  project. Default the switcher to the most-active source, persisted per browser;
  "all projects" is always one click away.

## Appendix — concrete findings the review queue must reproduce

| Finding | Members (real) | Action |
|---|---|---|
| Belongs to another project | `gw2-reshade` cluster (16) — GW2 ReShade config | Assign to `gw2-reshade` |
| Duplicate entity | `web frontend ("Pseudolife Cortex Console")` ↔ `Cortex Console web frontend` | Merge |
| Dubious edges | `memory_recall →runs-on→ docker-desktop`; `→stores-data-in→ postgres` | Prune |
| Orphan dyads | ~20 isolated 2-node communities | Review |
| Test artifacts | `payments/payments-db`, `pl-healthcheck-target`, `deploy-smoke-*`, `noise agent` | Delete |
