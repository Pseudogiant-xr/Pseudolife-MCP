<!-- Copy this block into your CLAUDE.md (Claude Code), AGENTS.md, or the
     equivalent standing-instructions file, so the memory loop fires every
     session. Same content as the fenced block in the top-level README's
     "Recommended agent setup" section. -->

## Memory — use it every session (tools: `mcp__pseudolife-memory__*`)
RECALL at task start:
- `memory_search(<task>)` for prior context/decisions/gotchas;
  `memory_lesson_search(<task>)` for what worked / what to avoid (heed `polarity:-`);
  `memory_fact_get(entity, attribute)` for one canonical value;
  `memory_world_search(<topic>)` when an external fact may be stale.
CAPTURE as durable things arise (one claim per call):
- `memory_store` for durable context (set `origin`: user/action/agent);
  `memory_fact_set` for a canonical single-value fact (correct by re-setting the slot);
  `memory_world_set(..., source_url=, source_quote=)` for a verified EXTERNAL fact (cite it);
  open a named sub-episode with `memory_episode_start` for a big multi-step task.
  Route verbose status/logs under `source="status"` (searchable, but excluded from
  the dream so they don't pollute the graph).
REFLECT at task end / when an outcome lands:
- `memory_outcome(task, outcome, about=, detail=)` for a success / dead-end / correction —
  the dream distils these into the lessons surfaced at your next session start.
