# Cortex provenance-aware contenders — design spec

Status: **approved (design)**, pending written-spec review + plan.
Target: **PseudoLife-MCP** `origin/master` (`1c375fd`). Core-only; no redacted deps.
Touches `pseudolife_memory/memory/cortex.py`, `service.py`, `mcp_server.py`,
`utils/config.py`, and tests.

## 1. Problem

The cortex's supersession decision (`CortexStore._should_supersede`) keys only on
timestamp + `supersede_confidence_margin`. The `support`/`origin` provenance tier
(`user` > `action` > `agent`) affects only how a fact *reports* its origin — it does
**not** guard the write. So a newer **agent** assertion silently overwrites an
earlier **user-stated** fact at the same slot (newer-wins, within the confidence
margin). The current value flips with no signal.

The dangerous real case: the agent *decides* to update something, the human only
says "yes"/"proceed", and the agent records the change as `origin="agent"`. The
update may be legitimate — but the system should **notice the discrepancy and let
the agent check in** rather than silently retire a user fact.

## 2. Goal

When a write would *lower the provenance tier* of a slot's canonical value (or
otherwise lose a conflict), **keep the current value canonical** and record the
incoming value as a visible **contender**, so the agent can surface the conflict and
confirm with the human. Nothing conflicting is ever silently dropped.

Non-goals: changing the continuum/bands; LLM dream; multi-slot reasoning.

## 3. The tier-rank rule (write path)

Add an explicit rank over provenance tiers:

```python
_TIER_RANK = {"user": 3, "action": 2, "agent": 1}   # unknown / "" -> 0

def _rank(origin: str | None) -> int:
    return _TIER_RANK.get((origin or "").strip().casefold(), 0)
```

`write_fact` is unchanged for the *insert* and *confirm (same value)* branches. At a
**genuine conflict** (slot exists, value differs), the decision becomes:

```
incoming_rank = _rank(support)            # the single tier on THIS write
current_rank  = _rank(cur.origin)         # strongest tier already backing the slot
tier_ok       = incoming_rank >= current_rank      # (always True if protection off)

if tier_ok and self._should_supersede(cur, confidence, t):
    -> supersede        # unchanged: newer-wins, gated by confidence margin
else:
    reason = "tier_downgrade" if not tier_ok else "below_confidence_margin"
    -> contend          # record/confirm a contender; current value stays
```

Properties:
- **User overrides anything** (rank 3 ≥ everything). **action over agent** supersedes.
  **agent over action / user** → contends. **agent-over-agent** = newer wins (as today).
- **Unknown tier (rank 0)** can *contest* a known-tier fact but never silently
  override it. A known-tier write to a *legacy unknown* fact supersedes normally
  (1 ≥ 0) → **no contender spam on pre-existing facts**.
- **Unification:** the existing same-tier *below-confidence-margin* path (which today
  silently drops the value) now also records a contender. One code path, one rule:
  *any conflict that does not supersede becomes a visible contender.*

## 4. The contender record + lifecycle

A contender is a real `CortexRecord` with **`status="contested"`** (alongside the
existing `current` / `superseded`; add `"retired"` for rejected contenders). It
carries its own `value`, `embedding`, `confidence`, `support`, `provenance`,
`asserted_at`, `last_confirmed`, and `supersedes_value` (the prior contender it
replaced, if any). It is appended to `self.records` but **never registered in
`self._current`**, so it cannot leak into the canonical answer.

Invariants and transitions (new helper `_contend(...)`):
- **At most one active (`contested`) contender per slot.**
  - Incoming value **differs** from the active contender → the old contender is
    demoted (`contested → superseded`, `superseded_by_value = new`) and a new
    contender is appended.
  - Incoming value **matches** the active contender → **confirm** it (`last_confirmed`
    bump, `provenance |=`, `support.add`, bounded `_reinforce` / `max(...)` — same
    rule as the current-record confirm).
