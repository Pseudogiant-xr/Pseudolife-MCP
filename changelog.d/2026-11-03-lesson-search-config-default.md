### Fixed (2026-11-03 — omitted retrieval limits follow configuration)

- Python MCP lesson search now uses `memory.lessons.top_k` when the caller omits
  `top_k`, so Console changes to the lesson limit affect the next search.
  Explicit positive limits keep their behavior, and explicit `null` is
  still rejected. The native daemon also reads that limit from startup
  configuration, retaining five as the default.
- Recall now uses `memory.recall.default_hops` and `default_top_k` when
  MCP or REST callers omit those arguments. The Console Recall page
  still supplies its own hop count. Explicit limits and
  existing invalid-argument handling retain their behavior.
