# Superseded-row compaction — design

**Date:** 2026-07-14
**Status:** approved for implementation
**Origin:** 2026-07-14 disk/release audit — corrections mark rows
`status='superseded'` in the canonical stores and nothing ever compacts them.
Rated low-urgency (small rows) but real: growth is unbounded over months of
correction-heavy use.

## 1. Problem

Every correction in the three canonical stores keeps the old version forever:

- `facts` — `CortexStore.write_fact` supersession, contender flows, dedupe,
  load-time healing all set `status='superseded'` (or `'retired'`) and append.
- `world_facts` — `WorldCortexStore.write_fact` newer-source-wins supersession.
- `lessons` — `LessonStore` supersession (same pattern).

The cost is **not primarily PG disk**. All three stores are hydrated wholesale
into RAM at startup (`hydrate_cortex` / `hydrate_world_cortex` /
`hydrate_lessons` load *every* row, superseded included), and each record
carries a 384-dim float32 embedding tensor (~1.5 KB) plus, for facts, an
optional slot embedding. Several paths scan the full record list linearly
(`records_for`, `_active_contender`, `_reindex_current`, `forget`,
`_cortex_change_index`). So unbounded superseded rows mean unbounded RAM,
slower scans, slower hydration, and larger per-slot syncs — on top of the PG
rows.

A fourth, smaller leak in the same class: `CortexStore.supersession_log` grows
without bound **in process memory** for the daemon's whole uptime (persistence
already caps it at the last 200 via `sync_cortex_slots`; the in-RAM list is
never trimmed).

## 2. Reader enumeration (the derived-state discipline: who consumes superseded rows?)

Per project CLAUDE.md, enumerated by grepping for the *state* (`superseded`,
`status`, `records`) rather than the API. Verified against source on this
branch (schema v22).

### facts (`CortexStore.records`, statuses `superseded` / `retired`)

| Reader | Where | What it needs |
|---|---|---|
| `service.history(entity, attribute)` | service.py `history()` → MCP `memory_history(entity, attribute)`, REST `/api/facts/history` | The per-slot version timeline. Reads ALL records at one slot via `records_for`. **Load-bearing user feature.** |
| `service.chain(entity)` | service.py `chain()` → MCP `memory_history(entity)` (no attribute), REST `/api/chain` | Iterates ALL records; emits a `fact_set` event per record and a `superseded` event per superseded record. |
| `_cortex_change_index()` | service.py — feeds lesson `re_verify` staleness | Only the **latest** change timestamp per entity: `max(asserted_at, superseded_at)`. On a supersession the successor's `asserted_at` equals the loser's `superseded_at`, so purging old superseded rows preserves the max. The one exception is a `retired` record (rejected contender: gets `superseded_at`, no successor) — covered by the min-age guard below. |
| `stats()` / `cortex_size()` / console Observatory | counts only | Counts change after compaction; that is the point. Not load-bearing. |
| `_reindex_current()`, `resolve()`, `_active_contender` | cortex.py | Touch only `current` / `contested` records. Never purge those statuses. |
| `supersedes_value` / `superseded_by_value` chain | display strings on the *successor* record | Denormalized text; survives purging the predecessor row. |
| memory_traces (engram cross-index) | schema v13 | Keyed on the **slot** `(entity_norm, attribute_norm)`, not `facts.id` (which is ephemeral by design). Purging superseded rows at a slot that still has a current record leaves traces fully functional. |
| dream (vocab, slot resolution, known-facts window) | cortex.py | `status == "current"` only. Unaffected. |
| backup / additive restore | ops | Fewer rows restore fine; the v19 one-live-row heal is orthogonal. |

### world_facts (`WorldCortexStore.records`)

No history/chain endpoint exists for world facts. Readers of superseded world
records: **`stats()` counts only.** Search/lookup filter to `current`.
Superseded world rows are pure audit weight today.

### lessons (`LessonStore.records`)

Same: no history endpoint; `lesson_search` / `lessons_dump` read
`current_records()` only; `_annotate_lesson_staleness` reads the *facts* churn
index, not superseded lessons. **`stats()` counts only.**

### entries (`entries` table + in-memory bands) — **no new mechanism needed**

Entries are already bounded and their superseded rows are load-bearing:

- Bands have hard capacities (defaults 2000/5000/10000) with
  reinforcement-weighted eviction, and eviction write-through deletes the PG
  row (`_on_band_evict` → `delete_entry_ids`). `memory_traces.entry_id` has
  `ON DELETE CASCADE`, so trace cleanup is automatic. Growth is bounded by
  band capacity, not by correction rate.
- Superseded entries are deliberately **included** in retrieval (v0.7.3) at
  `SUPERSEDED_SCORE_MULT = 0.55` with `superseded_by_text` rendered inline —
  this is what answers knowledge-update questions ("X changed from A to B")
  and is validated by the LongMemEval KU results. Purging them early would
  regress a measured capability.

