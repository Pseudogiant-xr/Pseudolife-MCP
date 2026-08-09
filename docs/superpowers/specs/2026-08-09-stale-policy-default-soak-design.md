# stale_policy default-flip soak — preregistration (2026-08-09)

## What this decides

Whether `memory.search.stale_policy` flips its shipped default from
`"annotate"` to `"quarantine"` — and nothing else. The H3 eval
(`evals/results/stale-policy-verdict.json`) already resolved the efficacy
question: both policies drive unqualified stale serving from the
0.4–0.5 flags-visible floor to 0.0 in every replicate (p 0.0005), recovery
of the quarantined value on explicit ask is 1.0, and the no-harm gate held
structurally. What the eval could NOT resolve — its own stated scope — is
production behavior: how often staleness is actually served on a live
bank, and what the wrapper does to real agent sessions and to the console.

## Preconditions

PR #125 merged and deployed. The soak itself is an OPERATOR ACTION, not a
code change: set `stale_policy: "quarantine"` in the live `config.yaml`
(the knob is deliberately not console-exposed yet), note the start date in
the ops log, and let normal usage run.

## Soak protocol

- **Duration**: 7 days minimum, spanning at least one dream-heavy workday
  and one idle weekend day (the chronicle soak precedent).
- **What to record, per check (one line each, ~daily)**:
  1. count of stale facts actually SERVED (grep daemon logs is not
     enough — `memory_stats` has no such counter, so the check is a
     `world_dump`/`cortex_dump` census of `stale: true` records plus a
     judgment call on whether any session touched them);
  2. any session transcript where the agent met a quarantined value —
     did it re-verify (the briefing's contract) or stall/misread?
  3. console spot-check: the wrapper renders in place of the value
     (known P2 cost) — does it obscure anything an operator needed?
  4. any `correct_with` follow-through on quarantined facts (the
     desired end state: quarantine → re-verify → fresh assert).

## Decision rule (preregistered)

Flip the default to `"quarantine"` iff, over the soak window:

1. **No stall**: zero observed sessions where a quarantined value left
   the agent unable to proceed when the underlying value was recoverable
   (`last_known_value` served and readable);
2. **No operator harm**: no console incident where the wrapper hid a
   value an operator needed with no workable path
   (`memory_fact_get`/history);
3. **Any-touch evidence**: at least one live serving of a stale record
   during the soak — a week where staleness never fired decides nothing
   (extend one week once; if still zero touches, record the honest
   conclusion that the default flip is a no-op for this bank today and
   ship `"demote"` instead as the cheap always-safe middle, or hold).

Fallback ladder: stall or operator harm attributable to the wrapper →
default flips to `"demote"` (identical measured efficacy, value field
untouched) instead of quarantine; both failing → annotate stands and the
finding routes back to the client-side briefing.

## Consistency obligations at flip time

Same-change docs discipline: README capabilities row if the default is
named there, `docs/guide/configuration.md` built-in-defaults bullet
(rewrite from "today's behavior" framing), CHANGELOG entry citing this
prereg, and the console-exposure decision (registry + gapfill test) made
explicitly — a flipped default without a console control is an operator
trap.

## Cost

Zero GPU. One config line, ~5 minutes/day of observation, one decision.