- `lookup()` / `search()` / `current_records()` already filter `status=="current"`,
  so they are unaffected. `vocab()` likewise (current only).
- `WriteResult.action` keeps the existing value **`"contested"`** (no new vocab word).
  The semantic shift: it now returns the **contender** record as `result.record`
  (previously it returned the unchanged current record); the current value remains
  reachable via `lookup`. The service layer (§6) adds the current value to the
  response so the caller sees both sides.
- Still appends to `supersession_log` with `decision="contested"` + the new `reason`.

### Load reconciliation
`_reindex_current` is generalised to reconcile **per status**: as it already keeps the
most-recently-confirmed `current` per slot and demotes duplicates, it now also keeps
the most-recently-confirmed **`contested`** per slot and demotes extra contested
records to `superseded`. This self-heals the at-most-one-contender invariant on load.

### Accessors
- `contenders_for(entity, attribute) -> list[CortexRecord]` — active (`contested`)
  records at the slot (0 or 1 under the invariant; list for generality).
- `resolve(entity, attribute, accept, now=None) -> WriteResult | None` — see §5.

## 5. Resolution (`resolve`) — store + tool

`CortexStore.resolve(entity, attribute, accept: bool, now=None)`:
- No active contender → return `None` (service maps to `{"resolved": False,
  "reason": "no_contender"}`).
- `accept=True` → **promote**: demote the current record (`current → superseded`,
  `superseded_by_value = contender.value`, `superseded_at = now`); set contender
  `status="current"`, `support.add("user")` (the human just confirmed),
  `last_confirmed = now`, `supersedes_value = old current value`; point `_current[key]`
  at it; log `decision="resolved"`, `reason="accepted"`. Returns `WriteResult("superseded", contender)`.
- `accept=False` → **reject**: contender `status="retired"`, `superseded_at = now`;
  current untouched; log `decision="resolved"`, `reason="rejected"`. Returns
  `WriteResult("contested", current)`.

## 6. Service layer (`service.py`)

- **`store()` / `_promote_slots()`** — unchanged call path; auto-promoted agent facts
  that conflict with a higher-tier current fact now naturally become contenders (this
  *is* the silent-overwrite case being fixed). `store()` return already carries
  `cortex_promoted`; no shape change needed there.
- **`cortex_write(...)`** — keeps the existing **flat** shape `{"action", ...record}`
  (the record is the contender for a contested write) and *adds* a `"current"` key so
  the agent sees both sides: `{"action": "contested", **_cortex_record_to_dict(contender),
  "current": <lookup dict|null>}`. Insert/confirm/supersede responses are unchanged
  (their `"current"` is simply absent / equals the record).
- **`cortex_contenders(entity, attribute) -> {"entity","attribute","contenders":[...]}`**
  — new thin wrapper over `contenders_for`.
- **`cortex_resolve(entity, attribute, accept) -> dict`** — new; wraps
  `CortexStore.resolve`, persists via `_save_cortex()`, returns
  `{"resolved": bool, "action": str|None, "current": <dict|null>, "retired": <dict|null>}`.
- **`cortex_search(...)`** — each returned current entry gains `"contested": bool` and,
  when true, `"contender_value"` / `"contender_origin"` (looked up via
  `contenders_for`), so the read path can flag discrepancies.
- **Persistence/fingerprint** — `_entry_fingerprint` already folds
  `len(cortex.records)` + `len(supersession_log)` + `sum(last_confirmed)`; contender
  writes/resolves move those, so autosave wakes correctly. No fingerprint change.

## 7. MCP tool surface (`mcp_server.py`)

- **`memory_fact_get`** → `{"record": <current|null>, "contenders": [<contested>...]}`
  (was `{"record": ...}` only). The deterministic check-in point.
- **`memory_search`** (cortex-first block) → the block currently reconstructs each
  fact dict from cherry-picked fields, so it must **forward** the new `"contested"`
  flag (+ `contender_value`/`contender_origin` when true) that `cortex_search` now
  emits, so the agent *notices* a discrepancy during normal recall, not only on an
  explicit lookup.