**Decision: entries are out of scope.** The existing eviction is the retention
mechanism.

### edges — **explicitly out of scope (load-bearing tombstones)**

A superseded edge is a *sticky removal*: the dream re-asserts relations with
`revive=False`, which relies on the superseded row existing to refuse
resurrection ("a dream re-assertion must not resurrect an edge a human
superseded"). Purging superseded edges would silently re-enable dream
re-creation of human-removed edges. Any future edge compaction needs a
separate tombstone design; do not fold it in here.

## 3. Mutation-path enumeration (who creates/removes superseded rows?)

In-memory (all propagate to PG via `dirty_slots` → `replace_slot_*`, which
deletes every row at a dirty slot and reinserts the in-memory survivors —
i.e. PG converges to memory per slot):

- `CortexStore.write_fact` (supersede), `_contend` (contender superseded by a
  newer contender), `resolve` (accept → old current superseded; reject →
  contender `retired` with `superseded_at` set), `dedup_siblings`,
  `_reindex_current` load-time healing, `forget` (hard delete).
- `WorldCortexStore.write_fact` (supersede), `forget`.
- `LessonStore.write` (supersede), `forget`.

Direct-PG paths that bypass the in-memory stores:

- `schema.ensure_schema` v19 heal (`UPDATE … SET status='superseded'` for
  duplicate live rows) — runs **before** hydration on daemon start, so memory
  loads the healed rows; convergence holds.
- Additive restore (`ops/restore`) inserts rows while the daemon is down;
  next start hydrates them. Convergence holds.

Consequence: compaction can operate **purely on the in-memory stores** and
mark purged slots dirty; the existing per-slot sync deletes the PG rows. No
new SQL, no DDL, no schema bump.

## 4. Retention semantics — decision

**Keep-newest-N per slot, with a minimum-age guard; hard delete the rest.**
Uniform policy across facts / world_facts / lessons.

For each slot, pool the non-live records (`status` in `superseded`,
`retired`); sort newest-first by `(superseded_at, asserted_at, insertion
index)` (the explicit ordinal makes ties deterministic — the slot-index
lesson); keep the first `keep_per_slot`; purge the remainder **only if**
`superseded_at < now − min_age_days`. `current` and `contested` records are
never touched. A legacy record with `superseded_at IS NULL` sorts oldest
(treated as 0.0) and is purge-eligible.

Defaults: `keep_per_slot = 3`, `min_age_days = 30`, `enabled = true`.

Growth bound: O(slots × N) + a 30-day churn window. `memory_history`
timelines keep their three most recent prior versions forever and their
*complete* history for 30 days — which covers the actual use ("what did this
just change from, and who did it") while bounding the tail. The min-age guard
also preserves `_cortex_change_index` for recent `retired` records (the only
reader whose signal lives solely on a non-live row).

### Alternatives considered

1. **Age-based archive table** (`facts_archive` etc., move instead of
   delete). Rejected: `history`/`chain` read from the hydrated in-memory
   stores, never from PG — archived rows would vanish from every existing
   reader anyway, making the archive a write-only grave that itself grows
   forever, at the price of a schema bump (the four-place ritual) and new
   sync code. If forensic depth is ever needed, the daily + pre-deploy
   backups already retain point-in-time snapshots.
2. **Pure age-based purge** (delete all superseded older than X). Rejected:
   wipes the timeline of stable, rarely-corrected facts wholesale; keep-N
   preserves the most-read part of every slot's history at negligible cost.
3. **Keep-N without min-age.** Rejected: a burst of corrections to one slot
   (the common failure mode being debugged *right now*) would immediately
   destroy the history you're about to ask for.

## 5. Design

### 5.1 Core: `pseudolife_memory/memory/compaction.py` (new, ~60 lines)

```python
def compact_store(store, *, keep_per_slot: int, min_age_days: float,
                  now: float | None = None) -> int
```

Duck-typed over the three stores (`records` list, `_current` dict,
`dirty_slots` set):

1. Group non-live records by `key`; select purge victims per §4.
2. Rebuild `store.records` without victims.
3. Rebuild the current index: call `store._reindex_current()` when present
   (CortexStore — keeps its healing semantics), else rebuild the
   `{key: idx}` comprehension used by the world/lesson hydrators.
4. Add every purged slot to `store.dirty_slots` (the invalidation hook that
   propagates deletion to PG).
5. Return the number of purged records.

`keep_per_slot <= 0` is clamped to 0 (age-only purge is then the behavior —
still guarded by min-age); `min_age_days < 0` treated as 0.

### 5.2 Service: `MemoryService.compact_superseded()`

Under `self._lock`: run `compact_store` over `self._cortex`, `self._world`,
`self._lessons` with the config knobs; when anything was purged, call the
matching `_save_cortex` / `_save_world` / `_save_lessons` (per-slot sync
deletes the PG rows); return
`{"facts": n, "world_facts": n, "lessons": n, "total": n}`. Returns zeros
(and skips saves) when `enabled` is false or nothing qualifies.

Also in this change: `CortexStore._log` trims the in-memory
`supersession_log` in place to its persisted cap (200) — same growth class,
one line, keeps RAM flat over months of daemon uptime.

### 5.3 Trigger: the dream sweep

`run_sweep_once` (memory/dream.py) calls `service.compact_superseded()` once
per tick, **before** the backlog gate — compaction must run even when no dream
fires. Cost per tick is one linear pass over the in-memory records (a few
thousand dataclasses; sub-millisecond) and most ticks purge nothing thanks to
min-age, so no extra throttle state is needed. Log at INFO when
`total > 0`, include the counts in the sweep result.

Known limitation (accepted): with `memory.dream.enabled = false` there is no
sweep thread, so no automatic compaction — the same trade the outcome-signal
pruning already makes (`prune_signals` runs inside the dream). Manual path:
`service.compact_superseded()` is a public service method; the console/REST
can grow a button later if wanted (YAGNI now).

### 5.4 Config

New block on `MemoryConfig` (utils/config.py), mirroring the existing nested
dataclass pattern:

```python
@dataclass
class CompactionConfig:
    enabled: bool = True
    keep_per_slot: int = 3
    min_age_days: float = 30.0
```

parsed from `memory.compaction.*`, plus three entries in the console settings
registry (web/config_io.py, group "Retention") so the knobs are visible where
`show_superseded` already lives.

### 5.5 Steady-state interleave check (discipline #2)

The daemon's steady state is store/search alternation; compaction runs on the
sweep timer, holds the service lock once, mutates the three cortex stores
only (never bands), and marks only genuinely-purged slots dirty — so the next
autosave syncs exactly those slots and retrieval-path caches (slot-token
index, band pattern matrices) are untouched because `band.entries` never
changes. Nothing rebuilds per-read; the win survives the real workload.

### 5.6 What the replaced code provided implicitly (discipline #3)

- **Iteration order**: `records` list order feeds `records_for` (history) and
  `chain`. Compaction preserves relative order of survivors (single filtered
  pass, no re-sort). `history()` re-sorts by tx_time anyway.
- **Index validity**: `_current` maps slot → list index; any removal
  invalidates it. Rebuilt unconditionally after a purge (the `forget`
  precedent).
- **Containment**: per-slot PG sync (`replace_slot_*`) treats memory as
  source of truth for a dirty slot — compaction relies on exactly that
  contract; no separate DELETE path is added.
- **Live-object reads**: `superseded_by_text`/`supersedes_value` on
  *surviving* records are untouched; retrieval-time supersession flags live
  on entries (out of scope).

## 6. Testing (TDD, watched RED first)

New `tests/test_compaction.py`:

1. **Unit — policy** (pure in-memory, per store type): never touches
   `current`/`contested`; keeps newest N non-live per slot; min-age guard
   holds a 4th recent version; `retired` records pooled with `superseded`;
   legacy `superseded_at=None` purged first; deterministic under ties
   (insertion ordinal); returns purge count; `keep_per_slot=0` + age-only.
2. **Unit — invariants**: `_current` still resolves every slot's current
   record after compaction (lookup round-trip); purged slots ⊆ `dirty_slots`
   (spot-check the hook is load-bearing: disable the `dirty_slots.add` and
   watch the PG test go red, per the review discipline).
3. **Service — config + persistence** (PG-backed, 127.0.0.1:5433): superseded
   rows beyond N and older than min-age disappear from the `facts` /
   `world_facts` / `lessons` tables after `compact_superseded()`; current
   rows and the newest-N survive; disabled config is a no-op returning zeros.
4. **Reader preservation**: `history()` still returns current + newest-N
   versions after compaction; `_cortex_change_index` unchanged for a
   supersede chain compacted to N=1.
5. **Sweep integration**: `run_sweep_once` invokes compaction even when the
   dream gate doesn't fire (below threshold).
6. **supersession_log cap**: 250 logged supersessions leave ≤ 200 in memory.

## 7. Shipping checklist mapping

- CHANGELOG `[Unreleased]` entry: behavior change (canonical-store audit
  history is now bounded: newest 3 per slot + 30-day full window; config
  `memory.compaction.*`).
- **No schema bump** — no DDL; the four-place ritual does not apply.
- Full suite with bench PG up; verify the PG-backed compaction tests ran.
- Deploy via `ops/update.ps1`; post-deploy live check: psql count of
  superseded facts rows before/after a forced sweep tick, and a
  `memory_history` call on a corrected slot still showing its recent
  versions.
