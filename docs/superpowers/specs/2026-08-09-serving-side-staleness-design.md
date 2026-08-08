# Serving-side staleness policy — preregistration (2026-08-09)

## Motivation (measured, not assumed)

The retention-interval eval (`evals/results/retention-interval-verdict.json`,
tag `ret-0809`) resolved the question this design depends on. With
`effective_confidence`/`stale` rendered as annotations, the answerer's
unqualified-stale-answer rate halves (1.0 → ~0.5, paired p 0.0002) — the
flags are a real decision input, not decoration. But the residual half is
**mechanistic**: flag compliance is fact-consistent across replicates, and
the non-compliant slots are exactly the value shapes that read as durable
(`deployed version 3.8.1`, `batch-size 500`). The answerer applies its own
judgment about which staleness warnings to heed, and that judgment keys on
value shape — precisely the discretion a serving-side policy removes.

Corollary finding worth carrying: the compact cortex block that
`memory_search` serves renders `age` but not the two flags (the full
payload lives on `memory_fact_get`/world reads) — so today's strongest
staleness signal never reaches the most-used read surface at all.

## Hypothesis

**H3**: a daemon-side stale-serving policy drives the unqualified-stale
rate below the flags-visible floor (≈0.5) without any loss on fresh facts,
because the policy binds regardless of how authoritative the value looks.

## Policy arms (both measured; the winner ships iff gates pass)

- **P1 — demote+warn**: stale records sort after non-stale records in
  every list surface, and carry a top-level
  `"warning": "stale — re-verify before relying on this value"` beside the
  existing flags. Structure of `value` unchanged (zero risk to
  programmatic consumers); tests whether a stronger, top-level textual
  signal plus ordering closes the gap.
- **P2 — value-quarantine**: a stale record's `value` field is replaced by
  `"(stale — re-verify; last known value below)"` and the original moves
  to `last_known_value`, beside `age` and the existing `correct_with`
  affordance. The answerer *cannot* quote the value as current without
  visibly engaging with the quarantine wrapper. Data is moved, never
  hidden.

Config: `memory.search.stale_policy: "annotate"` (today's behavior,
default) | `"demote"` | `"quarantine"`. Applied at the shared record
render sites (`service.py` cortex/world payload builders) so every read
surface — including the compact search block, closing the corollary gap —
behaves identically. No schema change.

## Preregistered gates (Study B machinery, `retention_interval_eval.py`)

New arms alongside the existing three, same 20-fact bank, 3 replicates,
temperature-0 on the reproducible bench server, deterministic classifier:

1. **Efficacy**: winning arm's stale-answer rate ≤ 0.2 (decisively under
   the 0.5 flags-visible floor), paired sign-flip vs flags-visible
   p < 0.05.
2. **No-harm**: fresh-fact answer rate stays 1.0 within 0.02 (the policy
   must not touch non-stale records at all — asserted structurally, not
   just statistically: fresh records' payloads byte-identical to the
   annotate arm).
3. **No data loss (P2 only)**: on a second question set that explicitly
   asks for the last known value ("what was the last recorded X?"), the
   answerer recovers the quarantined value ≥ 0.9 — quarantine must move
   information, not destroy it.
4. **Control**: evergreen-control answer rate unchanged at 1.0.

Fallback ladder: if P2 fails gate 3, P1 alone competes on gate 1; if
neither clears gate 1, the policy ships nowhere, the annotate default
stands, and the honest conclusion is that serving-side rendering cannot
close the answerer-discretion gap either — routing the problem to
client-side briefing (`CLAUDE.memory.md` contract) instead.

## Consistency constraint

`examples/CLAUDE.memory.md` (injected into user CLAUDE.mds, guard-tested
byte-identical to the session-hook block) already teaches "a fact marked
`stale: true` is a lead, not truth — re-verify". P2's wrapper text must
stay consistent with that contract, and the briefing gains one sentence on
`last_known_value` if P2 ships — same-change docs discipline.

## Cost

Implementation: one knob + one render-site change + watched-RED tests
(policy off = byte-identical payloads is the load-bearing test). GPU:
2 policy arms × 3 replicates × ~30 questions ≈ under an hour on the
reproducible server. No overnight window required.

## Risks / honesty

- The eval's synthetic facts are idealized; production hit rates on stale
  facts are unmeasured (same scoping caveat as ret-0809 — the claim is
  "the policy binds when staleness is served", not "staleness is served
  often").
- P2 changes a read-surface contract (`value` no longer always the raw
  value when stale). It ships knob-gated with the default unchanged;
  flipping the default is a separate, later decision with its own soak.
- The 0.2 efficacy bar is deliberately stricter than "beats 0.5" — a
  policy that merely ties the textual flags is not worth a contract
  change.
