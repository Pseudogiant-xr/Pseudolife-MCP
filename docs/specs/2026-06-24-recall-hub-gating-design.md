# Recall hub-gating — design

**Date:** 2026-06-24
**Branch:** `feat/recall-hub-gating` (off `master`)
**Status:** design approved, pending implementation plan

## Problem

`memory_recall` ([pseudolife_memory/memory/recall.py](../../pseudolife_memory/memory/recall.py))
walks the knowledge graph one hop per iteration to answer relational, multi-hop
questions. Seed selection is already query-first (precision 1.0): the
`MechanicalController` seeds only vocab entities word-present in the *query*,
using search hits as a fallback. That part is solved.

The remaining gap is **expansion-time**. The BFS loop does `frontier = newly`
every iteration — it expands *through every* newly discovered entity, including
super-connected hubs. Once recall reaches a hub like `postgres` or
`docker-desktop`, the next hop pulls in everything attached to it: large token
cost, no recall benefit. There is no guard.

This mirrors a hole that `graphify` (a static-codebase knowledge-graph tool)
plugs in its BFS: it refuses to *traverse through* high-degree hubs while still
*including* them as results. We port that single discipline into `run_recall`,
plus degree-aware frontier ordering and a per-hop expansion budget.

## Goals / non-goals

**Goals**
- Stop recall from exploding through hub entities, with no recall regression.
- Adaptive hub definition that survives bank growth (no stale constant).
- Keep `run_recall` DB-free and unit-testable (its current design).
- Validate against the real `run_recall` code path, not a parallel copy.

**Non-goals (deferred / out of scope)**
- The Track B insight/digest layer (community detection, god-nodes,
  surprising-connections, suggested-questions reports). Evaluated separately
  after Track A.
- Changing seed selection (already query-first / precision 1.0).
- LLM-driver changes.

## Design

### 1. Gating algorithm (`run_recall`)

The gate fires at **expansion time**, mirroring graphify's `_bfs`: a hub is
*included* in results (its facts/edges are captured when it is first
discovered) but never *expanded through*.

New keyword params, all optional so today's behavior is the default when unset:

```python
def run_recall(..., *, hops=3, top_k=5, max_entities=50,
               degree_fn: Callable[[str], int] | None = None,
               hub_threshold: int | None = None,
               expand_budget: int | None = None) -> RecallState
```

Each hop, before calling `graph_fn(name, 1)`, the frontier passes through a
small pure helper `_select_frontier(...)` that:

1. **Exempts seeds** — a seed always expands, even if it is itself a hub (the
   queried subject must be explored). Seeds are exempt from *all three* of the
   gate, the ordering, and the budget below; they always expand in full. Seed
   exemption only matters on hop 1, where `frontier == seeds`.
2. **Gates hubs** — for non-seeds, `degree_fn(name) >= hub_threshold` drops the
   entity from expansion. It stays in results; it is not transited.
3. **Orders by ascending degree** — the surviving non-seed candidates are
   sorted peripheral / more-specific first (least likely to explode), with a
   `(degree, name)` tiebreak for determinism.
4. **Applies the per-hop budget** — keep at most `expand_budget` of the ordered
   non-seed candidates (`0`/`None` ⇒ unlimited). The budget never drops a seed.

`degree_fn=None` *or* `hub_threshold=None` ⇒ no gating, output byte-identical to
the current loop. This is both the backward-compat path and the bench's
"gate off" arm. The existing `max_entities` cap remains the absolute backstop.

The hub itself keeps the facts captured at discovery
(`state.entity_facts[en] = node.get("facts", [])`); only its further expansion
is skipped.

### 2. Data flow (`service.recall`)

The hub threshold is a *global* percentile of the degree distribution, so it
cannot be computed inside the loop (which only sees entities it has touched).
The service computes it once and injects the result:

```python
degrees   = self._graph_degrees()          # {display_name: degree} from self._graph
threshold = _hub_threshold(degrees.values(), cfg.hub_percentile, cfg.hub_floor) \
            if cfg.hub_gate else None
state = run_recall(self.search, self.graph_neighborhood, vocab, query, controller,
                   hops=hops, top_k=top_k, max_entities=cfg.max_entities,
                   degree_fn=degrees.get if cfg.hub_gate else None,
                   hub_threshold=threshold, expand_budget=cfg.expand_budget or None)
```

**Display-name keying (correctness-critical):** the degree map is keyed by
entity **display name**, because that is the currency the loop speaks — vocab is
display names, `graph_fn` (`graph_neighborhood`) takes a display name, and
neighbor nodes carry `node["entity"] = display`. `_graph_degrees()` therefore
maps display name → degree from the read-model graph (`self._graph`).

Helpers:

```python
def _hub_threshold(degrees, percentile, floor) -> int:
    vals = sorted(degrees)
    if not vals:
        return floor
    idx = min(len(vals) - 1, int(len(vals) * percentile / 100))
    return max(floor, vals[idx])
```

**Folded-in deferred minors** (same code, noted earlier): clamp `top_k` in
`service.recall`, and route `cfg.default_hops` through the same `min(…, 5)`
clamp that an explicit `hops` already receives.

### 3. Config (`RecallConfig`, [pseudolife_memory/utils/config.py](../../pseudolife_memory/utils/config.py))

```python
hub_gate: bool = True         # master switch
hub_percentile: float = 95.0  # adaptive percentile
hub_floor: int = 8            # nothing below this degree is ever gated (BENCH-TUNED)
expand_budget: int = 0        # per-hop expansion cap; 0 = unlimited (BENCH-TUNED)
```

