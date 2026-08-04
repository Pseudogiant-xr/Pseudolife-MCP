# Aggregation-aware recall: timeline retrieval (Phase 1) + chronicle store (Phase 2)

Date: 2026-08-03. Status: design before code. Preregistered gates below;
baselines are the committed 2026-08-03 artifacts.

## Why

The 2026-08-03 full-type runs exposed one convergent weakness across two
independent benchmarks (`evals/results/beam-100k-verdict.json`,
`evals/results/longmemeval-all-oracle-qwen-27b-alltypes-0803.summary.json`):
consolidation preserves canonical facts and loses cross-session
aggregation/ordering.

- LongMemEval: hybrid 0.910 knowledge-update but 0.383 multi-session (rag
  control 0.504) and 0.534 temporal-reasoning (rag 0.526).
- BEAM 100K: hybrid 0.131 event_ordering, 0.245 summarization, 0.278
  contradiction_resolution.
- Row autopsy (same artifacts): on weak types the answer is almost never
  fact-shaped (`answer_in_current_fact`: multi-session 20/133, temporal
  11/133 — vs 35/78 KU, 42/70 single-session-user). Three failure classes:
  (a) events lost to note compression — hybrid context averages ~840
  tokens vs rag's ~1,200 and answers "I don't know" (35–42 IDK-fails on
  weak types vs 2 on strong); (b) answer material present but scattered
  across supersession chains the answerer cannot enumerate; (c) a stale
  canonical fact outranking correct episodic evidence.

Counts, durations, and orderings over ephemeral events are not
entity-attribute-value material; no extractor prompt fixes that. The
literature (survey 2026-08-03, world-cortex `arXiv:2502.01630`,
`arXiv:2605.23986`, `arXiv:2407.09450`, `arXiv:2601.07468`,
`arXiv:2605.06527`, `Hindsight (vectorize.io)`) converges on keeping a
time-anchored layer beside the fact store and never letting consolidation
be the sole surviving representation.

## Phase 1 — retrieval-side: contiguity, timeline channel, enumerable rendering

No schema bump. Three independent changes, each behind its own config knob
(default off until gated):

1. **Temporal-contiguity expansion** (EM-LLM, arXiv:2407.09450). After
   `cms.retrieve()` returns its fused top-k, expand each hit with its
   nearest temporal neighbors — same episode (fall back to same source),
   ordered by `entry.timestamp` — up to `n_neighbors` per hit (default 1
   each side), deduped against existing hits, marked
   `"via": "contiguity"` in the response so consumers and the bench can
   tell expansion hits from direct hits. Knob:
   `memory.search.contiguity_neighbors: int = 0` (off).
   Site: `pseudolife_memory/service.py::search` (post-retrieve, before
   `_entry_to_dict`), neighbor lookup added to the CMS layer beside the
   slot index — enumerate mutation paths per the derived-state checklist
   (`hydrate_cms`/`load()` bypass `store()`).

2. **Timeline retrieval channel.** When the query carries temporal intent
   (detected lexically: ordinal/temporal cue list — "first", "last",
   "when", "how many times", "how long", month names, "before/after"),
   add a channel that returns the query-relevant entries in ascending
   `timestamp` order with their dates rendered, fused via the existing
   BM25-fusion path in `pseudolife_memory/memory/cms.py`. Knob:
   `memory.search.timeline_channel: bool = False`.
   This is the missing fourth TEMPR channel (Hindsight runs
   semantic/BM25/graph/timeline; we have the first three).

