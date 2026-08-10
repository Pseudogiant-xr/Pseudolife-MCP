# Capture hygiene — two proposals from the 2026-08-10 alignment audit

**Date:** 2026-08-10
**Status:** proposed — agenda items for the 2026-08-12 chronicle soak review;
neither is implemented by the accompanying PR (both change core memory
semantics and belong behind preregistered gates)
**Evidence:** the 2026-08-10 alignment audit and its live incident (memory
entry "Live failure mode observed 2026-08-10…", source `pseudolife-mcp`)

## Context

The audit found four memory failure classes. Two were addressed immediately
(briefing capture-policy bullets; episode-handle resume). The remaining two
need design decisions, recorded here so the soak review can take them up
with the incident fresh.

## Proposal 1 — semantic confirmation on contested near-duplicates

**Problem.** `cortex_write` treats an agent-origin re-assert against a
user-supported current value as a contender (`action: "contested"`), even
when the incoming value is semantically the same statement in different
words. Observed live: two sessions executed the same correction list
minutes apart; the second write contested an already-correct slot, which
then needed a manual `memory_fact_resolve(accept=false)`.

**Sketch.** In the contested path only: embed the incoming value and the
current value; at similarity ≥ threshold, record a *confirmation* (bump
`last_confirmed`, merge `support`) instead of a contender. Below threshold,
current behaviour is unchanged. Origin ranking is untouched — this only
reclassifies agreement, never disagreement.

**Open questions for preregistration.**
- Threshold choice and its failure mode: two values that are *materially*
  different but lexically close (version strings, quantities) must NOT
  confirm — the number-led-scalar guard suggests digits should force the
  contender path regardless of similarity.
- Gate: replay the incident pair (must confirm) plus a corpus of true
  contradictions from fact history (must all still contest); zero
  reclassified contradictions to ship.

## Proposal 2 — capture policy for repo-derivable facts

**Problem.** A fact slot whose value is readable from the repo/config
(`version-published = 0.8.1`) drifts by construction: nothing re-asserts it
on release, and freshness decay only flags it (25 days live in this case) —
detection without closure.

**Options, cheapest first.**
1. **Briefing-only** (shipped with this PR): teach agents not to mint such
   slots. No schema change; relies on compliance.
2. **`derivable_from` provenance field** on facts: a slot carrying a
   repo-path pointer renders in recall with "re-verify at <path>" and could
   be bulk-audited by a doctor pass. Additive schema change (vNN bump).
3. **Write-time nudge**: `cortex_write` warns (not blocks) when the
   entity/attribute matches a deny-shaped list (version, schema, count,
   budget) — mirrors the number-led-scalar protection precedent.

**Recommendation.** Ship 1 (done), evaluate whether drift recurs before
buying 2 or 3 — the briefing bullet plus the correction lesson may already
close the loop, and the audit that found this is cheap to re-run.

## Non-goals

- No auto-sync of fact slots from repo state (the daemon has no repo
  access, and mirroring WHAT-IS into memory is the anti-pattern itself).
- No change to origin ranking or supersession ordering.
