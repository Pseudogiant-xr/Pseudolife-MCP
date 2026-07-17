# LongMemEval-V2 — scoping note (not yet a design)

**Status**: scoping only · **Date**: 2026-07-18 (autonomous session)
**Why**: the 2026-07-17 investigation ranked "publish LME-V2 numbers" as the
highest-value eval direction — the original LongMemEval authors moved the
benchmark to *agent experience memory*, which is far closer to Pseudolife's
real workload (lessons, gotchas, workflow knowledge) than user-chat QA.

## What it is

LongMemEval-V2 (arXiv 2605.12493, Wu et al.): 451 curated questions over
1,870 task trajectories from WebArena-style and ServiceNow-style
environments; haystacks up to 500 trajectories / 115M tokens. Five
abilities: static state recall, dynamic state tracking, workflow knowledge,
environment gotchas, premise awareness. Scored on answer accuracy AND query
latency. Best published method ("AgentRunbook-C") 72.5% vs 48.5% RAG
baseline — unsaturated. Dataset: HuggingFace `xiaowu0162/longmemeval-v2`;
official harness + baselines: github.com/xiaowu0162/LongMemEval-V2.

## Fit with Pseudolife

- **Workflow knowledge / environment gotchas / premise awareness** map
  directly onto the lessons loop and world-fact supersession — the layers
  LongMemEval-KU never exercised. This is the benchmark's core appeal.
- The eval contract ("memory system consumes trajectory history, returns
  compact evidence for QA") matches the existing bench arm structure
  (context-building → answer → judge), so `longmemeval_bench.py`'s phase
  split and the new `replicate.py` layer carry over.

## The three real costs

1. **Ingest adapter.** Trajectories are web-agent action logs (multimodal),
   not chat turns. `ingest_and_dream` needs a new trajectory→turns adapter,
   and a decision about what the "store" policy is (everything vs
   agent-voluntary simulation — ties into the capture experiment).
2. **Scale.** 115M-token haystacks dwarf KU-oracle (~122k). Full-bench runs
   are GPU-weeks with the 27B extractor. First pass must pick the smallest
   haystack tier and probably 2 of the 5 ability categories (workflow
   knowledge + environment gotchas — the lessons-shaped ones).
3. **Latency scoring** brings the in-RAM brute-force retrieval ceiling
   (architecture finding A) into the measured path for the first time.

## Recommended next step (needs user sign-off — GPU budget)

A 1-week pilot: download the dataset, build the trajectory adapter, run ONE
small haystack end-to-end with the qwen-27b extractor and the three
standard arms, and only then decide whether a full category run is worth
the GPU time. Do not start while the eval-hygiene replicate runs own the
GPU.
