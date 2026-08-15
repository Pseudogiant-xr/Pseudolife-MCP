# Flat-band vs continuum — definitive verdict, preregistration (2026-08-14)

## Gate outcomes (2026-08-15) — VERDICT: migrate flat, at leisure

Every gate ran; the amendments log below records all deviations. The
result is a clean sweep of TIES: under the current backbone the 8-band
continuum neither defends its complexity anywhere, nor loses anywhere —
the July effects in BOTH directions evaporated. Per the preregistered
verdict rule (ties favor the simpler structure; no non-portable
lifecycle consumer), the recommendation is **migrate flat**, with the
caveats below. Not implemented here — the user decides sequencing.

- **G0 (mirror fidelity): PASS, 1.000** mirror-vs-live agreement on all
  three rebuild passes (oracle, s continuum, s flat ingest) — the
  extended mirror (BM25 pool, recency-off) reproduces production
  retrieval exactly. Agreement vs the July-served contexts is 0.622
  (oracle) / 0.332 (s), equal to the real-search-vs-served number in
  every case: all divergence is backbone change, none is formula error.
- **G-R1 (production read path): TIE, both corpora.** Oracle
  (`…-abl25-off-{rag,hybrid}.compare.json`): rag 0.859 vs 0.885
  (Δ −2.6 pts, p = 0.619), hybrid 0.846 vs 0.833 (Δ +1.3, p = 1.0). s
  (`…-abl25-continuum-off-vs-abl25-flat-off-*.compare.json`): rag 0.859
  vs 0.833 (Δ +2.6, p = 0.684), hybrid 0.744 vs 0.795 (Δ −5.1,
  p = 0.348), cascade 0.859 vs 0.846 (Δ +1.3, p = 1.0). The July
  significant flat win (rag/hist −9.0 pts, p = 0.015) is gone —
  raw-RAG rose from ~0.58–0.65 to ~0.83–0.88 under
  Qwen3-Embedding + global-pool BM25, and the fragmentation penalty
  washed out. Cortex control: 0.7051 (oracle) / 0.2051 (s) byte-stable
  across every tag — zero noise floor.
- **G-R2 (recency steelman): FAIL.** Ramp-on continuum never beats the
  flat single-half-life arm at either base: hist (3600 s) rag Δ −2.6
  p = 0.619, hybrid Δ +1.3 p = 1.0; hist24 (86400 s) rag Δ 0.0
  p = 1.0, hybrid Δ +1.3 p = 1.0. The production answer to edge 2
  stands: the lever is off because it measured harmful, and turning it
  on buys nothing on the KU-hist slice.
- **Write side (formally closed):** the demotion cascade (2026-07-25)
  makes total capacity the real bound — survival on the s corpus is
  loss 0.0 for BOTH ingest arms
  (`longmemeval-ku-s-qwen-27b-wabl25-survival.json`), and the
  flat-ingest arm's answers are byte-identical to the flat-ranking arm
  (Δ 0.0, p = 1.0 on all three arms). The July 31.1%-eviction defeat
  measured code that no longer exists.
- **G-E1 (eviction policy, both arms evicting ~45% at capacity 257):
  TIE.** Evidence-turn survival 0.459 (scaled 8-band) vs 0.465 (flat),
  Δ −0.006, p = 1.0
  (`longmemeval-ku-s-qwen-27b-evict-policy-scaled257-vs-flat257.json`).
  Band-structured retention selects survivors no better than one flat
  `balanced` policy. GPU confirmatory not triggered.
- **G-E3 (promotion as importance signal): TIE.** Drop-set evidence
  fraction 0.009 vs 0.009 (p = 1.0) — the promotion/depth signal adds
  nothing measurable to eviction quality. (Same artifact as G-E1.)
