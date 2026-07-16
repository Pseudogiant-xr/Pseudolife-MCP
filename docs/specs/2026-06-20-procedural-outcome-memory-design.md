# Procedural / outcome memory ("lessons") — design spec

Status: **proposed (design)**, pending review + plan.
Target: **Pseudolife-MCP** `origin/master`. Add a third memory primitive beside the
declarative cortex (`facts`) and world cortex (`world_facts`): a *procedural*
store of **lessons** — what worked, what was a dead-end, and what got corrected,
keyed to a **task-type** and tagged with an **outcome**. Lessons are written
solely by the dream pass (single-writer), synthesised from cheap in-session
**outcome signals**, retrieved by embedding-on-query, and cross-referenced into
the knowledge graph via two new typed relations (`prefers` / `avoids`). Touches
`pseudolife_memory/storage/schema.py`, `…/storage/postgres.py`, `…/storage/sync.py`,
`…/memory/lessons.py` (new), `…/memory/dream.py`, `…/service.py`,
`…/mcp_server.py`, `…/utils/config.py`, docs, tests.

Related: [single-writer cortex design](2026-06-19-single-writer-cortex-design.md)
(the dream-is-sole-writer discipline this extends to lessons) and the
world-knowledge cortex (the parallel-store blueprint this is the third instance
of — `memory/world_cortex.py`, `world_facts`).

## 1. Motivation

Every memory Pseudolife stores today is **declarative**: an `(entity, attribute)
→ value` truth about the user's world (`facts`) or the external world
(`world_facts`). The system has no **procedural** memory — it never records the
outcomes of *its own work*. So across sessions it cannot learn that, for a given
kind of task, approach A worked, source B was a dead end, or the user corrected
Y → Z. It re-derives that context every time.

This is the one capability gap surfaced by the 2026-06-18 review of Perplexity
"Brain": its distinguishing primitive is agent-work memory tagged with
success / failure / correction signals, consolidated overnight into reusable
lessons. Pseudolife already matches or beats Brain on consolidation (the dream,
which is on-demand, not batch-only), the context graph (AGE/NetworkX), provenance
(world-cortex citations + cortex provenance tiers), sessions (episodes), and
removal (`*_forget` / supersession). Procedural/outcome memory — especially
*negative* "dead-end" memory — is the missing axis. It is **orthogonal** to the
known declarative-extraction-recall blocker (benchmark Tier B ≈ naive-RAG); this
is a new capability, not a fix for that one.

## 2. Goals / non-goals

**Goals**
- **A procedural primitive.** Store lessons keyed `(task-type, aspect) → lesson`,
  with `polarity` (do/avoid) and an `outcome` class (success / failure /
  correction), provenance back to the episode + signals that produced them.
- **First-class negatives.** A dead-end is a supersedable, retrievable record and
  a traversable graph edge — not an absence.
- **Single-writer.** The dream pass is the *sole automatic writer* of lessons,
  exactly as it is for the cortex. In-session capture only ever writes cheap
  *signals*, never lessons.
- **Reuse, don't reinvent.** Third instance of the cortex/world-cortex blueprint:
  same slot-keying, supersession, embedding, snapshot/hydrate, MCP shape.
- **Make the graph earn its keep.** Lessons cross-reference the existing knowledge
  graph so "what worked / what's a dead-end for X" is traversable via the
  existing `memory_graph` tool.

**Non-goals**
- **No prompt-injection block in this repo.** The auto-injected
  "## Lessons from past work" prefetch block is a provider/client prompt-assembly
  concern (the analogous "## World knowledge" block lives in a provider, not in
  this engine). v1 ships the engine + MCP tools; the injection block is a
  separate, downstream provider follow-on (§6).
- **No deterministic lesson floor.** With no extractor LLM present, lessons are
  not synthesised (signals are retained, not consumed) — consistent with
  single-writer cortex, which removed the regex floor. No regex/heuristic ever
  defines a lesson.
- **No AGE upgrade.** Edge-property mirroring into AGE and a Cypher-backed
  "dead-ends for X" query are deferred (§6) — the fragile AGE/search-path surface
  is out of scope for v1. Lessons are traversable via the NetworkX-backed
  `memory_graph`, which is the path that actually works today.
- **No visualization.** Human-facing graph viz is deferred (§6).
- **No lessons-as-graph-nodes.** Lesson bodies stay table rows; only their
  task-type and object become entities (§3.3).

## 3. Design

### 3.1 Data model — `lessons` table (schema v10)

Slot-keyed, structurally a sibling of `facts` / `world_facts` so the cortex
write / supersede / `_norm_key` / `_norm_value` logic is reused, but **physically
separate** for blast-radius isolation (a runaway synthesis can be truncated
without touching `facts` or `world_facts`).

