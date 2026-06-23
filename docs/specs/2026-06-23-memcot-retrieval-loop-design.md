# MemCoT-style Iterative Retrieval Loop — Measurement Harness (Design)

- **Date:** 2026-06-23
- **Status:** approved (brainstorming) — pending implementation plan
- **Branch:** `eval/memcot-retrieval-loop`
- **Author:** agent (Claude) + Pseudogiant

## Motivation

The 2026-06-23 Cowork research briefing surfaced **MemCoT: Test-Time Scaling
through Memory-Driven Chain-of-Thought** (arXiv 2604.08216): reframing retrieval
from single-shot passive matching into an *iterative, stateful search*
(Zoom-In to localize evidence, Zoom-Out to expand context). Our standing
blocker is **Tier-B multi-hop recall**: single-shot `search` is flat vector
retrieval, and although the graph now *holds* relation edges (post-GAM #2
graph-from-text), nothing traverses them across a query. MemCoT is the
retrieval-side complement to GAM's populate-side work.

This spec covers a **measurement harness only** — the smallest real test of
whether an iterative loop lifts multi-hop recall over today's single-shot
`search`. It is not a product change: nothing here touches the daemon, the
service API, or the live bank.

## Goal & non-goals

**Goal.** Quantify the multi-hop recall lift (and its cost) of an iterative
retrieval loop versus single-shot `search`, and cleanly attribute any lift to
*looping* versus *the graph*.

**Non-goals.**
- No change to the shipped retrieval path, MCP tools, or daemon.
- No LLM in the loop for this first test (a controller seam is left for later).
- Not a re-measurement of extraction quality — edges are seeded deterministically
  (extraction is already covered by `relations_bench`).

## Success criteria (measure-first)

No pre-committed pass/fail threshold. The harness reports a comparison curve and
we decide go/no-go from the numbers. Primary metric: **multi-hop answer recall**
— fraction of questions whose gold terminal answer appears in the arm's assembled
context. Secondary (cost): iterations, tokens read, latency. Results are broken
out **by hop-class** (1/2/3-hop) so the guardrail — *no single-hop regression and
no token blow-up on simple questions* — is read directly off the 1-hop row.

**Attribution** falls out of the three arms:
- `lift_from_looping = ArmB − Baseline`
- `lift_from_graph   = ArmA − ArmB`

## Architecture

One dev-only script: `evals/memcot_bench.py`, following existing bench
conventions. It forces `CUDA_VISIBLE_DEVICES=-1` + offline **before** importing
torch/service, and reuses `reset_bench`, `build_service`, `approx_tokens`, and
`value_present` from `ladder_sweep.py` (single source of truth for the isolated
`pseudolife_memory_bench` DB and the scoring primitives).

Per run:
1. `build_service` against the dedicated bench DB.
2. Ingest corpus snippets as `source="bench"` memories (so single-shot `search`
   has real text to retrieve).
3. Seed known-good edges via `svc.graph_relate(src, relation, dst,
   origin="bench", confidence=...)` — auto-creates entities, enforces the closed
   vocabulary, upserts the edge.
4. Run the three arms over the question set.
5. Score, print the comparison table, write `evals/results/memcot.json`.

No served model, no network, live `pseudolife_memory` bank never opened.

## Corpus

A small synthetic graph over the closed relation vocabulary (`depends-on`,
`runs-on`, `part-of`, `uses`, `stores-data-in`, `configures`, `related-to`).
~12–15 snippets forming ~4–5 chains. **Every fact appears twice**: once as a
natural-language snippet (ingested text) and once as a structured
`(src, relation, dst)` edge (seeded into the graph).

Questions are tagged by hop-class:
- **1-hop** (guardrail, ~3 Qs): the answer sits in a single snippet.
- **2-hop** (~4 Qs): e.g. "checkout-svc depends-on billing-lib" +
  "billing-lib runs-on jvm-21" → Q "what does checkout-svc run on?",
  gold = `jvm-21`. The answer snippet is lexically about `billing-lib`, not
  `checkout-svc`, so vector search alone tends to miss it.
