# Forgetting sweep — is a swept bank better than a wide one? (preregistered 2026-09-05)

The 2026-08-15 distractor-scale probe left one question open, in its own
words: *"no experiment forced eviction and asked which victims to pick;
none asked whether NOT evicting (a wider bank) beats it."* This probe
answers it.

That probe established the loss: evidence-in-top-6 falls 0.830 (1x) →
0.597 (15x) → 0.513 (31x) as the pool grows, and nothing ever evicts on
the flat default. It measured the **recovery ceiling** of a perfect sweep
(+0.233 at 15x) by treating the 1x arm as an oracle that removes every
foreign turn. It did not measure what any *realizable* sweep recovers,
because a realizable sweep does not get to know which turns are foreign —
it only gets an eviction score.

This one holds the pool construction fixed and varies the sweep. It is
the "realistic sweep vs oracle sweep" follow-up the 2026-08-15 spec's
fixed interpretation rule named.

## Amendment (2026-09-05, before the run) — which dumps

The smoke run of the control arm failed G-F0, and the reason is worth
recording rather than patching over: **`distractor_scale_probe.DUMP_DIR`
does not name the directory the 2026-08-15 artifact was produced from.**

That constant points at `results/banks/s-qwen-27b-ablbands-flat`, which on
this tree holds the **retired 384-d MiniLM replay** (files dated
2026-07-24). Re-selecting through it reproduces 11 of 30 checked
question×scale cells; pool sizes match exactly, rankings do not, and no
`select_topk` knob closes the gap (bm25 on/off, recency on/off, and the
slot channel suppressed were all tried — 6 to 11 of 30 each).

The v25 replay the artifact actually came from is **1024-d** — as this
probe's own predecessor spec says, "1024-d embeddings already computed" —
and sits in a sibling directory dated 2026-08-14/15. Re-selecting through
it reproduces **40 of 40** checked cells exactly, control fields and pool
sizes alike.

The sibling's directory-name suffix is machine-local, so this probe does
not hardcode it. It resolves the dump directory by **backbone dimension**:
the one `results/banks/s-qwen-27b-ablbands-flat*` directory holding 78
dumps whose `query_emb` is 1024-d, refusing with a listing when that is
ambiguous or absent, and `--dumps` overrides. The chosen directory name
and dimension are recorded in the artifact.

Nothing else in this spec changes: the arms, capacities, metrics, gates
and expected ordering above were written before any of this was known,
and G-F0 remains an abort condition rather than a caveat.

Two consequences worth stating for whoever reads this next:

- The distractor probe as committed cannot regenerate its own published
  artifact on this machine. It never noticed because it refuses to
  overwrite an existing result file — the guard that protects canonical
  numbers also hides that they have stopped reproducing.
- Any other analysis that inherited that `DUMP_DIR` constant is reading
  the retired backbone. Auditing them is out of scope here and is left as
  a follow-up.

## Design

Pure offline analysis, CPU only, no GPU, no judge, no daemon, no network.
Inherits the distractor probe's construction wholesale — `load_dumps`,
the fixed RNG-free rotation, `band_ablation.select_topk` (flat policy,
recency off, BM25 on), `_evidence_texts`, `_paired_permutation_p` — by
importing `evals/distractor_scale_probe.py`, not by copying it. Same 78
LongMemEval knowledge-update questions, same v25 flat-band dumps
(`evals/results/banks/s-qwen-27b-ablbands-flat/`), same five scales
(1x / 3x / 7x / 15x / 31x).

The one new step: after the pool for question q at scale N is built, a
**sweep** reduces it to a capacity C before selection. Everything
downstream is unchanged.

### Arms

| arm | what it evicts |
|---|---|
| `none` | nothing — the distractor probe's own numbers, reproduced as the control |
| `balanced` | lowest `RetentionPolicy.source_weighted_score` under `retention.balanced()` |
| `recency_heavy` | same, under `retention.recency_heavy()` |
| `surprise_heavy` | same, under `retention.surprise_heavy()` |
| `random` | uniformly at random, seeded — the floor any policy must beat |
| `oracle` | never a gold-evidence entry; uniformly at random among the rest — the ceiling victim choice can reach |

The three policy arms are the ones the product actually ships:
`pseudolife_memory/memory/miras/retention.py` builds them and
`MIRASBand._evict_one` (`band.py:140-151`) uses exactly
`retention.source_weighted_score(entry, now)`, evicting the minimum.
There is no standalone offline sweep entry point in the tree, so the
probe calls `source_weighted_score` directly over the pooled entries.