- **G-E4 (lifecycle consumers): no non-portable consumer, but four
  proven migration BREAKS** (`abl25-e4-flat-migration-smoke.json`, all
  claims smoke-verified): (1) `_on_band_evict` under n=1 restores
  delete-on-evict (cms.py:2091); (2) legacy `bands=` filters return
  empty silently; (3) file-mode state restore is band-name-keyed and
  loses every entry; (4) the promotion chain and its config surface go
  dead. Plus the `entries.band` stamp rewrite and the .pt-importer
  name mapping. Dream consolidation has ZERO band coupling (verified —
  the briefing's `update_interval` concern belongs to the promotion
  chain, not dreams); MTT/traces retention is band-count-agnostic.
  These are mandatory work items for any migration PR, not reasons to
  keep the bands.
- **G-E5 (live-bank replay, 202 real queries): bands do not defend.**
  Read paths diverge heavily (top-6 divergence 0.876, top-3 0.411,
  mean Jaccard 0.586 — `abl25-e5-live-replay.json`), but the blind
  position-swapped judge over the 177 divergent queries shows no
  significant preference: banded 0.5508, p = 0.130 (78 banded / 60
  flat / 39 ties, `abl25-e5-judged-preference.json`).
- **G-E6 (latency at 5250 resident, 1024-d): marginal fail on one of
  six metrics** (`abl25-e6-latency-{continuum,flat}.json`): flat store
  median 17.5 ms vs 11.0 ms (1.59x, bar was 1.5x); store p95 1.05x,
  dense retrieve ≤ 1.0x, BM25 retrieve 1.13x–1.16x all pass. The
  absolute regression (+6.5 ms/store) is negligible at production
  write rates but goes in the migration plan as a perf caveat.

**Verdict reasoning:** bands won no gate (first rule branch dead);
E1/E3 are ties and every consumer has a documented flat equivalent
(hybrid branch dead — banded lifecycle would keep all the complexity
for no measured benefit); therefore the third branch applies: migrate
flat, sequenced by the user, carrying the G-E6 caveat and the G-E4
work items. The honest framing: the migration buys code simplicity,
not accuracy — every accuracy difference measured here is inside the
noise, in both directions.

**Residuals** (stated in the gates, unchanged): the
`temporal-reasoning` question-type slice was out of scope (no served
rows under the current extractor, ~13 GPU-hours to build); E5 judge
preference leans banded (0.55) without reaching significance — worth a
one-line re-check if the migration ever ships and regrets appear.

This spec preregisters the rerun of the band ablations under the current
retrieval backbone plus six steelman edge cases, before any run starts.
It exists because the 2026-07 ablations were measured under a backbone
that no longer exists, and because the user-ratified 2026-08-13 TABLED
decision requires a clean receipt before any migration. Verdict only —
no migration is implemented from this work.

## Backbone deltas since the July ablations (verified in code today)

The July results cannot be extrapolated; four load-bearing pieces of the
retrieval stack changed on or after 2026-07-25:

1. **Embedder**: `Qwen/Qwen3-Embedding-0.6B`, dim 1024, query-side
   instruction prefix (`utils/config.py:13-38`, embedding-backbone-v25).
   July ran all-MiniLM-L6-v2 384-d.
2. **BM25 fusion ON by default** (`utils/config.py:164`, since
   2026-07-25). Its candidate pool is *global across all bands*
   (`memory/cms.py:954-972`) — band-structure-independent. The July
   ablations measured dense-only retrieval.
3. **Recency ramp OFF by default** (`utils/config.py:692`,
   `memory/cms.py:773`, since 2026-07-25 — measured up to 18 pts harmful
   on naive-RAG). With it off, timestamps never enter read ranking: the
   deployed continuum's read-time distinction is *only* per-band
   candidate pooling.
4. **Demotion cascade on eviction** (`memory/cms.py:2072`, since
   2026-07-25): capacity eviction demotes to the next band; only the
   deepest band's overflow is a true drop. The July-25 write-side result
   (31.1% loss at 6.4% utilisation) measured the pre-cascade
   delete-on-evict behavior, which no longer exists. Its BOUND note
   (incidence vs policy) is doubly superseded.

