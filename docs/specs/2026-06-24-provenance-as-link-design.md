# Provenance-as-link — the hippocampal index (design)

**Date:** 2026-06-24
**Branch:** `feat/provenance-as-link` (off `master`)
**Status:** design approved, pending implementation plan
**Provenance:** follows the Track A/B graphify work + the live-bank cleanup, which
surfaced that the cortex retains no pointer home to the episodes it was
consolidated from, and that dense storage is fine — the problem was extraction
discipline (now handled by `exclude_sources`). This adds the *link* between the
two stores.

## The idea, in one line

Give each canonical cortex fact a **bidirectional index** to the dense memory
episodes it was consolidated from, and let *use* of those episodes — by encoding
**and** by retrieval — protect them from forgetting.

## Brain mapping (the design lens)

| PseudoLife | Brain | Role |
|---|---|---|
| Bands (`entries`) | Hippocampus | fast, dense, **episodic**, capacity-limited and **evicting** (transient traces) |
| Cortex facts (`facts`) | Neocortex | slow, terse, **semantic**, durable |
| The dream | Systems consolidation | replays episodes → extracts cortical gist (facts/edges) |
| `memory_traces` (new) | The engram cross-index / hippocampal index | the link the cortical gist keeps back to its episodic traces |
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
A link table between facts and the episodes that formed them:

```sql
CREATE TABLE IF NOT EXISTS memory_traces (
  fact_id    BIGINT NOT NULL REFERENCES facts(id)   ON DELETE CASCADE,
  entry_id   BIGINT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  created_at DOUBLE PRECISION NOT NULL,
  PRIMARY KEY (fact_id, entry_id)
);
CREATE INDEX IF NOT EXISTS memory_traces_entry_idx ON memory_traces (entry_id);
```

- **`fact → entry`:** "this canonical fact was consolidated from these episodes."
- **`entry → fact`** (via `entry_idx`): "this episode became these facts."
- **`CASCADE` = fading is automatic.** When an episode finally evicts
  (hippocampal trace fades), its trace rows vanish; a fact that loses all its
  traces stands on its own (the cortical gist survives — the consolidation
  endpoint). So **surfaced `source_entries` are always live/dereferenceable** —
  a faded episode has already removed itself from every fact that pointed at it.
  (Note: the `facts` row is the cortex's audit trail and is *superseded*, not
  deleted, on ordinary `memory_fact_forget`/correction — so a superseded fact
  keeps its formation traces. The `fact_id` CASCADE fires only on a true fact-row
  delete, e.g. a cleanup/consolidation `DELETE`.)
- **Multi-trace by construction.** Each entry is dreamed exactly once
  (`dream_cursor`-gated — never re-dreamed). A fact's engram grows because
  *new, distinct* episodes assert the same slot over time: when a later episode
  is dreamed and its claim lands on an existing cortex slot (`cortex_write`
  returns `"confirmed"`), that fresh episode contributes its own trace row.

### 2. Consolidation wiring (the dream writes the engram)
Today the dream extracts claims from a batch of entry texts and `cortex_write`s
them, **discarding which entry produced each claim**. The change:
- The dream must **attribute each claim to its source entry**.
  - **Open implementation question (for the plan):** the cleanest mechanism —
    extract per-entry (one `extract` call per entry, claims inherit that
    entry_id), versus a batched extract whose claims carry a source index
    parallel to the input texts. The plan picks one; per-entry attribution is
    the simplest and most robust, at the cost of more extractor calls.
