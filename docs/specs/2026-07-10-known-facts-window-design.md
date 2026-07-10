# Known-facts window for the dream pass (TiMem-inspired)

**Date:** 2026-07-10
**Status:** CLOSED 2026-07-11 — gate FAILED, mechanism closed. Same-sitting
2x2 (n=78): e4b-ft cortex 0.603→0.513, qwen-27b cortex 0.577→0.500 with the
window on; both extractors regress ~0.08, so the mechanism (not e4b-ft
format shift) is at fault. Echo check PASS, ladder clean. Diagnostics:
e4b-ft losses were extraction losses (10/11 flips lost the gold answer from
current facts; supersessions 156→118); qwen-27b captured MORE answers
(answer_in_current_fact 28→33) yet scored lower — key gravity toward shown
slots degrades retrieval ranking. `known_facts_window` stays 0 (code inert,
default-off). Results archived in `evals/results/*-w0/-w20*`.
**Origin:** TiMem (arXiv 2601.02845) historical-window consolidation, adapted to
PseudoLife's flat slot cortex. Successor experiment to Stage 1.5 (closed
2026-07-10: training-side arms neutral or negative; forward hypothesis was
"fix key reuse at the point the extractor writes, not post-hoc").

## Problem

The dream extractor sees relevance-ranked slot *keys* (`_dream_vocab` →
`CortexStore.vocab_ranked`) but never current *values*. It therefore cannot
tell that a note contradicts an existing fact, and mints paraphrase-variant
keys instead of superseding — the knowledge-update failure mode measured on
the LongMemEval-KU bench (`answer_in_history_only`, low supersession rates on
oracle). TiMem's consolidation avoids this by feeding each consolidation call
a window of recent same-level memories so the LLM detects contradictions and
supersedes in-prompt.

## Mechanism

When `memory.dream.known_facts_window = N > 0`, `dream_run` additionally
fetches the current values of the top-N relevance-ranked slots and renders
them into the extractor's system prompt as a `Current known facts:` block:

```
Current known facts (for key reuse — if a note updates one of these, emit the
claim under the SAME entity and attribute with the new current value; never
emit a claim the notes do not state):
- <entity> — <attribute>: <value>
...
```

Relevance ranking reuses the existing batch-text embedding (one encode,
shared with the vocab hint). TiMem's literal "3 most recent same-level
memories" is translated to relevance-matching because PseudoLife's cortex is
a flat slot store, not a temporally scoped tree; recency would bench well on
small oracle banks and degrade on the live bank.

Downstream supersession machinery (slot collision → HLC supersede) is
unchanged. The intervention only makes collisions happen by showing the
extractor what exists.

## Touch points

| File | Change |
|---|---|
| `pseudolife_memory/memory/cortex.py` | `facts_ranked(emb, limit)` sibling of `vocab_ranked`, returning `(entity, attribute, value)` triples; values truncated to ~120 chars |
| `pseudolife_memory/service.py` | `_dream_known_facts(texts)` helper mirroring `_dream_vocab` (same embedding, never raises, falls back to `[]`); wired into both the batch and isolated-retry extraction paths in `dream_run` |
| `pseudolife_memory/memory/dream.py` | `DreamExtractor.extract` gains optional `known_facts` keyword (default `None` = today's behavior; NoOp/Regex ignore it); `OpenAICompatExtractor` renders the block |
| `pseudolife_memory/utils/config.py` | `memory.dream.known_facts_window: int = 0` — default **off**; nothing changes anywhere until deliberately enabled. Working value when enabled (bench + eventual deploy): **20** |
| `evals/longmemeval_bench.py` | `--window N` flag setting the config on the bench service; window state folded into the run tag |

## Echo risk (the designed-against failure mode)

A window fact the notes never mention must not be re-emitted as a fresh
claim. Guards:

1. Prompt instruction: the block is for key reuse only; never emit a claim
   the notes do not state.
2. A dedicated echo test: dream over notes unrelated to the window facts;
   assert no claim's value echoes a window value.
3. The extractor ladder (`evals/ladder_sweep.py`) re-run with the window on
   must hold stale_leak 0.0 and gold ≥ 0.9 (standing rule: re-run the ladder
   after any dream-write-path change).

## Bench design and gate

Same-sitting 2×2 on LongMemEval-KU **oracle**, extractor × window:

| | window off | window on |
|---|---|---|
| **e4b-ft** (shipped) | fresh re-baseline | gated arm |
| **qwen-27b** (ceiling) | control | mechanism control |

The qwen-27b pair exists because e4b-ft was fine-tuned on the windowless
prompt format — the block is a distribution shift for it. If 27B lifts and
e4b-ft doesn't, the mechanism is validated and the follow-up is
window-formatted datagen + retrain; if neither lifts, the mechanism is closed.

**Pass gate (all required):**
- e4b-ft window-on **cortex arm** ≥ window-off cortex arm **+ 0.05**
  (same-sitting re-baseline, not the archived 0.564)
- e4b-ft hybrid arm does not regress
- ladder clean with window on (stale_leak 0.0, gold ≥ 0.9)

**Secondary diagnostics (explain, don't gate):** `answer_in_current_fact` up,
`answer_in_history_only` down, supersession count up.

## Rollout

Gate passes → standard deploy: `ops/backup.ps1` first, tagged rollback image,
flip `known_facts_window` in live config, `up -d --no-deps pseudolife-daemon`
(never `down -v`), watch the next organic dream cycles. Gate fails → config
stays 0, code is inert, outcome logged to memory.

## Testing

- Unit: prompt-block formatting; `extract()` backward-compat (no
  `known_facts` → byte-identical request); `_dream_known_facts` fallback.
- Echo test (above).
- PG integration: end-to-end dream with window on writes/supersedes facts.
- Existing suite stays green (default-off flag; no existing test should move).

## Out of scope (deliberate)

- Session-summary level (TiMem L2) — only if this arm passes.
- Recall-side TiMem (complexity planning, recall gating) — 2 LLM calls/query
  is a live-latency risk on the CPU sidecar; revisit as an offline
  `rebuild_contexts` replay experiment.
- Post-extract key reconciliation — ruled out by Stage 1.5 arm B-prime
  (23 true merges / 3257 keys) and the stored key-mergeability lesson.
