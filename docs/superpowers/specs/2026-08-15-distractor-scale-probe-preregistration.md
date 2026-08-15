# Distractor-scale probe — does accumulation degrade retrieval? (preregistered 2026-08-15)

## Gate outcomes (2026-08-15, same day — no deviations from the design below)

Artifact: `evals/results/distractor-scale-probe-2026-08-15.json`
(78 paired questions, all with gold evidence present in their own dump).

- **G-D3 (sanity): PASS** — 1x evidence-in-top-6 = 0.830, well above the
  0.5 floor; the metric can gate.
- **G-D1 (quality): ACCUMULATION HURTS** — evidence-in-top-6 falls
  monotonically with pool size: 1x 0.830 → 3x 0.758 → 7x 0.684 →
  15x 0.597 → 31x 0.513. Paired 1x−15x delta **+0.233, p < 0.0001**
  (10k sign-flip perms, seed 0). The sweep agent has measured value
  with a recovery ceiling of ~23 points at ~7k entries in the
  off-topic-distractor regime.
- **G-D2 (latency): NOT justified yet** — median BM25 build+score:
  77 ms (1x) → 620 ms (15x) → 1339 ms (31x); fitted ~0.088 ms/entry
  puts the 1 s crossing at ~11.4k entries (~3 years of live-bank growth
  at +10/day). Quality binds long before latency does.

Per the fixed interpretation rule: **the follow-up sweep design inherits
this construction with a realistic-sweep arm replacing the 1x oracle.**
Note for the flat-band migration shipping alongside: this finding is
orthogonal to the band verdict — dilution hits banded and flat pooling
identically (both arms sat in the same pools all week); no band
structure fixes it, curation does.

The flat-band verdict left one forgetting question open: no experiment
has ever compared a lean bank against an accumulated one. Every capacity
experiment forced eviction and asked *which* victims to pick; none asked
whether carrying junk costs retrieval quality at all. This probe answers
that before any sweep-agent work is built — if an unswept bank holds up
at 10x scale, the sweep is premature and only the O(n) BM25 rebuild
needs attention.

## Design

Pure offline analysis over the existing v25 replay dumps
(`evals/results/banks/s-qwen-27b-ablbands-flat/`, 78 questions, ~470
turns each, 1024-d embeddings already computed) using the G0-validated
offline mirror (`band_ablation.select_topk`, flat policy, recency off,
BM25 on — fidelity 1.000 vs live search). No new embedding compute, no
GPU, no judge.

**Pooling construction**: for each question q, build banks at growing
distractor scale by concatenating q's own dump with the dumps of the
next K questions in a fixed rotation (question order sorted by
question_id, wrap-around; deterministic, no RNG). Scales:

| arm | pool | ~entries |
|---|---|---|
| 1x | own haystack only | ~470 |
| 3x | own + 2 others | ~1,400 |
| 7x | own + 6 others | ~3,300 |
| 15x | own + 14 others | ~7,000 |
| 31x | own + 30 others | ~14,500 |

The 1x arm doubles as the **perfect-sweep arm**: an oracle curation
that removes every foreign turn recovers exactly this bank, so
`metric(1x) − metric(Nx)` IS the maximum value a sweep could add at
that scale.

**Primary metric (per question, per scale)**: evidence hit rate — the
fraction of gold-evidence turns (`has_answer` markers, the
needle_survival convention) present in the mirror's top-6 selection.
Secondary: evidence-in-top-3; rank of the first evidence turn; and
whether ANY evidence turn is served (all-or-nothing recall).

**Latency metric**: wall-clock of the BM25 index build + score per
query at each scale (the mirror constructs the real `BM25Index`), plus
the dense cosine matmul time — measured on the same machine, reported
as medians over the 78 questions. This is the operational half: the
O(n) rebuild is the only thing known to bind before quality does.

**Statistics**: per-question paired deltas (1x vs each scale), sign-flip
permutation test, 10k perms, seed 0 — the house convention. 78 paired
questions per comparison.

## Preregistered gates

- **G-D1 (quality)**: accumulation *hurts* iff evidence-in-top-6 drops
  by ≥ 0.05 (absolute) from 1x to 15x with p < 0.05. If significant,
  the sweep agent has measured value at that scale and the recovery
  ceiling is the measured delta. If the drop is significant but < 0.05,
  report as "measurable but small — sweep is low priority". If not
  significant at 15x, check 31x before declaring accumulation free.
- **G-D2 (latency)**: index maintenance work is justified iff median
  per-query BM25 cost exceeds 1 s at ≤ 15x (~7k entries). Report the
  fitted per-entry cost either way — the number that predicts when the
  live bank (682 entries, ~+10/day) crosses it.
- **G-D3 (sanity)**: the 1x arm's evidence-in-top-6 must be ≥ 0.5 —
  if the mirror can't find evidence in the question's OWN haystack at
  useful rates, the metric is too weak to gate anything and the probe
  reports "inconclusive, needs the judged variant" instead of a verdict.

## Interpretation rules (fixed now)

- G-D1 fail + G-D2 fail → accumulation is free at these scales; the
  sweep agent is premature; revisit at 10x the live bank's size or when
  BM25 latency crosses 200 ms live.
- G-D1 pass → the sweep has a measured recovery ceiling; the follow-up
  design (realistic sweep vs oracle sweep) inherits this probe's
  construction with the sweep arm replacing the 1x oracle.
- G-D2 pass alone → build incremental BM25 index maintenance, not a
  sweep.

## Caveats stated up front

- Distractors here are *other users' conversations* (foreign
  haystacks) — maximally off-topic relative to real accumulated chatter
  (status logs, near-duplicates), which is semantically CLOSER to live
  queries and plausibly more harmful per entry. So a null here bounds
  only the off-topic-noise regime; a pass is a fortiori evidence. The
  realistic-chatter variant (synthesizing status-style near-duplicates)
  is explicitly out of scope for this probe and only worth designing if
  this one is null AND the live bank shows quality drift.
- Single embedder/backbone (v25); the conclusion is backbone-scoped
  like everything else this week.

## Execution

`evals/distractor_scale_probe.py`, CPU-only, writes
`evals/results/distractor-scale-probe-<date>.json` (per-question rows +
aggregates + gate verdicts; committed with any published claim).
Expected runtime: minutes (numpy over ~14k×1024 at the largest scale,
78 × 5 selections).
