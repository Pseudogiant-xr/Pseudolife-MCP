# Benchmarks

What the memory actually buys, measured. Part of the
[user guide](../../README.md#documentation); the full methodology and every
finding live in [`evals/README.md`](../../evals/README.md).

**The bench instrument changed on 2026-08-17.** Every accuracy on this page
was graded by a local **Qwen3.6-27B** answerer/judge; the bench stack has
since migrated to **Qwen3.8-27B**, and only the knowledge-update slice has
been re-run on it — the sole Qwen3.8 numbers here are that comparison's
right-hand column
([the knowledge-update slice](#the-knowledge-update-slice-78-of-the-500),
where both stacks are published side by side).
The instrument is a term in every number below: compare within a stack, and
treat a claim that has not been reproduced across judge families as
provisional.

Nearly every number on this page was measured on the **pre-v25 stack**: the
384-d MiniLM backbone, with the BM25 hybrid pool off. Both defaults have
since changed — the backbone swapped at schema v25 and BM25 flipped on
2026-07-25 — and the arms that read raw turns (naive RAG, hybrid) *select*
those turns with the retriever that changed, whose measured R@10 moved
0.572 → 0.809. The exceptions are the two tables at the top — the
500-question sweep and the knowledge-update slice — which ran end to end
on the post-v25 stack, and the held-fixed rebuild below them, re-judged
2026-07-29 on the reproducible server with its fact ranking under the v25
backbone (though even there extraction and raw-turn selection are held at
the pre-v25 run — see the note under that table). Read the rest as
historical measurements of the design, not as what the shipped
configuration scores today.

## LongMemEval

[LongMemEval](https://arxiv.org/abs/2410.10813) has six question types and
500 questions. The headline table below is the **whole benchmark**; the
**knowledge-update** subset (78 questions — the "user's facts change over
time" ability the HLC supersession spine exists for) is published under it
as a named sub-slice, because it is the type this design is built to win
and publishing it alone overstates the system. Sections after that hold
older KU-only measurements and say so. Everything local: extraction,
answering, and LLM-as-judge grading all run on the author's own hardware
(judge = Qwen3.6-27B at temperature 0), so compare *within* the table, not
against GPT-4o-judged leaderboards.

> **Reading the numbers.** Except for the ceiling tables below
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

## The full 500-question sweep — all six question types (2026-08-03)

The headline table is the **whole benchmark, not a slice**: all six
LongMemEval question types, 500 questions, oracle variant, run end to end
through the memory (qwen-27b extraction under the v25 embedding backbone,
BM25-on turn retrieval). Single pass — not replicated — graded by the local
Qwen3.6-27B judge. Artifact:
`evals/results/longmemeval-all-oracle-qwen-27b-alltypes-0803.summary.json`.

| arm | accuracy | context tokens/question |
|-----|----------|------------------------|
| naive RAG (top-6 turns) | 0.688 | ~1210 |
| cortex facts only | 0.416 | **~158** |
| hybrid (facts + top-3 turns) | 0.664 | ~842 |
| **commit-gated cascade** | **0.690** | ~883 |

Overall this is **a wash on accuracy at ~73% of naive RAG's context**:
0.690 vs 0.688 is one question in 500 on a single pass and carries no
weight as a win. The fact spine alone answers at ~13% of RAG's token
budget, at a large accuracy cost outside the types it is built for. The
**cascade** is a serving policy, not a fourth pipeline — serve the cortex
answer when that channel *commits*, fall back to RAG when it says "I don't
know". Routing reads only the response text, never correctness, so the
policy is deployable as-is; it is a *derived* metric
(`replicate.py cascade_correct`) over the judged rag/cortex arms.

Per type the picture is not uniform, and this is the honest shape of the
result:

| question type | n | naive RAG | cortex | hybrid | cascade |
|---|---:|---:|---:|---:|---:|
| knowledge-update | 78 | 0.859 | 0.756 | 0.910 | 0.936 |
| single-session-user | 70 | 0.929 | 0.671 | 0.957 | 0.943 |
| single-session-assistant | 56 | 0.911 | 0.571 | 0.964 | 0.929 |
| single-session-preference | 30 | 0.800 | 0.733 | 0.600 | 0.700 |
| temporal-reasoning | 133 | 0.526 | 0.150 | 0.534 | 0.526 |
| multi-session | 133 | 0.504 | 0.211 | 0.383 | 0.474 |

The consolidated spine helps where a fact changes and where the answer sits
inside one session; it loses where the answer must be aggregated across
sessions or ordered in time, because per-fact consolidation is exactly what
discards that structure. BEAM-100K reproduces the same shape on a
completely different corpus — see
[BEAM](../../evals/README.md#beam-long-term-memory-benchmark-beam_adapterpy).

Two limits on this table, both load-bearing. **(1)** The hybrid arm here
served 3 raw turns against the rag control's 6; the bench default was
budget-matched to 6/6 on 2026-08-21, so every hybrid row on this page is a
half-budget arm and is not comparable to post-flip hybrid numbers. **(2)**
It was graded by the Qwen3.6 judge and has not been re-judged since the
2026-08-17 migration — and on the one slice that *was* re-run, the cascade
row moved by −0.090 (below). Read the cascade row here as an upper bound.

## The knowledge-update slice (78 of the 500)

This is the slice the supersession spine is built for, measured on its own
with fresh qwen-27b extraction under the v25 backbone and reproducible q8_0
serving (`ceiling-e2e`, 2026-07-30: 3 byte-identical replicates —
std 0.0000). It was the README's front-door table until 2026-08-25; it is a
sub-slice of the 500-question sweep above and is published as one now.

| arm | accuracy | context tokens/question |
|-----|----------|------------------------|
| naive RAG (top-6 turns) | 0.859 | ~1237 |
| cortex facts only | 0.667 | **~259** |
| hybrid (facts + top-3 turns) | 0.833 | ~920 |
| **commit-gated cascade** | ~~**0.936**~~ (retired — see below) | ~702 |

> **RETIRED 2026-08-25 — the cascade's 0.936 does not survive the bench
> instrument (#188).** The 2026-08-17 migration to a Qwen3.8-27B
> answerer/judge re-ran the same 78 questions (`ceiling-v38`, also n=3 with
> std 0.0000). The naive-RAG control lands on 0.859 on both stacks; the
> cascade falls below it:
>
> | arm | Qwen3.6 stack (`ceiling-e2e`) | Qwen3.8 stack (`ceiling-v38`) |
> |---|---:|---:|
> | naive RAG (control) | 0.859 | 0.859 |
> | cortex facts only | 0.667 | 0.667 |
> | hybrid (facts + top-3 turns) | 0.833 | 0.846 |
> | **commit-gated cascade** | **0.936** | **0.846** |
>
> The mechanism is the routing gate, not the memory. The cascade serves the
> fact-spine answer unless that channel abstains, so its input is the
> *answerer's* willingness to say "I don't know": on the old stack the
> cortex arm abstained on **32 of 78** questions and its 46 commits were
> **46/46** correct; on the new stack it abstains **22 of 78** and its 56
> commits are **0.839** precise, so nine wrong answers are served where a
> RAG fallback would have rescued them. An adopter running Claude or GPT as
> the answerer has a different abstention rate and therefore a different
> number — which is what disqualifies 0.936 as a published claim about the
> memory system. (The migration moved extractor, answerer and judge
> together and the two runs' fact contexts are not byte-identical, so the
> artifacts do not isolate *which* term did it. That is the point: the
> claim was never instrument-independent, and nobody had measured whether
> it transferred.) Artifacts:
> `evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-e2e.agg.json`,
> `evals/results/longmemeval-ku-oracle-qwen-27b-ceiling-v38.agg.json`
> (abstention and commit-precision counts recomputed from the per-question
> `…-ceiling-e2e.jsonl` / `…-ceiling-v38.jsonl` rows).

Within the 2026-07-30 stack, two findings still stand. First, the v25
retriever + BM25 lifted raw-turn selection so much (R@10 0.572 → 0.809)
that the **concatenation hybrid no longer beats naive RAG on this slice** —
mixing facts and turns in one prompt costs stale-fact overrides and extra
abstentions. Second, the channels are strongly complementary (their
per-question union is 0.949) — but how much of that a commit gate can
capture is exactly what the judge migration showed to be
instrument-dependent.

> **Currency note (2026-08-25): the full-haystack confirmation is a
> 2026-07-30 measurement and has never been re-judged.** On the `_s`
> haystacks (~50 sessions/question, tag `casc-q8`, reproducible server
> verified by process inspection), the cascade scored **0.462 vs 0.346**
> for naive RAG — delta **+0.115** at **p = 0.011** (paired permutation,
> 10,000 draws, seed 0) — with **commit precision 0.714** on a 14/78 commit
> rate. It was graded by the Qwen3.6 answerer/judge, and its extraction
> predates even the earliest committed op-prompt artifact (`v5`, committed
> 2026-08-01) — the shipped pin has since moved to
> `ku_op_prompt_v10_stance_update.txt`. The same cascade metric lost 0.090
> on the oracle slice when the stack migrated. Until it is re-run, it
> is an engineering-log result, not a front-door claim, and it was removed
> from the README on 2026-08-25. Honest scoping at the time: the margin
> over the concatenation hybrid there (+0.064) was directional only
> (p = 0.18), and the commit rate was low. Artifact:
> `evals/results/casc-q8-confirmation.json`.

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
survive the v25 retrieval upgrade. The commit-gated cascade replaced it as
the published posture and has since been retired too (2026-08-25) — on the
current bench instrument no arm beats naive RAG on this slice. This table
remains valid for what it
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

> **CORRECTED 2026-08-25 (scorer defect #173).** Every number in the table
> above is superseded. The multiple-choice scorer's no-box fallback
> accepted any standalone `[A-Ha-h]` token, so the English article "a" in a
> truncated reasoning trace scored as answer **A**. Re-scoring the same
> committed run under the anchored scorer
> (`evals/results/lme-v2-smoke-slice2-rescored-strictmc.summary.json` and
> `…-slice2-compose-rescored-strictmc.summary.json`, produced offline by
> `evals/rescore_strict_mc.py`):
>
> | arm | default answer prompt | composition-aware prompt |
> |-----|----------------------|--------------------------|
> | naive RAG (control) | 0.162 → **0.149** | 0.284 → **0.257** |
> | cortex facts only | 0.068 → **0.068** | 0.216 → **0.176** |
> | hybrid | **0.243** → **0.203** | 0.284 → **0.270** |
>
> All ten flips are the same defect in the same direction (gold answer
> **A**, previously-correct → wrong); no row moved the other way. The
> ordering is unchanged, but the exact compose-prompt tie below was an
> artifact of it: corrected, hybrid leads naive RAG there by 0.013 — one
> question, which is still no measurable difference.

Hybrid leads under the default prompt. Under the composition-aware prompt
it **ties naive RAG** — so the complementarity is real but
prompt-dependent, not universal. (The tie is a superseded number; see the
correction above — 0.270 vs 0.257 on the corrected scorer, which is the
same "no measurable difference" reading.)

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
>
> **CORRECTED 2026-08-25 (scorer defect #173).** The three quoted pilot
> rows are superseded by the same re-score
> (`evals/results/lme-v2-smoke-slice1-rescored-strictmc.agg.json`): naive
> RAG `0.300 [0.30–0.30] | 0.433 [0.40–0.50]`, cortex
> `0.167 [0.00–0.30] | 0.200 [0.10–0.30]`, hybrid
> `0.500 [0.40–0.60] | 0.533 [0.50–0.60]`. Seven flips, all gold **A**.
> The pilot's own conclusion weakens further: under the composition-aware
> prompt hybrid now *ties* naive RAG in one of the three replicates rather
> than beating both channels in all three.

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