- After `cortex_write` returns the fact's id, insert a `memory_traces(fact_id,
  entry_id)` row (idempotent on the PK) and bump `entries.reinforcements` for
  that entry (+1). Manual `memory_fact_set` may optionally pass a
  `source_entry_id` for the same effect.

### 3. MTT retention (graded resistance to forgetting)
```sql
ALTER TABLE entries ADD COLUMN IF NOT EXISTS reinforcements INTEGER NOT NULL DEFAULT 0;
```

`reinforcements` is the **deliberate** strength signal, bumped by **encoding**
(+1 per trace row) and **explicit reinforce** (+`reinforce_increment`). Band
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
  one fact; forgetting a fact does not erase the episode's earned strength. The
  existing recency decay in `source_weighted_score` still applies, so unused
  strength loses out over time naturally.
- **Ambient retrieval reinforcement is already present:** `source_weighted_score`
  already factors `access_count` + recency, so retrieval implicitly protects an
  episode for free. `reinforcements` is the *new, durable, deliberate* layer on
  top.

`reinforcements ≥ trace-count`: trace rows are *formation provenance*;
`reinforcements` is *retention strength*; they diverge exactly when an episode is
reinforced by use.

### 4. Retrieval surface
- **`memory_get(entry_id)`** — dereference a pointer. Returns the full dense
  episode + `consolidated_into` (the facts it formed, via the reverse index) +
  bumps `access_count` (ambient). A faded (evicted) episode returns
  `{found: false, faded: true}` gracefully. *Read first.*
- **`memory_reinforce(entry_id)`** — the "decision you make": after reading a
  trace and judging it useful, bump `reinforcements` (+`reinforce_increment`). A
  separate act *after* `memory_get` (usefulness can't be judged before reading),
  which is why it is its own tool, not a flag on `memory_get`.
- **Surface the engram on facts:** `memory_fact_get` / `memory_facts` /
  `cortex_search` gain `source_entries: [entry_id, …]` so a fact advertises the
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
graded retention.

## Schema
v12 → **v13**, additive only: the `memory_traces` table (`CREATE TABLE IF NOT
EXISTS`) + the `entries.reinforcements` column (`ALTER TABLE ... ADD COLUMN IF
NOT EXISTS`, in `ensure_schema`'s additive-ALTER block). No destructive change;
a v12 bank upgrades cleanly (new column defaults 0, new table empty).

## Error handling / edge cases
- **Faded pointer:** an evicted episode has already cascaded out of every fact's
  `source_entries`; `memory_get` on a stale id returns `{faded: true}`, and
  `memory_reinforce` on a faded id is a graceful no-op (`{reinforced: false,
  faded: true}`).
- **Idempotent traces:** the `(fact_id, entry_id)` PK means re-asserting from the
  same episode doesn't double-insert; `reinforcements` is bumped only on a *new*
  trace row.
- **No deadlock under pressure:** the boost is relative; a band always evicts its
  weakest entry.
- **`retention_boost = 0`** → no behaviour change (safety / rollback).
- **Excluded sources** (`status`/`log`) are never dreamed, so they form no traces
  — consistent with the cleanup convention.

## Testing
- **Pure:** the `log1p(reinforcements)` boost changes eviction order
  deterministically (a high-`reinforcements` entry survives a capacity eviction
  that would otherwise drop it; with `retention_boost=0` the order is unchanged).
- **Storage (PG):** `memory_traces` insert/cascade — deleting a fact removes its
  traces; deleting (evicting) an entry removes its traces; the
  `entry → facts` reverse query works.
- **PG integration (`build_service`):** a dream over seeded episodes writes trace
  rows + bumps `reinforcements`; `memory_get` returns the episode +
  `consolidated_into`; `memory_reinforce` bumps strength; a faded id returns
  `{faded: true}`.
- **Tool tests:** `memory_get` / `memory_reinforce` shapes; `source_entries`
  present on `fact_get` / `memory_facts`.
- **Success criterion:** end-to-end round trip — fact → `source_entries` →
  `memory_get` → `memory_reinforce` → the episode's eviction-survival measurably
  improves; existing suite green; no schema-version regression.

## Files touched (anticipated)
- `pseudolife_memory/storage/schema.py` — `memory_traces` table + `reinforcements`
  column; `SCHEMA_META_VERSION` 12→13.
- `pseudolife_memory/storage/postgres.py` — trace write/read (`add_trace`,
  `traces_for_fact`, `facts_for_entry`), `reinforcements` bump, `get_entry(id)`.
- `pseudolife_memory/memory/cortex.py` / `service.py` — dream claim→entry
  attribution + trace writing during consolidation; `source_entries` on fact
  reads; `reinforce`.
- `pseudolife_memory/memory/miras/retention.py` (+ `band.py`) — the
  `log1p(reinforcements)` boost in `source_weighted_score`; `MemoryEntry` carries
  `reinforcements`.
- `pseudolife_memory/utils/config.py` — `TracesConfig`.
- `pseudolife_memory/mcp_server.py` — `memory_get`, `memory_reinforce`;
  `source_entries` surfacing.
- Tests across the above.

## Deferred (not v1)
- Surfacing `source_entries` in the `memory_graph` node facts / the digest.
- A Cortex Console "engram" view (fact ↔ episode timeline).
- Decay of `reinforcements` over time (relying on the existing recency term for
  now).
