# Arm B′: slot-key canonicalization for the Sonnet distill labels

**Date:** 2026-07-10
**Status:** approved (brainstorm 2026-07-10)
**Predecessor:** Stage 1.5 arm B (docs/superpowers/plans/2026-07-07-extractor-stage15-jepa-sonnet.md, Tasks 4–10)

## Problem

Arm B (E4B QLoRA retrained on Sonnet recall-boosted labels) lost the KU-oracle
gated eval: cortex 0.577 vs deployed baseline 0.615. Post-mortem with the
Qwen-free per-row diagnostics flipped the interpretation:

- **Capture is better, not worse.** `answer_in_current_fact` (string match, no
  LLM judge): sonnet 0.538 vs baseline 0.385, with denser banks (5.6 vs 8.4
  facts/question).
- **The entire deficit is supersession discipline.** All 13 sonnet
  present-but-wrong rows had the gold answer verbatim in the served context;
  Qwen abstained on 10 and answered the stale value on 3, because the bank held
  contradictory duplicate slots (e.g. `pre-approved-loan-amount: $400,000`
  coexisting with `loan-pre-approval-amount: $350,000`). Run stats: sonnet 78
  supersessions vs baseline 156.
- **Root cause is in the labels.** Per-question chain analysis of the training
  sets: Qwen labels reuse slot keys exactly 24.1% of claims with 9.9% near-miss
  duplicates; Sonnet labels reuse only 6.8% with 23.6% near-miss duplicates.
  Sonnet re-describes the same property with fresh key wording instead of
  reusing the hinted key. Sonnet's reuse + near-miss ≈ 30% — canonical merging
  of the near-misses restores reuse parity with Qwen without new labeling.

## Decisions (user-approved)

1. **Cheap deterministic repair first** — canonicalize keys in the existing
   Sonnet labels; no Sonnet re-label unless this fails.
2. **Repaired-Sonnet-only training set** (1,727 rows) — cleanest ablation vs
   arm B. The union-dedup set (repaired sonnet + 1,131 qwen-only sessions ≈
   2,858 rows; only 625 sessions overlap between the sets) is pre-analyzed and
   queued as B″ if B′ is marginal. Straight concat is rejected: 625 sessions
   would carry conflicting labels (Qwen ~5-claims-verbose vs Sonnet
   ~2-claims-dense for the same input).

## Design

### Component 1: canonicalizer inside `distill_datagen_sonnet.py --ingest`

New flag `--canonical-keys`. Placement is the load-bearing choice:
after per-question claim validation, **before** the deterministic vocab-chain
recompute. Walk each question's claims chronologically; when a claim's
`(entity, attribute)` is a near-miss of a key already seen for that entity in
the same chain, rewrite the attribute to the earlier (first-seen) key. Because
the vocab chain is recomputed afterwards, the prompts' vocab hints match the
repaired labels structurally — no separate prompt patching.

Source data: `evals/data/sonnet_out/*.jsonl` (all 50 question files intact).
Output: `evals/data/distill-extract-sonnet2.jsonl` (new file; the arm-B
dataset is left untouched for comparability).

**Matcher (conservative, deterministic):** normalize via the existing
`_norm_key`, split to tokens, lightly stem (strip -ed/-ing/-al/-s style
suffixes so approved ≡ approval), then merge on:
- stemmed-token-set **equality** (handles reordering + wording variants:
  `pre-approved-loan-amount` ≡ `loan-pre-approval-amount`), or
- subset-plus-one-generic-token (`wake-time` ≡ `wake-up-time`).

Explicitly NOT the loose ≥n−1 overlap rule used in the diagnostic measurement
(it would merge `bedroom-color` into `bedroom-size`).

### Component 2: verification before training

- Re-run the chain reuse metric on `distill-extract-sonnet2.jsonl`; **gate:
  exact reuse ≥ 0.20** (hard floor; expect 0.25–0.30 ≈ Qwen parity; arm B
  was 0.068).
- Hand-audit ~30 sampled merges (T7-style) for false positives.
- `distill_clean.py --src distill-extract-sonnet2.jsonl --dst
  distill-extract-sonnet2-clean.jsonl` (unchanged reuse of T6).

### Component 3: train (proven recipe verbatim)

`distill_train_e4b.py --data evals/data/distill-extract-sonnet2-clean.jsonl
--out-dir ~/e4b-sonnet2 --save-steps 25 --no-eval --logging-steps 10`, plain
SFT (no JEPA), compile ON (load-bearing for fused CE),
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, pre-flight VRAM headroom
check ≥3–4GB, frozen-step watchdog. ~2.5h + merge + GGUF Q4_K_M →
`evals/models/e4b-sonnet2-Q4_K_M.gguf`.

### Component 4: gated eval (identical procedure, tags `sonnet2` /
`sonnet2-baseline`)

Ladder on the bench sidecar (gate: gold_recoverable ≥ 0.9, stale_leak = 0.0 —
the hard backstop against false merges), then same-sitting KU-oracle pair vs
the deployed `e4b-extractor` GGUF, answer+judge both tags in one Qwen sitting.
**Deploy iff cortex > baseline AND > 0.564.** Extra diagnostics to report
alongside accuracy: supersession count (predict ≈ 2× arm B's 78) and
`answer_in_current_fact` (predict ≥ 0.53, i.e. capture preserved).

## Failure interpretations

- B′ within noise of baseline → run B″ (union-dedup set).
- Capture (`answer_in_current_fact`) **drops** vs arm B → the merges damaged
  labels; fall back to the full Sonnet re-label with a hard key-reuse mandate
  in `evals/prompts/sonnet_recall_system.md`.
- stale_leak > 0 at the ladder → hard stop; inspect merged keys.

## Out of scope

Straight-concat training, JEPA (arm A closed neutral), changes to the
production extraction prompt or dream pipeline, the dev-domain extractor
follow-on.
