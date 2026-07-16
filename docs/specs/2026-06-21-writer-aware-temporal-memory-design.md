# Writer-aware temporal memory (v0.4) — design spec

Status: **proposed (design)**, pending review + plan.
Target: **Pseudolife-MCP** `origin/master` (after the lessons/v10 feature, which is
merged at `f303846`). Give every memory write a robust temporal + provenance
stamp — `(tx_time, valid_time, hlc, writer_id, session_id)` — so the agent gains
a real *sense of time*, ordering is deterministic under load and immune to
wall-clock jitter, and multiple concurrent agents (Codex, several Claude
sessions, eventually another local agent) can share one bank with attribution and no
clobbering. Built **design-for-both**: a single shared writer-daemon runtime now
(simple, safe, covers every current case), with the independent-writer
machinery designed and schema-ready behind a config flag — flipping it on later
is a config change, not a migration. Also eliminates the latent
role/AGE-schema name collision (the shadow-schema footgun) at the root.

Touches `pseudolife_memory/storage/schema.py`, `…/storage/postgres.py`,
`…/storage/sync.py`, `…/storage/age.py`, `…/memory/cortex.py`,
`…/memory/world_cortex.py`, `…/memory/lessons.py`, `…/service.py`,
`…/mcp_server.py`, `…/daemon.py`, `…/shim.py`, `…/utils/config.py`,
`ops/docker-compose.yml`, a new `ops/migrate_v04.py` (guarded migration), docs,
tests.

Related: [procedural/outcome memory](2026-06-20-procedural-outcome-memory-design.md)
(the lessons store this stamps), [single-writer cortex](2026-06-19-single-writer-cortex-design.md)
(the dream-is-sole-writer discipline that keeps shared-daemon mode safe).

## 1. Motivation

Three problems, one primitive.

1. **No sense of time.** Every store column is already a sub-second epoch float,
   but the cortex/world/lesson read paths return raw floats — the agent perceives
   *no* time when recalling facts/lessons (only the associative stream renders
   "N days ago"). Lessons and corrections are especially recency-sensitive.
2. **Fragile ordering.** Supersession orders by wall-clock `asserted_at`
   (`cortex.py:_should_supersede`: rejects when `candidate_t < current.asserted_at`).
   An NTP step backwards makes a genuinely-later write look earlier → it is
   **silently dropped as stale**. Wall clocks are for display, never for ordering.
3. **Multi-writer is coming.** Dogfooding with Codex, multiple concurrent Claude
   sessions, and possibly another local agent sharing the bank. Today's snapshot-rewrite +
   in-memory-authoritative model is safe for multiple *clients of one daemon* but
   cannot support independent *writer processes*, and there is no per-write
   attribution.

A robust logical-clock entry is conceptually `(physical_time, counter,
writer_id)` — the `writer_id` is simultaneously the **ordering tiebreak** and the
**attribution tag**. So "sense of time," "deterministic ordering," and "who wrote
this" collapse into one stamp. We add it once.

**Plus a latent footgun surfaced during the v10 deploy (2026-06-21):** the DB
role `pseudolife` collides with the AGE graph schema name `pseudolife`, so a
connection that does not pin `search_path=public` resolves unqualified tables to
the AGE schema and reads/writes a **stale shadow `pseudolife.*` schema** instead
of the real `public.*` bank (observed live: real bank `public.entries=108`, shadow
`pseudolife.entries=43`). The daemon is safe only because it explicitly pins
public. In a multi-writer world with heterogeneous clients this is a split-brain
waiting to happen, so v0.4 eliminates it.

## 2. Goals / non-goals

**Goals**
- **One temporal/provenance stamp** on every canonical write (facts, world_facts,
  lessons, edges): `tx_time`, `valid_time`, `hlc_phys`, `hlc_logical`,
  `writer_id`, `session_id`, plus a `version` for OCC.