**Eviction is one stable selection, not a loop.** `_evict_one` picks
`min(range(len(scores)), key=...)`, i.e. the first minimum — lowest
insertion ordinal on a tie. The scores do not depend on which other
entries are resident, so repeatedly calling `_evict_one` down to size C
is exactly "keep the C highest by `(score, -ordinal)`, breaking ties
toward the later-inserted entry". The probe does the single sort and says
so here rather than paying 6,500 list pops per cell.

### Capacities

Two, both per question, both expressed in that question's own pool sizes:

- **C1** = the size of that question's 1x pool (its own dump alone, ~470–520
  entries). The aggressive capacity: at 15x it discards ~93% of the pool.
- **C3** = the size of that question's 3x pool (~1,400 entries).

A scale whose pool is already at or below the capacity is a **no-op** and
is reported as `swept: false` with the arm's numbers identical to `none`
(1x under C1; 1x and 3x under C3). No-ops are still emitted so the table
is rectangular.

### Metrics

Per question, per scale, per capacity, per arm — the distractor probe's
metric set unchanged: `evidence_in_top6` (primary), `evidence_in_top3`,
`any_evidence_served`, `rank_first_evidence`, `n_pool_entries`,
`select_topk_latency_ms`, `bm25_latency_ms`. One diagnostic is added
because it is what will explain any result: `evidence_survival`, the
fraction of that question's gold-evidence entries still present in the
swept pool. A sweep cannot serve what it has deleted, so
`evidence_in_top6 <= evidence_survival` always, and the gap between them
is the dilution the sweep failed to fix.

Statistics: per-question paired deltas, two-sided sign-flip permutation,
10,000 perms, seed 0 — `band_ablation._paired_permutation_p`, the house
convention, 78 paired questions per comparison.

## Preregistered gates

All primary comparisons are at **15x, capacity C1** — the distractor
probe's own primary scale, and the capacity at which a sweep is doing
real work. C3 and the other scales are reported but do not move a
verdict.

- **G-F0 (control)**: the `none` arm must reproduce
  `evals/results/distractor-scale-probe-2026-08-15.json` **exactly** —
  per question and per scale, on `evidence_in_top6`, `evidence_in_top3`,
  `any_evidence_served`, `rank_first_evidence` and `n_pool_entries`.
  Latency is excluded (machine- and load-dependent by construction). Any
  mismatch **aborts the run** and is reported as a failure, not a caveat:
  without an exact control the arms are not comparable to the published
  ceiling.
- **G-F1 (does any shipped sweep pay?)**: for each of `balanced`,
  `recency_heavy`, `surprise_heavy`, the paired delta
  `arm − none` on evidence-in-top-6 at 15x/C1. An arm **pays** iff the
  delta is ≥ **+0.05** with p < 0.05. If no arm pays, the answer to the
  open question is *the wider bank wins* — do not build a sweep agent on
  these scores.
- **G-F2 (is victim choice worth anything at all?)**: `oracle − none` at
  15x/C1, same bar (≥ +0.05, p < 0.05). This separates "forgetting is
  hopeless" from "forgetting is fine, these scores are bad". If G-F2
  fails too, no sweep at this capacity can help and the finding is about
  capacity, not about victim choice.
- **G-F3 (do the shipped scores beat coin-flipping?)**: each policy arm
  vs `random` at 15x/C1, paired, p < 0.05, either direction. A policy
  significantly **below** random is a finding about
  `source_weighted_score`, and is to be reported as such rather than
  softened.
- **G-F4 (sanity, inherited)**: the control's 1x evidence-in-top-6 must
  be ≥ 0.5, the distractor probe's G-D3 floor. It was 0.830 on
  2026-08-15; a different value here means the control did not reproduce
  and G-F0 has already aborted.

## Expected ordering — stated before running

**`oracle` > `none` > `random` > (`balanced` ≡ `surprise_heavy`) >
`recency_heavy`**, at 15x and 31x under C1.

The reasoning, so that a wrong prediction is legible as a wrong
prediction:

1. `oracle` first, and plausibly *above* the 1x number rather than merely
   equal to it: it keeps every evidence entry while thinning the pool to
   ~470, and the entries it keeps are drawn from foreign haystacks, which
   are more off-topic than the anchor's own turns that 1x competes
   against.
