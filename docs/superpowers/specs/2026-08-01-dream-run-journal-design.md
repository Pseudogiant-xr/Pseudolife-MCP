# Dream-run audit + pre-image journal + rollback — design

**Date:** 2026-08-01
**Status:** approved approach (schema v27: `dream_runs` + `dream_run_slots`, rollback via the normal write path)
**Follow-up to:** external finding arXiv:2607.27773 (ChronoMem — versioned/revertible agent
memory shipping into Google ADK), plus the standing consolidation-degradation warning
(arXiv:2605.12978)

## Problem

The dream pass is forward-only and anonymous. A dream that writes a bad batch of claims —
extractor regression, poisoned source entries, a prompt change gone wrong — leaves no run
identity, no record of what each slot held before, and no revert path short of a whole-bank
`pg_dump` restore. The supersession chain is not a substitute: `compact_superseded()` runs on
every sweep tick and purges non-live rows (keep-3 per slot past 30 days), so the history a
rollback would need is actively deleted in steady state.

## Design decisions (each is a judgement call — stated so a reader can second-guess them)

1. **Journal, not transaction.** Per-slot autocommit is deliberate (postgres.py's
   `lock_timeout` rationale); a dream pass stays N independent commits. The journal is a
   *forensic pre-image record*: the pre-image is read under a short `self._lock` and released
   before the write, so a concurrent `memory_fact_set` can interleave between capture and
   write. Accepted and documented — this is an audit/undo mechanism, not a serializable
   snapshot.
2. **Rollback keeps traces and does NOT rewind `dream_cursor`.** `has_trace` gates scalar
   claims but never member ops, so rewinding the cursor after a revert would re-add reverted
   members while suppressing reverted scalars — a worse state than either extreme. Traces
   stay: they are honest provenance of an attempt that was made and reverted.
3. **Scope v1: cortex fact writes only** (scalar + member). Relations, lessons, graph edges,
   and outcome signals are future work; each has its own reversal semantics and none is the
   corruption-risk hot path the claim loop is.
4. **The journal survives compaction by construction** — separate tables with their own
   retention (`dream.runs_keep`, newest-N runs, pruned in the sweep beside
   `compact_superseded`), never coupled to the facts-table supersession chain.

## Schema v27 (additive)

- `dream_runs`: one row per dream pass that had claims to write — id, started/finished,
  cursor before/after, pulled/claims counts, `tallies` JSONB (full tally + literal-gate
  counters + quarantined), `status` (`running | committed | failed | rolled_back`),
  extractor, writer_id, rolled_back_at.
- `dream_run_slots`: the pre-image journal — run_id FK **ON DELETE CASCADE**, seq, display
  entity/attribute plus norms, kind (scalar|member), op, prev_kind/prev_value/prev_status/
  prev_confidence/prev_support (`prev_status` NULL = slot or member did not exist),
  new_value, the write's actual returned `action`, src_entry_id (**no FK** — entries are
  evictable, and the `memory_traces` FK is the origin of the reflush-stall class), at.

Run-row lifecycle: a row exists **only when the pass had claims to write** (non-empty
`pairs`) — zero-pull sweeps, extractor failures/outages, and empty extractions leave no row.
This is deliberate: an outage retries every sweep tick, and a row per retry would burn the
newest-N retention window on passes that provably wrote nothing; for the rollback
precondition, *no row* and *wrote nothing* are equivalent. Statuses are therefore
`running | committed | failed | rolled_back` (no `held` status). A claim-write exception —
including the `_dream_reflush_stale` healed path — → **`failed`** (partial writes landed;
this is what makes the rollback precondition decidable). Success → `committed`, stamped
immediately after `dream_commit`, *before* the relations/lessons block (a relations failure
must not mislabel a run whose cortex writes and cursor advance really happened). A process
death leaves `running`; `prune_dream_runs` flips `running` rows older than 24 h to `failed`.

## Rollback contract

`memory_dream(action="rollback")` reverts the **latest committed run only**, and only when
no newer run is `failed` or `running` (either means unjournaled or partial uncertainty —
refuse). Reversal walks the journal in reverse seq through the normal service
write paths (supersede-back preserves audit; nothing is deleted):

| journaled | reversal |
|---|---|
| scalar `inserted` (prev NULL) | `CortexStore.retire_current` (new primitive — `forget` destroys history, `resolve` only touches contenders) |
| scalar `superseded` | `cortex_write(prev_value, prev_confidence, prev_support)`; a `contested` result is settled with `cortex_resolve(accept=True)` — rollback is explicit authority, a low-confidence prev must still win the slot back |
| scalar `confirmed` | skip (only confidence/last_confirmed moved; not expressible through the write path) |
| `contested` | `cortex_resolve(accept=False)` only if the live contender still equals new_value; else skip `superseded_by_later` |
| member `member_added` (prev not scalar) | `set_remove(new_value)` |
| member `member_added` over a converted scalar | `set_remove(new_value)`; if the sole surviving member is the converted scalar, unwind the one-way scalar→set conversion; otherwise leave the set (`partial: set_retained` — other members arrived independently) |
| member `member_removed` | `set_add(prev_value, …)` (fresh row — audit-honest) |
| anything that stored nothing | skip |

Double rollback is refused (status is `rolled_back`). File mode (`storage is None`) returns
`{"error": "requires_postgres"}`.

## Point-in-time reads

`memory_history` gains `as_of` (ISO or epoch): filter the slot's version chain by
`tx_time <= as_of`. Honest limitation, documented at the tool: compaction keeps only the
newest `keep_per_slot` (3) non-live versions past `min_age_days` (30), so an `as_of` older
than that window may return an incomplete chain. Whole-bank as-of reconstruction is out of
scope.

## Non-goals

- No whole-memory snapshot per write (ChronoMem's design) — the pre-image journal captures
  exactly the delta at a fraction of the cost, and `ops/backup.ps1` remains the whole-bank
  recovery path.
- No rollback of arbitrary historical runs (conflict resolution across interleaved writes is
  not worth v1 complexity; latest-committed-only is decidable).
- No REST surface in v1.
