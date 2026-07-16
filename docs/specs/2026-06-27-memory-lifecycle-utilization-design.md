# Reliably utilizing world facts, lessons, and episodes

- **Date:** 2026-06-27
- **Status:** Design approved; ready for implementation plan
- **Branch (proposed):** `feat/memory-lifecycle-utilization`

## Problem

Three memory subsystems are fully built (MCP tools, service methods, storage,
tests) but sit empty in the Cortex Console UI:

| Section | Current state | Root cause |
|---|---|---|
| **World facts** | 0 entries | Nothing ever calls `memory_world_set`. The dream does **not** extract world facts (they require a `source_url`+quote; the dream only writes personal-cortex facts, graph edges, lessons). |
| **Lessons** | 2 entries, both `origin:agent`, same task | The agent never calls `memory_outcome`; the only 2 lessons came from auto-captured `memory_fact_set` corrections feeding the dream. |
| **Episodes** | 1, a disposable `PLHC-SWEEP` test scaffold, 0 entries | Nothing opens/closes an episode around real work. |

The unifying cause: all three are **"agent-must-actively-invoke" subsystems with
no behavioral trigger**, and two of three have **no read surface** either. The
global CLAUDE.md only ever drove `memory_search` / `memory_store` /
`memory_fact_set`. The session-start briefing ([`briefing.py`](../../pseudolife_memory/memory/briefing.py))
injects only lessons + graph-uncertainties — world facts and episode recaps are
never surfaced, so even once populated they would stay invisible.

A prior design note (in the bank) already anticipated this: the
"auto-injected `## Lessons from past work` prompt block" was explicitly deferred
"until the concept is proven." That deferral is what the empty UI now reflects.

## Goals

- Populate and surface **episodes, lessons, world facts** reliably, without
  depending on the agent remembering.
- Drive the judgment-call writes (outcomes, cited world facts) with the best
  possible global CLAUDE.md.
- Reuse the proven SessionStart-briefing plumbing; do not invent new transport.

## Non-goals

- No changes to retrieval ranking, the dream extractor model, or the cortex.
- No new UI work in the Cortex Console (the data simply starts flowing into the
  existing panels).
- No automated/scheduled research crawler — world-fact population is driven by
  the agent during sessions (CLAUDE.md), surfaced by the briefing.

## Architecture

Three mechanisms, each owning the part of the loop it can do reliably:

| Mechanism | Owns | Why it's the right owner |
|---|---|---|
| **Lifecycle hooks** (harness-enforced) | Episode open/close | Sessions have hard start/end boundaries the harness fires on — zero dependence on the agent. |
| **Briefing read-surface** (extends the existing SessionStart injection) | Making populated data visible | One method (`session_briefing`) already injects lessons + unsure; world facts + recap fold in beside them. |
| **Optimized CLAUDE.md** | Judgment-call writes | `memory_outcome`, cited `memory_world_set`, task sub-episodes — a hook cannot decide these, only the agent can. |

**Data flow (session lifecycle):**

```
SessionStart hook ─┬─ pseudolife-mcp briefing      → GET  /api/briefing       → inject (lessons + unsure + world + recap)
                   └─ pseudolife-mcp episode-start  → POST /api/episode/start  {session_key,title,hint}
                          → idempotent: no-op if an episode is already open for this session_key
                            (resume/compact re-fires of SessionStart do not double-open)

   …agent works… every memory_store is auto-stamped with the open episode_id
   …agent calls memory_outcome at task end (CLAUDE.md-driven) → outcome signals

SessionEnd hook ──── pseudolife-mcp episode-end    → POST /api/episode/end    {session_key}
                          → close the open episode, then fire a fire-and-forget dream pass
                            (dream synthesises outcome-signals → lessons, surfaced next SessionStart)
```

**Phase split.** Phase 1 ships on proven plumbing (session-floor episodes +
read surfaces + CLAUDE.md). Phase 2 adds the one piece of net-new capability
(episode nesting for task sub-episodes).

## Design invariants

- **Never break session start/stop.** The episode/briefing CLIs swallow all
  errors and exit 0 when the daemon is down (mirrors current `briefing_cli.py`).
- **Idempotent hooks** keyed by Claude Code's `session_id`, because
  `SessionStart` fires on `startup` / `resume` / `clear` / `compact`.
- **Deploy = daemon-only rebuild.** Schema changes ship via
  `docker compose build daemon && docker compose up -d daemon`. The postgres
  container and its volumes are **never** touched (recorded pitfall lesson).
- **Additive schema only.** New columns via `ADD COLUMN IF NOT EXISTS`;
  `EpisodeManager.from_dict` already filters unknown keys, so the torch.save
  path is forward/backward-compatible.

---

## Phase 1 — session-floor episodes + read surfaces + CLAUDE.md

### 1a. Session-floor episodes (lifecycle plumbing)

**Model — [`memory/episodes.py`](../../pseudolife_memory/memory/episodes.py)**
- Add field `session_key: str | None = None` to the `Episode` dataclass.

