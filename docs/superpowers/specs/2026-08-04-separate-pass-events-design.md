# Separate-pass event extraction: chronicle without the claims tax

Date: 2026-08-04. Status: design before code. Preregistered gates below;
baselines are the committed 2026-08-03/04 artifacts.

## Why

Two measured facts from the chronicle line
(`evals/results/agg-recall-phase2-weak-verdict.json` + correction,
`evals/results/longmemeval-all-oracle-qwen-27b-ev-smoke-clean-0804.jsonl`):

1. **The v7 prompt's events section, riding the claims call, costs claim
   quality**: −0.053 (p 0.011, 7W/21L) on the weak-set vanilla hybrid vs
   the v5-extracted baseline — attention/token competition on real
   batches that the short op-probe is structurally unable to see.
2. **Correctly-scoped event serving looks mildly positive**: after the
   bench-reset contamination fix, the 12-question clean smoke scores
   hybrid_ev 0.750 vs hybrid 0.667 (+1/−0), and the row contamination
   had flipped to a loss no longer regresses. Directional, not
   decision-grade.

The obvious synthesis: **extract events in their own LLM call**, so the
claims call runs the shipped v5 prompt byte-identically and interference
is zero *by construction* rather than hoped-for. Cost: one extra
extractor call per dream batch, only when `memory.dream.chronicle` is on.

## Design

- **Events prompt** — a NEW standalone measured artifact,
  `evals/prompts/events_pass_v1.txt`, pinned byte-identical to
  `dream._EVENTS_SYSTEM_PROMPT` by a test (same pin pattern as the v5
  claims prompt). Language carries over the v7 events section's measured
  phrasing (well-formed blocks in both smokes): dated occurrences, exact
  `YYYY-MM-DD` or null, verbatim `date_phrase`, per-note `source`
  citation, the anchored worked example, no second Output block.
- **`OpenAICompatExtractor.extract_events(texts)`** — same endpoint,
  same numbered-notes user message, events system prompt, parsed by the
  existing `events_from_parsed`. Raises `ExtractorError` on failure like
  `extract`.
- **`service.dream_run` wiring** — when `chronicle` is on and the
  extractor exposes `extract_events`, the pass runs AFTER the claims
  loop and BEFORE `dream_commit`, once per batch. Event writes reuse the
  existing (already-tested) path unchanged: literal gate on the
  description, batch-corpus date-fabrication guard, exact dedup, journal
  kind `"event"` with `chronicle_event_id`, rollback-by-delete.
- **Failure is non-fatal**: an events-pass failure logs, sets
  `events_pass_failed: true` in the result, and the dream commits its
  claims normally. Rationale: events are an additive enrichment layer; a
  broken events endpoint must never stall consolidation. The lost events
  for that batch are not retried (the cursor has moved) — accepted and
  stated.
- **The inline path stays** (extractor-emitted `kind:"event"` dicts
  routed by the claim loop): harmless with the shipped v5 prompt (which
  emits none), and it keeps any future single-call extractor honest. The
  separate pass is the measured design; the v7 combined prompt does NOT
  ship.
- **No new knobs.** `memory.dream.chronicle` (default False) gates the
  pass; the bench enables it per-run with `--chronicle`. Crucially the
  bench run needs NO `--system-prompt-file`: the claims call uses the
  shipped v5 default — that is the whole point.

## Preregistered gates (one 500-question run, tag `ev2-sep-0804`)

Full `--types all` oracle run, 4 arms (rag / cortex / hybrid /
hybrid_ev), `--chronicle`, default claims prompt, reproducible qwen
server, `compare_arms.py` paired permutation (10k draws, seed 0), fresh
out-tags. Gates in evaluation order:

1. **rag control (validity):** rag vs `aggp1-variants-0803` rag on all
   500 shared questions must be exactly 0 flips. Nonzero ⇒ run invalid.
2. **Claims-inertness tripwire (the load-bearing one):** this run's
   vanilla hybrid vs `aggp1-variants-0803` hybrid must be exactly 0
   flips over all 500 questions. The claims call is byte-identical v5 on
   a deterministic pipeline, so ANY flip means the events pass perturbed
   claims extraction (e.g. via server state) — the run's central claim
   fails and the −0.053 concern returns. This gate turns "we believe the
   separate pass is interference-free" into a measured zero.
3. **Primary ship gate:** hybrid_ev vs same-run hybrid on multi-session
   + temporal-reasoning (n=266): improvement with p < 0.05.
