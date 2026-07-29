# Benchmarks

What the memory actually buys, measured. Part of the
[user guide](../../README.md#documentation); the full methodology and every
finding live in [`evals/README.md`](../../evals/README.md).

Every number on this page was measured on the **pre-v25 stack**: the 384-d
MiniLM backbone, with the BM25 hybrid pool off. Both defaults have since
changed — the backbone swapped at schema v25 and BM25 flipped on 2026-07-25
— and the arms that read raw turns (naive RAG, hybrid) *select* those turns
with the retriever that changed, whose measured R@10 moved 0.572 → 0.809.
Read what follows as historical measurements of the design, not as what the
shipped configuration scores today.

## LongMemEval — knowledge updates

Measured on the **knowledge-update subset of
[LongMemEval](https://arxiv.org/abs/2410.10813)** (78 questions) — the
"user's facts change over time" ability the HLC supersession spine exists
for. Everything local: extraction, answering, and LLM-as-judge grading all
run on the author's own hardware (judge = Qwen3.6-27B at temperature 0),
so compare *within* the table, not against GPT-4o-judged leaderboards.

> **Reading the numbers.** Accuracies below are single-run point
> estimates unless marked mean ± std, and they were measured on a stack
> that **was not bit-reproducible**. Repeated runs of an *identical*
> config varied by several points (observed spread: ~7.7 pp on the cortex
> arm at n=78); that was long attributed to answerer/judge noise, and
> root-caused on 2026-07-27 to the TurboQuant fork's fused TBQ4_0
> flash-attention KV cache instead — `evals/results/judge-determinism-check.json`
> records 6.8–7.7% verdict flips on byte-identical input, with
> `verdict.reproducible: false`. On the stock server with
> `--cache-type-k/v q8_0` the same pipeline reproduces exactly:
> `evals/results/regression_gate.baseline.json` records **std 0.0000 on
> all three arms at n=7**. So the numbers on this page carry a run-to-run
> band that a re-measurement would not, and small single-run differences
> between configs are not meaningful **here**. Re-measure rather than
> reinterpret; decision-grade comparisons use replicates and a paired test
> — see [Variance and replication](../../evals/README.md#variance-and-replication).

On the oracle variant (evidence sessions only), with the local-ceiling
extractor (5 replicates, 2026-07-19 context-persisted bank):

| arm | accuracy (mean ± std) | context tokens/question |
|-----|----------------------|------------------------|
| naive RAG (top-6 turns) | 0.567 ± 0.017 | 1638 |
| cortex facts only | 0.559 ± 0.029 | **~124** |
| **hybrid (facts + top-3 turns)** | **0.710 ± 0.019** | ~1043 |

The consolidated-facts posture beats naive RAG by ~14 points while
reading **~64%** of the context — and the fact spine alone matches RAG's
accuracy on **under 8% of its token budget**. Notably, the shipped E4B
fine-tune's replicated hybrid (0.762 ± 0.027, table below) beats this
27B ceiling — on knowledge updates, the specialised small extractor
outperforms generic bigger models.

> **These three numbers are stack-relative, and now measurably so.** The
> table above was judged on the TurboQuant fork. Re-judging the *same*
> contexts on the reproducible stock server (`ceiling-v25`, 3 replicates,
> std 0.0000) gives rag **0.6282**, cortex **0.5897**, hybrid **0.7308** —
> and the rag arm's context is copied **verbatim** by
> `rebuild_contexts.py`, so its **+0.0615** is the serving stack alone, on
> byte-identical input. At 3.7× this table's own rag std that is a
> systematic offset, not variance, and it is larger than most effects
> reported on this page. Compare numbers only within a stack; across stacks,
> re-measure. Artifact:
> `evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-v25.agg.json`.

## Replicated results (2026-07-18)

The first 5-replicate runs (same banks, answer/judge phase re-run per
replicate; mean ± std) on the shipped-default fine-tuned extractor
(`e4b-ft`, Arm-1) vs its same-model pre-fine-tune baseline:

| arm | Arm-1 (shipped default) | baseline | paired p (78 questions) |
|-----|------------------------|----------|-------------------------|
| naive RAG (control) | 0.574 ± 0.006 | 0.585 ± 0.015 | 0.41 |
| cortex facts only | 0.682 ± 0.017 | 0.603 ± 0.013 | **0.17** |
| hybrid | 0.762 ± 0.027 | 0.749 ± 0.015 | 0.83 |

The control arm is what bounds the rest: it is built from raw turns and
never touches the extractor, so both runs feed it *identical* input and
whatever it moves is pure measurement floor. It drifted −0.010. Against
that, the cortex arm's +0.079 is roughly eight times the floor — a real
effect by direction and size — and still does not clear significance on
the paired test at n=78. Artifacts:
`evals/results/longmemeval-ku-oracle-e4b-ft-arm1-vs-baseline-cortex.compare.json`,
`...-hybrid.compare.json`, `...-rag.compare.json` (10,000 permutations,
seed 0).

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
rather than chat sessions. The **complete 74-question `procedure`
category**, full 100-trajectory haystacks, scored by the benchmark's own
deterministic eval functions (single pass per prompt):

| arm | default answer prompt | composition-aware prompt |
|-----|----------------------|--------------------------|
| naive RAG (control) | 0.162 | 0.284 |
| cortex facts only | 0.068 | 0.216 |
| hybrid | **0.243** | 0.284 |

Hybrid leads under the default prompt. Under the composition-aware prompt
it **ties naive RAG** — so the complementarity is real but
prompt-dependent, not universal.

> **Superseding the pilot.** An earlier 10-question slice (3 replicates)
> reported — and this table now supersedes —
> `| naive RAG (control) | 0.300 [0.30–0.30] | 0.500 [0.40–0.60] |`,
> `| cortex facts only | 0.167 [0.00–0.30] | 0.233 [0.10–0.30] |`,
> `| hybrid | **0.533 [0.50–0.60]** | **0.633 [0.60–0.70]** |`,
> concluding that *hybrid beat both single channels in every replicate
> under both prompts*. **That conclusion does not survive the full
> category.** The pilot's ten questions were simply the first ten in
> dataset file order, and they proved far easier than the category as a
> whole — every arm scores roughly half as well across all 74. This is
> what a selection-biased pilot looks like from the other side, and it is
> the reason for running the expansion at all.

Read honestly: these are single-pass point estimates, not replicated, so
small differences between arms carry no weight — the hybrid-vs-rag tie
under the compose prompt should be read as "no measurable difference",
not as parity established. The cortex arm remains the most run-to-run
volatile (extractor generation varies between runs even at temperature
0). None of this carries the 78-question paired testing the KU results
above do.

The more useful number is the starting one: **every arm scored 0.000**
before five adapter and extraction fixes. The decisive fix was ours to make
because the bug was ours to have caused — the trajectory-mode extraction
prompt said "extract exactly two kinds of claim and nothing else", so the
model *correctly* discarded the knowledge-base protocol articles that the
gold answers were drawn from. Naming a third class (what a document
prescribes) recovered the category, and the lesson was folded back into the
shipped extraction prompt — see [what the extractor
captures](dreaming.md#what-the-extractor-captures).

## Embedding backbone — chosen on our own corpus

The schema-v25 backbone swap was decided by a recall shootout on 150
LongMemEval questions over a 74,183-turn haystack
(`evals/results/embedder-recall-shootout-20260727.json`), scoring recall@k
of the gold turns rather than end-to-end accuracy, so the retriever is
measured without the answerer in the way.

| model | dim | R@10 |
|---|---|---|
| all-MiniLM-L6-v2 (previous default) | 384 | 0.572 |
| granite-embedding-english-r2 | 768 | 0.662 |
| snowflake-arctic-embed-l-v2.0 | 1024 | 0.732 |
| bge-base-en-v1.5 | 768 | 0.742 |
| **Qwen3-Embedding-0.6B (instructed)** | **1024** | **0.809** |

The winner's margin over the shipped model is decisive on a paired McNemar
test at k=10 (78 questions gained, 7 lost, p ≈ 3e-16). Every arm ran at the
harness's `max_seq_length: 512` (MiniLM caps itself at 256); the Qwen arm
used the instruction prefix that is now `EmbeddingConfig.query_prefix` —
see [asymmetric query/document encoding](retrieval.md#asymmetric-query-and-document-encoding).

## Band structure — the continuum earns nothing on either side

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
retrieval ranking. That left one defence — the write side.

### The write side does not rescue it

The ablation above holds *ingest* fixed: both arms re-rank the same
surviving entries, so it cannot see what the banding does at write time.
A second ablation re-runs ingest itself through **one flat band at the
continuum's total capacity** (5,250 entries — the sum of all eight tiers),
so eviction and promotion never partition by tier and a different set of
entries survives. Run on the full-haystack `s` dataset (~488 turns per
question), where capacity pressure is real; 5 replicates, paired
permutation test over 78 questions.

**Write-side isolation** — identical flat ranking on both arms, so the
*only* difference is which entries survived ingest:

| arm | Δ continuum − flat (`wall`) | p | Δ (`hist`) | p |
|-----|---------------------------|------|-----------|------|
| naive RAG | −0.090 | 0.17 | −0.097 | 0.15 |
| hybrid | **−0.110** | **0.018** | **−0.108** | **0.027** |

**Whole system** — the continuum as designed (banded ingest *and* banded
ranking) against flat everything:

| arm | Δ continuum − flat (`wall`) | p | Δ (`hist`) | p |
|-----|---------------------------|------|-----------|------|
| naive RAG | **−0.274** | **0.0001** | **−0.251** | **0.0001** |
| hybrid | **−0.141** | **0.0038** | **−0.123** | **0.0153** |

(The cortex arm is omitted: its context is the fact block, which both arms
build identically, so the comparison is definitionally null. The two
`0.0001` p-values are the resolution floor of a 10,000-permutation test —
read them as "below 0.001", not as a precise estimate.)

The mechanism is capacity accounting, and it is visible without any
answering at all: at ~488 turns per question the continuum **evicts 31.1%
of everything stored** — the 200-entry `working` band overflows long
before promotion can drain it — while a flat pool of the *same total
capacity* evicts nothing. Discarding a third of the evidence costs
accuracy, and it does so on the arm that reads raw turns.

Read honestly, and this bounds the claim: because the flat arm never
evicts at all on this corpus, the comparison measures **eviction forced by
tier partitioning against no eviction** — not one eviction *policy*
against another. A corpus exceeding 5,250 turns per question would be
needed to test the policy itself. What is established is narrower and
still decisive for the design: partitioning a fixed capacity into
recency tiers throws away entries that an unpartitioned store of the same
size would have kept, and the memory is measurably worse for it.

Both of the continuum's defences have now been measured and neither
holds. The case for the banding, if there is one, has to be made on
grounds this benchmark does not reach — and the honest engineering
conclusion is that a flat store is the better default until such a case
is made.

## Extraction quality is the dominant factor

Running floor (Gemma 4 E2B, the smallest CPU-sidecar bake) vs ceiling
(Qwen3.6-27B) extractors with the RAG arm as a fixed control isolates
**extraction quality as the dominant factor** in fact-spine accuracy — the
measured case for upgrading the extractor when you have local compute to
spare (see [Dreaming — upgrading the extractor](dreaming.md#upgrading-the-extractor--bigger-local-models)).
Even the smallest bake beats naive-RAG at ~40× fewer tokens/query.

Read honestly: the floor/ceiling runs are single-run point estimates, not
replicates. The direction is safe anyway — the cortex arm collapses
0.564 → 0.192 when the extractor shrinks, while the RAG control moves
0.615 → 0.564 — a shift inside the run-to-run band, against a cortex
effect roughly five times it — but
finer-grained comparisons between adjacent extractor rungs are not
decision-grade under the same standard the tables above are held to.

The harder full-haystack (`_s`) results, the extractor-ladder screen used
to choose the default sidecar model, and the abstention-calibration sweep
are all in [`evals/README.md`](../../evals/README.md).
