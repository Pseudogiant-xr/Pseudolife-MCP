# Aggregate conversion guard — design

**Date:** 2026-07-31
**Status:** approved approach (Approach 1: number-led value detection, park as contender)
**Follow-up to:** `evals/results/c2op-gate-verdict.json` (definitive C2-op gate, PR #75)

## Problem

The definitive C2-op gate measured why the extractor op prompt block nets
negative on LongMemEval-KU despite improving scalar extraction on non-set
questions: the model applies `op:"add"` to aggregate/count slots, and
`CortexStore.add_member`'s unconditional one-way scalar→set conversion
destroys the stated-total scalar those questions are answered with
(`"total species: 32"` → a set of enumerated members that never sums back).
One spurious member-add is enough; there is no path back to scalar.

The gate's mechanism split: cortex on the 33 set-forming questions went
2 wins / 13 losses; on the 45 non-set questions 9 wins / 3 losses. The
damage is concentrated exactly at conversions over aggregate-valued
scalars.

## Design

### Detection

A module-level helper in `pseudolife_memory/memory/cortex.py`:

```python
_AGGREGATE_VALUE_RE = re.compile(r"^[$€£]?[+-]?\d")

def _is_aggregate_value(value: str) -> bool:
    """True when a scalar value reads as a number-led quantity — the class
    the C2-op gate measured being destroyed by scalar→set conversion."""
    return bool(_AGGREGATE_VALUE_RE.match((value or "").strip()))
```

Matches: `"32"`, `"27 species"`, `"$1,500"`, `"+3"`, `"-5"`, `"3.5 kg"`,
`"3rd place"` (accepted false positive — conservative is the point).
Does not match: `"gravel bike"`, `"Rosa's Diner"`, `"prod-eu"`, `""`.

The check applies to the **current scalar's value** at the slot being
converted, not to the incoming member value.

### Behavior change in `add_member`

At the conversion moment ([cortex.py:534-552](../../pseudolife_memory/memory/cortex.py)),
where a current scalar occupies the slot:

- If `_is_aggregate_value(cur.value)` is **false**: convert exactly as
  today. No behavior change.
- If **true**: do **not** convert. Route the incoming member value through
  the existing contender machinery and return its result:

```python
if idx is not None:
    cur = self.records[idx]
    if _is_aggregate_value(cur.value):
        return self._contend(cur, slot, emb, confidence,
                             {p for p in provenance if p}, t, support,
                             "member_add_blocked_aggregate",
                             cur.slot_embedding,
                             writer_id=writer_id, session_id=session_id)
    # ... existing conversion path unchanged ...
```

Consequences, all inherited from tested machinery:

- The scalar stays current and canonical; `slot_kind` still reports
  `"scalar"`; no set forms; serving is unaffected.
- The blocked value is auditable (`status="contested"`, audit-log reason
  `"member_add_blocked_aggregate"`) and recoverable: `resolve(accept=True)`
  consciously promotes it to the current **scalar** — an explicit human
  overwrite of the total, documented in the `add_member` docstring.
- At-most-one contender per slot: a repeat of the same blocked value
  confirms/reinforces the contender; a different blocked value supersedes
  the prior contender (last-writer-visible, earlier ones remain in the
  audit trail as superseded).
- Callers need no changes: the dream apply loop and `memory_set_add` both
  receive the established `"contested"` `WriteResult` status. Member ops
  write no traces, so the trace guard is untouched.

Ordering within `add_member`: the empty-value rejection
(`"member_invalid"`) stays first; the guard runs at the conversion branch;
dedup/cap logic is unreachable in the guarded case (no set exists).

### What the guard does NOT touch

- Slots that are already sets: adds proceed as today (there is no
  aggregate scalar left to protect).
- `remove_member`, `write_fact`, `resolve`, healing, dedup: unchanged.
  `resolve` on a guarded slot is the normal scalar-contender path; its
  existing refusal to promote into set slots is unaffected.
- Schema: **no bump.** Contested records already persist through the
  existing columns; the contender minted by `_contend` carries the default
  `kind="scalar"`, which is exactly right — if promoted, it is a scalar.

### Known v1 limitation (stated, accepted)

On membership-enumerating content where a count was stated first
("I own 3 bikes" → "picked up a gravel bike"), the guard suppresses set
formation; the latest blocked add sits as a contender. For KU-style
stated-total content this is precisely the protective behavior; for
enumerating content (LME-V2 trajectories) it is a measured trade-off to
revisit only if that content class is ever gated.

## Testing (TDD, watched RED each)

New cases in `tests/test_cortex_sets.py`:

1. Numeric scalar (`"27"`) + `add_member` → result status `"contested"`,
   scalar still current, `slot_kind == "scalar"`, `members()` empty,
   contender holds the incoming member value.
2. Number-led scalar with unit (`"27 species"`) and currency (`"$1,500"`)
   → same.
3. Non-numeric scalar (`"road bike"`) + `add_member` → converts exactly as
   before (existing conversion tests stay green).
4. Second differing blocked add supersedes the first contender
   (at-most-one invariant holds).
5. Repeat of the same blocked value → `"contested"` with confirmation
   (confidence reinforced, no second contender).
6. `resolve(accept=True)` on a guarded slot promotes the member value to
   current scalar; `resolve(accept=False)` retires it and the aggregate
   scalar survives.
7. Empty member value on a numeric-scalar slot → still `"member_invalid"`
   (rejection ordering pinned).
8. `_is_aggregate_value` unit table: the match/non-match lists above.

Plus a PG round-trip check: a `"member_add_blocked_aggregate"` contender
survives save/load with status and value intact (existing persistence
tests extended, no new columns).

## Measurement (pre-registered before any GPU run)

**Prompt artifact:** commit `evals/prompts/ku_op_prompt_v0.txt`, generated
as `evals/op_probe.py`'s `VARIANTS["v0-appended-block"]` string — byte-wise
the construction the definitive gate ran (shipped `_SYSTEM_PROMPT` with
the v0 block inserted before the `Return {"claims":[]}` line; block text
per commit `3d1333ff^`). A unit test pins the committed file equal to the
programmatic construction so the two can never drift.

**Run:** reproducible q8_0 server via `Start-Qwen` (server identity
verified by process command line), then

```
longmemeval_bench.py --dataset oracle --extractor qwen-27b \
    --tag c2op-guard --system-prompt-file evals/prompts/ku_op_prompt_v0.txt
```

Extraction is deterministic on this stack (variance baseline, 78/78
per-question identical), so the claim stream is identical to the c2op-e2e
run; every delta vs `c2op-e2e` is the guard's apply-time effect alone, and
every delta vs the committed ceiling-e2e control is block+guard combined.
No ladder or window-echo re-run: the extractor and prompt are byte-identical
to the pair that already passed both.

**Pre-registered readouts** (paired permutation, 10k draws, seed 0):

- cascade(guard) vs control (0.936), cascade(guard) vs c2op (0.795)
- count-class cortex correct (control 35/48, c2op 29/48 — expect recovery)
- member_facts / banks_with_sets (expect far below 118/33)

**Pre-registered decision rules:**

- cascade significantly **below** control (p < 0.05) → block stays held;
  guard still ships (it protects live `memory_set_add` use regardless).
- **Parity** with control → block stays held (no measured win on KU);
  guard ships.
- Significantly **above** control → propose shipping block + guard
  together (maintainer decision, as ever).

The prompt rule follow-up ("op never applies to counts/totals") stays in
reserve as a second single-variable arm only if this one fails.

## Shipping checklist deltas

- CHANGELOG entry under `[Unreleased]` (behavior change in `add_member`).
- `docs/guide/memory-model.md`: one paragraph on the guard in the sets
  section.
- Any published number from the gate run gets its evidence row in
  `tests/test_eval_evidence.py` in the same change.
- No schema bump, no version-pin test changes.
