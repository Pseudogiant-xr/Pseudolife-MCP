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
extractor:

| arm | accuracy | context tokens/question |
|-----|----------|------------------------|
| naive RAG (top-6 turns) | 0.615 | 1638 |
| cortex facts only | 0.564 | **59** |
| **hybrid (facts + top-3 turns)** | **0.705** | 979 |

The consolidated-facts posture beats naive RAG by 9 points while reading
~40% of the context — and the fact spine alone delivers 92% of RAG's
accuracy on **3.6% of its token budget**.

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
earlier single-run "+0.102" comparison overstated the effect. The
ceiling-extractor headline above (0.705 hybrid) is itself a single run
whose config predates context persistence and cannot be replicated
as-is; its nearest replicable sibling (`qwen-27b` window-0 bank) scores
hybrid 0.695 ± 0.017 across 5 replicates — treat 0.705 as the top edge
of that band, not a point fact.

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
