# MTT retention — Phase 2: reinforcement-weighted eviction (design)

**Date:** 2026-06-25
**Branch:** `feat/mtt-retention` (off `master`)
**Status:** design approved, pending implementation plan
**Provenance:** Phase 2 of provenance-as-link. Phase 1 (live, schema v13) added the
`entries.reinforcements` counter and writes it — encoding (+1 per new fact↔episode
trace) and explicit `memory_reinforce` (+1). **Nothing reads it yet.** This phase
makes band eviction read it, so a reinforced episode resists forgetting in
proportion to its strength — Multiple-Trace Theory (MTT), made operational as a
single additive term in the eviction score. See
[provenance-as-link design](2026-06-24-provenance-as-link-design.md) §3.

## The idea, in one line
A referenced/used episode should fade more slowly than an unused one — so add a
graded, diminishing-returns boost to its eviction-retention score, tuned by one
knob (`retention_boost`) defaulting to **0.0** (today's behaviour exactly).

## Architecture

### 1. The eviction boost
Band eviction (`MIRASBand._evict_one`, band.py) drops the entry with the lowest
`RetentionPolicy.source_weighted_score(entry, now)`. Today that is:

```python
return (base + 1.0) * weight           # base = eviction_score; weight = source_weights[source]
```

Add one term (protocols.py `source_weighted_score`):

```python
return (base + 1.0) * weight + retention_boost * math.log1p(entry.reinforcements)
```

- **Additive *after* the source-weight multiply** — an absolute, source-independent
  boost. A reinforced `llm_thinking` episode and a reinforced `user_msg` get the
  same absolute protection per unit of reinforcement; source weighting still
  governs the baseline. (Chosen over multiplying into `base`, which would scale the
  boost by source weight and double-count the source signal.)
- **`log1p` = diminishing returns** — graded, not unbounded; no single episode
  becomes immortal in a small band.
- **Relative, so no deadlock** — the band still evicts its weakest entry; the boost
  only changes *which* entry is weakest. A reinforced episode can still fade under
  enough pressure (MTT, not a hard pin).
- **`retention_boost = 0.0` (default) → `log1p` term vanishes → eviction is
  byte-identical to today.** `log1p(0) == 0`, so unreinforced entries are never
  affected regardless of `retention_boost`.

### 2. `reinforcements` lifecycle (DB-authoritative, no clobber)
`reinforcements` already lives on `entries` (schema v13) and is written **only** by
the race-safe `PostgresStorage.bump_reinforcements` (`UPDATE … = reinforcements +
delta`). Phase 2 makes the in-memory band aware of it for eviction scoring, while
keeping the DB the single source of truth:

- **Field:** add `reinforcements: int = 0` to `MemoryEntry` (titans_memory.py).
- **Load:** `load_entries` (postgres.py) currently selects `("id",) + _ENTRY_COLS`,
  and `_ENTRY_COLS` is **shared with `insert_entry`** and deliberately excludes
  `reinforcements`. So add `reinforcements` to `load_entries`' SELECT/row-dict
  **only** (e.g. `cols = ("id",) + _ENTRY_COLS + ("reinforcements",)`), NOT to
  `_ENTRY_COLS`. `row_to_entry` (sync.py) sets `reinforcements=row.get(
  "reinforcements", 0)`. New inserts keep using `_ENTRY_COLS` → the DB default
  (`0`) applies; `reinforcements` is never part of any insert/update path.
- **In-memory sync on bump:** add `cms.bump_entry_reinforcements(db_id, delta)` —
  scans the bands for the resident entry with that `db_id` and does
  `entry.reinforcements += delta` (no-op if not resident). Call it immediately
  after the DB bump in **both** writer paths, under the lock they already hold:
  - `MemoryService.reinforce(entry_id)` — after `bump_reinforcements(id, 1)`.
  - the dream loop in `dream_run` — after `bump_reinforcements(src_id, 1)` (inside
    the `with self._lock:` added in Phase 1's Critical fix).
  So a resident entry's eviction score reflects reinforcement immediately; on a
  daemon restart, `hydrate_cms` reloads the authoritative value from the DB.
- **No clobber:** because `reinforcements` is absent from `_ENTRY_COLS`,
  `update_access_counts`, and `entry_to_row`, no save path ever writes the
  in-memory value back. The only writer is `bump_reinforcements`. (This is the
  opposite sync direction from `access_count`, which the save cadence pushes
  in-memory→DB — deliberate, to avoid a stale-cache clobber of the counter.)

### 3. Config + wiring
Add to `TracesConfig` (config.py):
```python
retention_boost: float = 0.0   # weight on log1p(reinforcements) in band eviction; 0.0 = today
```
Thread `config.memory.traces.retention_boost` into the named-policy factories in
`retention.py` (`balanced` / `recency_heavy` / `surprise_heavy`) so every band's
`RetentionPolicy` carries it. Default 0.0 = no behaviour change, fully reversible.
(`reinforce_increment` is **deferred** — reinforce stays +1 — to keep the tuning
surface to the single `retention_boost` knob.)

### 4. The tuning bench (`evals/retention_bench.py`)
A dev-only, deterministic, CPU, **no-Postgres** bench (band eviction is pure
in-memory). Same philosophy as `seed_bench` / `memcot_bench`: drive the **real**
`MIRASBand` + production `RetentionPolicy`, not a re-implementation of the formula.

- **Workload:** build a band at a fixed capacity `N`; stream `M > N` synthetic
  entries with controlled timestamps (recency) and source mix; mark a designated
  subset "reinforced" by setting their `.reinforcements`. Let real `band.add(...)`
  trigger real `_evict_one`. Fully deterministic (fixed construction, no RNG or a
  seeded one).
- **Sweep** `retention_boost` over a grid (e.g. `0, 0.25, 0.5, 1, 2, 4`). For each
  value, run the identical workload and measure:
  - **reinforced-survival rate** — fraction of reinforced entries still resident.
  - **recency-displacement guard** — whether fresh *unreinforced* entries were
    evicted in favour of stale *reinforced* ones (e.g. mean recency of survivors,
    or a count of "fresh evicted while a stale reinforced survived").
- **Output:** a printed table + `evals/results/retention.json`, showing the
  reinforced-survival-vs-`retention_boost` curve.
- **Picking the default:** the "knee" — the smallest `retention_boost` that
  meaningfully protects reinforced episodes without immortalizing stale ones
  (recency still respected). That value is proposed to the user; shipping it is a
  follow-on decision (the knob ships at 0.0 regardless).

### 5. Testing
- **Deterministic eviction-order unit tests** (`tests/`):
  - `retention_boost=0` → the eviction victim is byte-identical to today (guard the
    no-op default).
  - positive `retention_boost` → a high-`reinforcements` entry survives an eviction
    that would otherwise drop it (lowest base score).
  - relative/no-deadlock → with all entries reinforced, the band still evicts its
    weakest.
- **In-memory sync test** → after `service.reinforce` (and a dream bump), the
  resident `MemoryEntry.reinforcements` reflects the increment.
- The bench is a dev tool, not a CI gate (a light "it runs and emits the table"
  smoke check is enough).

## Error handling / edge cases
- `retention_boost=0` → no behaviour change (rollback / safety).
- `log1p(0)=0` → unreinforced entries unaffected at any `retention_boost`.
- Bump on a **non-resident** entry → in-memory sync is a no-op; the DB value stands
  and is reloaded on next hydrate. (Resident-only matters: an evicted entry's row
  is deleted by the existing `on_evict` path, so its `reinforcements` is gone with
  it — an episode that fully faded is gone, by design.)
- Concurrency → both bump paths run under `self._lock` (the shared psycopg
  connection serialization honoured in Phase 1); the in-memory sync runs in the
  same critical section.

## Files touched (anticipated)
- `pseudolife_memory/memory/miras/protocols.py` — the `log1p` boost term + a
  `retention_boost` field on `RetentionPolicy`.
- `pseudolife_memory/memory/miras/retention.py` — thread `retention_boost` through
  the named-policy factories.
- `pseudolife_memory/memory/titans_memory.py` — `MemoryEntry.reinforcements`.
- `pseudolife_memory/storage/postgres.py` — `load_entries` selects `reinforcements`.
- `pseudolife_memory/storage/sync.py` — `row_to_entry` loads `reinforcements`.
- `pseudolife_memory/memory/cms.py` — `bump_entry_reinforcements(db_id, delta)`.
- `pseudolife_memory/service.py` — call the in-memory sync in `reinforce` + the
  dream loop; thread config into band/policy construction.
- `pseudolife_memory/utils/config.py` — `TracesConfig.retention_boost`.
- `evals/retention_bench.py` (new) + `evals/results/retention.json`.
- Tests across the above.

## Deferred / tabled for later research
- **Decay of `reinforcements` over time** — *tabled as a future-research item* (user
  request). For now we rely on the existing recency term in `source_weighted_score`:
  a reinforced-but-unused episode still loses ground to fresher entries naturally,
  so strength does not accumulate immortally even without explicit decay. Revisit
  as research: time-decayed reinforcements, reconsolidation dynamics, or coupling
  decay to access recency.
- `reinforce_increment` as a config knob (reinforce stays +1).
- Any learned / auto-tuned retention policy (MemoPilot-style RL write/forget policy
  is a separate, larger research direction).

## Success criterion
- With `retention_boost=0`, the full suite is byte-identical (no eviction change).
- With a positive value, reinforced episodes measurably survive a capacity eviction
  they would otherwise lose, and the band still evicts under pressure (no deadlock).
- The bench emits the reinforced-survival-vs-`retention_boost` curve, from which a
  data-driven default is proposed.
- Existing suite green; no schema change (Phase 2 is code-only — `reinforcements`
  already shipped in v13).