2. `none` second: the distractor probe already showed the pool costs
   ~0.23, but a bad sweep can cost more than dilution does, because
   deletion is not recoverable and dilution is only a ranking problem.
3. `random` third: it destroys evidence in proportion to the pool cut
   (~93% at 15x/C1), so evidence-in-top-6 should collapse toward
   `C / n_pool`.
4. `balanced` and `surprise_heavy` **identical to each other**, and below
   `random`. Identical because with `access_count` unavailable (see
   substitutions) both reduce to a strictly increasing function of
   surprise, and `source_weighted_score` applies the same ×0.05
   superseded multiplier to both — so both induce the same total order:
   all superseded entries below all live ones, ascending surprise within
   each group. Below `random` because that ordering is *anti-correlated
   with evidence value on this corpus*: see the disclosed data property
   below.
5. `recency_heavy` last. With `access_count` unavailable its base score
   is identically 0, so `source_weighted_score` returns 1.0 for every
   live entry and 0.05 for every superseded one, and the whole ranking
   collapses onto the insertion-ordinal tie-break. The probe's pool
   construction concatenates the anchor's own dump *first*, so
   oldest-inserted means the anchor's own turns — the ones holding the
   evidence. This arm should be near zero at 15x, and that is a
   **construction artifact, not a policy verdict**; it is called out
   here so it cannot be reported later as if it were one.

Expected on the capacity axis: C3 strictly better than C1 for every
evicting arm at every scale above C3, monotonically, because a larger
capacity is a strictly smaller cut.

## Substitutions — what the dumps do not carry

The band-state dumps record `text`, `emb`, `ts`, `hist_ts`, `source`,
`superseded_at`, `slots`. `MemoryEntry` fields that `source_weighted_score`
reads and the dump lacks are reconstructed or defaulted as follows. Each
is repeated in the artifact's `caveats` block.

1. **`access_count` = 0 for every entry.** Never dumped. Live it is
   bumped only by the served-results path (`cms.py:1573`), so in the
   dump-producing replay — ingest, then one search — it was 0 or 1 for at
   most six entries per bank. Consequence, stated above: `recency_heavy`
   degenerates to a superseded-first, then oldest-inserted-first policy,
   and `balanced` collapses onto `surprise_heavy`'s ordering. This is the
   single largest substitution in the probe and it is load-bearing for
   the expected ordering.
2. **`surprise_score` reconstructed exactly** as
   `clamp(1 − max_{j<i} cos(e_i, e_j), 0, 1)` over that entry's *own*
   dump in insertion order, with 1.0 for the first entry — which is
   `MIRASBand.compute_surprise` verbatim (`band.py:100-109`; the empty
   band returns 1.0). This is a reconstruction rather than a substitution
   because the flat preset used for these dumps has one band, so
   `overall_surprise = min(per_band_surprise)` (`cms.py:399-400`) is that
   band's value; `flat_cap` is 5,250 against ~470–520 stored entries, so
   nothing was ever evicted during the replay; and `turns_stored` equals
   the entry count in every dump, so the novelty gate rejected nothing
   and the surviving sequence *is* the sequence the live band scored
   against. Magnitude decay on superseded entries does not perturb it —
   `compute_surprise` normalises.
   The reconstruction is per-dump, not per-pool: an entry keeps the
   surprise the dump-producing run actually stamped on it. A live bank
   accumulating 31 questions' turns would have scored later entries
   against a larger pattern matrix and assigned them lower surprise. The
   concatenated pool never existed live, so there is no true value to
   miss; per-dump surprise is the only one that was ever real.
3. **`reinforcements` = 0, `retention_boost` = 0.0.** Never dumped. The
   MTT term is `retention_boost × log1p(reinforcements)`, so it vanishes
   at 0 reinforcements regardless of the boost — the daemon's
   `retention_boost = 1.0` would change nothing here.
4. **`timestamp` = the entry's `ts`; `now` = the dump's `search_time`**
   (the "wall" regime `select_topk` is called under). Inert given
   substitution 1: `now` and `timestamp` reach the eviction scores only
   through `age`, which every named policy uses as a divisor under
   `access_count`.