- **HLC-lite ordering.** A writer-aware monotonic clock is the ordering authority
  for supersession; wall time is display-only. Fixes the backwards-clock
  dropped-write bug *today*, in shared-daemon mode.
- **Bitemporal (point).** Separate `valid_time` (when it became true) from
  `tx_time` (when recorded), enabling "as of" reasoning and recency-aware lessons.
- **Writer/session attribution** on every write + the supersession log, enabling
  per-writer trust/policy, conflict attribution, and targeted rollback.
- **Design-for-both.** Shared-daemon runtime now (`write_mode=snapshot`); the
  independent-writer machinery (per-row upsert + CAS, DB-as-truth, cache
  invalidation, distributed HLC) designed and schema-ready, dormant behind
  `write_mode=occ`.
- **Eliminate the schema collision** at the root: rename the AGE graph off the
  role name **and** pin `search_path` on every connection, drop the stale shadow
  tables, and fix `meta.schema_version` to reflect reality.
- **Presentation:** relative-age on reads; a slot version-timeline tool; an
  ops rollback-by-writer.

**Non-goals**
- **No full interval bitemporal** (SQL:2011 `valid_from/to` × `tx_from/to`). A
  *point* `valid_time` + the existing supersession chain (transaction-time
  history) is enough; interval modeling is deferred (YAGNI).
- **No turning on independent writers in v0.4.** `write_mode=occ` ships designed
  but **off**; the runtime stays single-shared-daemon. Flipping it on (and the
  cache-invalidation hardening it needs) is a later, separately-validated step.
- **No new distributed-consensus dependency.** HLC is a few columns + a counter,
  not Raft/etcd. Cross-machine HLC update rules are specified but only active
  under `write_mode=occ`.