4. **Strong-set non-inferiority:** hybrid_ev vs same-run hybrid on
   knowledge-update + the three single-session types (n=234): no
   regression beyond margin 0.02 (spurious cue firings adding irrelevant
   event blocks are the risk being bounded).
5. BEAM event_ordering remains a directional check only, deferred (as in
   Phase 1/2 preregistrations).

Ship rule: gates 1–4 all pass ⇒ separate-pass chronicle becomes a ship
candidate (default-on is its own follow-up PR + human decision, with the
events prompt artifact shipping alongside). Any gate fails ⇒ chronicle
stays off, result recorded in
`evals/results/ev2-separate-pass-verdict.json` either way; every number
that reaches docs gets a `test_eval_evidence.py` row.

## Cost

One ~7–8.5 h overnight run (500 q × ~55–60 s: extraction + events pass +
4 answered/judged arms). Resumable by row; a pause costs only the
in-flight question. Implementation is small: the event-write path,
gates, journal, serving, and harness all exist and are tested — this
change is one prompt constant, one extractor method, one service hook,
and their tests.

## Non-goals

- No answer-time synthesis over events (counting/windowing) — that is
  the NEXT experiment if serving raw blocks passes; one variable at a
  time.
- No supersession/dedup logic beyond the existing exact match.
- No production default flip inside this change.

## 2026-08-06 amendment: the deferred BEAM re-run (preregistered before any GPU run)

The Phase 2 design deferred its BEAM gate; with the four LME gates passed
(`evals/results/ev2-separate-pass-verdict.json`), this amendment closes that
debt. Motivation baseline: the committed `beam100k-qwen-0802` run, where
hybrid collapses on exactly the abilities events target — event_ordering
0.13, summarization 0.24, contradiction_resolution 0.28 (float means;
`evals/results/beam-100K-qwen-27b-beam100k-qwen-0802.jsonl`).

Run: BEAM 100K tier, all 20 chats / 400 questions, `--extractor qwen-27b`,
`--chronicle`, tag `beam100k-ev-0806`, reproducible q8_0 judge
(`Start-Qwen`, never `-Fast`). The adapter gains `--chronicle` (sets the
LME `CHRONICLE` context flag + `svc.config.memory.dream.chronicle` per
chat service) and answers/judges the `hybrid_ev` arm beside the three
existing arms. `compare_arms.py` gains a `--metric score` mode (BEAM rows
carry per-question float rubric means, not `_correct` booleans; the
sign-flip permutation applies unchanged to float deltas, wins/losses =
sign counts) and BEAM row keying (`chat_id/type/index` when
`question_id` is absent).

Gates, in order (1–2 are validity; a failure invalidates 3–4):

1. **rag control** — rag vs the `beam100k-qwen-0802` rag, all 400 shared
   questions, `--metric score`: delta exactly 0. Nonzero = pipeline or
   judge drift; run invalid.
2. **claims-inertness** — this run's vanilla hybrid vs the 0802 hybrid:
   delta exactly 0. The events pass runs during ingestion; the claims
   bank and its retrieval must be untouched, as measured on LME.
3. **primary** — hybrid_ev vs same-run hybrid on `event_ordering`
   (n=40, baseline 0.13), paired sign-flip permutation 10k/seed 0:
   improvement with p < 0.05. Secondary (reported, not gating):
   summarization, contradiction_resolution, temporal_reasoning deltas.
4. **non-inferiority** — hybrid_ev vs same-run hybrid pooled over all
   remaining abilities: no regression beyond 0.02 mean score.

Ship rule: this run cannot flip any default (that decision belongs to the
live soak review, task #36). It exists to back or bound the event_ordering
claim on a second benchmark: 4/4 pass ⇒ the BEAM weak-ability claim in the
docs gains its events counterpoint with `test_eval_evidence.py` rows;
gate 3 fails with 1–2 passing ⇒ recorded as an honest negative — BEAM
event ordering needs answer-time synthesis, not just served blocks (the
already-named next experiment). Verdict either way:
`evals/results/beam-ev-verdict.json`. Never overwrite any
`beam100k-qwen-0802` artifact.

Power note, stated up front: n=40 per ability. A sign test needs roughly
6+ net wins to clear p<0.05, i.e. a real per-question effect on ~15% of
event_ordering rows. The LME temporal-reasoning effect was that large
(+0.090 on n=133 with events served on 71% of rows); if BEAM serving
rates are much lower, an underpowered null is the honest expected
outcome and will be recorded as such, not spun.
