# External-findings program — design

Date: 2026-07-03
Status: approved (user, 2026-07-03)
Origin: comparison review of GitNexus, graphify, and Penpax/Graphify Labs.

## Goal

Adopt five externally-validated ideas into Pseudolife-MCP, in two waves:

| ID | Feature | Source | Wave |
|----|---------|--------|------|
| G | Betweenness-centrality god-node ranking | Penpax | 1 |
| A | `memory_fact_get` ranked candidates on miss | GitNexus disambiguation | 1 |
| D | EXTRACTED / INFERRED / AMBIGUOUS edge tags | graphify | 1 |
| C | Lesson staleness "re-verify" flags | graphify learning overlay | 2 |
| F | Causal chain — "what led to X" | Penpax trace_path | 2 |

Explicitly deferred (out of scope): `memory_about` 360° view (recall already
bundles cortex + entries; 32-tool budget is tight), "why" rationale nodes in
`document_ingest` (low-traffic surface). Already shipped, not re-done:
surprising-connection push at session start (briefing.py).

## Ground rules

- Branches off `master`: `feat/external-findings-wave1`, then
  `feat/external-findings-wave2` (after wave 1 merges).
- **No schema migrations.** All five features are read-time or additive;
  edges already carry `origin` / `confidence` / `asserted_at` /
  `superseded_at`, lessons carry `about` / `asserted_at`.
- Tool-description budget test stays green (≤1,600 chars/tool, ≤18k total;
  `tests/test_tool_consolidation.py`).
- No daemon redeploy in this program — repo-only; deploy remains a separate
  gated rebuild.
- Every enrichment degrades gracefully: a failed or empty enrichment returns
  the un-enriched result, never an error.

## Wave 1

### G — Betweenness god-nodes

`god_nodes()` in `pseudolife_memory/memory/graph_insight.py` ranks by degree.
Change:

- Compute `nx.betweenness_centrality` on the undirected projection. Exact
  when the graph has ≤5,000 nodes (live bank is ~1–2k post-cleanup);
  k-sampled (`k=min(500, n)`) above that as a guard.
- Rank by betweenness descending, degree as tiebreak, id as final tiebreak
  (determinism).
- Output items keep `degree` and gain `betweenness` (rounded, 4 dp).
- Digest copy frames god-nodes as *bridges* (connectors of communities)
  rather than merely busiest nodes.
- Consumers (digest, briefing) keep working without structural change; the
  pinned ranking-order tests are updated as part of the feature.

### A — `memory_fact_get` ranked candidates on miss

Today a null `record` leaves the caller with nothing (the "null slot ≠
unknown topic" footgun that global CLAUDE.md warns about). Change, in
`service.py` (+ helper in `cortex.py`) surfaced by `mcp_server.py`:

- When `record` is null AND `contenders` is empty, add `candidates` (≤5):
  1. **same_entity** — current facts at the same alias-resolved entity,
     other attributes, recency-ranked.
  2. **similar_slot** — embedding similarity of `"{entity} {attribute}"`
     against current fact-slot embeddings, above a floor (0.35), excluding
     same-entity hits already listed.
- Candidate shape: `{entity, attribute, value, score, why}` where `why` ∈
  `same_entity | similar_slot`.
- Never fabricates a `record`; `record` stays null. Docstring gains one
  sentence pointing at `candidates`.
- If the embedder is unavailable, same-entity candidates alone are returned.

### D — Edge provenance tags

Pure derived tag, computed at read time (no storage):

- `EXTRACTED` — `origin` ∈ {`user`, `action`}.
- `AMBIGUOUS` — edge sits in `edge_proposals`, or `confidence < 0.5`, or is
  currently flagged dubious by the review analyzer.
- `INFERRED` — everything else (agent/dream origin or no origin,
  `confidence ≥ 0.5`).
- Precedence: EXTRACTED wins over AMBIGUOUS (an explicit human/action edge
  is never "ambiguous"); AMBIGUOUS wins over INFERRED.

Attached as `tag` on edge dicts in: `/api/graph` responses, `graph_review`
findings that carry edges, and `memory_graph` MCP responses. Atlas renders a
small badge per edge (static JS/CSS only).

## Wave 2

### C — Lesson staleness ("re-verify")

At lesson read time — `memory_lesson_search`, briefing lesson selection, and
`/api/lessons`:

- Resolve the lesson's `about` (fallback: its task `entity`) alias-aware
  against graph entities / cortex entities.
- If any canonical fact about that entity was asserted or superseded
  **after** `lesson.asserted_at`, attach `re_verify: true` and
  `re_verify_reason: "facts about <name> changed since this lesson"`.
- Computed per read; no stored state, so re-confirming the lesson
  (`last_confirmed` bump) naturally clears the flag when the comparison uses
  `max(asserted_at, last_confirmed)`.
- Briefing renders a `⚠ re-verify` suffix on flagged lessons.
- Resolution failure or no matching entity → lesson returned unflagged.

### F — Causal chain ("what led to X")

New service method `chain(entity, limit=20)`:

- Alias-resolve `entity`; assemble dated events from four streams:
  1. cortex fact assertions + supersession chains for the entity (per slot),
  2. entries mentioning the entity (traces / mention scan), stamped with
     their episode titles,
  3. live graph edges touching the entity (`asserted_at`),
  4. outcome/lesson signals whose `about` resolves to the entity.
- Merge → sort ascending by time → truncate to most recent `limit`.
- Event shape: `{t, kind: fact_set | superseded | entry | edge | lesson,
  summary, refs}` (`refs` carries ids usable with `memory_get` /
  `memory_history` / `memory_graph`).
- Missing streams degrade gracefully (e.g. no graph node → entries + facts
  only).

Surfaces:

- **MCP:** `memory_history`'s `attribute` becomes optional; entity-only
  calls return the chain. No new tool. (`chain` is the internal name;
  `svc.trace` is already retrieval diagnostics.)
- **Console:** `GET /api/chain?entity=…&limit=…`; a chain/timeline view
  reachable from the entity provenance drawer and the Atlas node panel.

## Testing

- TDD per feature; full suite (~785 tests) stays green.
- The only intentionally-changed pinned behavior is god-node ranking order.
- Tool-description budget assertions updated only if descriptions grew.
- Wave 2 adds PG-integration coverage for `chain` assembly and the
  staleness comparison.

## Rollout

Each wave: implement → whole-branch review → merge to master → push.
Daemon deploy is out of scope (separate gated rebuild with backup).
