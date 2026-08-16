# Benchmarks

What the memory actually buys, measured. Part of the
[user guide](../../README.md#documentation); the full methodology and every
finding live in [`evals/README.md`](../../evals/README.md).

Nearly every number on this page was measured on the **pre-v25 stack**: the
384-d MiniLM backbone, with the BM25 hybrid pool off. Both defaults have
since changed — the backbone swapped at schema v25 and BM25 flipped on
2026-07-25 — and the arms that read raw turns (naive RAG, hybrid) *select*
those turns with the retriever that changed, whose measured R@10 moved
0.572 → 0.809. The exception is the headline ceiling table below, re-judged
2026-07-29 on the current reproducible server with its fact ranking under
the v25 backbone — though even there extraction and raw-turn selection are
held at the pre-v25 run (see the note under the table). Read the rest as
historical measurements of the design, not as what the shipped
configuration scores today.

## LongMemEval — knowledge updates

Measured on the **knowledge-update subset of
[LongMemEval](https://arxiv.org/abs/2410.10813)** (78 questions) — the
"user's facts change over time" ability the HLC supersession spine exists
for. (The harness can extend a run to all six LongMemEval question types —
500 questions — via `--types`; the numbers below are the KU slice unless a
row says otherwise.) Everything local: extraction, answering, and LLM-as-judge grading all
run on the author's own hardware (judge = Qwen3.6-27B at temperature 0),
so compare *within* the table, not against GPT-4o-judged leaderboards.

> **Reading the numbers.** Except for the ceiling table directly below
> (re-judged 2026-07-29 on the reproducible stock server), accuracies on
> this page are single-run point estimates unless marked mean ± std, and
> they were measured on a stack
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

## End to end on the current stack — and the commit-gated cascade (2026-07-30)

The freshest measurement runs the **whole current pipeline** — fresh
qwen-27b extraction under the v25 embedding backbone, BM25-on turn
retrieval, reproducible q8_0 serving (`ceiling-e2e`: 3 byte-identical
replicates — std 0.0000). Oracle variant, 78 questions:

| arm | accuracy | context tokens/question |
|-----|----------|------------------------|
| naive RAG (top-6 turns) | 0.859 | ~1237 |
| cortex facts only | 0.667 | **~259** |
| hybrid (facts + top-3 turns) | 0.833 | ~920 |
| **commit-gated cascade** | **0.936** | ~702 |

Two findings, stated plainly. First, the v25 retriever + BM25 lifted
raw-turn selection so much (R@10 0.572 → 0.809) that the **concatenation
hybrid no longer beats naive RAG on this slice** — mixing facts and turns
in one prompt costs stale-fact overrides and extra abstentions. Second,
the channels remain strongly complementary (their per-question union is
0.949), and the **commit-gated cascade** captures almost all of it: serve
the cortex answer when that channel *commits*, fall back to RAG when it
says "I don't know". Routing uses only the response text — correctness is
never consulted — so the policy is deployable as-is. The cascade is a
*derived* metric (`replicate.py cascade_correct`) over the judged
rag/cortex arms: same artifacts, no fourth answered arm.

**Full-haystack confirmation (pre-registered).** On the `_s` haystacks
(~50 sessions/question, tag `casc-q8`, reproducible server verified by
process inspection), the cascade scores **0.462 vs 0.346** for naive RAG —
delta **+0.115** at **p = 0.011** (paired permutation, 10,000 draws,
seed 0) — with **commit precision 0.714** on a 14/78 commit rate. Honest
scoping: the margin over the concatenation hybrid there (+0.064) is
directional only (p = 0.18), so the decision-grade claim is
cascade-vs-RAG; and the commit rate is low — raising it is the current
retrieval workstream. Artifacts:
`evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-e2e.agg.json`,
`evals/results/casc-q8-confirmation.json`.

This table is **not comparable per-arm** to the held-fixed table below:
it re-ran extraction and turn selection on the current stack, while the
table below deliberately holds both at the 2026-07-19 configuration.

## The held-fixed rebuild (`ceiling-v25`)

On the oracle variant (evidence sessions only), with the local-ceiling
extractor (`ceiling-v25`: 3 replicates on the reproducible stock server,
byte-identical — std 0.0000; contexts rebuilt from the 2026-07-19
context-persisted bank dumps):

| arm | accuracy | context tokens/question |
|-----|----------|------------------------|
| naive RAG (top-6 turns) | 0.628 | 1638 |
| cortex facts only | 0.590 | **~182** |
| **hybrid (facts + top-3 turns)** | **0.731** | ~1102 |

Within this held-fixed frame the consolidated-facts posture beats naive
RAG by ~10 points while reading **~67%** of the context — and the fact
spine alone trails RAG by only ~4 points on **~11% of its token budget**.
**Retired as a headline (2026-07-30):** on the current end-to-end stack
(section above) the concatenation hybrid's margin over naive RAG does not
survive the v25 retrieval upgrade; the commit-gated cascade is the
posture that beats RAG there. This table remains valid for what it
isolates — the serving-stack offset and cortex fact ranking under v25
with 2026-07-19 extraction and turn selection. Notably, the shipped E4B
fine-tune's replicated hybrid (0.762 ± 0.027, table below) beat this
ceiling's own-stack figure (0.710 ± 0.019, superseded table below) in the
one comparison that is valid — same stack, both on the TurboQuant fork —
so on knowledge updates the specialised small extractor at least keeps
pace with generic bigger models. No cross-stack comparison against the
0.731 above is made.

> **Why this table was re-based (2026-07-29).** Its previous published
> numbers (superseded table below) were judged on the TurboQuant fork.
> Re-judging the *same* contexts on the reproducible stock server moved
> the naive-RAG arm **+0.0615** — and that arm's context is copied
> **verbatim** by `rebuild_contexts.py`, so on byte-identical input the
> move is the answerer/judge stack alone. At 3.7× the old measurement's
> own rag std that is a systematic offset, not variance: the old stack was
> scoring this slice about six points low, and headline claims measured
> against a mis-scored control were never really established. Two things
> are still held fixed by the rebuild: **extraction** (the 2026-07-19
> banks) and **raw-turn selection** (the pre-v25 retriever picked the
> turns; BM25 is not exercised) — only the cortex fact ranking runs under
> the v25 backbone. Compare numbers only within a stack; across stacks,
> re-measure. Artifact:
> `evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-v25.agg.json`.

**Superseded — the v2 / TurboQuant measurement** (5 replicates,
2026-07-19 context-persisted bank; judged on the nondeterministic fork,
~6 points low on the control arm — retained for the record, not
comparable to the table above):

| arm | accuracy (mean ± std) | context tokens/question |
|-----|----------------------|------------------------|
| naive RAG (top-6 turns) | 0.567 ± 0.017 | 1638 |
| cortex facts only | 0.559 ± 0.029 | **~124** |
| **hybrid (facts + top-3 turns)** | **0.710 ± 0.019** | ~1043 |

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
predecessor, hybrid 0.705, landed inside the replicated band — and
re-based 2026-07-29 onto the reproducible server, hybrid 0.731, with
the superseded TurboQuant figures retained above.)

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

> **Outcome (2026-08-15): shipped.** The flat store is now the default
> preset. A preregistered rerun under the current retrieval backbone
> (v25 embedder + BM25-on) plus six steelman edge cases — forced
> eviction at matched capacity, depth-scaled recency at two half-life
> bases, 202 real recorded queries under a blind judge, lifecycle
> consumers, latency — came back a tie on every gate, in both
> directions: the significant July deltas below (each way) did not
> reproduce, and the write-side loss was fixed by the 2026-07-25
> demotion cascade before the rerun (survival now 0.0 loss both arms).
> The July results below are retained as the historical record that
> opened the question; the rerun's full gates table lives in
> `docs/superpowers/specs/2026-08-14-flat-band-verdict-preregistration.md`
> with committed `abl25-*` artifacts.

The 8-band cosine continuum was the memory's headline structure, so it was
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

Both of the continuum's July defences were measured here and neither
held. The 2026-08-15 preregistered rerun then closed the two bounds this
page flags: the demotion cascade (2026-07-25) eliminated the
partition-forced eviction entirely (loss 0.0 both ingest arms — the
whole-system tables above describe code that no longer ships), and a
capacity-scaled corpus where **both** arms genuinely evict found the
banded retention stack ties a single flat policy on gold-evidence
survival (0.459 vs 0.465, p = 1.0). With every gate a tie, the flat
store became the default on 2026-08-15; the `continuum` preset remains
one config line away.

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