Also verified: slot channel (Pool 1.5) formula unchanged
(`cms.py:1451-1453`); timeline channel and contiguity expansion exist but
default OFF (`utils/config.py:625,630`); reranker default OFF; final
merge is sort-then-cut after all pools (`cms.py:1084-1085`). Live daemon
is master c4900e17, schema v29.

**Production read path today** = per-band dense top-k (no recency
term) + slot channel + global BM25 fusion. The offline mirror in
`evals/band_ablation.py` predates deltas 2 and 3 and must be extended
before any rerun; fidelity is gated (G0 below).

## Design overview

Two corpora, one fresh CPU replay each per ingest arm, all embeddings
under the v25 embedder:

- **oracle / e4b-ft / src-tag arm1** (78 KU questions, ~23 turns/q, no
  eviction pressure) — the July-19 ranking design, rerun.
- **s / qwen-27b / src-tag ""** (78 KU questions, ~470 turns/q) — the
  production-faithful whole-system check and the eviction testbed.

Served rows (questions + cortex fact blocks) are reused from the
committed July JSONLs; the cortex arm is therefore an identical-inputs
control in every comparison, and its cross-tag spread is the judge-noise
floor that bounds every claim. Fresh replay re-embeds every turn, so the
rag/hybrid raw-turn selection is fully v25.

New tags all carry the `abl25`/`wabl25` prefix so no July canonical
artifact is ever overwritten.

### Arm grid

Oracle (ranking; all from the same continuum-ingest dumps):

| tag | policy | recency | half-life base | purpose |
|---|---|---|---|---|
| `arm1-abl25-continuum-off` | per-band pooling | off | — | production read path, banded |
| `arm1-abl25-flat-off` | global pooling | off | — | production read path, flat |
| `arm1-abl25-continuum-wall` | per-band + ramp | on | 3600 s | July-19 comparability |
| `arm1-abl25-continuum-hist` | per-band + ramp | on | 3600 s | July-19 comparability |
| `arm1-abl25-flat-wall` | global + single | on | 3600 s | July-19 comparability |
| `arm1-abl25-flat-hist` | global + single | on | 3600 s | July-19 comparability |
| `arm1-abl25-continuum-hist24` | per-band + ramp | on | 86400 s | edge-2 steelman (daemon base, `service.py:657`) |
| `arm1-abl25-flat-hist24` | global + single | on | 86400 s | edge-2 steelman |

With recency off, wall and hist are mathematically identical (no
timestamp enters ranking), so the `-off` arms need no mode split.
BM25 is mirrored ON in every `abl25` arm (production default). The
ramp-on arms exist for comparability/steelman only and are explicitly
NOT the production configuration.

s (whole-system, production read path only):

| tag | ingest | ranking | purpose |
|---|---|---|---|
| `abl25-continuum-off` | 8-band | per-band pooling | as-deployed |
| `abl25-flat-off` | 8-band | global pooling | ranking-only isolation |
| `wabl25-flat-off` | flat @5250 | global pooling | whole-system flat |

Plus the survival artifact: with the cascade live, prediction is
loss_rate ≈ 0 for BOTH ingest arms on this corpus (~470 stored < 5250
total). If continuum loss_rate > 0.5% the cascade is not doing what
`cms.py:2072` claims and that becomes a finding in itself.

### Eviction-policy corpus (edge 1) — both arms genuinely evict

The s corpus cannot exceed 5250/question; instead capacity is scaled so
it exceeds capacity. Scaled continuum preset: per-band caps
`[10, 12, 15, 20, 29, 73, 49, 49]` (the 8 caps × 256/5250, rounded;
sum = 257), all other spec fields (update_interval, promotion_*,
retention_policy) unchanged. Flat arm: one band, cap 257 (identical
total), `balanced` retention (the fast-tier policy — same as July's
flat arm). ~470 turns/q → both arms drop ~45% through their own policy:
the continuum through promotion + per-tier retention + demotion cascade;
the flat pool through one retention score. Same corpus, same embedder,
same total capacity — only the *selection of survivors* differs.

