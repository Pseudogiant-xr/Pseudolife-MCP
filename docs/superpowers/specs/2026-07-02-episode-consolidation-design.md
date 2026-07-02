# Episode naming + fragmentation rework — design spec (2026-07-02)

## Problem

Since the daemon-owned episode rework (2026-06-30), the live episode list has
degenerated into fragments:

- **Useless titles.** The lazy open in `MemoryService._ensure_session_episode`
  titles every session `"session - YYYY-MM-DD HH:MM"` because the direct-HTTP
  transport carries no project signal. 16 of the 25 live episodes carry that
  generic title; `memory_session_title` exists but nothing prompts the agent
  to call it.
- **Fragmentation.** One logical work arc splinters across episodes two ways:
  1. *Reaper husks*: the idle reaper closes a session episode after 30 min;
     the next store from the **same** `mcp-session-id` opens a brand-new
     episode instead of resuming (live example: session key `022b33f9…` has
     two one-entry episodes at 14:51 and 15:36 on 2026-07-02).
  2. *Per-connection keying*: every Claude session is a new `mcp-session-id`,
     so a day of project work is N single-entry episodes (2026-07-02 alone:
     11 root episodes, 10 of them generic-titled with exactly 1 entry).
- **Nesting wart.** `service.episode_start` (agent sub-episode) called before
  the first store finds no open session leaf, so the "sub"-episode becomes a
  session-keyed **root**; `set_session_title` then renames *it* rather than a
  proper session root.

There is no way to repair the existing data: no rename-by-id, no merge.

## Goals

1. Admin primitives to consolidate what exists: `episode_rename`,
   `episode_merge` (service + REST).
2. Stop reaper-husk fragmentation: resume the same session's recently closed
   episode instead of opening a new one.
3. Make titles useful without agent cooperation: derive a content title at
   close when the title is still generic; nudge the agent to set a real title
   via the `memory_store` response.
4. Fix the `episode_start` nesting wart.

Out of scope: adopting pre-2026-06-30 unstamped entries into episodes
(rewrites history), LLM-generated titles (extractor dependency for marginal
gain), merging across genuinely distinct projects.

## Design

### A. `episode_rename(id, title)` — service + `POST /api/episodes/rename`

Rename any episode by id. Also rewrites the denormalised
`entry.episode_title` on every band entry stamped with that id (in-memory +
`storage.update_entry`). Returns the episode dict. Unknown id → `{ok: false}`.

### B. `episode_merge(source_ids, into=None, title=None, hint=None)` — service + `POST /api/episodes/merge`

Merge N episodes into one:

- Target: `into` (existing id) **or** a new root episode titled `title`
  (requires ≥1 source; new target inherits `session_key=None`,
  `started_at=min(sources)`, `ended_at=max(sources)` — an admin rollup is
  closed by construction unless `into` is an open episode).
- Sources must be **closed** (an open episode is someone's live session);
  open sources are skipped and reported.
- For each merged source:
  - Band entries with `episode_id == source` → re-stamped to target id+title
    (in-memory and via `storage.update_entry`).
  - DB-only rows (evicted entries, `signals.episode_id`) → bulk
    `storage.retarget_episode(old_id, new_id, new_title)` UPDATE on
    `entries` + `signals`.
  - Child episodes (`parent_id == source`) → re-parented to target.
  - Source episode deleted (manager + row).
- Target `started_at`/`ended_at` widened to span the sources.
- Returns `{ok, id, title, merged: [...], skipped_open: [...], entries_moved}`.

### C. Resume-on-return (reaper-husk fix)

`_ensure_session_episode`: before opening a new episode, look for the most
recently **closed** root with the same `session_key`. If
`now - ended_at <= PSEUDOLIFE_SESSION_RESUME_SECONDS` (default 21600 = 6 h),
reopen it (`ended_at = None`, clear `closed_by_new_start`) instead of creating
a husk. The same `mcp-session-id` is by construction the same client session,
so this is safe; the window exists only so a multi-day-idle session starts a
fresh episode. The end-of-session dream having already fired is fine — the
dream cursor makes the next close incremental.

### D. Auto-title on close

`_close_session_locked`: when the closing root's title still matches the
generic pattern (`session - YYYY-MM-DD HH:MM`), derive
`"{dominant_source} - {start stamp}: {snippet}"` where `dominant_source` is
the most frequent entry source in the episode subtree (preferring non-noise
sources — `status`/`log` only win when they're all there is) and `snippet` is
the first ~60 chars of the earliest entry's text (word-boundary truncated).
Sessions the agent titled (or the shim titled by cwd) never match the pattern
and are untouched. Empty episodes are pruned before titling matters.

Because the user's per-project `source` convention is stable
(`pseudolife`, `enshrouded`, …), the dominant source is an honest project
signal the transport can't provide.

### E. Nesting wart + untitled nudge

- `service.episode_start` calls `_ensure_session_episode(session_id)` first,
  so an early sub-episode nests under a real session root.
- `service.store` response gains `"episode_hint"` (only when the session
  episode is generic-titled): `"session episode is untitled — call
  memory_session_title('<project> - <topic>')"`. One-line, disappears once a
  title is set. The SessionStart briefing (global CLAUDE.md) gains a matching
  RECALL-beat line.

## Consolidation plan for the live bank (post-deploy, via REST)

- 10× generic `session - 2026-07-02 *` → new **"PseudoLife-MCP - 2026-07-02"**
  (hint: review-roadmap arc P0→P3, tool consolidation, graph-quality fixes).
  "Full deep codebase review (Fable 5, 2026-07-02)" stays its own named root.
- `session - 2026-07-01 16:49` + `… 23:27` + `session - 2026-07-02 00:00`
  (content dated 2026-07-01) → **"PseudoLife-MCP - 2026-07-01"**.
- `session - 2026-06-30 13:37` + `… 14:43` → **"PseudoLife-MCP - 2026-06-30"**
  (session-scoped-episodes rework arc).
- 3 roots titled "PseudoLife-MCP - 2026-06-29" → merged into the 6-entry one.
- Named episodes (brainstorms, hardening, ML-brief migration, llama.ccp)
  untouched.

Expected: 25 episodes → ~12, every title meaningful.

## Testing

TDD per feature: manager-level (reopen semantics), service-level
(merge/rename against a live PG-less file-mode service + storage-mocked
retarget), close-title derivation table-driven. Full suite must stay green.