**Service — [`service.py`](../../pseudolife_memory/service.py)** (two thin wrappers over the existing `episode_start`/`episode_end`)
- `episode_start_session(session_key, title, hint=None)` — **idempotent open**:
  if an episode is currently open *and* its `session_key == session_key`, return
  it unchanged; otherwise `start()` a new one stamped with `session_key`
  (auto-closing any stale prior open episode, as today).
- `episode_end_session(session_key)` — close the currently-open episode **only
  if** its `session_key` matches; otherwise no-op (return `{}`). After a
  successful close, schedule a **fire-and-forget dream** (background thread;
  the endpoint returns immediately so SessionEnd never blocks on the Gemma
  extractor). Reuses `dream_run(build_extractor(...))`.

**REST — [`web/routes.py`](../../pseudolife_memory/web/routes.py)** (POSTs beside the existing enumerated mutations)
```
POST /api/episode/start  {session_key, title, hint}  → svc.episode_start_session(...)
POST /api/episode/end    {session_key}               → svc.episode_end_session(...)
```

**CLI — new `pseudolife_memory/episode_cli.py`, dispatched from [`cli.py`](../../pseudolife_memory/cli.py)**
- New modes `episode-start` / `episode-end`. Torch-free, stdlib `urllib` only,
  mirroring `briefing_cli.py`: hit the running daemon, never auto-start one,
  **swallow all errors and exit 0**.
- Read Claude Code's **hook stdin JSON** for `session_id` (→ `session_key`) and
  `cwd` (→ title, e.g. `Pseudolife-MCP — 2026-06-27`). On malformed/empty stdin,
  fall back to a random `session_key` and still exit 0.

**Hooks — [`ops/install-hook.ps1`](../../ops/install-hook.ps1)**
- Extend the idempotent installer to register, alongside the existing briefing
  hook (never replacing it):
  - `SessionStart` → `pseudolife-mcp episode-start` (sibling group to briefing)
  - `SessionEnd`   → `pseudolife-mcp episode-end`
- Idempotency guard mirrors the existing one (bail if an `episode-start` /
  `episode-end` hook is already present). Back up `settings.json` first.

**Error handling / degradation:** daemon down → exit 0, the session runs
unstamped (acceptable). Malformed stdin → random key, exit 0. End with no
matching open episode → no-op.

### 1b. Briefing read surfaces

**`service.session_briefing(max_unsure=3, max_lessons=3, max_world=3)`** gathers
two more inputs; **`briefing.format_briefing(...)`** renders two more optional
blocks (empty blocks omitted, so a cold bank still injects nothing):

- **`## Verified world facts`** — `world_dump()` → drop `stale`, sort by
  `effective_confidence` desc, take `max_world`. Render
  `` - `entity` attribute: value — (source host) ``.
- **`## Where we left off`** — newest **closed** episode that has entries
  (`episode_list(include_open=False)` → `episode_summary`) → one line:
  title + entry count + top tags.

Plumb `max_world` through `GET /api/briefing` (query param) and a `--max-world`
flag in `briefing_cli.py` (mirrors `--max-lessons`).

### 1c. Optimized global CLAUDE.md

Replace the current memory section with an explicit **session lifecycle** so
every subsystem has a trigger (RECALL → CAPTURE → REFLECT):

```markdown
## Pseudolife-MCP memory — your long-term memory (use it every session)
One shared neural-memory bank across all sessions (tools: `mcp__pseudolife-memory__*`).
Treat it as a loop with three beats: RECALL at the start, CAPTURE as you go,
REFLECT at the end. Episodes are opened/closed for you by session hooks — every
memory you store is auto-stamped to the current session episode.

### 1. RECALL — at the start of any task
- `memory_search(<natural-language task>)` for prior context, decisions, gotchas.
- `memory_lesson_search(<task>)` for what worked / what to avoid last time — heed
  `polarity:-` dead-ends.
- `memory_fact_get(entity, attribute)` for one canonical value; if null, the slot is
  empty, NOT the topic — `memory_search`/`memory_facts` find it regardless.
- `memory_world_search(<topic>)` when the task turns on an external fact your frozen
  training may have stale (versions, prices, who-holds-a-role, research findings).

### 2. CAPTURE — as durable things arise (one claim per call)
- `memory_store` for durable context; set `origin` honestly (`user`/`action`/`agent`).
- `memory_fact_set(entity, attribute, value)` for a canonical single-value fact;
  correct by setting a new value at the same slot.
- `memory_world_set(entity, attribute, value, source_url=…, source_quote=…)` for any
  EXTERNAL fact you verified via web/docs. Route research findings HERE (cited), not
  into plain `memory_store` — that's what keeps the world cortex alive.
- Routing: verbose status/progress/logs → `source="status"` (or `"log"`) so they stay
  searchable but are excluded from dream extraction (no graph pollution). Reserve
  `memory_fact_set` / `memory_graph_relate` for terse canonical facts / real relations.

### 3. REFLECT — at the end of a task, or the moment an outcome lands
- `memory_outcome(task, outcome, about=…, detail=…)` whenever something WORKED
  (`success`), was a dead-end (`failure`), or the user corrected you (`correction`).
  These signals are the ONLY feeder for procedural LESSONS — the dream synthesises
  them into durable do/avoid guidance surfaced at your next session start. Logging
  outcomes is not optional bookkeeping; it is how you stop repeating mistakes.

Be judicious — one claim per call, skip fleeting chatter (the surprise gate drops
near-duplicates). The first memory call each session may lag a few seconds (CPU
warmup); tools run on CPU, never the GPU.
```