**Primary metric is offline and judge-free** (per the 2026-08 offline
replay lesson): per-question **evidence-turn survival** — the fraction
of stored turns from sessions in `answer_session_ids` present in the
final bank state. Paired across 78 questions, permutation test (10k
perms, seed 0). Secondary descriptive stats: per-band evidence density,
drop-set evidence fraction per arm (edge 3), total survivor counts
(must be ≈equal by construction; if not, report why).

GPU end-to-end (answer accuracy) for this corpus runs ONLY if G-E1's
survival delta is significant — survival is the mechanism; accuracy
confirms it matters.

## Preregistered gates

All significance tests: 5 replicates, paired permutation, 10k perms,
seed 0, via `replicate.py compare --out`; every number lands in a
committed artifact. A delta smaller than the cortex control arm's spread
is not a finding regardless of p-value.

- **G0 (mirror fidelity, blocks everything)**: the extended mirror's
  production-config selection (`continuum`, recency off, BM25 on) must
  agree with the *real* `svc.search` recorded at replay time at ≥ 0.95
  mean overlap on both corpora. Below that, fix the mirror before any
  GPU spend. (Agreement vs the July-served contexts is reported for
  interest but NOT gated — the embedder changed, divergence is
  expected.)
- **G-R1 (production read path)**: bands defend read-time pooling iff
  `abl25-continuum-off` beats `abl25-flat-off` at p < 0.05 on rag or
  hybrid, on either corpus, with |Δ| above the control spread. Flat
  wins iff the reverse. Neither significant → read-time tie (a tie
  favors the simpler structure, stated as such, not as a flat "win").
- **G-R2 (edge 2, recency steelman)**: depth-scaled decay defends iff a
  ramp-on continuum arm beats the matching flat single-half-life arm
  (same base) at p < 0.05 on rag or hybrid, at either base (3600 or
  86400). Note the production answer to edge 2 is already "the lever is
  off because it measured harmful" (`cms.py:769-772`); this gate tests
  the counterfactual. Scope limit, stated up front: KU-hist is the
  temporal slice with served rows; the `temporal-reasoning`
  question-type slice has no served rows under the current extractor
  and re-serving it costs ~13 GPU-hours — out of scope, recorded as a
  residual.
- **G-E1 (edge 1, eviction policy)**: band-structured retention defends
  iff continuum evidence-survival > flat evidence-survival at p < 0.05
  (paired, 78 questions). If significant in EITHER direction, run the
  GPU confirmatory (rag + hybrid, 5 replicates) and require the
  accuracy delta to agree in sign.
- **G-E3 (edge 3, promotion signal)**: the promotion/depth signal is
  informative iff the continuum's true-drop set has a lower evidence
  fraction than the flat arm's drop set at p < 0.05 (paired). Pure
  analysis of the edge-1 dumps; no extra runs.
- **G-E4 (edge 4, lifecycle consumers)**: enumerate every band-substrate
  consumer with file:line; for each, state what a flat migration does
  (no-op / degrade / break / needs redesign). Gate: bands defend iff at
  least one consumer *cannot* be given a documented flat-equivalent
  without loss of live functionality. Smoke evidence, not opinion, for
  each "degrade/break" claim.
- **G-E5 (edge 5, live-bank replay)**: replay recorded real queries (or
  the closest recoverable proxy — mechanics amended below once the
  telemetry recon lands, before any E5 run) against the live bank state
  under banded vs flat read paths. Report divergence rate (top-6
  membership change). If >20% of queries change top-3 membership, run a
  blind position-swapped LLM-judge preference (reproducible server, 5
  replicates); bands defend iff judged better at p < 0.05. Divergence
  ≤20% with no judged run → E5 reports "read paths near-identical on
  the production distribution".
- **G-E6 (edge 6, latency)**: migration must not regress performance:
  flat store median and p95 at 5250 resident ≤ 1.5× banded, same for
  search latency (measured with `bench_store_latency.py` extended to a
  banded arm + a search benchmark, embedder cost excluded from the
  store path — it is identical in both arms and dominates otherwise).
  Report the `detect_contradictions` full-scan share explicitly (94% of
  saturated store cost per its docstring).

