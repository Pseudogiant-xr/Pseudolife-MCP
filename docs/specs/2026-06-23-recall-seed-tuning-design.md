# `memory_recall` Seed-Selection Tuning (Design)

- **Date:** 2026-06-23
- **Status:** approved (brainstorming) — bench first, then implementation plan
- **Branch:** `feat/recall-seed-tuning`
- **Author:** agent (Claude) + Pseudogiant
- **Predecessor:** `memory_recall` live tool (`docs/specs/2026-06-23-memcot-live-wiring-design.md`, deployed `b04029c`).

## Motivation

Live testing of `memory_recall` showed the **mechanical seeder is too liberal**:
`MechanicalController.seed_entities` matches every known entity present in
`query + " ".join(hits)`, so a populous bank's co-mentioning hit snippets drag in
many irrelevant, often edge-less seeds. Observed: `"what does pseudolife-mcp run
on?"` seeded 7 entities (`pseudolife-mcp`, `postgres`, `docker-desktop`, `Brain`,
`Pseudolife`, `PseudoLife-MCP daemon`, `MemCoT`) — only `pseudolife-mcp` was the
real subject; the rest came from co-mentioning snippets. It still bridged, but
wastes graph calls and latency and muddies the result.

Goal: make the **baseline (no-LLM) seeding** as precise as possible —
**maximize seed precision with zero multi-hop recall loss** — keeping the LLM
driver as an opt-in (its per-call cost is ~seconds + a CPU spike on the Gemma
E2B sidecar vs the mechanical path's sub-ms seeding).

## Goal & non-goals

**Goal.** Pick, by measurement, the mechanical seed heuristic that maximizes
seed precision subject to no drop in `answer_recall` vs the current seeder; ship
it; re-deploy (gated). Also measure the LLM driver's real latency as the
documented perf comparison.

**Non-goals.**
- No change to `memory_search` or any existing retrieval path.
- `recall` stays strictly read-only.
- No enterprise-scale vocab indexing (out of scope; load-and-match still fine).
- The LLM driver is not promoted to default (measured too slow for the hot path).

## Current state

- `recall.py` `MechanicalController.seed_entities(query, hits, vocab)` →
  `[name for name in vocab if _mentions(query + " " + " ".join(hits), name)]`.
- `run_recall` already separates `query` and `hits`, so **query-first needs no
  signature change**.
- `storage.load_graph()` returns `edges` (`src_id`/`dst_id`), so per-entity
  **degree** is computable (map id→display, count incident non-superseded edges).
- `_recall_vocab` currently returns names only (display + aliases).

## Approach (measure-first, then implement the winner)

### 1. Seed bench — `evals/seed_bench.py`

Reuses `ladder_sweep.build_service`/`reset_bench` (dedicated `pseudolife_memory_bench`
DB, CPU only, live bank untouched). Corpus = multi-hop chains PLUS **cross-talk
snippets** that co-mention several entities (some irrelevant to any question's
subject), reproducing the live seed-noise. Each question carries:
- `subject`: the entity the question is about (the required seed),
- `relevant`: the gold on-path entity set (subject + intermediates to the answer),
- `gold`: the terminal answer entity.

**Per-variant metrics** (over the question set): `seed_precision = |seeds ∩
relevant| / |seeds|`; `seed_recall` = fraction of questions whose `subject` is
seeded; `answer_recall` = gold reached; cost = mean `entities_visited`, mean
`graph_calls`, mean `latency_ms`.

**Variants:**
- `V0` — current: entities in `query + hits` (liberal baseline).
- `A` — query-first: entities in the query; fall back to hits only if the query
  matches nothing.
- `A+B` — query-first + degree filter: drop hit-derived candidates with graph
  degree 0 (query subjects are never degree-dropped).
- `C` — ranked + capped: score (query>hit, degree>0, name specificity), take top-N.
- `LLM` — the `LLMController` against the served Gemma E2B sidecar; reports
  `seed_precision` (ceiling) and **measured mean latency** (the perf answer).

### 2. Selection rule

Winner = the **mechanical** variant with the highest `seed_precision` among those
with `answer_recall == V0.answer_recall` (zero recall loss) and `seed_recall == 1.0`
(every subject still seeded). Tiebreak: lowest cost. The `LLM` arm is reported for
the precision-ceiling + latency comparison only — not a default candidate.

### 3. Implementation (winner-scoped)

A pure `select_seeds(query, hits, names, degree, ...) -> list[str]` in `recall.py`
implementing the winning strategy; `MechanicalController.seed_entities` delegates
to it.
- If **A** wins: change is local to the seeder (query-first; no contract change;
  `degree` unused).
- If **A+B / C** wins: thread degree — `_recall_vocab` returns names **+** a
  `degree: dict[str,int]` (from `load_graph` edges), passed through `run_recall`
  to `seed_entities` (contract enriched uniformly for both controllers).

`memory_search` untouched; `recall` read-only; `LLMController` unaffected.

### 4. Live before/after (read-only confirmation)

Via the MCP HTTP client (a 2nd `MemoryService` deadlocks on `ensure_schema` —
established pattern): run the real query `"what does pseudolife-mcp run on"` —
before ≈ 7 seeds; after-deploy expect ~1–2 seeds with the
`pseudolife-mcp → postgres → docker-desktop` bridge preserved and
`low_confidence: false`.

### 5. Gated re-deploy

Proven path: `ops/backup.ps1` first → `docker compose build pseudolife-daemon` +
`up -d --no-deps pseudolife-daemon` (preserve both external volumes, never
`down -v`) → `/health` → live smoke (recall bridge + reduced seed count;
`memory_search` unchanged). Gated on explicit user approval.

## Sequencing

1. Write + commit this spec.
2. **Build + run the seed bench** → determine the winning mechanical variant
   (mirrors the GAM #2 relations bench: measure before planning the code).
3. `writing-plans` for the concrete winner-scoped implementation.
4. subagent-driven execution + per-task review + final review.
5. Gated re-deploy + live before/after.

## Resolved decisions

1. Objective: **max seed precision, zero answer-recall loss**.
2. Validation: **synthetic gold-labeled bench (decision) + live before/after (confirm)**.
3. LLM arm: **measured in the bench** for the latency/precision comparison; not promoted to default.
4. Implementation is **scoped to the bench winner** (decided before the plan).

## Out of scope / future

- Vocab indexing for very large banks.
- Auto-routing or hybrid (mechanical default + LLM only when seeds are ambiguous).
- LLM-driver prompt tuning.
