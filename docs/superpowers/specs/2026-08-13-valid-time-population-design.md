# Occurrence-time population for dream claims — preregistration (2026-08-13)

Status: SPEC ONLY. Implementation is deliberately queued behind two
blockers named at the bottom; the design is recorded now because three
independent sources this month converged on the same gap.

## Problem, precisely

`CortexRecord.valid_time` (v0.4, "event time") exists but nothing
populates it: the dream path leaves it defaulting to `tx_time`, so every
fact's "when it became true" is actually "when the system learned it".
A fact stated in session 3 about a session-1 event carries session-3
time. SodaMem (arXiv:2608.08055) persists mention time and occurrence
time separately and hits 92.8% LongMemEval-S; the same split is TOKI's
bitemporal algebra (2606.06240) and the dialogue-vs-event-time
distinction in 2601.07468. LongMemEval's temporal-reasoning category is
where the gap costs accuracy; the 2026-08-12 chronicle soak showed the
extractor CAN date things reliably when gated (188 events, 0 incorrect
dates, date-fabrication guard).

## Design (conservative slice — the only part proposed)

- Claims may carry `"occurred": "YYYY-MM-DD"` — same rules as the
  chronicle events pass: resolved only from dates VISIBLE in the note
  text (the batch-corpus `_DATE_LIKE_RE` fabrication guard, verbatim),
  exact calendar days only, null over invention. Parse-boundary
  validation identical to `events_from_parsed`'s date handling.
- The claim loop converts a surviving date to epoch and passes it as
  `valid_time` through `cortex_write` → `write_fact` (a new optional
  kwarg on the service method; the store already accepts it).
- **The HLC/`tx_time` ordering authority is untouched — invariant, not
  preference.** `valid_time` is display/query metadata; supersession
  ordering, confirm semantics, and contender routing read exactly the
  fields they read today. A test pins that two claims differing only in
  `occurred` route identically.
- Rollback must restore the pre-image's `valid_time` by the same
  recovery pattern PR #146 added for stance (from the superseded
  record's own row — the journal's fixed columns carry neither).
- Serving: `valid_time` already rides `_cortex_record_to_dict`; recall's
  temporal cues could later prefer it over `asserted_at`, but that is a
  ranking change and explicitly NOT in this slice.

## Why queued, not built tonight

1. **File overlap with PR #146** (claim parse whitelist, claim loop,
   `_rewrite_prev`): building off master conflicts; building atop the
   unmerged branch presumes the merge. Queue behind #146 landing.
2. **The prompt lesson is hours old**: `occurred` requires a prompt-rule
   addition — the same same-call field-addition class that just failed
   the KU gate twice (v7 events −0.053; v8 stance cortex −0.115,
   `stance-ku-paired-verdict.json`). Before ANY new field rides the
   claims call, the open diagnosis question — WHY the stance rule
   degraded knowledge-update extraction (update-statement interaction is
   the leading suspect; a serving-side answerer-timidity component is
   confirmed separately) — should be answered, or the new arm should be
   built expecting the same fate. The chronicle precedent (separate
   extraction pass, claims byte-untouched) is the proven fallback shape
   if the combined call taxes claims again.

## Preregistered gates (when implemented)

1. Watched-RED: parse boundary (format, fabrication guard), valid_time
   lands on the record, tx_time/HLC byte-identical routing, rollback
   recovery.
2. Ladder non-inferiority (fresh v-current control, same window).
3. KU-oracle paired vs same-window control (the load-bearing gate; the
   ladder is measured-blind to prompt taxes at this rung).
4. Temporal-reasoning slice of LongMemEval as the WIN condition — this
   feature exists to move that number, not just to be harmless; a
   harmless-but-useless result is a no-ship too.
