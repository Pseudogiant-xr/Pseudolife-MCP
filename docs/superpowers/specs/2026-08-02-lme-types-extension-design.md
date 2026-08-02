# LongMemEval harness: beyond the KU slice (`--types`)

Date: 2026-08-02. Status: design before code.

## Why

Two independent needs converge on the same harness change:

1. **Statistical power.** The dreamer comparison is stuck at the KU pool's
   ceiling (n=78): opus-vs-sonnet measured 5/0 paired cortex wins at
   p=0.063 (`dreamer-choice-verdict.json`) — direction consistent since
   July, never significant. The other five LongMemEval question types hold
   422 more questions.
2. **Comparability.** Competitors publish on the full LME-500 (Mem0's open
   suite runs exactly that). Our published numbers cover only the KU slice.

## Change

`evals/longmemeval_bench.py` gains `--types` (comma list or `all`;
default `knowledge-update`):

- `load_questions(dataset, types)` filters on the requested set.
- Artifact and bank names carry a type slug: `ku` (default — existing
  filenames stay byte-identical, no canonical renames), `all`, or joined
  short codes (`ms`, `tr`, `ssu`, `ssa`, `ssp`) for explicit lists.
- Rows persist `question_type`; summaries add a per-type accuracy
  breakdown when more than one type ran.

**Judge validity per type.** The current `_JUDGE_SYSTEM` is faithful to
the official KU judge and already carries the abstention clause (30 `_abs`
questions score on declining to answer). Its one KU-specific line — "The
question asks about updated knowledge…" — is wrong framing for the other
five types, so: KU rows keep the existing prompt **verbatim** (canonical
comparability), non-KU rows get a generic variant that drops only the
update clause. Rows without a persisted `question_type` (all pre-existing
files) fall back to the KU prompt, so re-judging old artifacts reproduces.

## Validation before any full run

Smoke: `--types all --limit 12` (~2/type) with the local Qwen extractor
and the reproducible server — checks ingestion, answering, judging, and
abstention behavior per type without spending shim calls. Gate for a full
run: every type produces judged rows, `_abs` rows abstain-or-fail rather
than error, and the per-type breakdown lands in the summary.

## Non-goals

- No change to KU-only defaults, filenames, or any published number.
- The full 500-question sonnet/opus extraction runs are a separate
  (overnight-scale) decision — this PR delivers the harness + smoke.
- Official-suite parity (Mem0 runs GPT-4o judge at top-200) is a
  comparability caveat, not a target; our judge stays local and
  reproducible.
