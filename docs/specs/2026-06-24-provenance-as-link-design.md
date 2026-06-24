# Provenance-as-link — the hippocampal index (design)

**Date:** 2026-06-24 (design correction 2026-06-25 — see "Anchor correction")
**Branch:** `feat/provenance-as-link` (off `master`)
**Status:** design approved, in implementation
**Provenance:** follows the Track A/B graphify work + the live-bank cleanup, which
surfaced that the cortex retains no pointer home to the episodes it was
consolidated from, and that dense storage is fine — the problem was extraction
discipline (now handled by `exclude_sources`). This adds the *link* between the
two stores.

## The idea, in one line

Give each canonical cortex fact a **bidirectional index** to the dense memory
episodes it was consolidated from, and let *use* of those episodes — by encoding
**and** by retrieval — protect them from forgetting.

## Anchor correction (2026-06-25): the engram is keyed on the SLOT, not `facts.id`

The first draft keyed `memory_traces` on `facts.id` with `ON DELETE CASCADE`. That
is **unworkable** given how the cortex persists: `cortex_write → _save_cortex →
snapshot_cortex → PostgresStorage.replace_facts`, which is a **snapshot rewrite**
— `DELETE FROM facts` then re-`INSERT` every current row *without* an id, so every
row gets a fresh `BIGSERIAL`. This runs after **every** cortex write. So `facts.id`
is **ephemeral** (regenerated on every save), and a trace keyed on it would be
`CASCADE`-wiped at the very next cortex write anywhere in the system.

**The fix:** anchor each trace on the cortex's **stable canonical slot**
`(entity_norm, attribute_norm)` — the identity the cortex actually preserves
across snapshot rewrites (it is the slot key, and it matches `facts.entity_norm /
attribute_norm`, which carry the `facts_slot_idx`). The episode side keeps its FK:
`entries.id` **is** stable (inserted once, updated in place), so `entry_id
REFERENCES entries(id) ON DELETE CASCADE` is correct and gives automatic fading.

## Brain mapping (the design lens)

| PseudoLife | Brain | Role |
|---|---|---|
| Bands (`entries`) | Hippocampus | fast, dense, **episodic**, capacity-limited and **evicting** (transient traces) |
| Cortex facts (`facts`) | Neocortex | slow, terse, **semantic**, durable |
| The dream | Systems consolidation | replays episodes → extracts cortical gist (facts/edges) |
| `memory_traces` (new) | The engram cross-index / hippocampal index | the link the cortical gist (a *slot*) keeps back to its episodic traces |
| `reinforcements` (new) | Trace strength | grows with encoding + retrieval; resists forgetting (MTT) |

Two memory-science results make the link more than a foreign key:
- **Multiple-Trace Theory (MTT):** vivid/episodic recall keeps needing the
  hippocampal trace, so a trace shouldn't fade while it still matters →
  **graded retention** of referenced episodes.
- **Testing effect / reconsolidation:** retrieving and *using* a memory
  strengthens it → **retrieval reinforces**, not just encoding. "Use it or lose
  it."

## Decisions (locked during brainstorming)

1. **Full engram:** index + MTT retention + bidirectional.
2. **Retention = in-place graded boost** (not promotion, not hard-pin): a
   referenced episode resists eviction in proportion to its strength, but can
   still fade under pressure.
3. **Strength is reinforced by encoding AND judged use** (explicit reinforce as
   the primary use-signal; ambient access already feeds retention).

## Architecture

### 1. The engram cross-index — `memory_traces`
A link table between cortex **slots** and the episodes that formed them:

```sql
CREATE TABLE IF NOT EXISTS memory_traces (
  entity_norm    TEXT   NOT NULL,
  attribute_norm TEXT   NOT NULL,
  entry_id       BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  created_at     DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (entity_norm, attribute_norm, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_traces_entry_idx ON memory_traces (entry_id);
```

- **`slot → entry`:** "the fact at this slot was consolidated from these
  episodes" (`traces_for_slot(entity_norm, attribute_norm)`).
- **`entry → slot/fact`** (via `entry_idx`): "this episode formed these slots" →
  resolved to the slots' *current* facts (`facts_for_entry`, a slot-join to
  `facts WHERE status='current'`).
- **`entry_id` CASCADE = fading is automatic.** `entries.id` is stable, so when an
  episode finally evicts (hippocampal trace fades) its trace rows vanish, and a
  slot that loses all its traces stands on its own (the cortical gist survives —
  the consolidation endpoint). So **surfaced `source_entries` are always
  live/dereferenceable** — a faded episode has already removed itself.