Columns (reused from the cortex shape unless noted):
- `id BIGSERIAL PRIMARY KEY`
- `entity TEXT` / `entity_norm TEXT` — the **task-type** descriptor
  ("deploy engine to host", "postgres role/schema collision").
- `attribute TEXT` / `attribute_norm TEXT` — the **aspect** ("approach",
  "pitfall", "tool-choice").
- `value TEXT` — the actionable lesson text.
- `polarity TEXT DEFAULT '+'` — `+` do-this / worked, `-` avoid / dead-end.
- `outcome TEXT` — **new** — `success | failure | correction` (the signal class
  that produced the lesson; orthogonal to polarity — a `correction` lesson is
  `polarity='+'` on the corrected value).
- `status TEXT`, `confidence REAL`, `origin TEXT`.
- `support JSONB`, `provenance JSONB` — provenance carries the contributing
  `episode_id`s and `outcome_signals` ids.
- `asserted_at`, `last_confirmed`, `supersedes_value`, `superseded_by_value`,
  `superseded_at` — supersession audit, identical semantics to cortex.
- `embedding vector(384)` — for embedding-on-query retrieval.
- `entity_id BIGINT REFERENCES entities(id)` — the task-type entity (§3.3).
- `object_entity_id BIGINT REFERENCES entities(id)` — the tool/source the lesson
  is about (§3.3).
- Index: `lessons_slot_idx ON lessons (entity_norm, attribute_norm, status)`
  (mirrors `facts_slot_idx`).

`SCHEMA_META_VERSION` bumps `9 → 10`. All DDL stays `CREATE TABLE IF NOT EXISTS`,
so `ensure_schema` is idempotent on every daemon start (no destructive migration;
the columns are new tables only).

### 3.2 In-session signals — `outcome_signals` table + capture

Cheap, append-only log the dream drains. Never user-visible as memory.