`hub_floor` and `expand_budget` defaults are **placeholders pending the bench
sweep** — set from measurement during implementation, not guessed. On a sparse
bank where no degree exceeds `hub_floor`, gating is inert by construction.

### 4. New MCP tools ([pseudolife_memory/mcp_server.py](../../pseudolife_memory/mcp_server.py))

- **`get_neighbors(entity, relation_filter=None)`** — a thin `@mcp.tool()`
  wrapper over `graph_neighborhood(entity, depth=1)`, optionally filtering edges
  by relation. No new graph logic; overlaps `memory_graph` purely as a clearer
  affordance.
- **`memory_path(source, target, max_hops=8)`** — a *dedicated* shortest-path
  query, **not** a wrapper over `graph_neighborhood`'s `to=` branch. It backs a
  small `service.graph_path(source, target, max_hops)` that runs a targeted
  bidirectional BFS (`nx.bidirectional_shortest_path`) between the two entity
  ids over the read-model graph (`self._graph`), with `max_hops` as a
  path-length **cutoff** (return "no path within max_hops" past it).

  Rationale: `graph_neighborhood` finds a path by first materializing a
  depth-bounded subgraph, then locating the target inside it. Raising that cap
  to reach longer paths fans out exponentially around hubs — re-creating the
  blast-radius problem the rest of Track A removes. A targeted bidirectional
  search instead walks *toward* the target, so cost is bounded by actual path
  length, not branching factor: no hub explosion, true shortest paths at any
  reasonable distance, and `graph_neighborhood`'s depth-3 cap stays untouched
  for its own neighborhood use.

## Validation

### Bench rework ([evals/memcot_bench.py](../../evals/memcot_bench.py))

The bench drives the **real** `run_recall` (no parallel copy). Three arms:

- **A1** baseline single-shot search (control; keeps `run_baseline`).
- **A2** `run_recall`, gating **off** (`degree_fn=None`).
- **A3** `run_recall`, gating **on**.

A2 vs A3 isolates the gating effect; A1 stays as the control that the loop still
beats single-shot.

An adapter `assembled_from_recall(result)` flattens `run_recall`'s dict
(`texts` + `entities[].facts` + entity names) into the same list the existing
`gold_recovered` / `tokens_read` scorers consume, so the metric harness is
reused. Per-question records gain an `entities` count.

**Hub fixture** added to the corpus: a high-degree `shared-config` that many
chain heads `depends-on` (checkout-service, order-service, web-portal,
mobile-app, analytics-ui, notify-service), each with a faithful co-mentioning
snippet. This turns the existing 2-hop *"What does checkout-service run on? →
jdk-21"* into the demonstrator: checkout-service's neighbors become
`{billing-engine, shared-config}`. A2 expands `shared-config` and drags in ~5
unrelated services (token bloat, zero recall gain); A3 includes it but does not
transit it, so `jdk-21` is still found via billing-engine — recall flat, tokens
and entities down.

### Success criteria

1. **No recall regression** — A3 recall == A2 recall on every hop-class. Hard
   gate.
2. **Blast-radius drop** — on the hub-adjacent question, A3 `mean_tokens` and
   `mean_entities` materially below A2. Exact threshold set after the first run
   (not pre-committed).
3. **Suite green** — existing `tests/test_recall.py` (11 tests) stays passing
   with gating defaulted on.

## Testing

New unit tests in `tests/test_recall.py`:

- Hub included-but-not-expanded: synthetic `graph_fn` with a hub H connected to
  many neighbors; assert H is in `entities` but H's neighbors are absent.
- Seed exemption: a seed that is itself a hub still expands.
- Degree-order + per-hop budget: only the `expand_budget` lowest-degree
  candidates expand.
- Backward compat: `degree_fn=None` reproduces current `entities`/`edges`
  exactly.
- `_hub_threshold` percentile + floor math, including empty and sparse inputs.

New wrapper tests for `memory_path` and `get_neighbors`.

Bench: manual `python evals/memcot_bench.py --run`, eyeballing criteria 1 and 2.
Dev-only, not CI.

## Edge cases

- Empty / sparse graph → `threshold = floor` → gating inert (nothing exceeds the
  floor).
- Neighbor entity missing from the degree map → degree 0, never a hub.
- All hop-2 candidates are hubs → frontier empties, recall = whatever was
  already found (the intended anti-explosion behavior).
- `expand_budget` interacts with `max_entities`, which remains the absolute
  backstop.
- Ascending-degree ordering uses a `(degree, name)` tiebreak for reproducible
  bench/test runs.

## Files touched

- `pseudolife_memory/memory/recall.py` — gating params + `_select_frontier`.
- `pseudolife_memory/service.py` — `recall()` wiring, `_graph_degrees()`,
  `_hub_threshold()`; top_k / default_hops clamps.
- `pseudolife_memory/utils/config.py` — four `RecallConfig` knobs.
- `pseudolife_memory/service.py` — also `graph_path()` (targeted bidirectional
  shortest path over the read-model) backing `memory_path`.
- `pseudolife_memory/mcp_server.py` — `memory_path`, `get_neighbors`.
- `evals/memcot_bench.py` — 3-arm rework, adapter, hub fixture, `entities`
  metric.
- `tests/test_recall.py` — gating + helper + wrapper tests.

## Provenance

Track A of the graphify competitive evaluation (2026-06-24). graphify's query
path is lexical (IDF + trigram + hub-gated BFS), not embedding-based; the
transferable mechanism here is the hub-gated traversal discipline, layered onto
PseudoLife's existing seeds. Track B (insight/digest layer) is deferred.