3. **Enumerable fact rendering + stale demotion.** The bench's fact
   garnish (`evals/longmemeval_bench.py:381` — "earlier values, oldest
   first: a -> b -> c") renders supersession chains inline where the
   answerer must count over them; the cuisine-count autopsy row shows it
   miscounting material that is fully present. Render chains and set
   members as enumerated lines (one value per line, dated where stamps
   exist). Same treatment in the production cortex block
   (`pseudolife_memory/mcp_server.py::memory_search` cortex rendering).
   Stale demotion per STALE (arXiv:2605.06527): when a surfaced fact's
   slot has a newer superseding version, the older version never renders
   above the newer one, and contested facts carry their marker in the
   first line, not a trailing field.

### Phase 1 preregistered gates

Baseline: `longmemeval-all-oracle-qwen-27b-alltypes-0803` (same banks —
extraction is untouched, so banks are reusable; answer/judge re-run only)
and `beam100k-qwen-0802`. Same harness, same reproducible qwen server,
`compare_arms.py` paired permutation, fresh `--out-tag` per run.

- **Control tripwire:** the rag arm is byte-identical inputs (raw turns,
  no retrieval changes reach it). Any rag delta ≠ 0 invalidates the run —
  investigate before reading other arms.
- **Ship rule (each knob independently, then combined):** hybrid
  multi-session + temporal-reasoning combined accuracy improves with
  p < 0.05 (paired, 10k draws, seed 0), AND no significant regression on
  knowledge-update or the single-session types (non-inferiority margin
  0.02). BEAM event_ordering direction must agree (n=40/ability is too
  small to gate on alone; it is a directional check, not a gate).
- Knobs that pass ship as defaults; knobs that fail stay off with the
  measured result recorded in the verdict artifact
  (`evals/results/agg-recall-phase1-verdict.json`).

### Amendment (2026-08-03, before any gate ran) — within-run variants

Implementation surfaced a wrong assumption above: "banks are reusable, so
answer/judge re-run only". `dump_bank` (`evals/longmemeval_bench.py`)
persists **cortex facts only** — no band entries — and knobs 1–2 operate
on band entries, so per-knob runs would each need full re-extraction
(~4 h × 4 = infeasible). Replacement design, strictly stronger pairing:

- **One run, five hybrid context variants per question**, built from the
  same live service right after its single ingest+dream: `hybrid`
  (vanilla), `hybrid_ctg` (contiguity_neighbors=1), `hybrid_tl`
  (timeline), `hybrid_enum` (enumerated facts, vanilla retrieval),
  `hybrid_all` (all three). Each variant is answered and judged in the
  same row. Every variant shares the identical bank, so deltas are
  knob-only with within-question pairing; the rag control is
  byte-identical across variants by construction (one pinned call), so
  the tripwire becomes an exact-equality invariant rather than a
  cross-run check.
- Gates unchanged in substance, re-anchored: each knob's ship rule is
  its variant vs the same-run `hybrid` arm (paired permutation, 10k
  draws, seed 0, `compare_arms --a-file == --b-file` with a `--types`
  filter added for the multi-session+temporal combined gate and the
  non-inferiority set). The cross-run alltypes-0803 baseline remains
  the sanity anchor for the vanilla hybrid arm (its accuracy should
  reproduce within extraction noise), not a gate input.
- Cost: one ~5–6 h run (extraction unchanged, +4 answered/judged arms
  per question) instead of four ~4 h runs.

## Phase 2 — chronicle store (schema v28): events as first-class records

The dream pass currently extracts only slot claims
(`pseudolife_memory/service.py` claim loop) from batched entries. Phase 2
adds a second extraction output: **event records**.

- **Schema v28**, table `chronicle_events`: id, `occurred_at` (nullable
  timestamptz — event time), `occurred_phrase` (the source's own words:
  "about a month ago" — kept verbatim per the literal-gate philosophy),
  `recorded_at` (transaction time — the mention-time/event-time split of
  arXiv:2601.07468 and Zep's bi-temporal model), actor, description,
  episode, `src_entry_id` (no FK — entries are evictable; same rationale
  as `dream_run_slots`), stamp (HLC), `invalidated_at` (nullable —
  contradiction handling invalidates, never deletes, per Zep/Graphiti).
  Index on (actor, occurred_at) and (episode, occurred_at).
  All 12 schema-bump sites per the shipping checklist, plus
  `tests/test_schema_v28.py` and the `pg_fixtures._ALL_TABLES` entry.
- **Extraction:** the existing batched dream call gains an `events`
  output section (same call, no extra LLM spend): dated occurrences with
  a source citation per event, date resolution anchored to the batch's
  session dates at extraction time (TReMu's dated-summary insight, code
  does the arithmetic later — not the LLM at answer time). The literal
  gate applies to event dates and numbers identically to claims. Events
  are additive-only in v1: no supersession logic beyond
  `invalidated_at`, no dedup beyond exact (actor, occurred_at,
  description-normalized) match.
- **Prompt lineage:** the extractor prompt is pinned
  (`tests/test_op_prompt_artifact.py`); the events section ships as a new
  measured prompt artifact through the existing op-probe + KU-gate path,
  exactly like v6 did. Count-exclusion and op-adoption probes must hold.
- **Serving:** `memory_search` response gains an `"events"` block (top-N
  chronicle hits in ascending occurred_at order) when the timeline
  channel fires; a `memory_dream` docstring bullet documents the new
  output; tool-description budget re-checked
  (`test_tool_consolidation.py`).
- **Journal/rollback:** chronicle writes journal into the existing
  `dream_run_slots` mechanism (kind `"event"`), so `dream_rollback`
  reverts them (delete-on-rollback is safe: additive-only records).

### Phase 2 preregistered gates

- **Ladder conformance first** (dream-write-path change → the ladder is
  mandatory per project memory): `gold_recoverable` and `stale_leak`
  non-inferior to the pre-change run on the same rung; a fresh
  `--out-tag`, canonical files untouched.
- **BEAM 100K re-run** (extraction changes → banks rebuild, full re-run,
  new tag): hybrid event_ordering and summarization improve vs
  `beam100k-qwen-0802`; rag-arm tripwire as in Phase 1. Aggregate hybrid
  must not regress (margin 0.02).
- **LME weak-type gate:** as Phase 1's ship rule, measured against
  whichever baseline is current when Phase 2 lands (Phase 1's verdict
  artifact if it shipped).
- Verdict artifact: `evals/results/agg-recall-phase2-verdict.json`.
  Every number that reaches the docs gets a `test_eval_evidence.py` row.

### Amendment (2026-08-04, at Phase 2 build start) — Phase 1 outcome + implementation decisions

Phase 1 ran and **failed every weak-type gate**
(`evals/results/agg-recall-phase1-verdict.json`): contiguity −0.147
(p 0.00000), timeline −0.011 (p 0.70120), enum −0.071 (p 0.00030),
combined −0.177 (p 0.00000), n=266 paired. All knobs stay default-off.
Consequences and decisions folded into the Phase 2 build:

- **Phase 2's LME weak-type baseline** is the `aggp1-variants-0803`
  vanilla hybrid arm (0.459 on the weak set), which reproduced the
  independent `alltypes-0803` run exactly — the strongest baseline
  available.
- **Serving gates on the temporal cue directly** (`has_temporal_cue`),
  NOT on `timeline_channel` — that knob failed its gate and stays off,
  and the spec's original "when the timeline channel fires" wording
  would have made the events block permanently unreachable.
- **HLC stamp** is implemented as the v11-convention column triplet
  (`hlc_phys`, `hlc_logical`, `writer_id`), not a text column.
- **`occurred_at` is `TIMESTAMPTZ NULL`**; the extractor may only emit
  an exact `YYYY-MM-DD`, and code accepts it only when the batch corpus
  actually contains a date-like span (`_DATE_LIKE_RE`) — a cheap
  fabrication guard: a batch with no date information cannot yield a
  dated event. Otherwise the event stores undated with its verbatim
  `occurred_phrase` and sorts by `recorded_at` behind dated rows
  (`occurred_at ASC NULLS LAST`).
- **Journal targeting**: `dream_run_slots` gains a nullable
  `chronicle_event_id` column (additive, part of v28) so kind
  `"event"` rows can be rolled back by exact id (delete — additive-only
  records make deletion safe).
- **Extraction transport**: events ride the same batched call and the
  same returned list, as dicts with `kind: "event"` — the claim loop
  routes them before slot resolution. Dedup is application-level exact
  `(actor_norm, occurred_at, description_norm)`.
- **Config**: `dream.chronicle: bool = False` until the gates pass; the
  search-response events block needs no knob of its own (an empty table
  serves nothing).
- **Prompt lineage**: variant `v7-chronicle-events` builds on the
  shipped v5 artifact (v6 failed its gate and never shipped); artifact
  `evals/prompts/ku_op_prompt_v7_events.txt`, pinned to the variant.
  The LME date anchors need no harness change — the bench already
  embeds each session's date in the turn text (`[2023/04/10 …] user:`).
- **The BEAM re-run gate is deferred past the build night** (~6.5 h does
  not fit alongside op-probe + ladder); it remains preregistered and
  blocks any claim about event_ordering/summarization until run.

### Amendment (2026-08-04, post-merge) — gate outcomes and substitutions

- **Weak-type gate: measured, then INVALIDATED (serving side).** The
  ev-weak-0804 run's hybrid_ev numbers were contaminated by a
  bench-harness bug (`ladder_sweep.reset_bench` never truncated
  `chronicle_events`, so events accumulated across all 266 questions —
  fixed with a regression lock, PR #95). The clean serving question is
  OPEN. What stands from that run: the rag control at exactly zero, and
  the **v7-prompt extraction effect: −0.053 (p 0.011) on vanilla-hybrid
  claims** — the events section competes with claims extraction on real
  batches, which the op-probe (necessary, not sufficient) cannot see.
  Consequence for any retry: extract events in a SEPARATE pass, never
  riding the claims call.
- **Ladder conformance: substituted by a differential inertness diff**
  (maintainer decision): 14 questions (12 tr + 2 supersession-heavy KU)
  run extract-only through pre-v28 code (e1c4954a) and merged master,
  same v5 prompt, chronicle off, reproducible server — **zero
  differences** (contexts byte-identical, tallies equal, all 114 bank
  facts equal; `evals/results/inert-diff-v28-verdict.json`, producer
  `evals/inert_diff.py`). Rationale: the ladder's breadth serves quality
  measurement; an inertness claim is better served by exact old-vs-new
  pairing on identical inputs, at ~1/4 the GPU cost. Deploy of the
  (default-off) v28 write path is unblocked; a full rung remains the
  escalation if a future diff ever comes back non-empty.

## Design decisions stated up front

- **Additive, never replacing.** Episodic entries, facts, and events
  coexist; nothing is dropped in favor of a summary (arXiv:2605.12978's
  degradation result; TiMem's own ablation — summaries alone lose to
  hierarchy+detail).
- **Event time ≠ mention time.** `occurred_at` resolved at extraction
  against session anchors; unresolvable dates keep `occurred_phrase`
  only and sort by `recorded_at` with an explicit `undated` marker —
  never a fabricated date (literal-gate philosophy).
- **The hierarchy layer (session/interval summaries, MemForest-style
  dirty-path trees) is Phase 3 and out of scope here** — and it is
  blocked on an autopsy of the July TiMem-inspired window-gate failure
  (−0.08 cortex, "key gravity", `2026-07-11-known-facts-window-closure`)
  before any re-attempt.

## Non-goals

- No GPT-4o judging and no comparison against vendor-published BEAM
  numbers — reader-model dependence swamps system deltas (an independent
  BEAM rerun moved contradiction_resolution 0.031→0.347 on harness alone);
  our gates stay same-harness, same-reader, rag-controlled.
- No RL/fine-tuned temporal components (Memory-T1 class) — frozen-LLM
  architecture stands.
- No GraphRAG-style community rebuilds — wrong maintenance profile for a
  cadence daemon.
- No new answering logic in the daemon: production serves context; the
  agent reading it stays the answerer.
