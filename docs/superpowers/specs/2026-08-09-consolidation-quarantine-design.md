# Consolidation quarantine for low-trust writes — preregistration (2026-08-09)

## Threat, precisely

The dream pass is the bank's poisoning amplifier: an episodic entry that
survives to consolidation becomes a canonical cortex fact with elevated
recall authority. The 2026 literature closes the argument that content
inspection cannot defend this path — MAFIA (arXiv:2608.03844) evades
semantic audits from the query interface alone ("factual cloaks", ~90%
attack success, detection suppressed to <8%), and this project's literal
gate checks fidelity-to-source, not trustworthiness-of-source, so it sits
in the same evaded class. What MAFIA does *not* evade are defenses keyed
on **who wrote**: provenance tiers, writer keying, and source tags — all
of which already exist here but none of which gates the *consolidation*
step. Today every non-`status` entry is dream-eligible regardless of
origin, and a fresh claim takes `current` subject only to slot-level
conflict policy.

## Design: two-man rule for low-trust claims

`memory.dream.quarantine_low_trust: bool = false` (ships off; flipping
the default is a separate decision after soak).

When on, a dream claim is **low-trust** iff every source entry backing it
(engram links) has `origin="agent"` AND a `source` outside
`memory.dream.trusted_sources` (default: empty set — operators opt
sources in). Low-trust claims never take `current` directly: they land
via the existing **contender-parking machinery** (the same path
`protect_provenance` uses), visible in `memory_fact_get` as contested,
promotable by exactly two routes:

1. explicit `memory_fact_resolve(accept=true)` — a deliberate act, and
2. independent confirmation — a later claim for the same slot+value from
   a different writer or a non-agent origin promotes it (support
   breadth, not repetition: the same writer restating does not count).

Nothing is dropped, hidden, or deleted: quarantined claims are stored,
searchable, auditable via engram links, and journaled in the dream run
like any other write (rollback covers them).

Deliberately NOT in scope: content scoring of any kind (the evaded
class), retroactive re-classification of existing facts, and any change
to the direct `memory_fact_set` path (a tool call by the user's own
agent under explicit slot semantics — that boundary is the model's, per
SECURITY.md).

## Preregistered gates

1. **Poisoning smoke (deterministic, CPU, the load-bearing test):** seed
   a hostile agent-origin entry asserting a false value for an existing
   user-origin fact; run a dream; assert the claim parks as contender —
   `current` unchanged — and that a follow-up from a second writer
   promotes it while a restatement from the same writer does not.
   Watched-RED as a test in `tests/test_dream_quarantine.py`.
2. **Common-path non-inferiority (ladder, sidecar rung):**
   `gold_recoverable` and `stale_leak` unchanged vs quarantine-off on the
   standard bank when `trusted_sources` includes the bench source — the
   normal single-user path must be byte-identical. Also run with
   `trusted_sources` EMPTY and report the parked count honestly: this is
   the cost profile of the paranoid configuration, not a gate.
3. **Live-bank replay (offline audit, no GPU):** replay the retained
   dream-run journals against the rule and report how many of the last
   50 runs' claims *would* have parked — the production friction
   estimate that decides whether default-on is ever proposed.

## Cost

Config knob + eligibility check at the claim loop + tests: small. Ladder
run for gate 2; everything else CPU. No new schema (contender machinery
and journals already exist — schema v27/v28 cover audit).

## Risks / honesty

- **This does not stop a poisoned entry from being stored or retrieved**
  — episodic search still surfaces it. The claim is narrower: poison
  does not silently gain *canonical* authority. SECURITY.md language
  must keep that distinction.
- **Friction risk is real**: an agent-origin-only workflow with no
  trusted sources parks every new fact. That is why the default is off,
  `trusted_sources` exists, and gate 3 measures the real rate before
  any default-on conversation.
- **Writer identity is spoofable by the writing client** (writer_id is
  self-reported over MCP). The rule raises the bar from "one convinced
  model call" to "two independent-looking writes or one human act"; it
  is a mitigation, not an authentication scheme. Cryptographic writer
  auth (cf. MutMem, arXiv:2608.02843) is out of scope and noted as the
  eventual stronger form.