5. **`source` = `"bench"` on every entry**, which is not in
   `_default_source_weights` and falls back to the 1.0 multiplier. The
   per-source retention tiering (`user_msg` 1.5, `llm_thinking` 0.2,
   `system` 10.0) therefore does no work on this corpus and is untested
   by this probe. A real bank's mix would spread these arms further
   apart; whether it spreads them in the right direction is not measured
   here.
6. **Ties are broken toward the later-inserted entry**, matching
   `_evict_one`'s first-minimum rule. Under substitution 1 this is not a
   detail — it is `recency_heavy`'s entire behaviour.

## Disclosed data property — checked before this spec was written

Honesty about ordering: the following was measured over the 78 dumps
*before* the expected ordering above was written, and it is why that
ordering puts two shipped policies below random. It is a property of the
corpus, not a result of the experiment.

Of 38,086 entries, 17,411 (**0.457**) carry `superseded_at`. Of the 286
gold-evidence entries, 180 (**0.629**) do — evidence is *more* likely to
be flagged superseded than an average turn, which is unsurprising for a
knowledge-update benchmark whose gold turns are the statements that later
turns update. `source_weighted_score` multiplies a superseded entry's
score by 0.05, unconditionally and below every live entry. So all three
shipped policies delete gold evidence first, by design, on this corpus.

The prediction is that this shows up as policy arms below the random
floor. If it does not, the prediction was wrong and the artifact will say
so.

> **Correction (2026-09-05, same day):** the two rates in the paragraph
> above were read off the *retired 384-d replay* — the same wrong
> directory the amendment at the top of this spec describes, measured
> before that problem was found. On the v25 dumps the experiment
> actually ran on, the rates are **0.7341** over all 38,086 entries
> (27,959 superseded) and **0.8636** over the 286 gold-evidence entries
> (247 superseded): the supersession flags come from contradiction
> detection at ingest, which is embedding-dependent, so a different
> backbone flags different entries. The direction is unchanged and the
> gap is wider, so the prediction it grounds stands as written. Both
> corrected numbers are backed by
> `evals/results/forgetting-sweep-corpus-props-20260905.json`
> (`forgetting_sweep_probe.py --corpus-props`); the stale pair is left
> visible above rather than edited away, because what a preregistration
> is for is showing what was believed before the run.

## Caveats stated up front

- **The corpus makes supersession adversarial.** LongMemEval
  knowledge-update is the one benchmark family where the answer-bearing
  turn is systematically the one that got superseded. The ×0.05
  multiplier was added for a real reason (a correction scoring below the
  stale fact it replaced, and being evicted first — see
  `protocols.py:88-97`), and this probe is not evidence that the
  multiplier is wrong in general. It is evidence about what it does when
  the thing you need to recall is the thing that changed.
- **Distractors are foreign haystacks**, inherited from the 2026-08-15
  construction: maximally off-topic, and therefore the easiest possible
  material for a sweep to identify and drop. A sweep that cannot beat
  `none` *here* will not beat it on realistic near-duplicate chatter.
  This bounds the result in the favourable direction for sweeping.
- **Anchor-first pool order** is a construction artifact that
  systematically disadvantages any position-tie-broken policy. Named in
  the expected ordering above; `recency_heavy` is the arm it hits.
- **Retrieval proxy, not a judged verdict.** Evidence-in-top-6 is what
  reaches the served window, not what an answerer then gets right. The
  distractor probe's G-D3 sanity gate (1x ≥ 0.5) is inherited to bound
  how much the proxy can be trusted.
- **Single embedder/backbone** (v25, 384-d), like every conclusion from
  this dump family.
- **Two capacities, both aggressive.** C1 and C3 are 7% and 20% of the
  15x pool. This probe does not sweep gently, and says nothing about a
  capacity set just below the accumulated size.

## Execution

`evals/forgetting_sweep_probe.py`, CPU-only
(`CUDA_VISIBLE_DEVICES=-1`, `HF_HUB_OFFLINE=1`), writes
`evals/results/forgetting-sweep-probe-20260905.json` and **refuses to
overwrite it** without `--force` — the house rule for canonical result
files. Per-question rows, per-cell aggregates, the four gate verdicts,
the control-reproduction check and the substitution caveats all live in
that one artifact. The bank dumps under `evals/results/banks/` are
gitignored, so a fresh worktree must link or copy them from the main
checkout before running.

Expected runtime: tens of minutes (the `none` arm alone is the
distractor probe's ~480 s; the swept arms select over capped pools, which
is cheaper per cell but there are sixty of them per question).
