# Benchmarks

What the memory actually buys, measured. Part of the
[user guide](../../README.md#documentation); the full methodology and every
finding live in [`evals/README.md`](../../evals/README.md).

## LongMemEval — knowledge updates

Measured on the **knowledge-update subset of
[LongMemEval](https://arxiv.org/abs/2410.10813)** (78 questions) — the
"user's facts change over time" ability the HLC supersession spine exists
for. Everything local: extraction, answering, and LLM-as-judge grading all
run on the author's own hardware (judge = Qwen3.6-27B at temperature 0),
so compare *within* the table, not against GPT-4o-judged leaderboards.

> **Reading the numbers.** Accuracies below are single-run point
> estimates unless marked mean ± std. Repeated runs of an *identical*
> config vary by several points from answerer/judge noise alone (observed
> spread: ~7.7 pp on the cortex arm at n=78), so small single-run
> differences between configs are not meaningful. Decision-grade
> comparisons use replicates and a paired test — see
> [Variance and replication](../../evals/README.md#variance-and-replication).

On the oracle variant (evidence sessions only), with the local-ceiling
extractor (5 replicates, 2026-07-19 context-persisted bank):

| arm | accuracy (mean ± std) | context tokens/question |
|-----|----------------------|------------------------|
| naive RAG (top-6 turns) | 0.567 ± 0.017 | 1638 |
| cortex facts only | 0.559 ± 0.030 | **~60** |
| **hybrid (facts + top-3 turns)** | **0.710 ± 0.019** | ~1000 |

The consolidated-facts posture beats naive RAG by ~14 points while
reading ~60% of the context — and the fact spine alone matches RAG's
accuracy on **under 4% of its token budget**. Notably, the shipped E4B
fine-tune's replicated hybrid (0.762 ± 0.027, table below) beats this
27B ceiling — on knowledge updates, the specialised small extractor
outperforms generic bigger models.

## Replicated results (2026-07-18)

The first 5-replicate runs (same banks, answer/judge phase re-run per
replicate; mean ± std) on the shipped-default fine-tuned extractor
(`e4b-ft`, Arm-1) vs its same-model pre-fine-tune baseline:

| arm | Arm-1 (shipped default) | baseline | paired p (78 questions) |
|-----|------------------------|----------|-------------------------|
| naive RAG (control) | 0.574 ± 0.006 | 0.585 ± 0.015 | — |
| cortex facts only | 0.682 ± 0.017 | 0.603 ± 0.013 | **0.17** |
| hybrid | 0.762 ± 0.027 | 0.749 ± 0.015 | 0.83 |

Read honestly: the Arm-1 fine-tune's cortex-arm gain has a +8-point point
estimate but does **not** clear the pre-registered p < 0.05 on the paired
per-question test — the fine-tune fixes some questions and regresses
others, so the evidence for the shipped default is *suggestive, not
confirmed*, and the hybrid arm shows no measurable benefit at all. The
earlier single-run "+0.102" comparison overstated the effect. (The
ceiling table above was renumbered 2026-07-19 from a fresh
context-persisted 5-replicate run — its historical single-run
predecessor, hybrid 0.705, landed inside the replicated band.)

## LongMemEval-V2 — agent trajectories and procedures

[LongMemEval-V2](https://arxiv.org/abs/2605.12493) (Wu et al.) is a
different content class from the KU benchmark above: WorkArena **agent
trajectories** — what an agent saw and clicked in an enterprise portal —
rather than chat sessions. A 10-question `procedure` slice, 3 replicates,
full 100-trajectory haystacks, scored by the benchmark's own deterministic
eval functions:

| arm | default answer prompt | composition-aware prompt |
|-----|----------------------|--------------------------|
| naive RAG (control) | 0.300 [0.30–0.30] | 0.500 [0.40–0.60] |
| cortex facts only | 0.167 [0.00–0.30] | 0.233 [0.10–0.30] |
| hybrid | **0.533 [0.50–0.60]** | **0.633 [0.60–0.70]** |

**Hybrid beat both single channels in every replicate under both prompts** —
the clearest evidence so far that the fact spine and raw associative recall
are complementary rather than redundant.

Read honestly: 10 questions in one category is a *pilot*, not a headline.
The spread is wide, the cortex arm is the most run-to-run volatile (the
extractor's generation varies between runs even at temperature 0), and none
of this carries the 78-question paired testing the KU results above do.

The more useful number is the starting one: **every arm scored 0.000**
before five adapter and extraction fixes. The decisive fix was ours to make
because the bug was ours to have caused — the trajectory-mode extraction
prompt said "extract exactly two kinds of claim and nothing else", so the
model *correctly* discarded the knowledge-base protocol articles that the
gold answers were drawn from. Naming a third class (what a document
prescribes) recovered the category, and the lesson was folded back into the
shipped extraction prompt — see [what the extractor
captures](dreaming.md#what-the-extractor-captures).

## Band structure — the continuum earns nothing on ranking

The 8-band cosine continuum is the memory's headline structure, so it is
worth asking what it buys. An offline ablation rebuilt every KU answer
context from the same banks with the bands collapsed into a **single flat
cosine pool**, under two timestamp regimes (`wall` — every entry stamped
now; `hist` — realistic aging), 5 replicates each, paired permutation test
over 78 questions:

| arm | Δ continuum − flat (`wall`) | p | Δ (`hist`) | p |
|-----|---------------------------|------|-----------|------|
| naive RAG | −0.067 | 0.10 | **−0.090** | **0.015** |
| cortex facts only | +0.008 | 0.76 | −0.010 | 0.53 |
| hybrid | −0.023 | 0.24 | +0.018 | 0.47 |

The continuum does not beat a flat pool anywhere, and under realistic aging
it is **significantly worse** at raw-turn selection. This is published
as-is because a negative result about one's own centrepiece is exactly the
kind that quietly goes unpublished: whatever the banding earns, it is not
retrieval ranking. Any case for it has to rest on the write side —
eviction, capacity, consolidation cadence — not on finding better answers.

## Extraction quality is the dominant factor

Running floor (Gemma 4 E2B, the smallest CPU-sidecar bake) vs ceiling
(Qwen3.6-27B) extractors with the RAG arm as a fixed control isolates
**extraction quality as the dominant factor** in fact-spine accuracy — the
measured case for upgrading the extractor when you have local compute to
spare (see [Dreaming — upgrading the extractor](dreaming.md#upgrading-the-extractor--bigger-local-models)).
Even the smallest bake beats naive-RAG at ~25× fewer tokens/query.

The harder full-haystack (`_s`) results, the extractor-ladder screen used
to choose the default sidecar model, and the abstention-calibration sweep
are all in [`evals/README.md`](../../evals/README.md).