- **3-hop** (~3 Qs): a longer chain.

`gold` = the terminal entity name (and/or its fact value), scored with the
existing `value_present` word-boundary check against the arm's assembled context.

Each corpus record carries: `snippet` (ingested text), the `edges` it implies
(seeded triples), the `question`, the `gold` answer, and a `hops` tag.

## Loop engine + Controller seam

A single engine:

```
run_loop(svc, question, controller, use_graph: bool, hop_cap=3) -> LoopResult
```

`LoopState` accumulates: `entities` (set), `texts` (list), `facts` (list),
`iterations`, `queries_issued`. Per iteration:
1. Issue the controller's queries via `svc.search` (fold hits' text into state).
2. If `use_graph`, call `svc.graph_neighborhood(entity, depth=1)` on each **new**
   frontier entity; fold discovered nodes/facts/edges into state.
3. Ask the controller for the next queries / new frontier, or to stop.

**Depth=1 per iteration** (not a single `depth=3` call) so N hops costs N
iterations — keeping `mean_iterations` an honest cost signal and preventing one
deep neighborhood call from trivially short-circuiting the measurement.

`Controller` is a tiny protocol:
- `seed_queries(question) -> list[str]`
- `expand(question, state) -> (next_queries, next_entities, stop)`

`MechanicalController` implements it deterministically: seed = `[question]`;
expand = take the new entities from the last graph pull, reformulate `search`
with their names, stop on no-new-entities or `hop_cap`. The **LLM seam** is a
future `LLMController(Controller)` subclass that calls a served model — zero
engine changes required.

## The three arms

Same engine, different settings:
- **Baseline** — single `svc.search(q, top_k)`, no loop.
- **Arm B (loop, no graph)** — `MechanicalController`, `use_graph=False`
  (expansion = re-search on discovered terms only).
- **Arm A (loop + graph)** — `MechanicalController`, `use_graph=True`.

## Gate

Per measure-first, all arms run on all questions; nothing is suppressed. A
`would_gate(question, baseline_result)` heuristic (enter the loop only when the
baseline is `low_confidence` or its top score is thin) is computed and **reported
as a would-have-decided annotation**, not enforced. This previews the shipped
gate ("don't loop on simple lookups") without distorting the measurement.

## Metrics & reporting

Per arm × hop-class × overall:
- `answer_recall` — gold present in assembled context.
- `mean_iterations`
- `mean_tokens_read` — `approx_tokens` over the assembled context.
- `mean_latency_ms`

Plus the two attribution deltas. Output: a printed table (mirroring
`ladder_sweep`'s report style) and `evals/results/memcot.json`.

## Isolation & safety

Dedicated `pseudolife_memory_bench` DB (created/truncated by the shared
`reset_bench`); CPU-forced; 4090 untouched; no LLM; no network. Identical safety
posture to the other benches. The live `pseudolife_memory` bank is never opened.

## Out of scope / future

- **LLM-driven controller** — `LLMController(Controller)` over a served model
  (the seam exists; not built here).
- **End-to-end variant** — ingest text → real dream extractor builds edges →
  loop retrieves. Confounds extraction with retrieval; deferred.
- **Provider-layer wiring** — if the lift justifies it, wrap the loop around
  `memory_search`/`memory_graph` at the provider/client layer, gated to
  multi-hop / thin-first-pass queries. Separate spec.

## Resolved decisions

1. Loop driver: **mechanical now + LLM seam** (controller protocol).
2. Graph population: **deterministic edges** (model-free; isolates the loop).
3. Success bar: **measure-first, no hard gate** (report the curve, decide after).
4. Bench shape: **3 arms** (baseline / loop-no-graph / loop+graph) for clean
   attribution.
