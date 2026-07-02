<!-- examples/commands/dream.md
Copy to .claude/commands/dream.md in any project to get /dream. -->
---
description: Consolidate recent PseudoLife memories into canonical cortex facts
---
Run a dream pass over the PseudoLife-MCP bank:

1. Call `memory_dream(action="status")`. If `would_fire` is false and there is
   no backlog, report "nothing to consolidate" and stop.
2. Call `memory_dream(action="pull")` (default limit).
3. From the pulled text, extract only **durable, current-state, slot-shaped**
   facts as `(entity, attribute, value)`. Skip narrative, in-progress work, and
   superseded states. Reuse existing slot keys where they fit.
4. Write each with `memory_fact_set` (origin `user` only for things the human
   stated; otherwise `agent`).
5. Call `memory_dream(action="commit", cursor=<newest timestamp from the pull>)`.
6. Report inserted / confirmed / contested counts. Surface any `contested`
   results to the user — those are conflicts to settle, not silent overwrites.