- **No FK to `facts`** (deliberate — see Anchor correction): `facts.id` is
  regenerated on every snapshot save, so it cannot anchor a durable link, and a
  `facts` `CASCADE` would fire on every write. Keying on the stable slot means a
  fact being **superseded or forgotten** does NOT remove the slot's formation
  traces — consistent with "no decrement on forget" (provenance earned by
  formation is not erased by forgetting a later value). A truly deleted slot that
  is never rewritten leaves harmless orphan trace rows until its episodes evict
  (then `entry_id` CASCADE reaps them); they never surface, because
  `source_entries` is read off a *live* fact and `facts_for_entry` filters
  `status='current'`.
- **Multi-trace by construction.** Each entry is dreamed exactly once
  (`dream_cursor`-gated — never re-dreamed). A slot's engram grows because *new,
  distinct* episodes assert the same slot over time: when a later episode is
  dreamed and its claim lands on an existing cortex slot (`cortex_write` returns
  `"confirmed"`), that fresh episode contributes its own `(slot, entry_id)` row.

### 2. Consolidation wiring (the dream writes the engram)
Today the dream extracts claims from a batch of entry texts and `cortex_write`s
them, **discarding which entry produced each claim**. The change:
- The dream **attributes each claim to its source entry** by extracting
  **per-entry** (one `extract` call per entry, so each claim inherits that
  entry's `db_id`). Chosen over a batched-extract-with-source-index as the
  simplest, most robust mechanism, at the cost of more extractor calls (relations
  extraction stays batched/unchanged).
- After `cortex_write`, insert a `memory_traces(entity_norm, attribute_norm,
  entry_id)` row keyed on the slot just written (`_norm_key(entity)`,
  `_norm_key(attribute)`) — idempotent on the PK — and bump
  `entries.reinforcements` for that entry (+1) **only when a new trace row was
  actually inserted**. No fact-id resolution and no ordering dependency on
  `_save_cortex` (the trace has no `facts` FK), so the write is inline in the
  per-entry loop. Manual `memory_fact_set` may optionally pass a
  `source_entry_id` for the same effect.

### 3. MTT retention (graded resistance to forgetting)
```sql
ALTER TABLE entries ADD COLUMN IF NOT EXISTS reinforcements INTEGER NOT NULL DEFAULT 0;
```

`reinforcements` is the **deliberate** strength signal, bumped by **encoding**
(+1 per *new* trace row) and **explicit reinforce** (+`reinforce_increment`). Band
eviction (`band._evict_one`) drops the entry with the lowest
`retention.source_weighted_score(entry, now)`; we add one term:

```
score += retention_boost * log1p(entry.reinforcements)
```

- **`log1p` = diminishing returns** (graded, not unbounded) — faithful to MTT;
  stops one super-referenced episode from becoming immortal in a small band.
- **Relative, so no deadlock:** the worst entry still evicts; the boost only
  changes *which* is worst. A referenced episode can still fade under enough
  pressure (MTT, not hard-pin).
- **No decrement-on-forget.** Strength is *earned by use*, not tethered to any
  one fact/slot; forgetting a fact does not erase the episode's earned strength.
  The existing recency decay in `source_weighted_score` still applies, so unused
  strength loses out over time naturally.
- **Ambient retrieval reinforcement is already present:** `source_weighted_score`
  already factors `access_count` + recency, so retrieval implicitly protects an
  episode for free. `reinforcements` is the *new, durable, deliberate* layer on
  top.

`reinforcements ≥ trace-count`: trace rows are *formation provenance*;
`reinforcements` is *retention strength*; they diverge exactly when an episode is
reinforced by use. (Phase 2 — `retention_boost` reads `reinforcements`; it is
unaffected by the slot-vs-fact-id anchor change.)

### 4. Retrieval surface
- **`memory_get(entry_id)`** — dereference a pointer. Returns the full dense
  episode + `consolidated_into` (the *current* facts at the slots it formed, via
  the reverse index) + bumps `access_count` (ambient). A faded (evicted) episode
  returns `{found: false, faded: true}` gracefully. *Read first.*
- **`memory_reinforce(entry_id)`** — the "decision you make": after reading a
  trace and judging it useful, bump `reinforcements` (+`reinforce_increment`). A
  separate act *after* `memory_get` (usefulness can't be judged before reading),
  which is why it is its own tool, not a flag on `memory_get`.
- **Surface the engram on facts:** `memory_fact_get` / `memory_facts` /
  `cortex_search` gain `source_entries: [entry_id, …]` — resolved by the fact's
  slot (`traces_for_slot(entity_norm, attribute_norm)`) — so a fact advertises the
  episodes behind it; the agent can `memory_get` them and `memory_reinforce` the
  ones that helped.

### The round trip (why this earns its keep)
`memory_search` / `fact_get` → see a fact + its `source_entries` →
`memory_get(id)` the episode behind it (ambient bump) → if it cracked the task,
`memory_reinforce(id)` → that episode now resists forgetting in proportion to its
usefulness. Encoding *and* retrieval both feed retention.

## Config (`TracesConfig`, nested under `MemoryConfig`)
```python
enabled: bool = True              # write trace rows during consolidation
retention_boost: float = 0.0      # weight on log1p(reinforcements); 0.0 = today's eviction exactly
reinforce_increment: int = 1      # strength added per explicit memory_reinforce
```
`retention_boost = 0.0` by default → eviction behaviour is byte-identical to
today, so the whole feature is additive and reversible; a positive value turns on
graded retention. (Phase 1 ships `enabled` only; `retention_boost` and
`reinforce_increment` land with Phase 2 — Phase-1 `memory_reinforce` bumps by 1,
== the `reinforce_increment` default.)

## Schema
v12 → **v13**, additive only: the `memory_traces` table (`CREATE TABLE IF NOT
EXISTS`) + the `entries.reinforcements` column (`ALTER TABLE ... ADD COLUMN IF
NOT EXISTS`, in `ensure_schema`'s additive-ALTER block). No destructive change;
a v12 bank upgrades cleanly (new column defaults 0, new table empty). **v13 is
unreleased** (never deployed), so the slot-keyed table shape is simply the v13
definition — no migration from any earlier `memory_traces` shape is needed in
production.

## Error handling / edge cases
- **Faded pointer:** an evicted episode has already cascaded out of every slot's
  traces (via the stable `entry_id` FK); `memory_get` on a stale id returns
  `{faded: true}`, and `memory_reinforce` on a faded id is a graceful no-op
  (`{reinforced: false, faded: true}`).
- **Idempotent traces:** the `(entity_norm, attribute_norm, entry_id)` PK means
  re-asserting the same slot from the same episode doesn't double-insert;
  `reinforcements` is bumped only on a *new* trace row.
- **Superseded / forgotten fact:** keying on the slot (not `facts.id`) means the
  formation traces survive a value change; `facts_for_entry` filters
  `status='current'`, so only live facts are ever surfaced.
- **No deadlock under pressure:** the boost is relative; a band always evicts its
  weakest entry.
- **`retention_boost = 0`** → no behaviour change (safety / rollback).
- **Excluded sources** (`status`/`log`) are never dreamed, so they form no traces
  — consistent with the cleanup convention.

## Testing
- **Pure:** the `log1p(reinforcements)` boost changes eviction order
  deterministically (a high-`reinforcements` entry survives a capacity eviction
  that would otherwise drop it; with `retention_boost=0` the order is unchanged).
  *(Phase 2.)*
- **Storage (PG):** `memory_traces` insert/idempotency/cascade — deleting
  (evicting) an entry removes its traces; the `entry → facts` reverse slot-join
  works; a value supersession keeps the slot's traces.
- **PG integration (`build_service`):** a dream over seeded episodes writes trace
  rows + bumps `reinforcements`; `memory_get` returns the episode +
  `consolidated_into`; `memory_reinforce` bumps strength; a faded id returns
  `{faded: true}`. **Durability:** a trace written by one dream survives a
  *subsequent* `cortex_write` (the regression the anchor correction fixes).
- **Tool tests:** `memory_get` / `memory_reinforce` shapes; `source_entries`
  present on `fact_get` / `memory_facts` / `cortex_search`.
- **Success criterion:** end-to-end round trip — fact → `source_entries` →
  `memory_get` → `memory_reinforce`; traces survive later cortex writes; existing
  suite green; no schema-version regression.

## Files touched (anticipated)
- `pseudolife_memory/storage/schema.py` — `memory_traces` table (slot-keyed) +
  `reinforcements` column; `SCHEMA_META_VERSION` 12→13.
- `pseudolife_memory/storage/postgres.py` — slot-keyed trace write/read
  (`add_trace(entity_norm, attribute_norm, entry_id, now) -> bool`,
  `traces_for_slot`, `facts_for_entry`), `reinforcements` bump, `get_entry(id)`.
- `pseudolife_memory/service.py` — dream claim→entry attribution + slot-keyed
  trace writing during consolidation; `source_entries` on fact reads; `reinforce`.
- `pseudolife_memory/memory/miras/retention.py` (+ `band.py`) — the
  `log1p(reinforcements)` boost in `source_weighted_score`; `MemoryEntry` carries
  `reinforcements`. *(Phase 2.)*
- `pseudolife_memory/utils/config.py` — `TracesConfig`.
- `pseudolife_memory/mcp_server.py` — `memory_get`, `memory_reinforce`;
  `source_entries` surfacing.
- Tests across the above.

## Deferred (not v1)
- Surfacing `source_entries` in the `memory_graph` node facts / the digest.
- A Cortex Console "engram" view (fact ↔ episode timeline).
- Decay of `reinforcements` over time (relying on the existing recency term for
  now).
