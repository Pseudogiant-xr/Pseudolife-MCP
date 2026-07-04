# Bespoke extraction model — design

**Status**: draft (scaffolding phase) · **Date**: 2026-07-04

## Why now — the decision gate resolved

The 2026-06-27 decision was: measure whether the shipped Gemma 4 E2B sidecar
is a strong-enough extractor before committing to a bespoke-model project.
The 2026-07-04 LongMemEval knowledge-update benchmark answered it:

| extractor | KU oracle, cortex arm | ladder stale_leak |
|---|---|---|
| Gemma 4 E2B (shipped floor) | **0.192** | 0.1 |
| Qwen3.6-27B (local ceiling) | **0.564** | 0.0 |

The RAG control stayed flat across extractors (0.56–0.62), isolating
extraction quality as the *only* moving part. The floor extractor loses
two-thirds of the fact spine's accuracy. Users with a big local model can
point the dream at it (`ops/.env` override), but the **default install** —
CPU sidecar, no GPU — is capped by E2B's extraction.

**Goal: a 2–4B extraction model that approaches 27B-class extraction
quality at sidecar cost, shipped as the default GGUF.**

## Success criteria (measured, existing harnesses)

1. **Ladder** (`evals/ladder_sweep.py`): gold ≥ 0.9, stale_leak = 0.0 —
   parity with the 27B on the axis that matters (consistent slot naming
   across initial/update turns, so supersession actually fires).
2. **LongMemEval-KU oracle** (`evals/longmemeval_bench.py`): cortex arm
   ≥ 0.40 (baseline 0.192; ceiling 0.564).
3. **Latency/size**: within ~1.5× of the current E2B sidecar (CPU
   inference budget unchanged; same Dockerfile.extractor packaging).

## Approach — two stages

### Stage 1: SFT distillation from the teacher we already own

Qwen3.6-27B (the measured local ceiling) generates silver labels over
conversation corpora using the **exact production prompt** (`_SYSTEM_PROMPT`
+ `_vocab_hint` + numbered-notes user message, `memory/dream.py`). The
student is LoRA/QLoRA-tuned on `(system+vocab, numbered notes) → claims
JSON` chat pairs. Current practice puts useful extraction gains at
200–500 curated examples; we target 1–3k (cheap — the teacher runs at
~93 tok/s locally).

Key data-design points (implemented in `evals/distill_datagen.py`):

- **Corpus**: LongMemEval `_s` haystack sessions from questions **outside**
  the knowledge-update subset, further excluding any session id that
  appears in *any* KU question's haystack — zero contamination of the eval.
  (Haystacks are real multi-session chat with timestamps, the exact
  production input shape.)
- **Vocab evolution simulated**: sessions of one haystack are processed in
  chronological order against one simulated bank; the vocab hint grows from
  the teacher's own prior claims. This teaches the *slot-key-reuse*
  behaviour that makes supersession work — the specific capability the
  floor model lacks (the ladder showed E2B splitting initial/update onto
  sibling slots).
- **Quality gates on silver labels**: JSON parses, schema-valid claims,
  `source` indices in range, entity/attribute non-empty. Rows that fail are
  dropped, not repaired.
- **Empty-claims examples kept** (capped share): the student must learn to
  emit `{"claims":[]}` on smalltalk, or it will hallucinate facts.

### Stage 2 (upgrade path): on-policy self-distillation

[OPSD, arXiv 2601.18734](https://arxiv.org/abs/2601.18734) — the
teacher-with-privileged-context trick maps cleanly onto extraction: the
teacher conditions on (notes + gold claims), the student on notes alone,
with token-level distribution matching along the student's own
trajectories. Extracts more signal per example than SFT. Only worth it if
Stage 1 plateaus below the success criteria; the SFT dataset is reusable.

## Student candidates

| model | why | risk |
|---|---|---|
| Gemma 4 E2B (current sidecar) | zero packaging change; QAT variants exist | MatFormer fine-tune support in tooling to verify |
| Gemma 4 E4B | already the "no-GPU middle option" in README; more headroom | ~2× sidecar cost |
| Qwen3-1.7B-class | OPSD paper's demonstrated size; strong JSON discipline | new license/template in the sidecar image |

Decide after Stage-1 data exists — same dataset trains any of them.

## Training + packaging (4090)

QLoRA via unsloth/TRL, single 4090, hours not days at these sizes. Export:
merge → GGUF convert → Q4 quantize → `ops/Dockerfile.extractor`
`MODEL_URL`/mount (the bake path already exists). Ladder + KU oracle rerun
gates the swap; the extractor is single-purpose, so catastrophic forgetting
of general chat is acceptable — JSON extraction fidelity is the only
regression axis (guard: the relation bench + lesson-synthesis bench should
not regress if the model also serves those paths).

## Risks

- **Silver-label ceiling**: the student can't exceed the 27B teacher. Fine —
  the target IS teacher parity at sidecar cost.
- **Teacher quant noise**: the 27B runs UD-Q4_K_XL + tbq4_0 KV; a ladder
  A/B against mainline q8-KV serving is queued to bound this before mass
  generation.
- **Gemma licence**: fine-tuned redistribution is permitted under the Gemma
  terms with notice; verify before publishing the GGUF.
- **Eval overfitting**: LongMemEval-KU stays held out entirely; the ladder
  corpus is synthetic and disjoint.

## Deliverables checklist

- [x] This design doc
- [x] `evals/distill_datagen.py` — teacher-labeling pipeline (resumable,
      contamination-filtered, quality-gated, chat-JSONL output)
- [ ] Generate ~1–3k rows (needs the 27B up; fits alongside the overnight
      GPU schedule)
- [ ] Label-quality audit (sample ~50 rows by hand)
- [ ] Training run (unsloth QLoRA) + ladder gate
- [ ] LongMemEval-KU oracle rerun with the tuned student as extractor
- [ ] GGUF export + sidecar bake + docs