- **`memory_fact_resolve(entity, attribute, accept: bool = true)`** — **new tool**.
  Docstring steers usage: "After you check in with the human about a conflicting
  canonical fact, call this — `accept=true` to adopt your value (recorded as
  user-confirmed), `accept=false` to keep the existing one." Wraps `cortex_resolve`.
- **`memory_fact_set`** — unchanged surface; still works (`origin="user"` always
  supersedes). Docstring gains one line pointing to `memory_fact_resolve` for the
  "an agent value is parked as a contender" case.
- `test_all_tools_registered` gains `"memory_fact_resolve"`.

## 8. Config (`utils/config.py`)

`CortexConfig` gains `protect_provenance: bool = True`. Threaded into
`CortexStore.__init__(protect_provenance=...)` (constructed in `service._ensure_init`).
When **False**, the conflict branch reverts to pre-change behavior exactly
(`tier_ok=True`; below-margin → log `"contested"` and keep current, **no** contender
record) — a clean kill switch.

## 9. Backward compatibility

- `status` is a free-form string already round-tripped by `save`/`load`; new
  `"contested"`/`"retired"` values need **no schema bump** (stays v7). Pre-change
  saves have no contested records → load unchanged.
- Existing tests: **`test_lower_confidence_candidate_does_not_supersede` passes
  unmodified** — it asserts `action=="contested"`, current value unchanged, and
  `supersession_log >= 1`, all still true. All other `test_cortex*` /
  `test_cortex_promotion` assertions are unaffected.

## 10. Tests (TDD)

`tests/test_cortex.py` (store-level):
1. `test_agent_write_contends_user_fact_not_supersede` — user fact current; later
   agent write at same slot → `action=="contested"`, user value still current,
   one `contested` contender present with the agent value.
2. `test_tier_rank_action_over_agent_supersedes_agent_over_action_contends`.
3. `test_user_write_supersedes_any_tier` (user over action/agent).
4. `test_below_margin_same_tier_now_records_contender` (unification; current unchanged,
   contender present, `action=="contested"`).
5. `test_at_most_one_active_contender_newer_value_supersedes_prior`.
6. `test_contender_confirm_reinforces_same_value`.
7. `test_unknown_tier_contests_known_but_known_supersedes_legacy_unknown`.
8. `test_resolve_accept_promotes_contender_and_marks_user_confirmed`
   (old current → superseded; contender → current; `support` contains `"user"`).
9. `test_resolve_reject_retires_contender_current_unchanged`.
10. `test_contested_and_retired_survive_persistence_roundtrip`.
11. `test_load_reconciles_duplicate_contested_to_one_active`.
12. `test_protect_provenance_false_restores_pure_newer_wins` (no contender; below-margin
    drops as before).

`tests/test_cortex_service.py` / new `test_cortex_contenders.py` (service-level):
13. `test_store_agent_fact_parks_contender_against_user_fact` (via `store`/auto-promote).
14. `test_cortex_write_contested_response_includes_current_and_contender`.
15. `test_cortex_resolve_accept_then_lookup_returns_new_value` (+ persists).
16. `test_cortex_search_flags_contested_entries`.

`tests/test_mcp_server.py`:
17. `memory_fact_resolve` in `test_all_tools_registered`.
18. `test_memory_fact_get_returns_contenders_via_mcp`.
19. `test_memory_fact_resolve_accept_reject_via_mcp_dispatch`.

## 11. Out of scope / honest limits

- The MCP cannot *force* the agent to check in — it can only surface the contender
  (`action=="contested"` at write, `contested:true` in search, `contenders` in
  `fact_get`). Whether the host acts on it depends on the model.
- Origin fidelity still depends on the caller (no conversation feed); most MCP stores
  default to `agent`. Unchanged from the cortex baseline.
- This guards *currency/provenance*, not generation: it stops a silent overwrite, not
  a model misreading a faithfully-surfaced fact.
