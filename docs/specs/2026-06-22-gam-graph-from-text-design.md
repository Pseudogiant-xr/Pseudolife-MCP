# GAM #2 — graph-from-text (dream-populated relations) — design

**Date:** 2026-06-22 · **Status:** approved (design), pending plan
**Sub-project:** #2 of 3 on the GAM track (built on #1 graph foundation; #3 is the
two-tier episodic/semantic GAM). Branch: `feat/gam-graph-from-text`.
**Background:** the Tier-B benchmark finding (2026-06-17) — *"graph only populates
from explicit `memory_graph_relate`, NOT from free-text … multi-hop fails on
ingested text"*; the extractor-ladder methodology (`evals/ladder_sweep.py`); the
sub-project #1 `GraphStore` foundation (`docs/specs/2026-06-22-graph-foundation-design.md`).

## Goal

Teach the **dream** to extract `(src, relation, dst)` triples from the recent
associative stream and write them into the `GraphStore`-backed knowledge graph, so
`memory_graph` works on **ingested free text** — directly attacking the Tier-B
multi-hop blocker.

**Constraints (the decisions that shape this):**
- **Dream-only** — the single-writer dream is the sole automatic graph-from-text
  writer (no store-time extraction; preserves the single-writer cortex design).
- **Populate-only, measure first** — write triples into the graph and make
  `memory_graph` queryable on ingested text; do **not** change retrieval/prefetch
  yet. Then re-run the free Tier-B screen to quantify the lift before building any
  auto-surfacing.
- **Closed vocab + `related-to` fallback** — extracted relations resolve to the
  existing registry; anything unmatched is **kept** as `related-to` (a generic
  association), never coined as a new predicate. The benchmark records the
  `related-to` share as the signal for whether to expand the registry later.
- **Benchmark-decided mechanism** — combined-vs-separate extraction is chosen by
  data on the shipped Gemma-2B floor, not assumed.

**Non-goals:** retrieval/auto-surface changes (deferred slice), store-time
extraction, registry expansion, the episodic event-graph (#3).

## The benchmark-first spine (load-bearing)

Before wiring anything into the shipped dream, a new dev-only eval —
`evals/relations_bench.py` (sibling to `ladder_sweep.py`) — scores **two prompt
variants**:

- **(A) Combined:** the existing `extract()` returns `{claims, relations}` in one
  pass over the entry text.
- **(B) Separate:** a new `extract_relations()` call (mirrors `extract_lessons`),
  a triples-only prompt.

For each variant × model — **Gemma-2B E2B (shipped floor)** and a **Qwen ceiling**
(the 4090 or homelab endpoint, whichever is reachable;
skip unreachable rungs like the ladder sweep) — it reports:

- **triple precision / recall** vs a hand-annotated gold corpus (a triple matches
  when src/dst entities match after `norm_name` and the relation matches after
  closed-vocab resolution);
- **`related-to` share** — fraction of kept edges that fell back to `related-to`
  (vocab-expansion signal);
- **latency** (wall-time per consolidation) and **JSON-parse failure rate**.

**Gate:** the shipped variant must clear a precision/recall floor on **Gemma**
(the optimization target; Qwen is the ceiling/reference). The winner becomes the
shipped extraction path; the loser is dropped. No memory bank is needed — the
bench calls the endpoint and scores against gold directly. Forces
`CUDA_VISIBLE_DEVICES=-1` for the CPU rung; leaves the 4090 alone except the
explicit Qwen ceiling call.

### Benchmark result (2026-06-22, `evals/relations_bench.py`, Gemma E2B floor)

| variant | P | R | F1 | pair-recall | related-to | parse-fail |
|---|---|---|---|---|---|---|
| **separate** | 0.75 | 0.75 | **0.75** | 0.85 | 0.05 | 0 |
| combined | 0.59 | 0.50 | 0.54 | 0.65 | 0.06 | 0 |

**Decision: ship the `separate` `extract_relations()` call** (a 3rd dream call,
mirroring `extract_lessons`) — it beats combined on F1 *and* latency on the weak
2B. **No registry expansion** — the `related-to` fallback share is only 0.05, so
the closed vocab covers real text. The pair-recall→F1 gap (0.85→0.75) is relation
*mislabeling*, a later prompt-tuning lever, not a structural issue. Qwen ceiling
rungs were unreachable (4090 off / homelab down) — Gemma floor is the gate anyway.

## Extraction → validation → graph write

- **Output type:** `RelationClaim {src: str, relation: str, dst: str,
  confidence: float}`. The relations prompt is seeded with the **closed registry**
  (builtin names + descriptions) so the model selects from it — analogous to how
  `extract` is seeded with the cortex slot vocab.
- **Validate / map:** each relation resolved via `graph.resolve_relation` (norm +
  fuzzy match); **unmatched → `related-to`** (kept, not dropped). Entities
  resolved/created via `_resolve_or_create_entity` (alias-aware; **entities stay
  pinned to the Postgres hub** per the #1 boundary). Self-loops (src == dst after
  norm) are dropped.
- **Write (new, single-writer):** `MemoryService._link_dream_relations(relations)`
  — mirrors `_link_lesson_graph`: upserts entity nodes + edges via
  `self._graph.upsert_edge(...)` with `origin="agent"` (dream-inferred) and a
  tunable confidence (default ~0.6, below explicit `graph_relate`'s 0.8 and
  lessons' 0.7). Re-assertion bumps confidence (existing `upsert_edge` behaviour),
  so repeated mentions strengthen an edge.

## Data flow & failure isolation

`dream_run` pulls entries → extractor returns claims (+ relations) →
`cortex_write` facts (existing) → **resolve+map+upsert relation edges (new)** →
`dream_commit`. **Failure isolation:** relation extraction/writing is best-effort
(like `synthesize_lessons`) — a relations parse/extract failure must **not** drop
the fact claims or break the dream. In the combined variant, the claims are
committed even when the `relations` array is missing/malformed.

## Config

`memory.dream.extract_relations: bool` (default **on** — the single-writer dream is
the graph's text writer) plus `memory.dream.relation_confidence: float`
(default 0.6). Off = exactly today's behaviour.

## Success criteria (verifiable)

1. `relations_bench.py` runs and reports both variants on Gemma + a reachable
   Qwen ceiling; a winner clears the Gemma precision/recall gate.
2. A live dream over realistic text populates graph edges — `memory_graph` returns
   ingested relations **and** their derived transitive/inverse edges.
3. The free **Tier-B screen** shows multi-hop recall on ingested text lifting
   meaningfully above the pre-change baseline; the `related-to` share is recorded
   (expand-or-not signal).
4. Full suite green; relations failure-isolation covered by a test.

## Testing

Unit: `resolve_relation` mapping + `related-to` fallback + self-loop drop;
`_link_dream_relations` (entities pinned to storage, edges via `GraphStore`,
origin/confidence correct); `dream_run` integration (relations populated end to
end against the test PG); failure isolation (malformed relations ⇏ lost claims).
Plus the bench harness itself (a tiny gold corpus + the scorer).

## Build order (for the plan)

1. The bench harness + gold corpus + both prompt variants → **run it, pick the
   winner** (this resolves combined-vs-separate before the wiring is finalized).
2. Implement the winning extractor path (`RelationClaim` + the chosen
   `extract`/`extract_relations` shape).
3. `_link_dream_relations` + `dream_run` wiring + config + failure isolation.
4. Tests; then re-run the Tier-B screen for the success gate.