- **No change to slot identity.** `writer_id`/`session_id` are metadata on the
  record/version, never part of the `(entity, attribute)` key (that would
  re-fragment slots per writer — the opposite of the cortex's purpose).

## 3. Design

### 3.1 The stamp (schema, additive)

Add to `facts`, `world_facts`, `lessons`, `edges` (all additive,
`ADD COLUMN IF NOT EXISTS`, backfilled for existing rows):

| column | type | meaning |
|---|---|---|
| `tx_time` | `DOUBLE PRECISION` | wall-clock record time (**display only**). Backfill = `asserted_at`. |
| `valid_time` | `DOUBLE PRECISION` | event time — when it became true. Backfill = `asserted_at`; set from a signal/episode when known. |
| `hlc_phys` | `BIGINT` | HLC physical component (ms). |
| `hlc_logical` | `INT` | HLC logical counter (ties within a ms). |
| `writer_id` | `TEXT` | stable writer identity (`claude-code`/`codex`/`agent`). Backfill = `"legacy"`. |
| `session_id` | `TEXT` | ephemeral per-connection uuid. Backfill = `NULL`. |
| `version` | `INT NOT NULL DEFAULT 1` | per-slot OCC counter (dormant until `write_mode=occ`). |

Ordering key everywhere supersession decides: **`(hlc_phys, hlc_logical,
writer_id)`** — never `tx_time`. (Two columns over a packed bigint for clarity
and to dodge overflow.)

### 3.2 HLC-lite

A single daemon-held clock advanced on every write:
```
now_ms = floor(wall_ms())
if now_ms > hlc_phys:   hlc_phys, hlc_logical = now_ms, 0
else:                   hlc_logical += 1          # same/backwards ms → bump counter
```
- **Shared-daemon (now):** trivially monotonic — physical ms with a counter for
  same-ms ties. Backwards wall-clock can never lower the HLC, so the dropped-write
  bug is gone.
- **Independent-writers (dormant, `write_mode=occ`):** the standard HLC *receive*
  rule — on read of a remote row, advance local HLC past `max(local, remote)`;
  `writer_id` breaks ties for a total order. Implemented behind the flag.

`_should_supersede` changes from a wall-clock comparison to an HLC comparison
(with the confidence guard unchanged).

### 3.3 Writer / session identity

- **Stable `writer_id`:** from `PSEUDOLIFE_WRITER_ID` (env). Defaults: the shim
  sets it for the local client; the daemon falls back to `"unknown"`.
- **Ephemeral `session_id`:** a uuid the daemon mints per MCP connection.
- **Handshake (shared-daemon):** the client (shim / Codex / another local agent) sends its
  `writer_id` as a header on the daemon connection (e.g. `X-PL-Writer`); the
  daemon binds `(writer_id, session_id)` to that connection and stamps every
  write it performs on that connection's behalf. The daemon remains the sole
  writer, so this is attribution metadata, not a second writer.
- Stamped onto: facts, world_facts, lessons, edges, **the cortex supersession
  log** (so "who clobbered whom" is auditable), and `outcome_signals`.

### 3.4 Bitemporal (point)

`valid_time` is a single timestamp = when the fact/lesson became true. The dream
sets it from the contributing signal/episode time when known (e.g. a lesson's
`valid_time` = when the work happened, not when the dream ran); otherwise it
defaults to `tx_time`. Transaction-time history is already provided by the
supersession chain (`supersedes_value`/`superseded_at`). Full interval bitemporal
is deferred.

### 3.5 The snapshot ↔ OCC seam (`storage.write_mode`)

- **`snapshot` (default, now):** today's full-table rewrite + in-memory-
  authoritative store under the single daemon lock. All agents are *clients* of
  the one daemon → zero clobbering. The new columns are written; `version` and
  the HLC receive-rule are inert.
- **`occ` (designed, dormant):** per-row upsert with compare-and-swap
  (`UPDATE … WHERE version = :read_version`; 0 rows ⇒ a concurrent writer won ⇒
  re-resolve via supersession), DB-as-source-of-truth, the in-memory store
  becomes a read-through/short-TTL cache, and the distributed HLC receive-rule
  activates. Enabled only when independent writer processes actually exist.

Because every OCC/HLC column is present from day one, enabling `occ` is a
code-path + config change, never a data migration.

### 3.6 Schema-collision elimination (root-cause + belt)

A guarded, backup-first one-time migration (`ops/migrate_v04.py`, dry-run
default), plus a permanent code change:

1. **Rename the AGE graph** off the role name: `age.py` `GRAPH_NAME`
   `"pseudolife"` → configurable (default `"pseudolife_graph"`). Because AGE is a
   rebuildable mirror of the `public.edges`/`entities` truth tables, the migration
   is: create the new graph → `age_sync` rebuild → `drop_graph("pseudolife", true)`.
   No source data is at risk.
2. **Universal `search_path` pinning:** every connection (daemon today; the OCC
   writers later; any helper) issues `SET search_path TO public, ag_catalog`. The
   daemon already does this in `PostgresStorage.__init__`; make it a hard
   invariant and assert it.
3. **Drop the stale shadow relational tables** in the old `pseudolife` schema
   (`entries/facts/relations/edges/entities/entity_aliases/episodes/meta`) — the
   AGE graph rename removes the graph tables, so after this the `pseudolife`
   schema is empty/gone and the cluster `"$user"` default falls through to
   `public` naturally. Scoped, reversible-by-restore (backup first), never touches
   `public.*`.
4. **`meta.schema_version` reflects reality:** `ensure_schema` switches from
   `INSERT … ON CONFLICT DO NOTHING` to an upsert that **updates** the recorded
   version to `SCHEMA_META_VERSION` on every upgrade (today it is stuck at the
   first-init value — observed `8` on a v10 bank).

After (1)+(3), an unpinned connection can no longer hit a shadow schema (none
exists); (2) guarantees it regardless. Defense in depth — root cause removed *and*
runtime guaranteed.

### 3.7 Presentation

- **Relative-age** (`_relative_time`, already in `context_builder`) added to the
  cortex/world/lesson serialisers and context blocks ("asserted 3 days ago,
  confirmed yesterday").
- **`memory_history(entity, attribute)`** — walk a slot's version timeline
  (current + superseded, each with writer + tx/valid time). The supersession log
  already holds the data.
- **Ops `retire_by_writer(writer_id | session_id)`** — targeted rollback of one
  agent's contributions (the dogfooding safety net), dry-run-first like
  `dedup_cortex`.

## 4. Config

`storage.write_mode: "snapshot" | "occ"` (default `snapshot`); `graph.name`
(default `pseudolife_graph`); writer identity via `PSEUDOLIFE_WRITER_ID` env;
`memory.time.relative_age: bool` (default True). `compose` sets a distinct
`PSEUDOLIFE_WRITER_ID` per client where relevant.

## 5. Testing

- **HLC monotonicity:** rapid writes get strictly increasing `(hlc_phys,
  hlc_logical)`; a simulated backwards wall-clock still supersedes correctly
  (regression for the dropped-write bug).
- **Ordering authority:** `_should_supersede` uses HLC not `tx_time`; equal HLC
  resolves by `writer_id`.
- **Keying:** a write stamps `writer_id`/`session_id`; the supersession log
  records who superseded whom; the handshake binds identity per connection.
- **Bitemporal:** `valid_time` set from a signal differs from `tx_time`; defaults
  to `tx_time` when unknown.
- **Collision migration:** on a bank with a seeded shadow `pseudolife.*`, the
  migration renames the graph (graph queries still work via `age_sync`), drops
  the shadow tables, and an unpinned connection then resolves to `public`;
  `meta.schema_version` becomes current. Dry-run mutates nothing.
- **OCC seam (dormant):** with `write_mode=occ` on a test DB, a CAS conflict
  (stale `version`) is detected and re-resolved; `snapshot` mode is unchanged.
- **Migration idempotency** + **no data loss** (counts before/after).
- **Presentation:** relative-age fields present; `memory_history` returns the
  timeline; `retire_by_writer` dry-run lists, `--apply` retires only that writer.

## 6. Migration & phasing

1. **Phase 1 (live in v0.4, shared-daemon):** add the stamp columns (additive +
   backfill); HLC-lite ordering; writer/session keying + handshake; bitemporal
   point; presentation; the collision-elimination migration; the version-bump
   fix. All work in `write_mode=snapshot`; forward-compatible.
2. **Phase 2 (dormant in v0.4):** the OCC write path, distributed HLC receive-rule,
   and cache invalidation — code present behind `write_mode=occ`, validated on a
   test DB, **not** enabled in the live runtime.
3. **Phase 3 (separate, later):** flip `write_mode=occ` if/when an independent
   writer process is actually deployed (e.g. a local daemon per machine), with
   its own validation pass.

The collision-elimination migration runs once, **backup-first** (`ops/backup.ps1`),
dry-run-by-default, on the live bank — same discipline as `dedup_cortex`.

## 7. Decisions (locked at design)

1. **One stamp:** `(tx_time, valid_time, hlc_phys, hlc_logical, writer_id,
   session_id, version)` on facts/world_facts/lessons/edges; all additive.
2. **HLC is the ordering authority; wall-clock is display-only.** Fixes the
   backwards-clock dropped-write bug now.
3. **Writer/session = metadata, never slot identity.** `writer_id` doubles as the
   HLC tiebreak and the attribution tag.
4. **Bitemporal = point `valid_time`** + existing supersession chain; interval
   bitemporal deferred.
5. **Design-for-both:** `write_mode=snapshot` runtime now; `occ` machinery
   schema-ready + dormant; enabling it later is config, not migration.
6. **Schema collision eliminated by BOTH** AGE-graph rename (root cause; low-risk
   because AGE is a rebuildable mirror) **and** universal `public` pinning (runtime
   belt), plus shadow-table cleanup and the `meta.schema_version` upsert fix.