Phase 2 adds one line under RECALL/CAPTURE: "for a substantial multi-step task,
open a named sub-episode with `memory_episode_start` (it nests under the session)."

---

## Phase 2 — episode nesting (task sub-episodes)

**Model — `episodes.py`**
- Add field `parent_id: str | None = None` to `Episode`.
- Treat `EpisodeManager.current_id` as the **leaf of a stack**, not "the one
  open episode":
  - **Agent sub-episode** (`memory_episode_start`): if an episode is already
    open, the new one's `parent_id = current_id` and the parent **stays open**;
    `current_id` → child. (Behavior change: today it auto-closes the parent.)
  - **Hook session floor** (`episode_start_session`): unchanged — root
    (`parent_id=None`), idempotent by `session_key`.
  - `end()` **pops**: `current_id = closed.parent_id` (back to the parent)
    instead of `None`.
  - **Cascade close:** `episode_end_session` closes the matching root *and* any
    still-open descendants (covers a forgotten sub-episode `end()`).
  - `stamp()` is unchanged — it already uses `current_id`, now correctly the leaf.

**Recall — `service`/`memory_search`**
- A `memory_search(episodes=[root_id])` filter expands to the episode's
  **subtree**, so a session-scoped query still returns everything done in its
  sub-episodes. Add a small `_episode_subtree(id)` helper used by the episode
  filter.

**Schema/sync — [`storage/schema.py`](../../pseudolife_memory/storage/schema.py), [`storage/sync.py`](../../pseudolife_memory/storage/sync.py)**
- Additive `ALTER TABLE episodes ADD COLUMN parent_id` (IF NOT EXISTS);
  `sync.episode_row` carries `parent_id`; bump the episode schema version.

**CLAUDE.md** — add the sub-episode line noted above.

---

## Testing

**Unit**
- Session-key idempotency: double `episode_start_session` with same key → one
  open episode; `episode_end_session` with mismatched key → no-op.
- Nesting: child nests under open parent; parent stays open; `stamp()` → leaf;
  `end()` → `current_id` back to parent; `episode_end_session` cascade-closes
  orphaned descendants.
- Briefing: `## Verified world facts` and `## Where we left off` rendering, and
  empty-omission when there is nothing to show.
- Subtree episode-filter expansion returns child-episode entries.
- Migration: `parent_id` column present after migrate.

**Live smoke (running daemon, Gemma extractor)**
- `episode-start` ×2 with same key → 1 open; a `memory_store` → stamped; agent
  `memory_episode_start` nests; `episode-end` → closed + dream fires; next
  `briefing` shows recap + world facts.

## Deploy

Per the recorded lesson, **daemon-only**:
```
docker compose build daemon && docker compose up -d daemon
```
The `ALTER`/`ADD COLUMN` is additive and idempotent, so it is safe; the postgres
container and its volumes are never touched. Back up first out of habit
(`ops/backup.ps1`). Re-run `ops/install-hook.ps1` once to register the new
SessionStart/SessionEnd episode hooks.

## Files touched (summary)

| File | Phase | Change |
|---|---|---|
| `pseudolife_memory/memory/episodes.py` | 1, 2 | `session_key` (1), `parent_id` + stack `end()`/cascade (2) |
| `pseudolife_memory/service.py` | 1, 2 | `episode_start_session` / `episode_end_session` (+ fire-and-forget dream); `session_briefing` world+recap; subtree filter (2) |
| `pseudolife_memory/web/routes.py` | 1 | `POST /api/episode/start`, `POST /api/episode/end`; `max_world` on `/api/briefing` |
| `pseudolife_memory/episode_cli.py` (new) | 1 | torch-free `episode-start` / `episode-end` |
| `pseudolife_memory/cli.py` | 1 | dispatch the new modes |
| `pseudolife_memory/memory/briefing.py` | 1 | render world-facts + recap blocks |
| `pseudolife_memory/briefing_cli.py` | 1 | `--max-world` |
| `ops/install-hook.ps1` | 1 | register SessionStart episode-start + SessionEnd episode-end |
| `pseudolife_memory/storage/schema.py`, `storage/sync.py` | 2 | `parent_id` column + sync |
| `~/.claude/CLAUDE.md` (user global) | 1, 2 | lifecycle rewrite (out of repo) |
| `tests/test_episodes.py`, `tests/test_briefing.py` (+ migration test) | 1, 2 | unit coverage above |

## Decisions locked

- **Scope:** all three subsystems, hooks + CLAUDE.md, **hybrid** episodes.
- **Episode unit:** session floor (hooks) + task sub-episodes (agent, Phase 2).
- **Dream-on-end:** yes, **fire-and-forget** in a daemon background thread.
- **Spec location:** project `docs/specs/` (existing convention).
