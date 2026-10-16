### Fixed (2026-10-16 — /dream no longer asks for a lesson fold no tool can do)
- Step 6 of the `/dream` judgment session told an agent to fold anything a
  dropped duplicate lesson added into the surviving lesson before retiring
  it. No tool writes lesson text: lessons are distilled from outcomes, and
  the curation judge may pick a survivor but never author replacement
  text. An agent following the step could only retire the lesson and lose
  its point, or stall. The step now retires a duplicate lesson only when
  the survivor already says everything it does, and otherwise leaves the
  pair listed with the missing point named in the report. A world fact can
  still be folded with `memory_world_set`, but a rewrite keeps only what the
  new call passes and the listing truncates values. So the step now reads
  both slots in full with `memory_world_search` first, rewrites only when one
  record's source backs the whole merged value (re-passing its
  `source_url`, `source_quote`, `freshness_class` and `retrieved_at`), and
  otherwise leaves the pair listed.
- The command ships in the plugin, so existing installs pick it up with
  `pseudolife-mcp update` (release mode) or `ops/update.ps1 -All` /
  `ops/update_clients.py` (checkout mode).
