# Sonnet extractor prompt v3 — coverage mandate (design + preregistration)

Date: 2026-08-02. Status: pre-registered before measurement.

## Problem

The 2026-07-27 smarter-extractor comparison (full-78 KU-oracle, deterministic
Qwen re-judge, `evals/results/extractor-comparison-smoke0726-qwenjudge.json`)
left the cloud-dreamer choice unresolved: Fable 0.885 vs Sonnet 0.808 cortex,
direction positive in every view but nothing significant at n=78 (noise floor
~6 rag-control flips).

A per-question autopsy of the discordant pairs (this document's motivating
evidence, run 2026-08-02 on the persisted `-qwenjudge.jsonl` rows) shows the
loss class is not answer *errors* but answer *absence*:

- 9 questions Sonnet loses / Fable wins — **all nine are "I don't know"
  abstentions**, none are wrong answers.
- 7/9 pair with visibly thinner Sonnet banks (9v13, 6v11, **0v15**, 5v17,
  5v11 facts); one session about the user's yoga-for-anxiety routine yielded
  **zero** extracted facts.
- 2/9 had the answer in the bank (`answer_in_current_fact=true`) and are
  answering-layer losses, out of scope for a prompt fix.
- Of the 6 both-lose questions, 4 are double abstentions of the same shape.
- Fable's own 3 losses are also abstentions; its mean bank is 13.8 facts vs
  Sonnet's 11.3.

Missed facts cluster into nameable classes: personal quantities and records
(5K time 25:50, $400k pre-approval, 3x/week yoga, 4→5 engineers, 15
baseballs), current locations of possessions (painting above the bed,
sneakers in the shoe rack), and facts about named third people (Rachel's
employer/move). Under-extraction — the v1 selectivity disease that v2's
RECALL FIRST attenuated but did not cure — remains the dominant loss mode
for **both** cloud extractors.

## Hypothesis

Adding targeted coverage mandates for the measured miss classes to the v2
prompt lifts KU-oracle cortex accuracy by rescuing abstentions, without
regressing precision (stale leak / supersession discipline).

## Change (v3 = v2 verbatim + additions, schema byte-compatible)

1. Calibration nudge in RECALL FIRST: "8–15" → "10–20" claims per 20+ note
   batch (Fable's winning banks average 13.8; Sonnet's 11.3).
2. New NUMBERS ARE FACTS block: quantities attached to the user's life —
   times, amounts, frequencies, counts, durations, prices — are always
   durable facts.
3. New WHERE THINGS ARE block: the current location/storage of a possession
   is a durable fact.
4. Extension of the third-person rule: facts about named people in the
   user's life (friends, colleagues, family) are durable facts under that
   person's entity — not just the résumé/bio case v2 names.
5. Anti-zero guard appended to the smalltalk rule: a batch in which the user
   discusses their own life is never pure smalltalk.

## Preregistered gates (in order; measurement after this file is written)

1. **Ladder conformance** (`ladder_sweep.py --rung sonnet-5
   --system-prompt-file evals/prompts/sonnet_extractor_v3.md --out-tag
   sonnetv3-0802`): gold_recoverable = 1.0 and stale_leak = 0.0 required
   before spending the full run. Abort on failure.
2. **Full-78 KU-oracle** (`longmemeval_bench.py --dataset oracle --extractor
   sonnet-5 --system-prompt-file … --tag sonnetv3-0802`), answer+judge on
   the reproducible Qwen server (Start-Qwen, never -Fast).
3. **Ship rules** (all must hold to adopt v3 as the production shim prompt):
   - Paired cortex vs the v2 baseline
     (`longmemeval-ku-oracle-sonnet-5-smoke0726-qwenjudge.jsonl`) net
     positive, with the discordant-pair count exceeding the rag-control
     disagreement on the same pair of runs (noise floor).
   - At least 4 of the 13 autopsied abstention losses flip to correct
     (9 sonnet-loses + 4 both-lose abstentions).
   - Supersession count within 20% of the v2 run's 344 (coverage must not
     come from double-booking slots instead of superseding).
   - rag arm agrees with the v2 baseline within the established noise floor
     (~6 flips); a larger rag delta invalidates the comparison.
   - stale_leak 0.0 on the ladder (gate 1).
4. **Comparison context**: also report paired v3-vs-Opus. Fable is excluded
   as a dreamer candidate on cost (user decision, 2026-08-02); its banks
   remain evidence for the coverage analysis only. If v3 ≥ Opus's 0.859
   headline, the model-swap question closes in Sonnet's favor; if v3
   improves but stays below, the swap question stays open (Sonnet vs Opus)
   and this run becomes its new Sonnet arm.

## Amendment (2026-08-02, after the v3 run, before the v2 rebuild's results)

Gate 3's rag tripwire fired: the v3 run's rag arm scored 0.859 (1237 ctx
tokens) vs the smoke0726 baseline's 0.564 (1638 ctx tokens) — far beyond
the ~6-flip noise floor, with the contexts themselves differing. Cause
identified in git history: the 2026-07-27+ serving changes (per-record BM25
fusion `a9a17c27`, set-slot surfacing `026439a6`, cascade metric
`3bd7ad18`) altered retrieval for every arm. The smoke0726 baseline is from
a different harness era and the cross-era pairing is void, exactly as the
rule intended.

Remedy: rebuild the v2 arm under the current harness (plain
`--extractor sonnet-5`, tag `sonnetv2-0802` — the shim's launch-time prefix
override supplies v2). All gate-3 rules now evaluate v3 against
`sonnetv2-0802`; the supersession rule becomes "within 20% of the
same-harness v2 count" (the 344 was also cross-era). The abstention-flip
rule is additionally checked against the same-harness v2 run's failures.
This amendment is written after the v3 numbers were seen (cortex 0.821,
hybrid 0.923, cascade 0.885, 192 supersessions, 7/13 autopsied flips vs
the old baseline) but before any v2-rebuild result exists.

## Non-goals

- Answering-layer fixes (2 autopsied losses have the fact banked; separate
  work).
- The production `_SYSTEM_PROMPT` (sidecar/local path) — that prompt is
  pinned to measured artifacts and out of scope here.
- Retrieval-side (oracle→s gap).

## Risks

- The 2026-08-01 lesson stands: even innocuous prompt additions can regress
  the cascade generally — hence the full pre-registered gate rather than a
  smoke.
- Over-extraction risk: the calibration nudge could push noise facts that
  dilute CORTEX_TOP_K=24 contexts; the supersession and stale-leak gates are
  the tripwires.
- Mechanism note: the v2 baseline reached Claude via the shim's launch-time
  prefix override; v3 rides the request's system message
  (`--system-prompt-file`). Byte content seen by the model is equivalent
  (prompt + appended vocab hint) — recorded here so a future reader knows
  the transport differed.
