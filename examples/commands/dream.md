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

## Deep mode (`/dream deep`)

Full-corpus graph consolidation plus Step C — turning link candidates into
reviewed proposals:

1. `memory_dream(action="deep")` — dry-run preview. Report the would-* counts
   (items flagged `already_proposed: true` will be skipped by apply).
2. `memory_dream(action="deep", apply=true)` — the daemon snapshots the graph
   tables first (`snapshot` in the response is the undo file); a
   `snapshot_failed` error means nothing was changed — investigate, don't retry
   blindly.
3. Work the returned `candidates` (Step C). For each pair, judge from the
   `src_snippets` / `dst_snippets` evidence, never from names alone:
   - **Related** (one uses/contains/produces the other, etc.): submit via
     `memory_graph_review(action="propose", proposals=[{src, relation, dst,
     rationale}])` with a specific relation and a one-line rationale.
   - **Distinct** (similar names or shared context only — e.g. opposite verbs,
     siblings under one parent):
     `memory_graph_review(action="dismiss_pair", src=..., dst=...)` so the pair
     never resurfaces and stops occupying a top-k slot.
   - **Unsure**: leave it — the pair stays visible for the Console's Atlas
     queue. Do not guess.
4. Report: superseded / merged / proposed / dismissed counts and the snapshot
   filename. Proposals still need a human verdict (`accept_link` /
   `reject_link` or Atlas).
