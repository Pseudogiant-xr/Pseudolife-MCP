<!-- The standing memory-loop instructions. Plugin users DON'T need this
     file: the daemon serves the same text as session context via the
     SessionStart hook (override it with <data_dir>/hook-instructions.md).
     For non-plugin setups, copy this block into your CLAUDE.md
     (Claude Code), AGENTS.md, or the equivalent standing-instructions file.
     Kept byte-identical to MEMORY_LOOP_BLOCK in
     pseudolife_memory/web/session_hook.py (guard-tested). -->

## Memory — your long-term memory; use it every session (tools: `mcp__pseudolife-memory__*`)
One shared memory bank across all sessions. Treat it as a loop with three
beats: RECALL at the start, CAPTURE as you go, REFLECT at the end. Session
episodes open/close automatically — every memory you store is auto-stamped
to the current session episode.

RECALL — at the start of any task:
- `memory_search(<natural-language task>)` for prior context, decisions, gotchas.
- `memory_lesson_search(<task>)` for what worked / what to avoid last time —
  heed `polarity:-` dead-ends.
- `memory_fact_get(entity, attribute)` for one canonical value. If null, the
  slot is empty, NOT the topic — `memory_search` finds it regardless; never
  conclude "nothing on X" from a single `fact_get` guess.
- `memory_world_search(<topic>)` when the task turns on an external fact your
  training may have stale (versions, prices, who-holds-a-role, findings).
- `memory_recall(<question>)` when the answer needs multi-hop chaining across
  related facts.
- Results are compact (`{id, text, source, tags, score}`). An entry carrying
  `superseded_by_text` has been corrected — use the replacement text, not the
  entry. Pass `verbose=true` only when debugging retrieval.

CAPTURE — as durable things arise (one claim per call):
- Name the session EARLY: `memory_session_title("<project> - <topic>")`.
- `memory_store` for durable context; set `origin` honestly
  (`user`/`action`/`agent`) and use a stable `source` per project/topic so
  search can scope its results.
- `memory_fact_set(entity, attribute, value)` for a canonical single-value
  fact; correct by re-setting the same slot (history is kept for audit).
- `memory_world_set(entity, attribute, value, source_url=, source_quote=)`
  for any EXTERNAL fact you verified via web/docs — route research findings
  here (cited), not into plain `memory_store`.
- Open a named sub-episode with `memory_episode_start(title)` for a big
  multi-step task; `memory_episode_end` pops back.
- Route verbose status/progress/logs under `source="status"` — searchable,
  but excluded from dream extraction so they don't pollute the graph.
- Never store secrets: no tokens, API keys, passwords, or credentials.

REFLECT — at task end, or the moment an outcome lands:
- `memory_outcome(task, outcome, about=, detail=)` whenever something WORKED
  (`success`), was a dead-end (`failure`), or the user corrected you
  (`correction`). These signals are the only feeder for procedural LESSONS —
  the dream distils them into the do/avoid guidance surfaced at your next
  session start. Logging outcomes is how you stop repeating mistakes.

Be judicious — one claim per call; skip fleeting chatter (the surprise gate
drops near-duplicates; `stored=false` is not an error). The first memory call
of a session may lag a few seconds (one-time warmup).