## Verdict rule (fixed before results)

- Bands win **any** of G-R1 / G-R2 / G-E1+confirmatory / G-E5 →
  **keep bands** (read side earns its structure), with the losing gates
  noted.
- Bands lose all read gates but win G-E1/G-E3 (write-side selection) or
  G-E4 shows non-portable lifecycle consumers → **hybrid**: flat
  read-time selection, banded lifecycle.
- Bands lose everything and G-E4 consumers all have documented flat
  equivalents → **migrate flat**, sequenced later by the user; G-E6
  regression would add a perf caveat to the migration plan, not change
  the verdict.

## Execution discipline

- GPU phases run ONLY on the reproducible `Start-Qwen` config
  (q8_0 KV); the GPU is currently occupied by the daily-driver server —
  all judged phases hold until it frees, then the prior server state is
  restored afterwards. CPU phases (replays, rebuilds, survival analysis,
  latency bench) run now.
- Heartbeat + ledger per the unattended-operation rules for any
  background phase; affirmative first-output verification within 2 min
  of every launch.
- Full suite + bench Postgres before any commit; artifacts committed
  with claims; `tests/test_eval_evidence.py` rows in the same change as
  any published number.

## Amendments

(Recorded here as they occur, never silently.)

1. **2026-08-14, before any E1 dump was read** — evidence unit for
   G-E1/G-E3 sharpened from "turns of `answer_session_ids` sessions" to
   the per-turn `has_answer` markers the dataset actually carries
   (discovered in `evals/needle_survival.py`, whose July artifact is the
   direct precedent: needles evicted at 1.21x base under the
   pre-cascade delete-on-evict). Session-level turns remain the
   fallback for datasets without turn markers. Rationale: fewer, sharper
   needles; same test, less dilution.
2. **2026-08-14, before any E5 run** — E5 mechanics finalized after the
   telemetry recon: the daemon records no query text anywhere (verified:
   no query/search table in schema v29, access logs off), so the query
   corpus is harvested from local Claude Code transcripts
   (`evals/live_replay_flat_ab.py harvest`; 202 distinct real
   `memory_search` queries). Replay runs offline against two restored
   copies of the 2026-08-14 01:47 bank backup (665 entries) — arm A
   hydrates the live 8-band topology, arm B a single flat band at the
   continuum total capacity via the unknown-band hydration fallback.
   The query corpus and per-query selections contain private text and
   are never committed; the committed artifact carries aggregates only.
3. **2026-08-15, mid-campaign (operational, no metric change)** — a
   forced Windows update rebooted the box mid-run on 08-14; all
   workloads resumed from their cursors. Post-reboot desktop apps hold
   ~2.1 GB VRAM, which made the bench server's 100k-token KV OOM under
   load (crash 04:33, `qwen-server.err`: `CUDA error: out of memory`);
   the server now runs `-c 32768`, verified compute-inert by a
   byte-identical judge smoke vs the 100k config
   (`abl25-e5-judge-smoke{,-c32k}.json`). Separately, the judge script
   originally degraded a dead server into all-tie verdicts — that
   invalid artifact was deleted and the script now aborts on 5
   consecutive call failures or >20% unparseable verdicts. Replicate
   aggregates on the truly-deterministic q8_0 server show std=0.0000
   (byte-identical replicates); that is the no-drift receipt, and
   significance rests on question-level pairing as preregistered — the
   July aggregates' nonzero spread predates the 2026-07-27 determinism
   fix.
4. **2026-08-14 03:35, before the full E5 judge run** — within-order
   judge replicates reduced 5 → 1: a paired determinism probe on the
   reproducible q8_0 server returned byte-identical verdicts on an
   identical rerun (`abl25-e5-judge-smoke{,2}.json`, per_query equal),
   so repeats of an identical prompt carry zero information. Both
   presentation orders are retained (the axis with real variance). The
   5-replicate convention still applies to the accuracy arms, whose
   replicates re-run generation and do vary.
