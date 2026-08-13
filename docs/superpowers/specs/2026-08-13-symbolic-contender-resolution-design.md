# Symbolic-first contender resolution — preregistration (2026-08-13)

Status: SPEC ONLY — implementation deliberately deferred to a gated
cycle. Written during the 2026-08-13 night shift; the maintainer has not
yet reviewed the equivalence classes, which are the load-bearing design
decision.

## Problem, precisely

Contenders park and wait. The only exits today are a human
`memory_fact_resolve`, the quarantine's independent second witness, or
implicit retirement when the slot moves on. LatticeMind
(arXiv:2608.08236, verified 2026-08-12) measured a two-stage design —
cheap SYMBOLIC conflict checks first, LLM reconciliation only for what
they cannot settle — at 0.97 vs 0.61 on label-blind ConflictBank, with
the symbolic layer alone worth 12–14 points. PseudoLife already has two
symbolic resolvers inline in `write_fact` (normalized value equality →
confirm; compression echo → confirm). What it lacks is (a) any richer
equivalence than string normalization, so `"45 seconds"` vs `"45s"`
parks a contender a human must clear, and (b) any maintenance pass over
the contender population — a parked value equivalent to the current one
sits as apparent conflict forever, polluting `memory_fact_get`'s
contested signal.

Note on scope: the *surfacing* half of the original review item is
already shipped — `memory_fact_get` returns `{record, contenders}` and
documents non-empty contenders as unsettled conflict; `memory_recall`
marks cortex facts `contested`. Nothing to build there.

## Design (v1 proposal — NOT approved)

A **contender-equivalence sweep** as a dream-sweep maintenance step
(the `retype_quarantined_links` pattern: off the hot path, bounded per
tick):

- For each slot with an active contender, test the contender's value
  against the current value with symbolic equivalence only:
  1. numeric equality after unit-affix stripping, reusing the literal
     gate's measured matcher semantics (`08`↔`8`, `3.20`↔`3.2`,
     spelled numbers) — the one equivalence machinery in the tree with
     a false-positive record we know (1.3–1.7% firing, almost all
     genuinely unbacked);
  2. the existing compression-echo predicate, applied symmetrically.
- On equivalence: the contender CONFIRMS the current record (support
  union, confidence reinforce — the same effect its write would have
  had if the values had normalized equal) and retires with a
  `resolved:symbolic_equivalence` provenance marker. Never the reverse
  direction: a contender never wins a slot symbolically.
- Explicitly NOT in v1: semantic/embedding similarity (that is the
  LLM-reconciler tier and a different trust conversation), any
  resolution of QUARANTINE-parked contenders (the two-man rule's parks
  are trust holds, not value disputes — the sweep must skip any
  contender carrying `quarantine:low_trust`), and any change to
  `write_fact`'s inline routing.

## Why deferred

This changes conflict semantics (a class of contender stops being
visible), and the equivalence classes themselves are a judgment call
with a false-merge failure mode ("$400,000" vs "400,000 pre-approval
cap"? "v2" vs "2"?). The 2026-08-13 KU-gate episode (stance prompts
passed every cheap gate and failed the 78-question paired run) is fresh
evidence that plausible write-path changes need the expensive gate, not
just unit tests.

## Preregistered gates (when implemented)

1. Watched-RED unit tests per equivalence class, plus the two negative
   classes above pinned as NON-equivalent.
2. Quarantine exclusion smoke: a `quarantine:low_trust` contender is
   never symbolically resolved (composition with the two-man rule).
3. Ladder non-inferiority (`gold_recoverable`, `stale_leak`).
4. Live-bank replay audit: run the sweep read-only against the
   production bank's contender population; publish would-resolve pairs
   for human review BEFORE the first live run (the gate-3-style
   friction/false-merge estimate; artifact
   `evals/results/symbolic-resolution-replay.json`).
5. KU-oracle unchanged (v5 prompt, sweep on vs off) — conflict-routing
   changes are exactly what its knowledge-update questions stress.
