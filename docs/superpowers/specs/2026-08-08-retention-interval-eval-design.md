# Retention-interval eval — preregistration (2026-08-08)

## Motivation

Every eval this project runs is a snapshot: build a bank, ask questions,
score. Nothing measures behavior as a function of *time since the
information landed* — which is exactly the axis the shipped freshness
machinery (`freshness_class`, `effective_confidence`, `stale`) claims to
govern, and exactly the axis two August papers put benchmarks on:
ScrubJay-MEM (arXiv:2608.04746) introduces a held-out
retention-interval test (TGT) with a generalization-gap metric, and
LeanMem (arXiv:2608.03463) makes temporal dynamics a first-class
routing criterion. We ship staleness flags we have never evaluated.

A code-grounding pass (2026-08-08) sharpened the question. The
freshness layer (`pseudolife_memory/memory/freshness.py`) computes
`effective_confidence(now)` and `is_stale(now)` — `now` is already a
first-class parameter throughout — but at serving time these are
**annotations only**: `service.py` reports them on the read surface
(`service.py:139-140`, `:189-190`) and no ranking, filtering, or
demotion in `memory_search` or the fact read path consumes them. Their
entire effect on answers is mediated by whether the *answerer* heeds
the reported flags. The primary question is therefore not "is the decay
curve right" but **"is the freshness machinery an active decision point
or decoration"** — the same question that closed the
distinctiveness-signal thread on 2026-07-11 (signal with no consumer,
shelved with a reopen trigger).

## Hypotheses (preregistered)

- **H1 (supersession is time-invariant).** On the KU slice, gold
  answer rate and stale-serve rate are *flat* in simulated query-time
  offset: nothing in the serving path reads the clock, so answering the
  same bank at +0, +30, +365 simulated days must produce byte-identical
  contexts and identical scores. Expected outcome: exact zero deltas.
  This is a calibration fact worth owning: it certifies that
  supersession (the KU defense) does not silently rot with age, and it
  becomes the regression guard for any future change that *does* put
  time into serving.
- **H2 (flags move answers — or they don't).** For facts whose
  validity genuinely decays without an in-corpus update (volatile
  class: prices, versions, role-holders), a served-but-stale fact
  carries `stale: true` and a decayed `effective_confidence`. H2 tests
  whether the answerer behaves differently when those flags are
  present vs stripped: on questions whose stored volatile fact is aged
  past its class staleness horizon, the flags-visible arm should
  abstain or hedge more often than the flags-stripped arm. If the two
  arms are indistinguishable, the machinery is decoration at answer
  time, and the roadmap consequence is serving-side (rank/demote/gate
  on staleness) — not better perishability estimation.

**Decision rule for the ScrubJay follow-up** (auto-classified
per-item perishability π/τ): built only if H2 shows flags move
answers (there is a consumer) AND the miss analysis shows class-level
granularity is the binding error source. If H2 fails, the next unit of
work is a serving-side staleness policy under its own preregistration,
and π/τ estimation stays shelved beside the distinctiveness signal.

## Design

### Study A — KU interval sweep (deterministic, CPU)

- Bank: one build of the LME KU-oracle bank per extractor rung
  (reuse the existing ladder build path; extractor = `e4b-v3` sidecar
  rung, the shipped default).
- Query-time injection: the harness runs in-process against
  `MemoryService`; simulated `now` is injected at the read seam (the
  freshness functions' `now=` parameter via a harness-owned wrapper, or
  a monkeypatched clock at the service boundary — implementation
  detail, but **no production knob is added**; the eval must not grow
  the config surface).
- Offsets: `{0, 7, 30, 90, 365}` days after the last haystack session
  date, applied at query time only.
- Metrics per offset: ladder `gold` and `stale_leak` (existing
  deterministic definitions), plus a context-hash per question —
  identical hashes across offsets is the strongest form of the H1
  claim.
- Pass condition: H1 holds iff all deltas across offsets are exactly
  zero. Any nonzero delta is a *finding* (something time-dependent is
  live in serving) and gets a mechanism join before anything else is
  claimed.

### Study B — flag-efficacy arms (small-n, judged)

- Seed bank: ~40 synthetic volatile facts in the four freshness
  classes, values dated so that at query time half are past their
  staleness horizon (`is_stale` true, decayed `effective_confidence`)
  and half are fresh. Seeded via the normal write path
  (`memory_fact_set` / `memory_world_set`), not SQL — the eval must
  exercise the real read surface.
- Two arms, same bank, same questions:
  - **flags-visible**: the served context carries
    `effective_confidence` and `stale` exactly as `memory_search` /
    fact reads render them today;
  - **flags-stripped**: identical context with the two fields removed
    (harness-side post-processing of the served block; nothing in the
    daemon changes).
- Questions: "what is <entity>'s <attribute>?" for each seeded fact.
  Scoring is deterministic-first: an answer that states the stored
  value unqualified counts as *served-stale* when the fact is past
  horizon; abstention/hedging is detected by the existing judge's
  abstention clause (judged calls on the reproducible `Start-Qwen`
  server only — never `-Fast`).
- Metric: stale-answer rate (unqualified stale value served as truth)
  per arm, fresh-fact accuracy per arm (the no-harm check — flags must
  not scare the answerer off *valid* facts).
- H2 holds iff flags-visible has a lower stale-answer rate than
  flags-stripped (sign-flip permutation over paired questions,
  `compare_arms.py` machinery, p < 0.05) with fresh-fact accuracy
  non-inferior (within 0.02).

### Controls

- Study A is its own control (H1 predicts exact zero; any nonzero is
  measurement or mechanism, and the context-hash distinguishes them).
- Study B carries a **no-flag-no-decay control**: the same questions
  against a bank where every fact is evergreen — bounds base-rate
  hedging by the answerer independent of any staleness signal.
- The house rag-arm rule does not apply (no model-vs-model
  comparison); the identical-input control here is the shared bank +
  shared question set across arms, with only the rendered flags
  varying.

## Artifacts

- `evals/retention_interval_eval.py` — writes
  `evals/results/retention-interval-<tag>.json` by default (every
  bench writes a file).
- Verdict: `evals/results/retention-interval-verdict.json` — prereg
  reference, commands, per-offset table (Study A), per-arm paired
  results with p-values (Study B), H1/H2 outcomes, decision.
- Any number that reaches the docs gets a `test_eval_evidence.py` row
  in the same change.

## Cost

Study A: CPU + one bank build per rung (extraction on the sidecar;
no judged calls — gold/stale_leak are deterministic). Study B: ~40
questions × 2 arms + control, answer+judge on the reproducible server —
well under an hour of GPU. No overnight window needed; if run
unattended anyway, the standing launch-verification/heartbeat/ledger
rules apply.

## Risks / honesty

- **Study A is expected to be boring.** Exact-zero is the likely
  outcome and is being preregistered as such — the value is the
  certificate plus the regression guard, not a delta.
- **Study B's synthetic facts are not production traffic.** A clean H2
  pass says the flags *can* move answers under ideal rendering; it
  does not measure how often production queries hit stale facts. The
  verdict must scope its claim accordingly.
- **Judge dependence in Study B** is bounded by the deterministic-first
  scoring (unqualified-stale-value detection is string-shaped); the
  judge only arbitrates hedging, and replicate disagreement there
  flags drift onto the wrong server (`replicate.py` warns).
- **The "decoration" outcome is a real branch**, not a failure: it
  converts directly into the serving-side staleness preregistration
  and removes π/τ estimation from the roadmap until a consumer exists.
