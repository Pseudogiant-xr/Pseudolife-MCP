# BEAM adapter — external-benchmark comparability (design, pre-implementation)

Date: 2026-08-02. Status: design only; implementation gated on the
judge-model decision below.

## Why BEAM

The 2026-08 benchmark survey (world cortex: `BEAM benchmark /
provenance_and_fit`) picked BEAM (arXiv 2510.27246, ICLR 2026) as the one
external memory benchmark worth adapting to:

- **Independent**: academically authored, open harness
  (`github.com/mohammadtavakoli78/BEAM`) — unlike LoCoMo (6.4% gold errors,
  fading) and unlike vendor-run suites.
- **Tests our bets**: its ten dimensions include contradiction resolution
  (→ supersession), event ordering (→ temporal stamps), abstention, and
  preference tracking — the axes Pseudolife's cortex was designed around.
- **Competitor-published**: Hindsight (64.1% @10M) and Mem0 (70.1% @1M,
  self-reported) both publish on it — a credible comparison surface.
- **Scale-honest**: 100K–10M-token conversations do not fit a context
  window, closing the "LME-S fits in context now" criticism.

## Shape of the adapter

BEAM's harness evaluates a *memory system* through ingest/answer hooks over
its procedurally generated conversations, nugget-scoring answers
(1.0/0.5/0.0 per atomic fact). The adapter is one new eval module
(`evals/beam_adapter.py`) exposing Pseudolife as that system:

1. **Ingest**: stream conversation turns through `store()` +
   dream-per-session cadence (the production shape, same as the LME bench);
   the extractor is the deployed dreamer (Claude shim primary — the
   per-request model override makes the model an adapter parameter).
2. **Answer**: `build_contexts`-style retrieval (cortex/hybrid arms) + the
   local answerer; BEAM's own nugget scorer judges.
3. **Isolation**: bench Postgres, per-conversation truncation — the live
   bank never touched (ladder/LME discipline).
4. Artifacts per run committed with the claim (`evals/results/beam-*.json`),
   evidence rows in `test_eval_evidence.py` for any published number.

## The open decision (operator): judge/answer model

Published BEAM numbers use GPT-4o for answer generation and judging. Two
honest options:

- **A. Local-reproducible** (Qwen answerer + nugget scorer): zero API cost,
  bit-reproducible, but numbers are only self-comparable — publishable with
  an explicit "not comparable to GPT-4o-judged results" caveat.
- **B. Same-model comparability** (GPT-4o via API for answer+judge): direct
  comparability with Hindsight/Mem0 rows; estimated cost scales with tier —
  the 100K tier (100 conversations × ~2k questions total) is the affordable
  entry; 1M+ tiers multiply token volume roughly linearly.

Recommendation: start with A at the 100K tier to validate the adapter
end-to-end, then decide B with a measured token count in hand rather than
an estimate.

## Non-goals / risks

- 10M-tier runs: out of scope until the 100K tier is clean (wall-clock and
  storage are both untested at that scale).
- BEAM's procedural generator is versioned — pin the dataset artifacts we
  run against, or numbers drift with their releases.
- No claims in docs until an artifact + evidence row exist (house rule).