`outcome_signals` columns: `id BIGSERIAL PK`, `task TEXT`, `outcome TEXT`
(`success|failure|correction`), `about TEXT NULL` (entity/tool/source the outcome
concerns), `detail TEXT NULL` (what worked / what the dead-end was), `polarity
TEXT NULL`, `origin TEXT`, `episode_id TEXT NULL`, `created_at DOUBLE PRECISION`,
`consumed_at DOUBLE PRECISION NULL` (the dream's drain cursor — NULL = pending).

Two producers:
1. **Explicit** — new MCP tool `memory_outcome(task, outcome, about=None,
   detail=None, polarity=None)` appends one pending row. It deliberately does
   **not** write a lesson (single-writer). Returns `{recorded: true, signal_id}`.
2. **Auto-tagged corrections** — when cortex supersession replaces a fact's value
   (the existing supersede path in `service.py` / `cortex.py`), emit an
   `outcome_signals` row with `outcome='correction'`, `origin='action'`,
   `about` = the fact's entity, `detail` = `"<attribute>: <old> → <new>"`. This
   captures user/agent corrections for free — no agent action required.

Retention: a pending signal is kept until consumed; an unconsumed signal older
than `signal_retention_days` (default 30) is pruned on the dream sweep so the log
can't grow unbounded when no extractor is configured.

### 3.3 Graph realization of the hybrid model

A lesson row is **not** a graph node. Instead:
- Its **task-type** becomes an entity with `etype='task-type'` (auto-created via
  the existing `_resolve_or_create_entity`), so it is visually and
  programmatically separable from real-world entities. `lessons.entity_id` FKs to
  it (mirroring how `facts.entity_id` links a fact's subject).
- Its **object** (the `about` tool/source/approach) becomes/*resolves to* an
  entity; `lessons.object_entity_id` FKs to it.
- The outcome becomes a **typed edge** between them, using two new builtin
  relations appended to `_BUILTIN_RELATIONS` (`storage/postgres.py:58`):
  - `prefers` — "src (task-type) prefers approach/tool dst" (positive lessons).
  - `avoids` — "src (task-type) should avoid dead-end dst" (negative lessons).
  - Both: `transitive=False`, `inverse_of=None`, `src_type='task-type'`,
    `dst_type=None` (soft type — mismatches warn, never reject, per the existing
    `graph_relate` policy).

So `memory_graph("deploy engine to host")` returns
`--avoids--> "tar --same-owner"`, `--prefers--> "tar --no-same-owner"` with no
new node type and no change to traversal code. Real-world-entity neighborhoods
stay clean because task-type nodes carry the distinct `etype` (a future
`exclude_etypes` filter on `memory_graph` is noted in §6, not required for v1).

Edge creation reuses the existing `graph_relate` machinery (entity auto-create +
table edge + best-effort AGE mirror); it runs only when the dream writes/updates
a lesson, so signals never touch the graph.

### 3.4 Dream synthesis (sole canonical writer)

Extend the dream sweep (the path around `service.py` `dream_run` and
`memory/dream.py`) so that, **in addition to** declarative cortex consolidation,
it:
1. Drains pending `outcome_signals` (oldest-first, bounded batch), gathering each
   signal's `task`, `outcome`, `about`, `detail`, and light episode context.
2. Synthesises **lessons** via the configured extractor LLM with a
   lesson-specific prompt (cluster signals by task-type; emit structured claims:
   `task_type`, `aspect`, `lesson_text`, `polarity`, `outcome`, `about`). The
   extractor abstraction is reused; only the prompt/schema differs.
3. Writes each lesson through the new `LessonStore` (slot resolution + supersession
   dedup), then upserts the task-type/object entities and the `prefers`/`avoids`
   edge (§3.3).
4. Marks the contributing signals `consumed_at = now`.

**No-extractor behaviour (single-writer-consistent):** when `build_extractor()`
resolves to the `NoOpExtractor`, lesson synthesis is **skipped** — signals remain
pending (subject to retention pruning), and the existing startup WARNING about a
missing extractor is extended to mention lessons. No deterministic lesson floor
is ever written.

### 3.5 Retrieval

`MemoryService.lesson_search(query, top_k)` = embedding cosine over current
`lessons` rows, reusing the world-cortex search machinery (embed query → rank by
cosine → return current records with `polarity`, `outcome`, `confidence`,
`effective_confidence`, provenance). Negative (dead-end) lessons are returned
with their polarity intact so a caller can surface them prominently. This is the
exact pattern `world_search` already uses; no new retrieval algorithm.

### 3.6 Module / storage layout (third blueprint instance)

- `memory/lessons.py` — `LessonStore` (slot-keyed; reuses cortex `_norm_key` /
  `_norm_value`; supersession; `outcome` + `polarity`; `current_records`).
  Mirrors `memory/world_cortex.py`.
- `storage/schema.py` — `lessons` + `outcome_signals` DDL; `SCHEMA_META_VERSION
  9 → 10`.
- `storage/postgres.py` — `_LESSON_COLS`; `replace_lessons` / `load_lessons`;
  `outcome_signals` CRUD (`add_signal`, `pending_signals`, `consume_signals`,
  `prune_signals`); append `prefers` / `avoids` to `_BUILTIN_RELATIONS`.
- `storage/sync.py` — `snapshot_lessons` / `hydrate_lessons` (entity-id linking
  via `entity_id_map`, exactly like `snapshot_cortex` at `sync.py:133-136`).
- `service.py` — `self._lessons` init + `hydrate_lessons` in `_ensure_init`;
  `_save_lessons`; `lesson_search`; `record_outcome`; the supersession→signal
  hook; dream-synthesis wiring; lesson→entity/edge upserts.
- `mcp_server.py` — `memory_outcome`, `memory_lessons`, `memory_lesson_search`,
  `memory_lesson_forget` (§4).
- `utils/config.py` — `LessonsConfig` dataclass wired into `MemoryConfig`
  (mirrors `cortex` at `config.py:419` + the from-dict at `:561`).

### 3.7 Config

New `LessonsConfig`: `enabled: bool = True`, `top_k: int = 5`,
`min_confidence: float = 0.0`, `signal_retention_days: int = 30`,
`synthesize_in_dream: bool = True`. A dream that has `synthesize_in_dream=False`
or `lessons.enabled=False` skips signal drain entirely (signals still pruned by
retention).

## 4. MCP surface

- `memory_outcome(task, outcome, about=None, detail=None, polarity=None)` —
  record an in-session signal. Single-writer: writes a signal, never a lesson.
- `memory_lessons(limit=120)` — list current lessons (parity with
  `memory_facts` / `memory_world_facts`).
- `memory_lesson_search(query, top_k=5)` — embedding retrieval (§3.5).
- `memory_lesson_forget(task, aspect=None)` — retire a lesson (reversible
  supersede), parity with `memory_world_forget`, for inspect/remove + manual
  correction.
- Lessons are also reachable through the **existing** `memory_graph` (task-type
  entities + `prefers`/`avoids` edges) and `memory_graph_query`; no new graph
  tool.

## 5. Testing

- **`LessonStore`:** slot keying, polarity, `outcome` round-trip, supersession
  (a newer lesson for the same `(task,aspect)` retires the old), `current_records`
  excludes superseded. Mirrors `test_world_storage.py` + cortex tests.
- **Signal capture:** `memory_outcome` appends a pending row and writes **no**
  lesson; correction auto-tag fires an `outcome='correction'` signal when a
  cortex fact is superseded.
- **Dream synthesis (stub extractor):** pending signals → synthesised lessons →
  signals marked consumed; a second sweep with no new signals is a no-op.
- **No-extractor:** dream with `NoOpExtractor` writes no lessons and leaves
  signals pending; warning emitted.
- **Graph linkage:** a synthesised positive lesson creates a `prefers` edge and a
  `task-type` entity; a negative lesson creates an `avoids` edge; `memory_graph`
  on the task-type returns them.
- **Retrieval:** `lesson_search` ranks by cosine and returns polarity/outcome;
  dead-ends retrievable.
- **Retention:** an unconsumed signal older than `signal_retention_days` is pruned.
- **MCP registration:** the four new tools appear in the registered-tool set
  (extend `test_mcp_server.py::test_all_tools_registered`).
- **Migration:** `ensure_schema` on a v9 bank creates `lessons` +
  `outcome_signals` + the two relations idempotently and reports v10.

## 6. Deferred / out of scope

- **Provider injection block.** The auto-injected "## Lessons from past work"
  prefetch block (scope-budgeted, no-op when empty, dead-ends first) is a
  provider/client follow-on — exactly as world-knowledge injection was done
  outside this engine. Out of scope for the v1 engine release.
- **Make AGE real.** Mirroring edge properties (`outcome`/`confidence`/recency)
  into AGE and a Cypher-backed "dead-ends for task-types like X" query. Today AGE
  is a thin read-only mirror that carries no edge properties and is bypassed by
  the NetworkX `memory_graph`; upgrading it is a separate, larger piece touching
  the fragile role↔schema/search-path area.
- **Visualization.** A human-facing, outcome-colored graph view (green=worked,
  red=dead-end) — the highest user-visible payoff, deferred per the v1 scope call.
- **`exclude_etypes` filter on `memory_graph`** to hide `task-type` nodes from
  real-world-entity neighborhoods — additive, separable; not required for v1.
- **Eager lesson synthesis** (synthesise on episode-end rather than on the dream
  cadence) — accept dream-cadence eventual consistency for v1, matching the
  cortex's deferred eager-dream trigger.

## 7. Touches

- `pseudolife_memory/storage/schema.py` — `lessons` + `outcome_signals` DDL;
  `SCHEMA_META_VERSION 9 → 10`.
- `pseudolife_memory/storage/postgres.py` — `_LESSON_COLS`; `replace_lessons` /
  `load_lessons`; `outcome_signals` CRUD; `prefers` / `avoids` appended to
  `_BUILTIN_RELATIONS` (`:58`).
- `pseudolife_memory/storage/sync.py` — `snapshot_lessons` / `hydrate_lessons`.
- `pseudolife_memory/memory/lessons.py` — **new** `LessonStore`.
- `pseudolife_memory/memory/dream.py` — lesson-synthesis prompt/path; warning text.
- `pseudolife_memory/service.py` — `_lessons` init/hydrate/`_save_lessons`;
  `lesson_search`; `record_outcome`; supersession→signal hook; dream wiring;
  lesson→entity/edge upserts.
- `pseudolife_memory/mcp_server.py` — `memory_outcome`, `memory_lessons`,
  `memory_lesson_search`, `memory_lesson_forget`.
- `pseudolife_memory/utils/config.py` — `LessonsConfig` + `MemoryConfig` wiring.
- `README.md`, `CHANGELOG.md` — procedural-memory primitive; new tools; the
  `prefers`/`avoids` relations; single-writer + no-extractor behaviour.
- Tests — new `test_lessons_storage.py`, dream-synthesis + signal + graph-linkage
  + retrieval cases; extend `test_mcp_server.py`.

## 8. Decisions (locked at design)

1. **Hybrid graph model:** lesson bodies are table rows; their **task-type and
   object become entities** (`etype='task-type'`), and the outcome is a typed
   `prefers` / `avoids` edge. No lessons-as-nodes.
2. **Capture = in-session signals → dream synthesis.** `memory_outcome` +
   auto-tagged corrections write signals only; the dream is the sole automatic
   lesson writer. No deterministic lesson floor.
3. **v1 = end-to-end engine loop:** store + signals + dream synthesis + graph
   edges + retrieval tools. Defer AGE edge-property mirroring and visualization.
4. **Retrieval = embedding-on-query** over the lesson table (reuse `world_search`);
   graph edges serve structured traversal + future viz.
5. **Storage = a third parallel store** (`lessons` / `LessonStore`), cloning the
   cortex/world-cortex blueprint for full blast-radius isolation.
6. **Injection block is a provider follow-on**, not part of this engine release.
