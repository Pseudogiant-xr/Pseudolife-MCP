# Supersede-at-discovery affordances — design

Date: 2026-07-29
Status: approved for implementation (autonomous session; user review at PR)

## Problem

A session recalled the world fact `MCP spec 2026-07-28 /
sessionless_identity_implications`, discovered from the code that it described
work as pending which had shipped 11 days earlier (commit fdaee51e), reported
the contradiction to the user in prose — and did not correct the record. The
user had to ask "did you supersede the entry?" (outcome signal 327,
2026-07-29). The SessionStart briefing's TRUST ORDER section already
instructs "correct the memory rather than silently picking one"; instruction
text at session start demonstrably does not produce the behavior at
discovery time, often thousands of tokens later.

Every stale fact recalled and not corrected is re-believed by the next
session, so reliability here compounds — in both directions.

## Why the failure happens

At the moment of discovery the agent must (a) remember a procedural norm
stated once at session start, (b) reconstruct which tool corrects which
store (`memory_fact_set` vs `memory_world_set`), and (c) re-derive the exact
slot key. Each step is a drop-off point. The fix is to move the affordance
to the moment and place of recall: the tool response itself names the exact
correction call for the exact slot, so acting costs a copy-paste, not a
recalled procedure.

## Candidate mechanisms evaluated

### 1. Tool-response affordances (chosen, primary)

When `memory_search` (cortex block), `memory_fact_get`, or
`memory_world_search` return a fact that is **contested**, **stale**, or
**aged**, attach:

```json
"correct_with": "memory_world_set(entity='MCP spec 2026-07-28',
    attribute='sessionless_identity_implications',
    value=<the verified current value>, source_url=<citation>)"
```

(cortex facts get the `memory_fact_set(...)` form, without `source_url`).

**Aged** means: the fact's freshness class has a TTL and its age (anchored on
`last_confirmed`, falling back to `retrieved_at`/`asserted_at`) exceeds
**TTL/3** — volatile → 7 days, slow → 90 days, evergreen → never. The
fraction is chosen so the incident fact (volatile, 11 days old, **not** yet
`stale` — staleness fires at 2×TTL = 42 days) would have carried the
affordance. Gating on the existing `stale` flag alone would have missed it;
that is the one place this design deliberately goes beyond the v23 fields.
Evergreen facts never nudge: a birthday does not need a weekly "still
true?" tag, and attaching templates to every durable fact would be pure
token noise.

When at least one fact in a response carries `correct_with`, the response
also carries a single top-level `correction_note` stating the norm at the
moment it applies: if observed reality contradicts a flagged fact, execute
the correction call now (or re-assert the same value to confirm it);
noticing without writing leaves the error for the next session.

Properties: deterministic, read-only, no schema change, token cost only on
aged/contested/stale facts, and testable at the tool layer.

Out of scope on this surface: `memory_recall`'s compact projection drops
timestamps entirely (adding currency there is a separate piece of work);
band *entries* (`memory_supersede` territory — the incident class is facts);
the wiki/console human surfaces.

### 2. Session-end reflection (evaluated, deferred)

Idea: at episode close (or dream), detect "fact was recalled this session
AND related store/outcome entries contradict it" and surface a review
finding, like the graph review queue.

Deferred for three reasons:

- **The observed failure leaves no trace to detect.** The agent narrated
  the contradiction in prose and stored nothing; contradiction detection
  over bank contents cannot see a contradiction that never entered the
  bank. The detectable set is precisely the sessions that already
  half-behaved.
- **The deterministic variant needs persistence.** "Recalled while stale,
  never re-verified" requires a recall log that survives daemon restarts —
  a schema bump with its four-place ripple (project conventions) — which is
  disproportionate while mechanism 1's effect is unmeasured.
- **The dream already provides a partial net** for sessions that stored
  evidence: extraction writes facts and same-slot writes supersede, so a
  stored "X shipped" claim can displace a stale "X pending" fact when the
  extractor lands on the same slot key (not guaranteed — slot-key drift is
  a known limitation, see the 2026-07-26 v1/v2 incident).

Follow-up trigger: if supersede-at-discovery failures recur after this
ships, revisit with a persistent per-session recall log surfaced as a
briefing review queue.

### 3. Briefing sharpening (chosen, paired with 1)

Wording alone is known-insufficient, but the briefing is where the norm is
taught and the affordance is where it is applied — they should reference
each other. TRUST ORDER gains ~4 lines: recall results mark aged/contested
facts with a ready-made `correct_with` call; run it the moment the mismatch
is noticed; correcting is part of discovering, not a follow-up. Both halves
of the byte-pin (`session_hook.py` MEMORY_LOOP_BLOCK and
`examples/CLAUDE.memory.md`) change identically in one commit. Budget:
block is 4186 chars of a 9500 shared cap; the addition is ~370 chars.

## Implementation

- `pseudolife_memory/memory/freshness.py`: `needs_correction_nudge(
  freshness_class, anchor_ts, now=None) -> bool` — TTL/3 gate, pure stdlib,
  single source of truth for the threshold.
- `pseudolife_memory/mcp_server.py`: module constant `CORRECTION_NOTE`;
  per-fact template builders; wiring into the three tool responses
  (`_compact_world` keep-list gains `correct_with`). Deliberately **no**
  docstring mentions: the core tool-manifest budget sits at 9480 of 9500
  chars (`test_tool_consolidation.py::test_descriptions_fit_tier_budgets`),
  and the field is self-teaching in-band — `correction_note` states the
  norm in the same response, which is also cheaper than eager docstring
  context on every session.
- `pseudolife_memory/web/session_hook.py` + `examples/CLAUDE.memory.md`:
  the TRUST ORDER addition, byte-identical.
- Tests: `tests/test_freshness.py` (gate unit tests),
  `tests/test_correction_affordance.py` (tool-layer, monkeypatched service,
  in the style of `test_cortex_fact_currency.py`);
  `tests/test_plugin_packaging.py` continues to pin the briefing halves.
- CHANGELOG under `[Unreleased]`.

No service or storage changes; no schema bump.

## Success criterion

A session that recalls the incident-shaped fact (volatile, 11 days old,
uncontested, not yet stale) receives, in the same tool response, the exact
`memory_world_set` call that corrects it, plus the norm stating it must be
executed at discovery. Behavioral reliability is then observed via outcome
signals; recurrence triggers the mechanism-2 follow-up.
