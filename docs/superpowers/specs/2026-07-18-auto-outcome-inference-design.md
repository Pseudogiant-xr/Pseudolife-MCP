# Auto-outcome inference at episode close — design

**Status**: approved (interactive brainstorm) · **Date**: 2026-07-18
**Predecessor**: the 2026-07-18 capture measurement (live bank, N=89 root
episodes: 88/89 store ≥1 substantive entry, but 31/89 close with ZERO
outcome signals, and failures are logged 4.5× less often than successes —
25 failure / 23 correction / 112 success). The REFLECT beat, not capture,
is the memory loop's measured leak. Outcome signals are the only feeder
for lessons.

## What this builds

When the idle reaper closes a session episode that stored something but
logged no outcomes, the daemon infers up to 3 outcome signals from the
episode's own contents, marks them `origin="inferred"`, and lets the
already-firing end-of-session dream synthesize lessons from them at a
confidence discount. Daemon-owned, no client changes, no new schema.

## Decisions from brainstorm (with the options rejected)

1. **Trust model: direct-with-discount.** Inferred signals flow into the
   existing dream→lessons path immediately; lessons built purely from
   inferred signals start at confidence 0.4 (explicit-signal lessons start
   at 0.6) and carry `provenance={"inferred"}`. A later explicit signal on
   the same `(task, aspect)` slot raises confidence through the normal
   confirm path. *Rejected*: quarantine-until-reviewed (Console queue —
   review queues go unvisited per the user's own usage), asymmetric
   auto-consume-failures-only (v2 candidate).
2. **Input scope: all daemon-visible context.** Episode + sub-episode
   titles, the episode subtree's entries, supersession events, and any
   explicit signals (none, given the gating). The daemon never sees the
   conversation; what was never stored cannot be inferred — accepted
   ceiling, measured rather than papered over. *Tabled*: a client-side
   Stop-hook session summary (richer input, new privacy surface — its own
   future project).
3. **Gating: zero-signal episodes only.** Trigger = root episode closing
   with ≥1 entry anywhere in its subtree and zero outcome signals across
   the subtree. Targets the measured 35% silent-session gap with no
   dedup problem and minimal extractor load. *Deferred to v2*: the
   failure-only top-up for episodes that logged only successes (needs
   fuzzy dedup against explicit signals; wait for v1 live data).
4. **Placement: a new dream stage** (`infer_outcomes`) running inside
   `dream_run_auto` before lesson synthesis — the reaper already fires
   that dream on session close (`reap_idle_sessions` →
   `_close_session_locked(sk, run_dream=True)` →
   `_fire_and_forget_dream`). Reuses extractor tiering (Sonnet shim /
   sidecar fallback), the locked-pull → unlocked-extract → locked-commit
   discipline, and cursor semantics. *Rejected*: inline at close (runs
   under the global lock; extractor calls take minutes on CPU) and a
   dedicated worker thread (second lifecycle, no benefit).

## Mechanics

### Stage: `infer_outcomes`

Two verified integration facts the implementation depends on:
`_close_session_locked` fires the end-of-session dream for ANY non-empty
closed episode regardless of signals (`fire = bool(run_dream and result
and not pruned)`, service.py:2701), so zero-signal closes do reach the
dream. But the dream's own `would_fire` gate counts pending work
(unextracted entries, undrained signals) — a zero-signal episode may
contribute nothing to today's counts. **`dream_status`/`would_fire` must
therefore also count pending inference candidates** (episodes past the
cursor matching the gating), or the fired dream will no-op past them.

- **Cursor**: `meta` key `outcome_inference_cursor` — epoch of the newest
  episode close processed. No new tables or columns anywhere in this
  design; `origin` on `outcome_signals` is already free text.
- **Scan** (locked pull): root episodes with
  `ended_at > cursor`, session-keyed, ≥1 entry in the subtree, zero
  outcome signals in the subtree. Collect per-episode context: titles,
  entries **including `status`/`log` sources** — the
  `dream.exclude_sources` convention protects the fact graph from status
  noise; outcome inference is a different consumer for which status dumps
  are the richest material. This exception is deliberate and documented
  in the code and in `docs/guide/episodes.md`.
- **Extract** (unlocked): one extractor call per candidate episode.
  Output contract: JSON list of ≤ `infer_outcomes_max_signals` claims
  `{task, outcome, about, detail}`. Validation identical to
  `record_outcome`: `outcome ∉ {success, failure, correction}` → the
  claim is dropped, never coerced (refuse-don't-coerce, service.py:1573).
  `task` must be non-empty; `polarity` is left for the dream to infer as
  usual.
- **Commit** (locked): surviving claims written via the existing
  `add_signal` path with `origin="inferred"` and the episode's id, then
  the cursor advances. The same dream's lesson stage drains them.

### Trust plumbing

- `extract_lessons` receives each signal's `origin`; the synthesis prompt
  labels inferred signals as machine-inferred.
- A lesson synthesized *only* from inferred signals: initial
  `confidence=0.4`, `provenance={"inferred"}`. Mixed inferred+explicit:
  normal 0.6. Confirmation by later explicit signals follows the existing
  lesson confirm/supersede path unchanged.

### Config

Under `memory.lessons`:

- `infer_outcomes: bool = True` — **default on**; the feature exists to fix
  silent sessions, and the kill-switch is documented in
  `docs/guide/configuration.md`.
- `infer_outcomes_max_signals: int = 3`.

### Idempotency & failure

- Structural idempotency: after a successful inference the episode is no
  longer zero-signal; the cursor only covers it once.
- Extractor unreachable → stage skipped, cursor held, next dream retries
  (existing dream discipline).
- Malformed/empty extractor output for an episode → log one warning;
  after `2` attempts on the same episode the cursor advances past it
  (a poison episode must not wedge the stage; this bounded-retry is the
  one deliberate advance-past-failure and is logged as such). Attempt
  count is kept in the cursor's meta value (`{"ts": …, "retry": {…}}`),
  still no DDL.
- Inference finding nothing is a valid outcome: cursor advances, nothing
  written.
- All writes go through the existing `_txn` COMMITTED discipline.

## Evaluation (per the 2026-07-18 eval-hygiene rules)

1. **Bench rung**: extend `evals/lesson_synthesis_bench.py` with an
   inference fixture set — ~8 hand-authored episodes (deploy-succeeded,
   dead-end-hit, user-corrected-me, mixed-session,
   ambiguous-must-abstain ×2, status-only-session, single-entry-thin) →
   expected signal sets, scored on outcome-enum correctness, task
   grounding, and abstention. Optimization target = the shipped Gemma E4B
   sidecar; Qwen 27B as ceiling reference only (standing testing
   principle).
2. **Live success criterion** (2–3 weeks after deploy, measured on the
   real bank with the same read-only queries as the 2026-07-18 baseline):
   outcome coverage of substantive sessions ≥90% (baseline 65%); failure+
   correction share of new signals above the baseline 30%; lesson bench
   stays green; spot-check that surfaced lessons cite inferred provenance
   honestly.
3. **Regression discipline**: this touches the dream write path → re-run
   the extractor ladder before deploy (standing rule), and
   `evals/regression_gate.ps1` before commit (it should pass untouched —
   the stage does not alter retrieval/serving).

## Error surface & observability

- `dream_status` gains an `infer_outcomes` block (episodes scanned /
  signals written / retries pending) so the Console's loop-health tile
  can show the REFLECT beat working.
- Log lines at stage start/end with counts; per-episode warnings on drops.

## Out of scope (recorded)

Client-side session summaries; failure-only top-up for signaled episodes
(v2, gated on v1 live data); Console review UI for inferred signals;
multi-writer quarantine tiers; any new schema version.
